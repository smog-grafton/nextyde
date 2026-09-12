from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from dataclasses import replace
import fcntl
import hashlib
import hmac
import logging
import os
from pathlib import Path
import shutil
import threading
import time
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
import orjson
from telethon import utils as telethon_utils

from app.db import StateStore
from app.filename import storage_safe_filename
from app.link_parser import TelegramMessageReference, parse_telegram_reference
from app.object_storage import ObjectStorageManager, S3MultipartUploader
from app.processing_profiles import normalize_processing_options
from app.media_processing import analyze_media_for_processing, prepare_media_for_processing, MediaVideoRepairRequired
from app.telegram_worker import TelegramPipeWorker, unsupported_media_message


LOGGER = logging.getLogger("telebot.telescope")
TERMINAL_STATUSES = {"ready", "failed", "cancelled", "destroyed"}
MEDIA_STAGES = {"inspecting", "preparing_mp4", "converting_audio", "optimizing_video", "validating"}


class TelescopeTransferStalledError(TimeoutError):
    """Telegram yielded no bytes for the configured watchdog period."""


class TelescopeDiskCapacityError(RuntimeError):
    """Temporary storage cannot safely accommodate a staged media job."""


class TelescopeScheduler:
    """Durable acquisition, media preparation and verified object delivery."""

    def __init__(self, worker: TelegramPipeWorker, store: StateStore,
                 storage: ObjectStorageManager | None = None) -> None:
        self.worker, self.store, self.settings = worker, store, worker.settings
        self.storage = storage or ObjectStorageManager()
        self._global_sem = asyncio.Semaphore(self.settings.telescope_max_active_jobs)
        self._download_sem = asyncio.Semaphore(getattr(self.settings, "telescope_max_downloads", 2))
        self._media_sem = asyncio.Semaphore(getattr(self.settings, "telescope_max_media_jobs", 1))
        self._shared_media_sem = getattr(worker, "_transcode_sem", self._media_sem)
        self._remux_sem = asyncio.Semaphore(getattr(self.settings, "telescope_max_remux_jobs", 2))
        self._inspect_sem = asyncio.Semaphore(getattr(self.settings, "telescope_max_inspections", 2))
        self._upload_sem = asyncio.Semaphore(getattr(self.settings, "telescope_max_uploads", 2))
        self._channel_sems: dict[str, asyncio.Semaphore] = {}
        self._active_tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._dispatch_task = self._callback_task = None
        self._wake = asyncio.Event()
        self._http = httpx.AsyncClient(timeout=self.settings.telescope_callback_timeout_seconds)
        self.work_root = Path(getattr(self.settings, "temp_dir", Path(store.db_path).parent / "temp")) / "telescope"
        self._service_lock = None
        self._disk_reservations: dict[str, int] = {}
        self._reports: dict[str, tuple[str, float]] = {}
        self._last_cleanup = 0.0

    async def start(self) -> None:
        # Take the process lock before recovery: a second server must not
        # requeue and run work that this server still owns.
        lock_path = Path(self.store.db_path).with_suffix(".telescope.lock")
        self._service_lock = lock_path.open("a+b")
        try:
            fcntl.flock(self._service_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._service_lock.close()
            self._service_lock = None
            raise RuntimeError("Another Telescope scheduler owns this state database; run one web worker.") from exc
        self.work_root.mkdir(parents=True, exist_ok=True)
        await self.store.recover_telescope_jobs()
        # Repair the crash window between terminal state commit and outbox enqueue.
        for job in await self.store.terminal_telescope_jobs():
            if job["status"] in TERMINAL_STATUSES:
                await self._emit_event(job, job["status"])
        self._dispatch_task = asyncio.create_task(self._dispatch_loop(), name="telescope-dispatch")
        self._callback_task = asyncio.create_task(self._callback_loop(), name="telescope-callbacks")
        self._wake.set()

    async def stop(self) -> None:
        tasks = [task for task in (self._dispatch_task, self._callback_task) if task is not None]
        tasks.extend(self._active_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._http.aclose()
        if self._service_lock:
            self._service_lock.close()
            self._service_lock = None

    async def submit(self, telegram_url: str, *, storage_target: str | None = None,
                     portal_source_id: str | int | None = None,
                     metadata: dict[str, Any] | None = None,
                     processing: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.settings.telescope_enabled:
            raise RuntimeError("Telescope imports are disabled.")
        reference = parse_telegram_reference(telegram_url)
        options = normalize_processing_options(processing)
        requested = storage_target or self.settings.telescope_default_storage_target
        if requested not in {"auto", "r2", "contabo", *self.storage.targets}:
            raise ValueError("Unknown Telescope storage target.")
        job_id = (str(uuid5(NAMESPACE_URL, f"teletyde:telescope:portal:{portal_source_id}"))
                  if portal_source_id is not None and str(portal_source_id).strip() else str(uuid4()))
        existing = await self.store.get_telescope_job(job_id)
        if existing is not None:
            return existing
        job = await self.store.create_telescope_job({
            "job_id": job_id, "telegram_url": reference.canonical_url(), "reference": reference.as_dict(),
            "portal_source_id": portal_source_id, "storage_target": requested,
            "metadata": metadata or {}, "processing": options,
        })
        await self._emit_event(job, "accepted")
        self._wake.set()
        return job

    async def cancel(self, job_id: str) -> bool:
        updated = await self.store.request_telescope_cancel(job_id)
        event = self._cancel_events.get(job_id)
        if event:
            event.set()
            task = self._active_tasks.get(job_id)
            if task:
                task.cancel()
        elif updated and job_id not in self._active_tasks:
            await self.store.update_telescope_job(job_id, status="cancelled", completed_at=time.time())
            await self._emit_event(await self.store.get_telescope_job(job_id), "cancelled")
        self._wake.set()
        return updated

    async def retry(self, job_id: str) -> bool:
        updated = await self.store.requeue_telescope_job(job_id)
        self._wake.set()
        return updated

    async def destroy(self, job_id: str) -> bool:
        """Delete a terminal job's local files while preserving remote output."""
        job = await self.store.get_telescope_job(job_id)
        if job is None or job.get("status") not in {"ready", "failed", "cancelled"}:
            return False
        if job_id in self._active_tasks and not self._active_tasks[job_id].done():
            return False

        directory = self._job_dir(job_id)
        if job.get("status") != "ready":
            await self._abort_upload(job)
        if directory.is_dir() and not directory.is_symlink():
            with (directory / ".job.lock").open("a+b") as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False
                await asyncio.to_thread(shutil.rmtree, directory)

        await self.store.update_telescope_job(job_id, status="destroyed", completed_at=time.time())
        self._wake.set()
        return True

    async def cleanup_terminal_files(self, *, force: bool = False) -> int:
        """Remove terminal Telescope working directories and return a count."""
        removed = 0
        retention = getattr(self.settings, "telescope_failed_retention_hours", 72) * 3600
        now = time.time()
        for job in await self.store.terminal_telescope_jobs():
            if job["job_id"] in self._active_tasks or job["status"] not in TERMINAL_STATUSES - {"destroyed"}:
                continue
            success = job["status"] == "ready" and job.get("callback_status") in {"delivered", "not_configured"}
            expired = now - float(job.get("completed_at") or job["updated_at"]) > retention
            if not force and not success and not expired:
                continue
            if await self.destroy(job["job_id"]):
                removed += 1
        return removed

    async def _dispatch_loop(self) -> None:
        while True:
            try:
                self._active_tasks = {key: task for key, task in self._active_tasks.items() if not task.done()}
                room = max(0, self.settings.telescope_max_active_jobs - len(self._active_tasks))
                if room:
                    for job in await self.store.next_queued_telescope_jobs(room):
                        job_id = str(job["job_id"])
                        self._active_tasks[job_id] = asyncio.create_task(self._run_job(job_id), name=f"telescope-{job_id}")
                if time.monotonic() - self._last_cleanup > 60:
                    await self._cleanup_terminal_files()
                    self._last_cleanup = time.monotonic()
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Telescope dispatcher failed")
                await asyncio.sleep(1)

    def _job_dir(self, job_id: str) -> Path:
        # Job ids from storage are not trusted as paths either.
        return self.work_root / hashlib.sha256(job_id.encode()).hexdigest()

    async def _run_job(self, job_id: str) -> None:
        cancel_event = self._cancel_events.setdefault(job_id, asyncio.Event())
        job_lock = None
        try:
            job = await self.store.get_telescope_job(job_id)
            if job is None:
                return
            if job.get("cancel_requested"):
                cancel_event.set()
                raise asyncio.CancelledError()
            directory = self._job_dir(job_id)
            directory.mkdir(parents=True, exist_ok=True)
            job_lock = (directory / ".job.lock").open("a+b")
            try:
                fcntl.flock(job_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # An orphaned child retains this flock until it exits. Never
                # infer ownership from a PID (which could already be reused).
                await self._report(job_id, "waiting_for_process", 0, force=True)
                await asyncio.sleep(5)
                await self.store.update_telescope_job(job_id, status="queued")
                return
            async with self._global_sem:
                await self._transfer(job_id, self._reference_from_job(job), cancel_event, process_lock_fd=job_lock.fileno())
        except asyncio.CancelledError:
            current = await self.store.get_telescope_job(job_id)
            if cancel_event.is_set() or (current and current.get("cancel_requested")):
                await self._abort_upload(current)
                await self.store.update_telescope_job(job_id, status="cancelled", last_error="Cancelled by administrator.", completed_at=time.time())
                await self._emit_event(await self.store.get_telescope_job(job_id), "cancelled")
            else:
                await self.store.update_telescope_job(job_id, status="queued")
            raise
        except (TelescopeTransferStalledError, TelescopeDiskCapacityError) as exc:
            current = await self.store.get_telescope_job(job_id)
            if int((current or {}).get("attempts") or 0) < self.settings.telescope_job_max_attempts:
                await self.store.update_telescope_job(job_id, status="waiting_for_space" if isinstance(exc, TelescopeDiskCapacityError) else "retrying", last_error=str(exc)[:4000])
                await self._emit_event(await self.store.get_telescope_job(job_id), "progress", discriminator="retry")
                await asyncio.sleep(self.settings.telescope_retry_delay_seconds)
                await self.store.update_telescope_job(job_id, status="queued")
            else:
                await self._fail_job(job_id, exc)
        except Exception as exc:
            await self._fail_job(job_id, exc)
        finally:
            if job_lock:
                job_lock.close()
            self._disk_reservations.pop(job_id, None)
            self.storage.release(job_id)
            self._cancel_events.pop(job_id, None)
            self._reports.pop(job_id, None)
            self._wake.set()

    async def _fail_job(self, job_id: str, exc: Exception) -> None:
        LOGGER.exception("Telescope job %s failed: %s", job_id, exc)
        await self.store.update_telescope_job(job_id, status="failed", last_error=str(exc)[:4000], completed_at=time.time())
        await self._emit_event(await self.store.get_telescope_job(job_id), "failed")

    async def _report(self, job_id: str, stage: str, percent: float, *, force: bool = False, **values: Any) -> None:
        now = time.monotonic()
        previous_stage, previous_time = self._reports.get(job_id, ("", 0.0))
        interval = getattr(self.settings, "telescope_progress_interval_seconds", 3.0)
        if not force and stage == previous_stage and now - previous_time < interval:
            return
        self._reports[job_id] = (stage, now)
        job = await self.store.get_telescope_job(job_id)
        if not job:
            return
        pct = min(100, max(0, float(percent)))
        metadata = dict(job.get("metadata") or {})
        metadata.update(stage=stage, stage_progress=pct, **values)
        overall = job.get("progress", 0)
        if stage == "downloading":
            metadata["download_progress"] = pct
            overall = pct * .35
        elif stage in MEDIA_STAGES:
            metadata["processing_progress"] = pct
            overall = 35 + pct * .45
        elif stage == "uploading":
            metadata["upload_progress"] = pct
            overall = 80 + pct * .18
        elif stage == "verifying":
            overall = 99
        await self.store.update_telescope_job(job_id, status=stage, progress=min(99, int(overall)), metadata_json=metadata)
        current = await self.store.get_telescope_job(job_id)
        await self._emit_event(current, "progress", discriminator=f"{stage}:{int(pct)}:{int(now // interval)}")

    def _reserve_disk(self, job_id: str, source_bytes: int) -> None:
        required = int(source_bytes * getattr(self.settings, "telescope_disk_expansion_factor", 3.0))
        self.work_root.mkdir(parents=True, exist_ok=True)
        reserve = getattr(self.settings, "telescope_disk_reserve_mb", 2048) * 1024 * 1024
        pending = 0
        for active_id, budget in {**self._disk_reservations, job_id: required}.items():
            directory = self._job_dir(active_id)
            present = sum(path.stat().st_size for path in directory.glob("*") if path.is_file() and not path.is_symlink())
            pending += max(0, budget - present)
        available = shutil.disk_usage(self.work_root).free
        if available < pending + reserve:
            raise TelescopeDiskCapacityError(f"Insufficient temporary disk space: {available} bytes free; {pending + reserve} bytes required including active jobs and safety reserve.")
        self._disk_reservations[job_id] = required

    async def _blocking(self, fn, *args, cancel_event: asyncio.Event, **kwargs):
        # Cancelling to_thread alone leaves its subprocess running. Signal the
        # owning thread and join it before releasing resource and file locks.
        stop = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(fn, *args, should_cancel=lambda: stop.is_set() or cancel_event.is_set(), **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            stop.set()
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _transfer(self, job_id: str, reference: TelegramMessageReference, cancel_event: asyncio.Event,
                        *, process_lock_fd: int | None = None) -> None:
        job = await self.store.get_telescope_job(job_id)
        checkpoint = dict(job.get("checkpoint") or {})
        directory = self._job_dir(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        source = directory / "source.media"
        # A verified-output checkpoint is required before recognizing a remote
        # object. Legacy raw Telegram uploads never satisfy this boundary.
        if checkpoint.get("output_validated") and job.get("object_key") and job.get("storage_target"):
            target = self.storage.resolve(job["storage_target"], 0)
            uploader = self._uploader(target)
            await self._report(job_id, "verifying", 0, force=True)
            recovered = await uploader._verified_result(job["object_key"], job["mime_type"], int(checkpoint["output_size"]), missing_ok=True)
            if recovered:
                await self._complete(job_id, recovered)
                return
        if not source.is_file() or source.stat().st_size != checkpoint.get("source_size"):
            await self._report(job_id, "resolving", 0, force=True)
            _, message = await self.worker.resolve_message_reference(reference)
            if not self.worker._is_supported_media(message):
                raise RuntimeError(unsupported_media_message(reference.message_id))
            total = int(getattr(message.file, "size", 0) or 0)
            if total <= 0:
                raise RuntimeError("Telegram did not provide a valid media size.")
            original_name = storage_safe_filename(self.worker._extract_file_name(message) or f"message-{reference.message_id}.bin")
            mime = str(getattr(message.file, "mime_type", "") or "application/octet-stream")
            # Switching an old direct-stream job to staged media must abandon
            # its unvalidated multipart bytes, never append processed MP4 to it.
            if not checkpoint and job.get("multipart_upload_id"):
                await self._abort_upload(job)
            document_id = str(getattr(getattr(message, "document", None), "id", ""))
            if checkpoint and (checkpoint.get("source_size") != total
                               or checkpoint.get("telegram_document_id", document_id) != document_id):
                (directory / "input.part").unlink(missing_ok=True)
            checkpoint.update(source_size=total, original_name=original_name, original_mime=mime,
                              telegram_document_id=document_id)
            await self.store.update_telescope_job(job_id, bytes_total=total, checkpoint_json=checkpoint)
            self._reserve_disk(job_id, total)
            channel = str(reference.peer_id or reference.username or "unknown").lower()
            channel_sem = self._channel_sems.setdefault(channel, asyncio.Semaphore(self.settings.telescope_max_downloads_per_channel))
            async with channel_sem, self._download_sem:
                await self._download(job_id, message, total, source, cancel_event)
            checkpoint["download_complete"] = True
            await self.store.update_telescope_job(job_id, checkpoint_json=checkpoint)
        self._reserve_disk(job_id, int(checkpoint["source_size"]))
        if cancel_event.is_set():
            raise asyncio.CancelledError()
        loop = asyncio.get_running_loop()

        def media_progress(stage, values):
            values = dict(values)
            pct = values.pop("progress_pct", 0) or 0
            future = asyncio.run_coroutine_threadsafe(self._report(job_id, stage, pct, **values), loop)
            future.result()

        async with self._inspect_sem:
            analysis = await self._blocking(analyze_media_for_processing, self.settings, source,
                                           job.get("processing"), cancel_event=cancel_event,
                                           progress_callback=media_progress)
        await self._report(job_id, "waiting_for_media", 0, force=True)
        if analysis.decision.encodes_video:
            prepared = await self._prepare_video(job, source, media_progress, cancel_event, process_lock_fd, analysis)
        else:
            # Cheap remux/audio work must not wait behind a movie-length x264 encode.
            try:
                async with self._remux_sem:
                    prepared = await self._prepare(job, source, media_progress, cancel_event, process_lock_fd, analysis,
                                                   allow_video_fallback=False)
            except MediaVideoRepairRequired:
                analysis.decision = replace(analysis.decision, encodes_video=True,
                    method="full_transcode" if analysis.decision.encodes_audio else "video_transcode",
                    reason="Stream-copy validation failed; compatibility repair required.")
                prepared = await self._prepare_video(job, source, media_progress, cancel_event, process_lock_fd, analysis)
        output = Path(prepared.delivery_path)
        if output.parent != directory or ".part" in output.name:
            raise RuntimeError("Media engine returned an unsafe or incomplete delivery path.")
        result_metadata = prepared.metadata()
        output_digest = await self._blocking(self._file_digest, output, cancel_event=cancel_event)
        previous_digest = checkpoint.get("output_sha256")
        checkpoint.update(output_validated=True, output_size=output.stat().st_size, output_file=output.name,
                          output_sha256=output_digest)
        formats = (prepared.output_probe.container_format or "").split(",")
        mime = ("video/mp4" if prepared.output_probe.is_mp4 else
                "video/x-matroska" if "matroska" in formats else
                "video/webm" if "webm" in formats else
                "video/quicktime" if "mov" in formats else "application/octet-stream")
        filename = storage_safe_filename(checkpoint["original_name"])
        if mime == "video/mp4":
            filename = str(Path(filename).with_suffix(".mp4"))
        # Preserve multipart state only when it describes this validated output.
        changes = {}
        if not job.get("checkpoint", {}).get("output_validated") or previous_digest != output_digest:
            if job.get("multipart_upload_id"):
                await self._abort_upload(job)
            changes.update(multipart_upload_id=None, multipart_parts_json=[], bytes_transferred=0, object_key=None)
        await self.store.update_telescope_job(job_id, checkpoint_json=checkpoint, processing_result_json=result_metadata,
                                             file_name=filename, mime_type=mime, bytes_total=output.stat().st_size, **changes)
        job = await self.store.get_telescope_job(job_id)
        total = output.stat().st_size
        target = self.storage.reserve(job_id, job.get("storage_target") or job.get("storage_target_requested"), total)
        created = datetime.fromtimestamp(float(job["created_at"]), tz=timezone.utc)
        object_key = job.get("object_key") or self.storage.build_object_key(target, job_id, filename, created.strftime("%Y/%m/%d"))
        await self.store.update_telescope_job(job_id, storage_target=target.key, object_key=object_key)
        await self._report(job_id, "waiting_for_upload", 0, force=True)
        async with self._upload_sem:
            offset = int(job.get("bytes_transferred") or 0)
            started = time.monotonic()

            async def chunks():
                with output.open("rb") as handle:
                    handle.seek(offset)
                    while True:
                        if cancel_event.is_set():
                            raise asyncio.CancelledError()
                        chunk = await asyncio.to_thread(handle.read, 1024 * 1024)
                        if not chunk:
                            return
                        yield chunk

            async def progress(current, total_bytes, upload_id, parts):
                # Multipart checkpoints are durable work and must be persisted
                # even when progress notifications are throttled.
                await self.store.update_telescope_job(job_id, bytes_transferred=current, multipart_upload_id=upload_id, multipart_parts_json=parts)
                elapsed = max(.001, time.monotonic() - started)
                speed = max(0, current - offset) / elapsed
                await self._report(job_id, "uploading", current * 100 / total_bytes, elapsed_seconds=elapsed,
                                   eta_seconds=(total_bytes - current) / speed if speed else None,
                                   transfer_speed_bytes_per_second=int(speed))

            async def verifying():
                await self._report(job_id, "verifying", 0, force=True)

            uploaded = await self._uploader(target).upload(chunks(), object_key=object_key, mime_type=mime, total_bytes=total,
                          upload_id=job.get("multipart_upload_id"), completed_parts=job.get("multipart_parts"),
                          bytes_transferred=offset, progress_callback=progress, verify_callback=verifying, cancelled=cancel_event.is_set)
        if cancel_event.is_set():
            raise asyncio.CancelledError()
        self.storage.release(job_id, committed=True)
        await self._complete(job_id, uploaded)

    @staticmethod
    def _file_digest(path, should_cancel=None):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                if should_cancel and should_cancel():
                    raise asyncio.CancelledError()
                digest.update(chunk)
        return digest.hexdigest()

    async def _download(self, job_id, message, total, source, cancelled):
        partial = source.parent / "input.part"
        offset = partial.stat().st_size if partial.exists() else 0
        offset = min(total, offset) // 4096 * 4096
        dc_id, location = telethon_utils.get_input_location(message.media or message)
        request_size = min(512 * 1024, max(4096, self.settings.download_chunk_size))
        request_size -= request_size % 4096
        stream = self.worker.client.iter_download(location, offset=offset, request_size=request_size,
                                                  chunk_size=request_size, file_size=total, dc_id=dc_id)
        started, received = time.monotonic(), offset
        try:
            with partial.open("r+b" if partial.exists() else "w+b") as handle:
                handle.truncate(offset)
                handle.seek(offset)
                iterator = stream.__aiter__()
                while True:
                    if cancelled.is_set():
                        raise asyncio.CancelledError()
                    try:
                        chunk = await asyncio.wait_for(iterator.__anext__(), self.settings.telescope_telegram_stall_timeout_seconds)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError as exc:
                        raise TelescopeTransferStalledError(f"Telegram produced no media bytes for {self.settings.telescope_telegram_stall_timeout_seconds} seconds.") from exc
                    if not chunk:
                        continue
                    self._check_disk_floor()
                    if received + len(chunk) > total:
                        raise RuntimeError("Telegram download exceeded its declared size.")
                    handle.write(chunk)
                    received += len(chunk)
                    elapsed = max(.001, time.monotonic() - started)
                    speed = (received - offset) / elapsed
                    await self._report(job_id, "downloading", received * 100 / total, streamed_bytes=received,
                                       elapsed_seconds=elapsed, transfer_speed_bytes_per_second=int(speed),
                                       eta_seconds=(total - received) / speed if speed else None)
                handle.flush()
                os.fsync(handle.fileno())
            if received != total:
                raise TelescopeTransferStalledError(f"Telegram download incomplete: {received}/{total} bytes; retained for retry.")
            partial.replace(source)
            await self._report(job_id, "downloading", 100, force=True, streamed_bytes=received)
        finally:
            await stream.close()

    def _uploader(self, target):
        return S3MultipartUploader(target, self.settings.telescope_multipart_part_size_mb * 1024 * 1024,
                                   self.settings.telescope_multipart_max_attempts, self.settings.telescope_upload_retry_base_ms)

    def _check_disk_floor(self):
        reserve = getattr(self.settings, "telescope_disk_reserve_mb", 2048) * 1024 * 1024
        if shutil.disk_usage(self.work_root).free < reserve:
            raise TelescopeDiskCapacityError("Temporary disk safety reserve reached; source retained for retry.")

    async def _prepare_video(self, job, source, progress, cancel_event, lock_fd, analysis):
        async with self._media_sem:
            if self._shared_media_sem is self._media_sem:
                return await self._prepare(job, source, progress, cancel_event, lock_fd, analysis)
            async with self._shared_media_sem:
                return await self._prepare(job, source, progress, cancel_event, lock_fd, analysis)

    async def _prepare(self, job, source, progress, cancel_event, lock_fd, analysis=None, allow_video_fallback=True):
        def guarded_progress(stage, values):
            self._check_disk_floor()
            progress(stage, values)
        return await self._blocking(prepare_media_for_processing, self.settings, source, job.get("processing"),
                                    progress_callback=guarded_progress, cancel_event=cancel_event,
                                    process_lock_fd=lock_fd, analysis=analysis, allow_video_fallback=allow_video_fallback)

    async def _abort_upload(self, job):
        if job and job.get("multipart_upload_id") and job.get("object_key") and job.get("storage_target"):
            try:
                target = self.storage.resolve(job["storage_target"], 0)
                await self._uploader(target).abort(job["object_key"], job["multipart_upload_id"])
            except Exception:
                LOGGER.warning("Unable to abort multipart upload for job %s; bucket lifecycle must expire it.", job["job_id"], exc_info=True)
            await self.store.update_telescope_job(job["job_id"], multipart_upload_id=None, multipart_parts_json=[], bytes_transferred=0)

    async def _complete(self, job_id, result):
        job = await self.store.get_telescope_job(job_id)
        callback = bool(job.get("portal_source_id") and self.settings.telescope_callback_url and self.settings.telescope_callback_secret)
        await self.store.update_telescope_job(job_id, status="ready", progress=100, bytes_transferred=result["bytes"],
                                             result_json=result, callback_status="pending" if callback else "not_configured",
                                             last_error=None, completed_at=time.time())
        await self._emit_event(await self.store.get_telescope_job(job_id), "ready")

    async def _cleanup_terminal_files(self):
        await self.cleanup_terminal_files()

    async def _emit_event(self, job: dict[str, Any], event_type: str, discriminator: str = "") -> None:
        if (
            not job.get("portal_source_id")
            or not self.settings.telescope_callback_url
            or not self.settings.telescope_callback_secret
        ):
            return
        attempt = int(job.get("attempts") or 0)
        event_id = str(uuid5(NAMESPACE_URL, f"teletyde:{job['job_id']}:{attempt}:{event_type}:{discriminator}"))
        metadata = job.get("metadata") or {}
        payload = {
            "event_id": event_id,
            "event_type": event_type,
            "job_id": job["job_id"],
            "attempt": attempt,
            "portal_source_id": job.get("portal_source_id"),
            "status": "ready" if event_type == "ready" else job.get("status"),
            "progress": int(job.get("progress") or 0),
            "bytes_total": int(job.get("bytes_total") or 0),
            "bytes_transferred": int(job.get("bytes_transferred") or 0),
            "storage": job.get("result"),
            "telegram": job.get("reference"),
            "file_name": job.get("file_name"),
            "mime_type": job.get("mime_type"),
            "error": job.get("last_error"),
            "processing": job.get("processing") or {},
            "processing_result": job.get("processing_result") or {},
            "metrics": job.get("processing_result") or {},
            "output_metadata": (job.get("processing_result") or {}).get("output_probe"),
            "stage": metadata.get("stage", job.get("status")),
            "stage_progress": metadata.get("stage_progress", 0),
            "elapsed_seconds": metadata.get("elapsed_seconds"),
            "eta_seconds": metadata.get("eta_seconds"),
            "download_progress": metadata.get("download_progress", 0),
            "processing_progress": metadata.get("processing_progress", 0),
            "upload_progress": metadata.get("upload_progress", 0),
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        }
        await self.store.enqueue_telescope_callback(
            {"event_id": event_id, "job_id": job["job_id"], "event_type": event_type, "payload": payload}
        )

    async def _callback_loop(self) -> None:
        while True:
            try:
                if not self.settings.telescope_callback_url or not self.settings.telescope_callback_secret:
                    await asyncio.sleep(5)
                    continue
                callbacks = await self.store.due_telescope_callbacks()
                for callback in callbacks:
                    await self._deliver_callback(callback)
                await asyncio.sleep(1 if callbacks else 3)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                LOGGER.exception("Telescope callback dispatcher failed")
                await asyncio.sleep(3)

    async def _deliver_callback(self, callback: dict[str, Any]) -> None:
        body = orjson.dumps(callback["payload"])
        signature = hmac.new(
            self.settings.telescope_callback_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        try:
            response = await self._http.post(
                self.settings.telescope_callback_url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-Teletyde-Signature": f"sha256={signature}",
                    "X-Teletyde-Event": str(callback["event_id"]),
                },
            )
            response.raise_for_status()
            await self.store.mark_telescope_callback_delivered(
                str(callback["event_id"]),
                str(callback["job_id"]),
                str(callback["event_type"]),
            )
        except Exception as exc:  # noqa: BLE001
            await self.store.mark_telescope_callback_failed(
                str(callback["event_id"]),
                str(callback["job_id"]),
                str(exc),
                int(callback.get("attempts") or 0),
            )

    @staticmethod
    def _reference_from_job(job: dict[str, Any]) -> TelegramMessageReference:
        reference = job.get("reference") or {}
        return TelegramMessageReference(
            type=str(reference["type"]),
            message_id=int(reference["message_id"]),
            original_url=str(reference.get("original_url") or job["telegram_url"]),
            username=reference.get("username"),
            channel_internal_id=reference.get("channel_internal_id"),
            peer_id=reference.get("peer_id"),
            topic_id=reference.get("topic_id"),
        )
