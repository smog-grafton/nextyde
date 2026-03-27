from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable, Awaitable

ProgressCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]

import orjson
from telethon import TelegramClient, events
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
from app.video_prepare import prepare_video_for_delivery

LOGGER = logging.getLogger("telebot")
MEDIA_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm"}


class JobCancelledError(Exception):
    """Raised when a job is cancelled via the web UI or API."""


class AlreadyProcessedError(Exception):
    """Raised when the message was already processed (success or failed) and we skip."""
TEMP_FILE_MAX_AGE_SECONDS = 86400  # 24 hours
TELEGRAM_LINK_PATTERN = TELEGRAM_LINK_RE


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
        self._sem = asyncio.Semaphore(settings.max_concurrent_downloads)
        self._watched_ids: set[int] = set()

    async def start(self) -> None:
        await self.store.init()
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_old_temp_files()
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
        await self.cdn.close()
        await self.client.disconnect()

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

    def _cleanup_old_temp_files(self) -> None:
        now = time.time()
        removed = 0
        for path in self.settings.temp_dir.iterdir():
            if not path.is_file():
                continue
            try:
                if now - path.stat().st_mtime > TEMP_FILE_MAX_AGE_SECONDS:
                    path.unlink()
                    removed += 1
            except OSError as exc:  # noqa: BLE001
                LOGGER.warning("Could not remove old temp file %s: %s", path, exc)
        if removed:
            LOGGER.info("Cleaned up %s old temp file(s)", removed)

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
            job["status"] = "starting"
            job["progress_pct"] = 0
            job["message"] = "Resolving link…"

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
                job["file_name"] = data.get("file_name", "")
                if status == "downloading":
                    job["message"] = f"Downloading {data.get('file_name', '')}…"
                elif status == "uploading":
                    job["message"] = "Uploading to CDN…"
                    job["progress_pct"] = 99
                elif status == "done":
                    job["message"] = "Done. File deleted."
                    job["progress_pct"] = 100
                    job["result"] = data.get("cdn_response")
                elif status == "downloaded":
                    job["message"] = "Ready. Copy the link, paste in CDN import, then click Destroy."
                    job["progress_pct"] = 100
                    job["temp_path"] = data.get("temp_path", "")
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

        async with self._sem:
            file_name = sanitize_filename(self._extract_file_name(message) or f"message_{message_id}.bin")
            download_only = progress_extra is not None and progress_extra.get("download_only") is True
            if download_only and progress_extra and progress_extra.get("job_id"):
                job_dir = self.settings.temp_dir / str(progress_extra["job_id"])
                job_dir.mkdir(parents=True, exist_ok=True)
                temp_file = job_dir / file_name
            else:
                temp_file = self.settings.temp_dir / file_name
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

            try:
                await self._invoke_progress(progress_callback, "downloading", {"file_name": file_name, "progress_pct": 0})

                def download_progress(current: int, total: int) -> None:
                    if progress_extra is not None and progress_extra.get("cancelled"):
                        raise JobCancelledError("Job cancelled")
                    self._progress(file_name, current, total)
                    if progress_extra is not None and total:
                        progress_extra["progress_pct"] = min(99, int(current * 100 / total))
                        progress_extra["file_name"] = file_name

                LOGGER.info(
                    "Downloading %s from %s (msg %s)",
                    file_name,
                    metadata.get("telegram_channel"),
                    message_id,
                )
                await message.download_media(
                    file=temp_file,
                    progress_callback=download_progress,
                )

                if progress_extra is not None and progress_extra.get("cancelled"):
                    raise JobCancelledError("Job cancelled")
                delivery_file = temp_file
                if self.settings.video_prep_enabled:
                    prep_result = prepare_video_for_delivery(self.settings, temp_file)
                    delivery_file = prep_result.delivery_path
                    metadata.update(
                        {
                            "video_prep_applied": prep_result.changed,
                            "video_prep_reason": prep_result.decision.reason,
                            "video_prep_input_size": prep_result.source_size_bytes,
                            "video_prep_output_size": prep_result.output_size_bytes,
                            "video_prep_profile": {
                                "codec": "libx264",
                                "audio_codec": "aac",
                                "crf": self.settings.video_prep_crf,
                                "preset": self.settings.video_prep_preset,
                                "max_height": self.settings.video_prep_max_height,
                                "faststart": True,
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
                if download_only:
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
                raise
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("Failed processing %s: %s", file_name, exc)
                await self._invoke_progress(progress_callback, "failed", {"error": str(exc), "file_name": file_name})
                raise
            finally:
                if not download_only and self.settings.delete_after_upload:
                    try:
                        temp_file.unlink(missing_ok=True)
                    except Exception:  # noqa: BLE001
                        LOGGER.warning("Could not delete temp file: %s", temp_file)
                    optimized_temp_file = temp_file.with_name(f"{temp_file.stem}.delivery.mp4")
                    if optimized_temp_file != temp_file:
                        try:
                            optimized_temp_file.unlink(missing_ok=True)
                        except Exception:  # noqa: BLE001
                            LOGGER.warning("Could not delete optimized temp file: %s", optimized_temp_file)

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

    @staticmethod
    def _progress(file_name: str, current: int, total: int) -> None:
        if not total:
            return
        pct = int(current * 100 / total)
        if pct in {1, 5, 10, 25, 50, 75, 100}:
            LOGGER.info("%s download progress: %s%%", file_name, pct)
