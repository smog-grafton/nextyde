from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app import web
from app.config import Settings
from app.media_probe import AudioStreamInfo, MediaProbeResult, VideoStreamInfo
from app.video_prepare import analyze_video_for_delivery


def make_settings(temp_dir: Path) -> Settings:
    return Settings(
        tg_api_id=1,
        tg_api_hash="hash",
        tg_phone="+256700000000",
        tg_session_name="session",
        tg_2fa_password=None,
        tg_login_code=None,
        tg_watch_targets=[],
        tg_join_targets=[],
        temp_dir=temp_dir,
        db_path=temp_dir / "state.db",
        log_level="INFO",
        max_concurrent_downloads=1,
        max_concurrent_transcodes=1,
        web_max_active_jobs=3,
        scan_last_messages=0,
        download_chunk_size=1024 * 1024,
        delete_after_upload=True,
        poll_interval_seconds=30,
        cdn_upload_url="https://cdn.example.com/upload",
        cdn_api_token=None,
        cdn_timeout_seconds=3600,
        cdn_source="telegram",
        cdn_notify_url=None,
        cdn_notify_token=None,
        default_category=None,
        default_language=None,
        default_vj=None,
        download_only=False,
        temp_public_url="https://telebot.example.com",
        ffmpeg_binary=None,
        ffprobe_binary=None,
        video_prep_enabled=True,
        video_prep_max_height=720,
        video_prep_crf=22,
        video_prep_preset="veryfast",
        video_prep_min_size_mb_for_transcode=50,
        video_prep_target_max_mb=1024,
        video_prep_keep_original_on_success=False,
        video_prep_timeout_seconds=7200,
        temp_file_ttl_hours=24,
    )


class DummyWorker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def process_link(self, link: str, job: dict | None = None) -> dict:
        if job is not None:
            job["status"] = "done"
            job["message"] = "Done."
        return {"link": link}


class WebBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.old_worker = web.worker
        self.old_authorized = web.authorized
        self.old_jobs = dict(web.jobs)
        web.jobs.clear()
        self.temp_dir_ctx = tempfile.TemporaryDirectory()
        self.temp_dir = Path(self.temp_dir_ctx.name)
        web.worker = DummyWorker(make_settings(self.temp_dir))
        web.authorized = True

    async def asyncTearDown(self) -> None:
        web.jobs.clear()
        web.jobs.update(self.old_jobs)
        web.worker = self.old_worker
        web.authorized = self.old_authorized
        self.temp_dir_ctx.cleanup()

    def test_normalize_links_splits_multiline_and_dedupes(self) -> None:
        normalized = web._normalize_links(
            "https://t.me/demo/1\n\nhttps://t.me/demo/1",
            ["https://t.me/demo/2"],
        )
        self.assertEqual(
            normalized,
            [
                {"link": "https://t.me/demo/1", "link_key": "demo:1"},
                {"link": "https://t.me/demo/2", "link_key": "demo:2"},
            ],
        )

    def test_normalize_links_rejects_more_than_three(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            web._normalize_links(
                "\n".join(
                    [
                        "https://t.me/demo/1",
                        "https://t.me/demo/2",
                        "https://t.me/demo/3",
                        "https://t.me/demo/4",
                    ]
                ),
                None,
            )
        self.assertEqual(ctx.exception.status_code, 422)

    async def test_api_process_reuses_existing_active_job(self) -> None:
        web.jobs["existing-job"] = {
            "job_id": "existing-job",
            "status": "downloading",
            "progress_pct": 40,
            "message": "Downloading",
            "file_name": "",
            "link": "https://t.me/demo/1",
            "link_key": "demo:1",
            "result": None,
            "error": None,
            "temp_path": None,
            "download_only": False,
            "_ts": 1.0,
            "updated_ts": 1.0,
        }

        response = await web.api_process(web.ProcessRequest(link="https://t.me/demo/1"))
        self.assertEqual(response["job_id"], "existing-job")
        self.assertEqual(response["jobs"], [{"job_id": "existing-job", "status": "downloading", "link": "https://t.me/demo/1"}])
        self.assertEqual(len(web.jobs), 1)

    async def test_api_process_enforces_active_job_limit(self) -> None:
        for index in range(3):
            web.jobs[f"job-{index}"] = {
                "job_id": f"job-{index}",
                "status": "queued",
                "progress_pct": 0,
                "message": "Queued",
                "file_name": "",
                "link": f"https://t.me/demo/{index}",
                "link_key": f"demo:{index}",
                "result": None,
                "error": None,
                "temp_path": None,
                "download_only": False,
                "_ts": float(index),
                "updated_ts": float(index),
            }

        with self.assertRaises(HTTPException) as ctx:
            await web.api_process(web.ProcessRequest(link="https://t.me/demo/99"))
        self.assertEqual(ctx.exception.status_code, 429)

    async def test_temp_url_and_legacy_redirect_use_real_filename(self) -> None:
        file_path = self.temp_dir / "job-1" / "movie.delivery.mp4"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b"video")
        web.jobs["job-1"] = {
            "job_id": "job-1",
            "status": "downloaded",
            "progress_pct": 100,
            "message": "Ready",
            "file_name": "movie.delivery.mp4",
            "link": "https://t.me/demo/1",
            "link_key": "demo:1",
            "result": None,
            "error": None,
            "temp_path": str(file_path),
            "download_only": True,
            "_ts": 1.0,
            "updated_ts": 1.0,
        }

        temp_url = web._build_temp_url(web.worker, web.jobs["job-1"])
        self.assertEqual(temp_url, "https://telebot.example.com/api/temp/job-1/movie.delivery.mp4")

        response = await web.api_temp_file_legacy("job-1")
        self.assertEqual(response.headers["location"], "/api/temp/job-1/movie.delivery.mp4")


class VideoPrepAnalysisTests(unittest.TestCase):
    def test_analyze_video_for_delivery_builds_capped_attempts(self) -> None:
        source_probe = MediaProbeResult(
            path=Path("/tmp/movie.mkv"),
            extension=".mkv",
            size_bytes=2_700 * 1024 * 1024,
            duration_seconds=7200.0,
            container_format="matroska,webm",
            video=VideoStreamInfo(codec="hevc", width=1920, height=1080, pix_fmt="yuv420p", frame_rate=23.976),
            audio=AudioStreamInfo(codec="aac", channels=2, sample_rate=48000),
        )
        settings = make_settings(Path("/tmp/telebot-tests"))

        with patch("app.video_prepare.require_media_tools", return_value=SimpleNamespace(ffprobe=SimpleNamespace(path="ffprobe"))):
            with patch("app.video_prepare.probe_media", return_value=source_probe):
                analysis = analyze_video_for_delivery(settings, source_probe.path)

        self.assertTrue(analysis.decision.should_transcode)
        self.assertEqual(analysis.decision.mode, "cap")
        self.assertEqual(len(analysis.attempts), 3)
        self.assertEqual([attempt.target_height for attempt in analysis.attempts], [720, 576, 480])
        self.assertGreater(analysis.attempts[0].video_bitrate_kbps, analysis.attempts[1].video_bitrate_kbps)
        self.assertGreater(analysis.attempts[1].video_bitrate_kbps, analysis.attempts[2].video_bitrate_kbps)

