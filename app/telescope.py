from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import logging
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
from app.telegram_worker import TelegramPipeWorker


LOGGER = logging.getLogger("telebot.telescope")
TERMINAL_STATUSES = {"ready", "failed", "cancelled"}


class TelescopeTransferStalledError(TimeoutError):
    """Raised when Telegram yields no data for the configured watchdog period."""


class TelescopeScheduler:
    """Persistent direct Telegram-to-object-storage queue and callback dispatcher."""

    def __init__(
        self,
        worker: TelegramPipeWorker,
        store: StateStore,
        storage: ObjectStorageManager | None = None,
    ) -> None:
        self.worker = worker
        self.store = store
        self.settings = worker.settings
        self.storage = storage or ObjectStorageManager()
        self._global_sem = asyncio.Semaphore(self.settings.telescope_max_active_jobs)
        self._channel_sems: dict[str, asyncio.Semaphore] = {}
        self._active_tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._dispatch_task: asyncio.Task[None] | None = None
        self._callback_task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._http = httpx.AsyncClient(timeout=self.settings.telescope_callback_timeout_seconds)

    async def start(self) -> None:
        await self.store.recover_telescope_jobs()
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

    async def submit(
        self,
        telegram_url: str,
        *,
        storage_target: str | None = None,
        portal_source_id: str | int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.settings.telescope_enabled:
            raise RuntimeError("Telescope direct storage imports are disabled.")
        reference = parse_telegram_reference(telegram_url)
        if portal_source_id is not None and str(portal_source_id).strip():
            job_id = str(uuid5(NAMESPACE_URL, f"teletyde:telescope:portal:{portal_source_id}"))
            existing = await self.store.get_telescope_job(job_id)
            if existing is not None:
                return existing
        else:
            job_id = str(uuid4())
        job = await self.store.create_telescope_job(
            {
                "job_id": job_id,
                "telegram_url": reference.canonical_url(),
                "reference": reference.as_dict(),
                "portal_source_id": portal_source_id,
                "storage_target": storage_target or self.settings.telescope_default_storage_target,
                "metadata": metadata or {},
            }
        )
        await self._emit_event(job, "accepted")
        self._wake.set()
        return job

    async def cancel(self, job_id: str) -> bool:
        updated = await self.store.request_telescope_cancel(job_id)
        event = self._cancel_events.get(job_id)
        if event:
            event.set()
        self._wake.set()
        return updated

    async def retry(self, job_id: str) -> bool:
        updated = await self.store.requeue_telescope_job(job_id)
        self._wake.set()
        return updated

    async def _dispatch_loop(self) -> None:
        while True:
            try:
                self._active_tasks = {
                    job_id: task for job_id, task in self._active_tasks.items() if not task.done()
                }
                # Waiting tasks do not hold a global transfer slot. Claiming a
                # bounded batch keeps the persistent DB queue authoritative.
                room = max(0, 100 - len(self._active_tasks))
                if room:
                    for job in await self.store.next_queued_telescope_jobs(room):
                        job_id = str(job["job_id"])
                        task = asyncio.create_task(self._run_job(job_id), name=f"telescope-{job_id}")
                        self._active_tasks[job_id] = task
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                LOGGER.exception("Telescope queue dispatcher failed")
                await asyncio.sleep(1)

    async def _run_job(self, job_id: str) -> None:
        cancel_event = self._cancel_events.setdefault(job_id, asyncio.Event())
        job = await self.store.get_telescope_job(job_id)
        if job is None:
            return
        reference = self._reference_from_job(job)
        channel_key = str(reference.peer_id or reference.username or "unknown").lower()
        channel_sem = self._channel_sems.setdefault(
            channel_key,
            asyncio.Semaphore(self.settings.telescope_max_downloads_per_channel),
        )

        try:
            await self.store.update_telescope_job(job_id, status="waiting_for_slot")
            async with channel_sem:
                async with self._global_sem:
                    if cancel_event.is_set():
                        raise asyncio.CancelledError("Telescope import cancelled")
                    await self._transfer(job_id, reference, cancel_event)
        except asyncio.CancelledError:
            if cancel_event.is_set():
                await self.store.update_telescope_job(
                    job_id,
                    status="cancelled",
                    last_error="Cancelled by administrator.",
                    completed_at=time.time(),
                )
                cancelled = await self.store.get_telescope_job(job_id)
                if cancelled:
                    await self._emit_event(cancelled, "cancelled")
            else:
                # Service shutdown: the startup recovery pass will safely
                # return this resumable job to the queue.
                await self.store.update_telescope_job(job_id, status="queued")
            raise
        except TelescopeTransferStalledError as exc:
            current = await self.store.get_telescope_job(job_id)
            attempts = int((current or {}).get("attempts") or 0)
            if attempts < self.settings.telescope_job_max_attempts:
                LOGGER.warning(
                    "Telescope job %s stalled on Telegram attempt %s/%s; retrying in %ss",
                    job_id,
                    attempts,
                    self.settings.telescope_job_max_attempts,
                    self.settings.telescope_retry_delay_seconds,
                )
                await self.store.update_telescope_job(
                    job_id,
                    status="retrying",
                    last_error=str(exc)[:4000],
                )
                await asyncio.sleep(self.settings.telescope_retry_delay_seconds)
                await self.store.update_telescope_job(job_id, status="queued")
            else:
                await self._fail_job(job_id, exc)
        except Exception as exc:  # noqa: BLE001
            await self._fail_job(job_id, exc)
        finally:
            self._cancel_events.pop(job_id, None)
            self._wake.set()

    async def _fail_job(self, job_id: str, exc: Exception) -> None:
        LOGGER.exception("Telescope job %s failed: %s", job_id, exc)
        await self.store.update_telescope_job(
            job_id,
            status="failed",
            last_error=str(exc)[:4000],
            completed_at=time.time(),
        )
        failed = await self.store.get_telescope_job(job_id)
        if failed:
            await self._emit_event(failed, "failed")

    async def _transfer(
        self,
        job_id: str,
        reference: TelegramMessageReference,
        cancel_event: asyncio.Event,
    ) -> None:
        await self.store.update_telescope_job(job_id, status="resolving", progress=0)
        job = await self.store.get_telescope_job(job_id)
        if job:
            await self._emit_event(job, "started")
        entity, message = await self.worker.resolve_message_reference(reference)
        if not self.worker._is_supported_media(message):
            raise RuntimeError(
                f"Telegram message {reference.message_id} has no supported downloadable video/document."
            )

        original_name = self.worker._extract_file_name(message) or f"message-{reference.message_id}.bin"
        file_name = storage_safe_filename(original_name)
        total_bytes = int(getattr(message.file, "size", 0) or 0)
        mime_type = str(getattr(message.file, "mime_type", "") or "application/octet-stream")
        if total_bytes <= 0:
            raise RuntimeError("Telegram did not provide a valid media size for this message.")

        job = await self.store.get_telescope_job(job_id)
        if job is None:
            return
        # Once a multipart upload has started it must stay on the same target,
        # even if `auto` priorities change while Teletyde is restarting.
        target = self.storage.resolve(
            job.get("storage_target") or job.get("storage_target_requested"),
            total_bytes,
        )
        created_at = datetime.fromtimestamp(float(job["created_at"]), tz=timezone.utc)
        object_key = job.get("object_key") or self.storage.build_object_key(
            target,
            job_id,
            file_name,
            created_at.strftime("%Y/%m/%d"),
        )
        transferred = int(job.get("bytes_transferred") or 0)
        parts = list(job.get("multipart_parts") or [])
        upload_id = str(job.get("multipart_upload_id") or "") or None

        await self.store.update_telescope_job(
            job_id,
            status="transferring",
            file_name=file_name,
            mime_type=mime_type,
            bytes_total=total_bytes,
            storage_target=target.key,
            object_key=object_key,
        )

        dc_id, input_location = telethon_utils.get_input_location(message.media or message)

        last_event_pct = int(job.get("progress") or 0) // 10 * 10
        started = time.monotonic()
        last_stream_report = 0.0
        last_logged_pct = -1
        streamed = transferred
        durable = transferred
        transfer_metadata = dict(job.get("metadata") or {})

        async def telegram_chunks():
            nonlocal last_event_pct, last_stream_report, last_logged_pct, streamed
            request_size = min(512 * 1024, max(4096, self.worker.settings.download_chunk_size))
            request_size -= request_size % 4096
            stream = self.worker.client.iter_download(
                input_location,
                offset=transferred,
                request_size=request_size,
                chunk_size=request_size,
                file_size=total_bytes,
                dc_id=dc_id,
            )
            iterator = stream.__aiter__()
            try:
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            iterator.__anext__(),
                            timeout=self.settings.telescope_telegram_stall_timeout_seconds,
                        )
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError as exc:
                        raise TelescopeTransferStalledError(
                            "Telegram produced no media bytes for "
                            f"{self.settings.telescope_telegram_stall_timeout_seconds} seconds."
                        ) from exc

                    if cancel_event.is_set():
                        raise asyncio.CancelledError("Telescope import cancelled")
                    if not chunk:
                        continue

                    chunk_bytes = bytes(chunk)
                    streamed += len(chunk_bytes)
                    pct = min(99, int(streamed * 100 / total_bytes))
                    now = time.monotonic()
                    if last_stream_report == 0.0 or now - last_stream_report >= 2.0 or pct >= last_logged_pct + 10:
                        elapsed = max(0.001, now - started)
                        speed = int(max(0, streamed - transferred) / elapsed)
                        transfer_metadata["streamed_bytes"] = streamed
                        transfer_metadata["durable_bytes"] = durable
                        transfer_metadata["transfer_speed_bytes_per_second"] = speed
                        transfer_metadata["eta_seconds"] = int((total_bytes - streamed) / speed) if speed else None
                        await self.store.update_telescope_job(
                            job_id,
                            status="transferring",
                            progress=pct,
                            metadata_json=transfer_metadata,
                        )
                        last_stream_report = now
                        if pct >= last_logged_pct + 10 or last_logged_pct < 0:
                            last_logged_pct = pct // 10 * 10
                            LOGGER.info(
                                "Telescope job %s receiving Telegram media: %s/%s bytes (%s%%, %s bytes/s)",
                                job_id,
                                streamed,
                                total_bytes,
                                pct,
                                speed,
                            )
                        milestone = pct // 10 * 10
                        if milestone >= last_event_pct + 10:
                            last_event_pct = milestone
                            current_job = await self.store.get_telescope_job(job_id)
                            if current_job:
                                await self._emit_event(current_job, "progress", discriminator=str(milestone))
                    yield chunk_bytes
            finally:
                await stream.close()

        async def progress(
            current: int,
            total: int,
            current_upload_id: str,
            current_parts: list[dict[str, Any]],
        ) -> None:
            nonlocal durable, last_event_pct
            durable = current
            pct = min(99, int(current * 100 / total)) if total else 0
            elapsed = max(0.001, time.monotonic() - started)
            speed = int(max(0, current - transferred) / elapsed)
            transfer_metadata["streamed_bytes"] = max(streamed, current)
            transfer_metadata["durable_bytes"] = current
            transfer_metadata["transfer_speed_bytes_per_second"] = speed
            transfer_metadata["eta_seconds"] = int((total - current) / speed) if speed else None
            await self.store.update_telescope_job(
                job_id,
                status="transferring",
                progress=pct,
                bytes_transferred=current,
                multipart_upload_id=current_upload_id,
                multipart_parts_json=current_parts,
                metadata_json=transfer_metadata,
            )
            milestone = pct // 10 * 10
            if milestone >= last_event_pct + 10:
                last_event_pct = milestone
                current_job = await self.store.get_telescope_job(job_id)
                if current_job:
                    await self._emit_event(current_job, "progress", discriminator=str(milestone))

        uploader = S3MultipartUploader(
            target,
            self.settings.telescope_multipart_part_size_mb * 1024 * 1024,
            self.settings.telescope_multipart_max_attempts,
            self.settings.telescope_upload_retry_base_ms,
        )

        async def verifying() -> None:
            await self.store.update_telescope_job(job_id, status="verifying", progress=99)

        result = await uploader.upload(
            telegram_chunks(),
            object_key=object_key,
            mime_type=mime_type,
            total_bytes=total_bytes,
            upload_id=upload_id,
            completed_parts=parts,
            bytes_transferred=transferred,
            progress_callback=progress,
            verify_callback=verifying,
            cancelled=cancel_event.is_set,
        )
        callback_configured = bool(job.get("portal_source_id")) and bool(
            self.settings.telescope_callback_url and self.settings.telescope_callback_secret
        )
        await self.store.update_telescope_job(
            job_id,
            status="ready",
            progress=100,
            bytes_transferred=total_bytes,
            result_json=result,
            callback_status="pending" if callback_configured else "not_configured",
            completed_at=time.time(),
        )
        completed = await self.store.get_telescope_job(job_id)
        if completed:
            await self._emit_event(completed, "ready")

    async def _emit_event(self, job: dict[str, Any], event_type: str, discriminator: str = "") -> None:
        if (
            not job.get("portal_source_id")
            or not self.settings.telescope_callback_url
            or not self.settings.telescope_callback_secret
        ):
            return
        event_id = str(uuid5(NAMESPACE_URL, f"teletyde:{job['job_id']}:{event_type}:{discriminator}"))
        payload = {
            "event_id": event_id,
            "event_type": event_type,
            "job_id": job["job_id"],
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
