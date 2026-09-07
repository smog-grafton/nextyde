from __future__ import annotations

import asyncio
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.db import StateStore
from app.object_storage import ObjectStorageManager, ObjectStorageTarget, S3MultipartUploader
from app.telescope import TelescopeScheduler, TelescopeTransferStalledError


def target(key: str, *, priority: int = 0, used: int = 0) -> ObjectStorageTarget:
    return ObjectStorageTarget(
        key=key,
        label=key,
        provider="s3",
        endpoint="https://objects.example",
        region="auto",
        bucket="movies",
        public_url="https://cdn.example",
        path_prefix="videos",
        access_key_id="key",
        secret_access_key="secret",
        use_path_style_endpoint=False,
        enabled=True,
        writable=True,
        priority=priority,
        capacity_bytes=1000,
        reserve_percent=10,
        known_used_bytes=used,
    )


class ObjectStorageManagerTests(unittest.TestCase):
    def test_auto_prefers_highest_priority_safe_target(self) -> None:
        manager = ObjectStorageManager(
            {
                "legacy": target("legacy", priority=20),
                "new": target("new", priority=100),
                "full": target("full", priority=200, used=899),
            }
        )

        self.assertEqual(manager.resolve("auto", 100).key, "new")

    def test_explicit_target_enforces_safe_capacity(self) -> None:
        manager = ObjectStorageManager({"full": target("full", used=850)})

        with self.assertRaisesRegex(RuntimeError, "safe capacity"):
            manager.resolve("full", 100)

    def test_public_url_encodes_filename_without_destroying_path(self) -> None:
        storage = target("demo")
        self.assertEqual(
            storage.object_url("videos/telescope/job/Movie Name's Cut.mkv"),
            "https://cdn.example/videos/telescope/job/Movie%20Name%27s%20Cut.mkv",
        )


class FakeS3Client:
    def __init__(self) -> None:
        self.part_sizes: list[int] = []
        self.completed_parts: list[dict] = []
        self.complete_calls = 0

    def create_multipart_upload(self, **kwargs):
        return {"UploadId": "upload-1"}

    def upload_part(self, **kwargs):
        self.part_sizes.append(len(kwargs["Body"]))
        return {"ETag": f"etag-{kwargs['PartNumber']}"}

    def complete_multipart_upload(self, **kwargs):
        self.complete_calls += 1
        self.completed_parts = kwargs["MultipartUpload"]["Parts"]
        return {}

    def head_object(self, **kwargs):
        return {"ContentLength": 6 * 1024 * 1024, "ContentType": "video/x-matroska", "ETag": '"final"'}

    def abort_multipart_upload(self, **kwargs):
        return {}


class MultipartUploaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_is_split_into_bounded_s3_parts_and_verified(self) -> None:
        fake = FakeS3Client()
        with patch("app.object_storage.boto3.client", return_value=fake):
            uploader = S3MultipartUploader(target("demo"), 5 * 1024 * 1024, 2, 100)

        async def chunks():
            for _ in range(6):
                yield b"x" * (1024 * 1024)

        progress: list[int] = []

        async def record(current, total, upload_id, parts):
            progress.append(current)

        result = await uploader.upload(
            chunks(),
            object_key="videos/movie.mkv",
            mime_type="video/x-matroska",
            total_bytes=6 * 1024 * 1024,
            progress_callback=record,
        )

        self.assertEqual(fake.part_sizes, [5 * 1024 * 1024, 1024 * 1024])
        self.assertEqual([part["PartNumber"] for part in fake.completed_parts], [1, 2])
        self.assertEqual(progress[-1], 6 * 1024 * 1024)
        self.assertEqual(result["public_url"], "https://cdn.example/videos/movie.mkv")

    async def test_restart_recovers_object_completed_before_result_was_persisted(self) -> None:
        fake = FakeS3Client()
        with patch("app.object_storage.boto3.client", return_value=fake):
            uploader = S3MultipartUploader(target("demo"), 5 * 1024 * 1024, 2, 100)

        async def chunks():
            raise AssertionError("Telegram must not be downloaded again")
            yield b""  # pragma: no cover

        result = await uploader.upload(
            chunks(),
            object_key="videos/movie.mkv",
            mime_type="video/x-matroska",
            total_bytes=6 * 1024 * 1024,
            upload_id="already-completed-upload",
            completed_parts=[{"PartNumber": 1, "ETag": "etag-1"}],
            bytes_transferred=6 * 1024 * 1024,
        )

        self.assertEqual(fake.complete_calls, 0)
        self.assertEqual(result["bytes"], 6 * 1024 * 1024)


class TelescopeStateStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_retry_does_not_fake_transfer_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StateStore(str(Path(temp_dir) / "state.db"))
            await store.init()
            created = await store.create_telescope_job(
                {
                    "job_id": "callback-job",
                    "telegram_url": "https://t.me/demo/1",
                    "reference": {"type": "public_channel_message", "username": "demo", "message_id": 1},
                    "storage_target": "auto",
                }
            )
            await store.enqueue_telescope_callback(
                {
                    "event_id": "event-1",
                    "job_id": "callback-job",
                    "event_type": "progress",
                    "payload": {"event_id": "event-1"},
                }
            )
            await store.mark_telescope_callback_failed("event-1", "callback-job", "HTTP 404", 0)
            updated = await store.get_telescope_job("callback-job")

            self.assertEqual(updated["updated_at"], created["updated_at"])
            self.assertEqual(updated["callback_attempts"], 1)

    async def _transfer_fixture(self, stream):
        temp_dir = tempfile.TemporaryDirectory()
        store = StateStore(str(Path(temp_dir.name) / "state.db"))
        await store.init()
        await store.create_telescope_job(
            {
                "job_id": "transfer-job",
                "telegram_url": "https://t.me/demo/1",
                "reference": {"type": "public_channel_message", "username": "demo", "message_id": 1},
                "storage_target": "demo",
            }
        )
        message = SimpleNamespace(
            media=object(),
            file=SimpleNamespace(size=20 * 1024 * 1024, mime_type="video/mp4"),
        )

        class Client:
            def iter_download(self, *args, **kwargs):
                return stream

        settings = SimpleNamespace(
            telescope_max_active_jobs=4,
            telescope_max_downloads_per_channel=2,
            telescope_callback_timeout_seconds=5,
            telescope_callback_url=None,
            telescope_callback_secret=None,
            telescope_telegram_stall_timeout_seconds=0.02,
            telescope_multipart_part_size_mb=8,
            telescope_multipart_max_attempts=2,
            telescope_upload_retry_base_ms=100,
            download_chunk_size=1024 * 1024,
        )

        class Worker:
            def __init__(self):
                self.settings = settings
                self.client = Client()

            async def resolve_message_reference(self, reference):
                return object(), message

            def _is_supported_media(self, value):
                return True

            def _extract_file_name(self, value):
                return "movie.mp4"

        storage_target = replace(target("demo"), capacity_bytes=0)
        scheduler = TelescopeScheduler(Worker(), store, ObjectStorageManager({"demo": storage_target}))
        return temp_dir, store, scheduler

    async def test_transfer_reports_telegram_bytes_before_first_s3_part(self) -> None:
        class OneChunkStream:
            def __init__(self):
                self.sent = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.sent:
                    raise StopAsyncIteration
                self.sent = True
                return b"x" * (512 * 1024)

            async def close(self):
                return None

        temp_dir, store, scheduler = await self._transfer_fixture(OneChunkStream())
        observed = {}

        class InspectingUploader:
            async def upload(self, chunks, **kwargs):
                iterator = chunks.__aiter__()
                await iterator.__anext__()
                observed.update(await store.get_telescope_job("transfer-job"))
                await iterator.aclose()
                return {
                    "storage_target": "demo",
                    "bucket": "movies",
                    "object_key": "videos/movie.mp4",
                    "public_url": "https://cdn.example/videos/movie.mp4",
                    "bytes": 20 * 1024 * 1024,
                }

        try:
            with patch("app.telescope.telethon_utils.get_input_location", return_value=(1, object())), patch(
                "app.telescope.S3MultipartUploader", return_value=InspectingUploader()
            ):
                await scheduler._transfer(
                    "transfer-job",
                    scheduler._reference_from_job(await store.get_telescope_job("transfer-job")),
                    asyncio.Event(),
                )
            self.assertEqual(observed["bytes_transferred"], 0)
            self.assertEqual(observed["metadata"]["streamed_bytes"], 512 * 1024)
            self.assertGreater(observed["progress"], 0)
        finally:
            await scheduler._http.aclose()
            temp_dir.cleanup()

    async def test_transfer_watchdog_rejects_a_silent_telegram_stream(self) -> None:
        class SilentStream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.sleep(60)

            async def close(self):
                return None

        temp_dir, store, scheduler = await self._transfer_fixture(SilentStream())

        class ConsumingUploader:
            async def upload(self, chunks, **kwargs):
                async for _ in chunks:
                    pass

        try:
            with patch("app.telescope.telethon_utils.get_input_location", return_value=(1, object())), patch(
                "app.telescope.S3MultipartUploader", return_value=ConsumingUploader()
            ):
                with self.assertRaisesRegex(TelescopeTransferStalledError, "no media bytes"):
                    await scheduler._transfer(
                        "transfer-job",
                        scheduler._reference_from_job(await store.get_telescope_job("transfer-job")),
                        asyncio.Event(),
                    )
        finally:
            await scheduler._http.aclose()
            temp_dir.cleanup()

    async def test_queue_and_multipart_state_survive_store_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            store = StateStore(str(db_path))
            await store.init()
            await store.create_telescope_job(
                {
                    "job_id": "job-1",
                    "telegram_url": "https://t.me/demo/1",
                    "reference": {"type": "public_channel_message", "username": "demo", "message_id": 1},
                    "portal_source_id": 42,
                    "storage_target": "auto",
                    "metadata": {"title": "Movie"},
                }
            )
            claimed = await store.next_queued_telescope_jobs(1)
            self.assertEqual(claimed[0]["status"], "resolving")
            await store.update_telescope_job(
                "job-1",
                status="transferring",
                bytes_transferred=32,
                multipart_upload_id="upload-1",
                multipart_parts_json=[{"PartNumber": 1, "ETag": "etag"}],
            )

            reopened = StateStore(str(db_path))
            await reopened.init()
            self.assertEqual(await reopened.recover_telescope_jobs(), 1)
            recovered = await reopened.get_telescope_job("job-1")

            self.assertEqual(recovered["status"], "queued")
            self.assertEqual(recovered["multipart_upload_id"], "upload-1")
            self.assertEqual(recovered["multipart_parts"][0]["PartNumber"], 1)
            self.assertEqual(recovered["bytes_transferred"], 32)

    async def test_portal_submission_is_idempotent_and_dashboard_job_has_no_callback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StateStore(str(Path(temp_dir) / "state.db"))
            await store.init()
            settings = SimpleNamespace(
                telescope_enabled=True,
                telescope_default_storage_target="auto",
                telescope_max_active_jobs=4,
                telescope_max_downloads_per_channel=2,
                telescope_callback_timeout_seconds=5,
                telescope_callback_url="https://portal.example/callback",
                telescope_callback_secret="secret",
                telescope_job_max_attempts=3,
                telescope_retry_delay_seconds=0,
            )
            scheduler = TelescopeScheduler(SimpleNamespace(settings=settings), store)
            try:
                first = await scheduler.submit("https://t.me/demo/1", portal_source_id=42)
                duplicate = await scheduler.submit("https://t.me/demo/1", portal_source_id=42)
                dashboard = await scheduler.submit("https://t.me/demo/2")

                self.assertEqual(first["job_id"], duplicate["job_id"])
                self.assertNotEqual(first["job_id"], dashboard["job_id"])
                self.assertEqual(len(await store.list_telescope_jobs()), 2)
                callbacks = await store.due_telescope_callbacks()
                self.assertEqual(len(callbacks), 1)
                self.assertEqual(callbacks[0]["job_id"], first["job_id"])
            finally:
                await scheduler._http.aclose()

    async def test_scheduler_enforces_four_global_and_two_per_channel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StateStore(str(Path(temp_dir) / "state.db"))
            await store.init()
            settings = SimpleNamespace(
                telescope_enabled=True,
                telescope_default_storage_target="auto",
                telescope_max_active_jobs=4,
                telescope_max_downloads_per_channel=2,
                telescope_callback_timeout_seconds=5,
                telescope_callback_url=None,
                telescope_callback_secret=None,
                telescope_job_max_attempts=3,
                telescope_retry_delay_seconds=0,
            )
            scheduler = TelescopeScheduler(SimpleNamespace(settings=settings), store)
            for channel in ("channel_a", "channel_b"):
                for message_id in range(1, 5):
                    await scheduler.submit(f"https://t.me/{channel}/{message_id}")

            active_global = 0
            active_by_channel = {"channel_a": 0, "channel_b": 0}
            maximum_global = 0
            maximum_by_channel = {"channel_a": 0, "channel_b": 0}
            completed = 0
            all_completed = asyncio.Event()

            async def fake_transfer(job_id, reference, cancel_event):
                nonlocal active_global, maximum_global, completed
                channel = str(reference.username)
                active_global += 1
                active_by_channel[channel] += 1
                maximum_global = max(maximum_global, active_global)
                maximum_by_channel[channel] = max(maximum_by_channel[channel], active_by_channel[channel])
                try:
                    await asyncio.sleep(0.03)
                finally:
                    active_by_channel[channel] -= 1
                    active_global -= 1
                    completed += 1
                    if completed == 8:
                        all_completed.set()

            scheduler._transfer = fake_transfer
            await scheduler.start()
            try:
                await asyncio.wait_for(all_completed.wait(), timeout=3)
            finally:
                await scheduler.stop()

            self.assertEqual(maximum_global, 4)
            self.assertEqual(maximum_by_channel, {"channel_a": 2, "channel_b": 2})

    async def test_stalled_job_retries_then_becomes_failed_at_attempt_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StateStore(str(Path(temp_dir) / "state.db"))
            await store.init()
            await store.create_telescope_job(
                {
                    "job_id": "stalled-job",
                    "telegram_url": "https://t.me/demo/1",
                    "reference": {"type": "public_channel_message", "username": "demo", "message_id": 1},
                    "storage_target": "auto",
                }
            )
            settings = SimpleNamespace(
                telescope_max_active_jobs=4,
                telescope_max_downloads_per_channel=2,
                telescope_callback_timeout_seconds=5,
                telescope_callback_url=None,
                telescope_callback_secret=None,
                telescope_job_max_attempts=2,
                telescope_retry_delay_seconds=0,
            )
            scheduler = TelescopeScheduler(SimpleNamespace(settings=settings), store)
            try:
                await store.next_queued_telescope_jobs(1)
                with patch.object(
                    scheduler,
                    "_transfer",
                    side_effect=TelescopeTransferStalledError("Telegram produced no bytes."),
                ):
                    await scheduler._run_job("stalled-job")
                self.assertEqual((await store.get_telescope_job("stalled-job"))["status"], "queued")

                await store.next_queued_telescope_jobs(1)
                with patch.object(
                    scheduler,
                    "_transfer",
                    side_effect=TelescopeTransferStalledError("Telegram produced no bytes."),
                ), self.assertLogs("telebot.telescope", level="ERROR"):
                    await scheduler._run_job("stalled-job")
                failed = await store.get_telescope_job("stalled-job")
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(failed["attempts"], 2)
            finally:
                await scheduler._http.aclose()


if __name__ == "__main__":
    unittest.main()
