from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.telegram_worker import TelegramPipeWorker


class DownloadSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_complete_download_is_renamed_from_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "source.mkv"
            worker = self.worker(directory)

            async def download(_location, **kwargs):
                self.assertEqual(kwargs["file"].name, "source.mkv.part")
                self.assertFalse(destination.exists())
                kwargs["file"].write_bytes(b"complete")

            worker.client = SimpleNamespace(download_file=download)
            message = SimpleNamespace(file=SimpleNamespace(size=8), media=object())
            with patch("app.telegram_worker.telethon_utils.get_input_location", return_value=(1, object())):
                await worker._download_media_to_path(message, destination)
            self.assertEqual(destination.read_bytes(), b"complete")
            self.assertFalse(destination.with_name("source.mkv.part").exists())

    async def test_truncated_download_never_becomes_final(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "source.mkv"
            worker = self.worker(directory)

            async def download(_location, **kwargs):
                kwargs["file"].write_bytes(b"partial")

            worker.client = SimpleNamespace(download_file=download)
            message = SimpleNamespace(file=SimpleNamespace(size=1024), media=object())
            with patch("app.telegram_worker.telethon_utils.get_input_location", return_value=(1, object())):
                with self.assertRaisesRegex(RuntimeError, "size mismatch"):
                    await worker._download_media_to_path(message, destination)
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_disk_shortage_fails_before_telegram_download(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = self.worker(directory)
            worker.client = SimpleNamespace(download_file=AsyncMock())
            message = SimpleNamespace(file=SimpleNamespace(size=1024), media=object())
            with patch("app.telegram_worker.shutil.disk_usage", return_value=SimpleNamespace(free=1)):
                with self.assertRaisesRegex(RuntimeError, "Insufficient temporary disk"):
                    await worker._download_media_to_path(message, Path(directory) / "source")
            worker.client.download_file.assert_not_called()

    async def test_legacy_cleanup_does_not_expire_telescope_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected = root / "telescope" / "job" / "source.media"
            protected.parent.mkdir(parents=True)
            protected.write_bytes(b"checkpoint")
            old = time.time() - 100000
            os.utime(protected, (old, old))
            worker = self.worker(directory)
            worker.cleanup_temp_files()
            self.assertTrue(protected.exists())

    @staticmethod
    def worker(directory):
        worker = object.__new__(TelegramPipeWorker)
        worker.settings = SimpleNamespace(temp_dir=Path(directory), temp_file_ttl_hours=1,
                                          download_chunk_size=512 * 1024, telescope_disk_reserve_mb=0)
        worker._active_temp_paths = set()
        worker._job_registry = None
        return worker
