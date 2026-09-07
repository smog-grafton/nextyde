from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.db import StateStore
from app.object_storage import ObjectStorageManager, ObjectStorageTarget, S3MultipartUploader
from app.telescope import TelescopeScheduler


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


if __name__ == "__main__":
    unittest.main()
