from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable, Awaitable

ProgressCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]

import orjson
from telethon import TelegramClient, events, utils as telethon_utils
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.types import DocumentAttributeFilename
from telethon.errors import UsernameInvalidError, UsernameNotOccupiedError

from app.cdn_client import CdnClient
from app.config import Settings
from app.db import StateStore
from app.filename import extract_metadata, sanitize_filename
from app.link_parser import TELEGRAM_LINK_RE
from app.media_tools import detect_media_tools
from app.video_prepare import (
    VideoPrepCancelledError,
    analyze_video_for_delivery,
    prepare_video_for_delivery,
)

LOGGER = logging.getLogger("telebot")
MEDIA_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm"}


class JobCancelledError(Exception):
    """Raised when a job is cancelled via the web UI or API."""


class AlreadyProcessedError(Exception):
    """Raised when the message was already processed (success or failed) and we skip."""


TELEGRAM_LINK_PATTERN = TELEGRAM_LINK_RE
HOUSEKEEPING_INTERVAL_SECONDS = 3600


class TelegramPipeWorker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.store = StateStore(str(settings.db_path))
        self.client = TelegramClient(
            settings.tg_session_name,
            settings.tg_api_id,
            settings.tg_api_hash,
            sequential_updates=True,
        )
        self.cdn = CdnClient(settings)
        self._download_sem = asyncio.Semaphore(settings.max_concurrent_downloads)
        self._transcode_sem = asyncio.Semaphore(settings.max_concurrent_transcodes)
        self._watched_ids: set[int] = set()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._job_registry: dict[str, dict[str, Any]] | None = None
        self._active_temp_paths: set[str] = set()

    async def start(self) -> None:
        await self.store.init()
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        await self.start_housekeeping()
        if self.settings.video_prep_enabled:
            tools = detect_media_tools()
            LOGGER.info(
                "Video prep tools at startup: ffmpeg=%s ffprobe=%s",
                "ready" if tools.ffmpeg.available else f"missing ({tools.ffmpeg.error})",
                "ready" if tools.ffprobe.available else f"missing ({tools.ffprobe.error})",
            )
        await self.client.connect()

        if not await self.client.is_user_authorized():
            await self.client.send_code_request(self.settings.tg_phone)
            code = (
                (self.settings.tg_login_code or "").strip()
                or input("Enter Telegram login code: ").strip()
            )
            if not code:
                raise RuntimeError("Login code required. Set TG_LOGIN_CODE in .env for headless or enter interactively.")
            try:
                await self.client.sign_in(self.settings.tg_phone, code)
            except SessionPasswordNeededError:
                if not self.settings.tg_2fa_password:
                    raise RuntimeError("Telegram 2FA is enabled. Set TG_2FA_PASSWORD in .env")
                await self.client.sign_in(password=self.settings.tg_2fa_password)

        me = await self.client.get_me()
        LOGGER.info("Signed in as %s (%s)", getattr(me, "first_name", "unknown"), me.id)

        await self._join_targets()
        await self._resolve_watch_targets()
        await self._scan_recent_messages()

        self.client.add_event_handler(self._on_new_message, events.NewMessage())
        self.client.add_event_handler(self._on_text_with_link, events.NewMessage())
        LOGGER.info("Watching %s Telegram targets", len(self._watched_ids))
        await self.client.run_until_disconnected()

    async def stop(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
        await self.cdn.close()
        await self.client.disconnect()

    def bind_job_registry(self, job_registry: dict[str, dict[str, Any]] | None) -> None:
        self._job_registry = job_registry

    async def start_housekeeping(self, job_registry: dict[str, dict[str, Any]] | None = None) -> None:
        if job_registry is not None:
            self.bind_job_registry(job_registry)
        self.cleanup_temp_files()
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(HOUSEKEEPING_INTERVAL_SECONDS)
            self.cleanup_temp_files()

    def _mark_job_expired(self, job_id: str, latest: Path | None, latest_mtime: float) -> None:
        if self._job_registry is None:
            return
        job = self._job_registry.get(job_id)
        if job is None:
            job = {
                "job_id": job_id,
                "status": "expired",
                "progress_pct": 100,
                "message": "Temp file expired and was removed.",
                "file_name": latest.name if latest is not None else "",
                "result": None,
                "error": None,
                "temp_path": None,
                "download_only": True,
                "_ts": latest_mtime,
                "updated_ts": time.time(),
            }
            self._job_registry[job_id] = job
            return

        job["status"] = "expired"
        job["message"] = "Temp file expired and was removed."
        job["temp_path"] = None
        job["progress_pct"] = 100
        job["updated_ts"] = time.time()

    def cleanup_temp_files(self) -> None:
        if not self.settings.temp_dir.exists():
            return

        ttl_seconds = self.settings.temp_file_ttl_hours * 3600
        now = time.time()

        for child in list(self.settings.temp_dir.iterdir()):
            try:
                if child.is_dir():
                    files = sorted((path for path in child.rglob("*") if path.is_file()), key=lambda path: path.stat().st_mtime)
                    if not files:
                        child.rmdir()
                        continue

                    latest = files[-1]
                    if (now - latest.stat().st_mtime) <= ttl_seconds:
                        continue
                    if any(str(path.resolve()) in self._active_temp_paths for path in files):
                        continue

                    self._mark_job_expired(child.name, latest, latest.stat().st_mtime)
                    for path in reversed(list(child.rglob("*"))):
                        if path.is_file():
                            path.unlink(missing_ok=True)
                        elif path.is_dir():
                            try:
                                path.rmdir()
                            except OSError:
                                pass
                    try:
                        child.rmdir()
                    except OSError:
                        pass
                    continue

                if not child.is_file():
                    continue
                if (now - child.stat().st_mtime) <= ttl_seconds:
                    continue
                if str(child.resolve()) in self._active_temp_paths:
                    continue
                child.unlink(missing_ok=True)
            except OSError as exc:  # noqa: BLE001
                LOGGER.warning("Could not clean temp path %s: %s", child, exc)

    def _track_temp_path(self, path: Path | None) -> None:
        if path is None:
            return
        self._active_temp_paths.add(str(path.resolve()))

    def _release_temp_path(self, path: Path | None) -> None:
        if path is None:
            return
        self._active_temp_paths.discard(str(path.resolve()))

    def _resolve_download_part_size_kb(self) -> int:
        chunk_bytes = min(self.settings.download_chunk_size, 512 * 1024)
        chunk_bytes = max(4096, chunk_bytes - (chunk_bytes % 4096))
        return max(4, chunk_bytes // 1024)

    async def _download_media_to_path(
        self,
        message: Any,
        destination: Path,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        file_size = int(getattr(message.file, "size", 0) or 0)
        dc_id, input_location = telethon_utils.get_input_location(message.media or message)
        await self.client.download_file(
            input_location,
            file=destination,
            part_size_kb=self._resolve_download_part_size_kb(),
            file_size=file_size or None,
            progress_callback=progress_callback,
            dc_id=dc_id,
        )

    async def _acquire_stage_slot(self, semaphore: asyncio.Semaphore, progress_extra: dict[str, Any] | None = None) -> None:
        while True:
            if progress_extra is not None and progress_extra.get("cancelled"):
                raise JobCancelledError("Job cancelled")
            try:
                await asyncio.wait_for(semaphore.acquire(), timeout=0.5)
                return
            except asyncio.TimeoutError:
                continue

    async def _join_targets(self) -> None:
        for target in self.settings.tg_join_targets:
            try:
                if "/+" in target or "joinchat/" in target:
                    invite_hash = target.split("+")[-1].split("/")[-1]
                    await self.client(ImportChatInviteRequest(invite_hash))
                else:
                    await self.client(JoinChannelRequest(target))
                LOGGER.info("Joined target: %s", target)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Could not join %s: %s", target, exc)

    async def _resolve_watch_targets(self) -> None:
        targets = self.settings.tg_watch_targets or self.settings.tg_join_targets
        if not targets:
            LOGGER.warning(
                "No watch targets configured. Set TG_WATCH_TARGETS or TG_JOIN_TARGETS to watch channels; "
                "you can still use process-link for single t.me URLs."
            )
            return

        for target in targets:
            try:
                entity = await self.client.get_entity(target)
                self._watched_ids.add(entity.id)
                LOGGER.info("Watching target: %s (%s)", getattr(entity, "title", target), entity.id)
            except (UsernameInvalidError, UsernameNotOccupiedError, ValueError) as exc:
                LOGGER.warning(
                    "Skipping invalid or unresolvable target %r: %s. Use a real @channel or t.me invite.",
                    target,
                    exc,
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Could not resolve watch target %r: %s", target, exc)

    async def _scan_recent_messages(self) -> None:
        targets = self.settings.tg_watch_targets or self.settings.tg_join_targets
        if not targets or not self._watched_ids or self.settings.scan_last_messages <= 0:
            return
        for target in targets:
            try:
                entity = await self.client.get_entity(target)
                if entity.id not in self._watched_ids:
                    continue
                LOGGER.info("Scanning last %s messages in %s", self.settings.scan_last_messages, target)
                async for message in self.client.iter_messages(entity, limit=self.settings.scan_last_messages):
                    if self._is_supported_media(message):
                        await self._handle_message(message, catch_up=True)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Could not scan %r: %s", target, exc)

    def _is_supported_media(self, message: Any) -> bool:
        if not getattr(message, "media", None):
            return False
        file_name = self._extract_file_name(message)
        if not file_name:
            return False
        return Path(file_name).suffix.lower() in MEDIA_EXTENSIONS

    def _extract_file_name(self, message: Any) -> str | None:
        if not getattr(message, "document", None):
            return None
        for attr in message.document.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                return attr.file_name
        return getattr(message.file, "name", None)

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        message = event.message
        chat = await event.get_chat()
        chat_id = getattr(chat, "id", None)
        if chat_id not in self._watched_ids:
            return
        if not self._is_supported_media(message):
            return
        await self._handle_message(message, catch_up=False)

    async def _on_text_with_link(self, event: events.NewMessage.Event) -> None:
        message = event.message
        text = (message.text or "").strip()
        if not text:
            return
        chat = await event.get_chat()
        chat_id = getattr(chat, "id", None)
        if chat_id not in self._watched_ids:
            return
        match = TELEGRAM_LINK_PATTERN.search(text)
        if not match:
            return
        channel_ref, msg_id_str = match.group(1), match.group(2)
        try:
            msg_id = int(msg_id_str)
        except ValueError:
            return
        try:
            entity = await self.client.get_entity(channel_ref)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Could not resolve t.me link %s: %s", text.strip(), exc)
            return
        try:
            fetched = await self.client.get_messages(entity, ids=msg_id)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Could not fetch message %s: %s", msg_id, exc)
            return
        if not fetched or (isinstance(fetched, list) and not fetched):
            LOGGER.warning("Message %s not found", msg_id)
            return
        target = fetched[0] if isinstance(fetched, list) else fetched
        if not self._is_supported_media(target):
            LOGGER.debug("Message %s is not supported media", msg_id)
            return
        await self._handle_message(target, catch_up=False)

    async def process_link(
        self,
        link: str,
        progress_callback: ProgressCallback | None = None,
        job: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch a t.me message by URL, process it (download → CDN → notify), return result or raise.
        If job dict is provided, it is updated with status, progress_pct, message, and result/error."""
        from app.link_parser import parse_telegram_link

        progress_extra = job if job is not None else {}
        if job is not None:
            job["status"] = "queued"
            job["progress_pct"] = 0
            job["message"] = "Resolving link…"
            job["updated_ts"] = time.time()

        parsed = parse_telegram_link(link)
        if not parsed:
            raise ValueError(f"Invalid t.me URL: {link}")
        channel_ref, message_id = parsed
        entity = await self.client.get_entity(channel_ref)
        messages = await self.client.get_messages(entity, ids=message_id)
        message = messages[0] if isinstance(messages, list) and messages else messages
        if not message:
            raise ValueError("Message not found or not accessible")
        result_holder: dict[str, Any] = {}

        async def cb(status: str, data: dict[str, Any]) -> None:
            if job is not None:
                job["status"] = status
                job["progress_pct"] = data.get("progress_pct", progress_extra.get("progress_pct", 0))
                if data.get("file_name"):
                    job["file_name"] = data.get("file_name", "")
                job["updated_ts"] = time.time()
                if status == "downloading":
                    job["message"] = f"Downloading {data.get('file_name', '')}…"
                elif status == "waiting_to_prepare":
                    job["message"] = data.get("message", "Waiting for video slot…")
                elif status == "uploading":
                    job["message"] = "Uploading to CDN…"
                    job["progress_pct"] = 99
                elif status == "preparing":
                    job["message"] = data.get("message", "Preparing video…")
                    job["progress_pct"] = data.get("progress_pct", max(20, progress_extra.get("progress_pct", 20)))
                elif status == "done":
                    job["message"] = "Done. File deleted."
                    job["progress_pct"] = 100
                    job["result"] = data.get("cdn_response")
                elif status == "downloaded":
                    job["message"] = "Ready. Copy the link, paste in CDN import, then click Destroy."
                    job["progress_pct"] = 100
                    job["temp_path"] = data.get("temp_path", "")
                elif status == "cancelled":
                    job["message"] = "Cancelled."
                elif status == "failed":
                    job["message"] = data.get("error", "Failed")
                    job["error"] = data.get("error")
            if progress_callback:
                out = progress_callback(status, data)
                if asyncio.iscoroutine(out):
                    await out
            if status == "done" and data:
                result_holder["cdn_response"] = data.get("cdn_response")
                result_holder["metadata"] = data.get("metadata")
            if status == "downloaded" and data:
                result_holder["temp_path"] = data.get("temp_path")

        try:
            await self._handle_message(
                message,
                catch_up=False,
                progress_callback=cb,
                progress_extra=progress_extra,
            )
        except AlreadyProcessedError:
            raise
        if result_holder.get("temp_path"):
            return result_holder
        if "cdn_response" not in result_holder:
            raise RuntimeError("Processing did not complete (upload or notify failed). You can retry the same link.")
        return result_holder

    async def _handle_message(
        self,
        message: Any,
        catch_up: bool,
        progress_callback: ProgressCallback | None = None,
        progress_extra: dict[str, Any] | None = None,
    ) -> None:
        chat = await message.get_chat()
        chat_id = getattr(chat, "id", 0)
        message_id = int(message.id)

        if await self.store.is_processed(chat_id, message_id):
            LOGGER.debug("Skipping already processed message %s:%s", chat_id, message_id)
            if progress_callback:
                await self._invoke_progress(progress_callback, "skipped", {"message": "Already processed"})
            raise AlreadyProcessedError("Already processed (previous run succeeded or failed). Try another link.")

        file_name = sanitize_filename(self._extract_file_name(message) or f"message_{message_id}.bin")
        download_only = progress_extra is not None and progress_extra.get("download_only") is True
        if download_only and progress_extra and progress_extra.get("job_id"):
            job_dir = self.settings.temp_dir / str(progress_extra["job_id"])
            job_dir.mkdir(parents=True, exist_ok=True)
            temp_file = job_dir / file_name
        else:
            temp_file = self.settings.temp_dir / file_name
        optimized_temp_file = temp_file.with_name(f"{temp_file.stem}.delivery.mp4")
        metadata = extract_metadata(file_name)
        metadata.update(
            {
                "telegram_chat_id": chat_id,
                "telegram_message_id": message_id,
                "telegram_channel": getattr(chat, "title", None) or getattr(chat, "username", None),
                "telegram_date": message.date.isoformat() if getattr(message, "date", None) else None,
                "telegram_catch_up": catch_up,
                "telegram_size": getattr(message.file, "size", None),
                "telegram_mime_type": getattr(message.file, "mime_type", None),
            }
        )

        self._track_temp_path(temp_file)
        self._track_temp_path(optimized_temp_file)
        delivery_file = temp_file
        keep_temp_files = False
        upload_complete = False

        def is_cancelled() -> bool:
            return bool(progress_extra is not None and progress_extra.get("cancelled"))

        try:
            await self._invoke_progress(progress_callback, "downloading", {"file_name": file_name, "progress_pct": 0})
            progress_state = {"last_logged_pct": -1}

            def download_progress(current: int, total: int) -> None:
                if is_cancelled():
                    raise JobCancelledError("Job cancelled")
                if total:
                    pct = int(current * 100 / total)
                    if pct != progress_state["last_logged_pct"] and pct in {1, 5, 10, 25, 50, 75, 100}:
                        progress_state["last_logged_pct"] = pct
                        LOGGER.info("%s download progress: %s%%", file_name, pct)
                if progress_extra is not None and total:
                    progress_extra["progress_pct"] = min(99, int(current * 100 / total))
                    progress_extra["file_name"] = file_name

            LOGGER.info(
                "Downloading %s from %s (msg %s)",
                file_name,
                metadata.get("telegram_channel"),
                message_id,
            )
            await self._acquire_stage_slot(self._download_sem, progress_extra)
            try:
                await self._download_media_to_path(message, temp_file, download_progress)
            finally:
                self._download_sem.release()

            if is_cancelled():
                raise JobCancelledError("Job cancelled")

            prep_result = None
            prep_analysis = None
            if self.settings.video_prep_enabled:
                await self._invoke_progress(
                    progress_callback,
                    "preparing",
                    {"file_name": file_name, "progress_pct": 20, "message": "Analyzing media…"},
                )
                loop = asyncio.get_running_loop()

                def prep_progress(stage: str, data: dict[str, Any]) -> None:
                    pct = int(data.get("progress_pct", 20))
                    attempt_index = data.get("attempt_index")
                    attempt_count = data.get("attempt_count")

                    def _update_progress() -> None:
                        if progress_extra is not None:
                            progress_extra["progress_pct"] = pct
                            progress_extra["file_name"] = file_name

                    loop.call_soon_threadsafe(_update_progress)
                    msg = "Preparing video…"
                    if stage == "probing_started":
                        msg = "Analyzing media…"
                    elif stage == "transcoding_started":
                        msg = "Transcoding for web delivery…"
                    elif stage == "transcoding_progress":
                        msg = f"Transcoding for web delivery… ({pct}%)"
                    elif stage == "probe_output":
                        msg = "Validating compressed output…"
                    elif stage == "prep_skipped":
                        msg = "Source already delivery-ready. Skipping transcode."
                    elif stage == "prep_done":
                        msg = "Video preparation complete."
                    if attempt_count and attempt_count > 1 and stage in {"transcoding_started", "transcoding_progress"}:
                        msg += f" attempt {attempt_index}/{attempt_count}"
                    if progress_callback:
                        loop.call_soon_threadsafe(
                            lambda: loop.create_task(
                                self._invoke_progress(
                                    progress_callback,
                                    "preparing",
                                    {"file_name": file_name, "progress_pct": pct, "message": msg},
                                )
                            )
                        )

                prep_analysis = await asyncio.to_thread(
                    analyze_video_for_delivery,
                    self.settings,
                    temp_file,
                    prep_progress,
                )
                metadata.update(
                    {
                        "video_prep_applied": False,
                        "video_prep_reason": prep_analysis.decision.reason,
                        "video_prep_mode": prep_analysis.decision.mode,
                        "video_prep_target_max_bytes": prep_analysis.decision.target_max_bytes,
                    }
                )

                if is_cancelled():
                    raise JobCancelledError("Job cancelled")

                if prep_analysis.decision.should_transcode:
                    await self._invoke_progress(
                        progress_callback,
                        "waiting_to_prepare",
                        {
                            "file_name": file_name,
                            "progress_pct": max(21, progress_extra.get("progress_pct", 20) if progress_extra else 20),
                            "message": "Waiting for video slot…",
                        },
                    )
                    await self._acquire_stage_slot(self._transcode_sem, progress_extra)
                    try:
                        prep_result = await asyncio.to_thread(
                            prepare_video_for_delivery,
                            self.settings,
                            temp_file,
                            prep_progress,
                            is_cancelled,
                            prep_analysis,
                        )
                    except VideoPrepCancelledError as exc:
                        raise JobCancelledError(str(exc)) from exc
                    finally:
                        self._transcode_sem.release()

                    delivery_file = prep_result.delivery_path
                    await self._invoke_progress(
                        progress_callback,
                        "preparing",
                        {
                            "file_name": delivery_file.name,
                            "progress_pct": 97 if prep_result.changed else 100,
                            "message": "Transcode complete, finalizing output…" if prep_result.changed else "Source already delivery-ready. Skipping transcode.",
                        },
                    )
                    metadata.update(
                        {
                            "video_prep_applied": prep_result.changed,
                            "video_prep_input_size": prep_result.source_size_bytes,
                            "video_prep_output_size": prep_result.output_size_bytes,
                            "video_prep_attempts_used": prep_result.attempts_used,
                            "video_prep_profile": {
                                "codec": "libx264",
                                "audio_codec": "aac",
                                "crf": self.settings.video_prep_crf if prep_result.decision.mode != "cap" else None,
                                "preset": self.settings.video_prep_preset,
                                "max_height": self.settings.video_prep_max_height,
                                "faststart": True,
                                "mode": prep_result.decision.mode,
                                "size_cap_mb": self.settings.video_prep_target_max_mb if prep_result.decision.enforce_size_cap else None,
                            },
                            "video_codec_out": (
                                prep_result.output_probe.video.codec
                                if prep_result.output_probe and prep_result.output_probe.video
                                else (prep_result.source_probe.video.codec if prep_result.source_probe.video else None)
                            ),
                            "audio_codec_out": (
                                prep_result.output_probe.audio.codec
                                if prep_result.output_probe and prep_result.output_probe.audio
                                else (prep_result.source_probe.audio.codec if prep_result.source_probe.audio else None)
                            ),
                            "video_width_out": (
                                prep_result.output_probe.video.width
                                if prep_result.output_probe and prep_result.output_probe.video
                                else (prep_result.source_probe.video.width if prep_result.source_probe.video else None)
                            ),
                            "video_height_out": (
                                prep_result.output_probe.video.height
                                if prep_result.output_probe and prep_result.output_probe.video
                                else (prep_result.source_probe.video.height if prep_result.source_probe.video else None)
                            ),
                            "video_frame_rate_out": (
                                prep_result.output_probe.video.frame_rate
                                if prep_result.output_probe and prep_result.output_probe.video
                                else (prep_result.source_probe.video.frame_rate if prep_result.source_probe.video else None)
                            ),
                        }
                    )
                    if prep_result.changed and not self.settings.video_prep_keep_original_on_success:
                        try:
                            if temp_file != delivery_file and temp_file.is_file():
                                temp_file.unlink()
                                LOGGER.info(
                                    "Video prep cleanup: removed source file after optimization: %s",
                                    temp_file,
                                )
                        except OSError as exc:
                            LOGGER.warning("Could not remove source file after video prep: %s", exc)
                else:
                    metadata.update(
                        {
                            "video_prep_input_size": prep_analysis.source_probe.size_bytes,
                            "video_prep_output_size": prep_analysis.source_probe.size_bytes,
                            "video_prep_attempts_used": 0,
                            "video_codec_out": prep_analysis.source_probe.video.codec if prep_analysis.source_probe.video else None,
                            "audio_codec_out": prep_analysis.source_probe.audio.codec if prep_analysis.source_probe.audio else None,
                            "video_width_out": prep_analysis.source_probe.video.width if prep_analysis.source_probe.video else None,
                            "video_height_out": prep_analysis.source_probe.video.height if prep_analysis.source_probe.video else None,
                            "video_frame_rate_out": prep_analysis.source_probe.video.frame_rate if prep_analysis.source_probe.video else None,
                        }
                    )

            if download_only:
                keep_temp_files = True
                await self._invoke_progress(
                    progress_callback,
                    "downloaded",
                    {"temp_path": str(delivery_file), "file_name": delivery_file.name, "metadata": metadata},
                )
                LOGGER.info("Download only: %s ready at %s", delivery_file.name, delivery_file)
            else:
                await self._invoke_progress(progress_callback, "uploading", {"file_name": delivery_file.name})
                LOGGER.info("Uploading %s to CDN", delivery_file.name)
                cdn_response = await self.cdn.upload_file(delivery_file, metadata)
                cdn_payload = cdn_response.get("data", cdn_response) if isinstance(cdn_response, dict) else cdn_response
                await self.store.mark_processed(
                    chat_id,
                    message_id,
                    delivery_file.name,
                    "uploaded",
                    orjson.dumps(cdn_response).decode("utf-8"),
                )
                await self.cdn.notify(
                    {
                        "status": "uploaded",
                        "telegram": metadata,
                        "cdn_response": cdn_payload,
                    }
                )
                upload_complete = True
                await self._invoke_progress(
                    progress_callback,
                    "done",
                    {"cdn_response": cdn_payload, "metadata": metadata, "file_name": delivery_file.name},
                )
                LOGGER.info("Finished %s", delivery_file.name)
        except JobCancelledError:
            LOGGER.info("Job cancelled: %s", file_name)
            await self._invoke_progress(progress_callback, "cancelled", {"file_name": file_name})
            if progress_extra is not None:
                progress_extra["status"] = "cancelled"
                progress_extra["message"] = "Cancelled"
                progress_extra["updated_ts"] = time.time()
            raise
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Failed processing %s: %s", file_name, exc)
            await self._invoke_progress(progress_callback, "failed", {"error": str(exc), "file_name": file_name})
            raise
        finally:
            should_keep_temp = keep_temp_files or (upload_complete and not self.settings.delete_after_upload)
            if not should_keep_temp:
                try:
                    temp_file.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    LOGGER.warning("Could not delete temp file: %s", temp_file)
                if optimized_temp_file != temp_file:
                    try:
                        optimized_temp_file.unlink(missing_ok=True)
                    except Exception:  # noqa: BLE001
                        LOGGER.warning("Could not delete optimized temp file: %s", optimized_temp_file)
            self._release_temp_path(temp_file)
            self._release_temp_path(optimized_temp_file)

    async def _invoke_progress(
        self,
        progress_callback: ProgressCallback | None,
        status: str,
        data: dict[str, Any],
    ) -> None:
        if not progress_callback:
            return
        try:
            out = progress_callback(status, data)
            if asyncio.iscoroutine(out):
                await out
        except Exception:  # noqa: BLE001
            pass
