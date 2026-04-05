from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

from dotenv import load_dotenv


load_dotenv()


def _csv(name: str) -> List[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _csv_ints(name: str, default: str) -> tuple[int, ...]:
    raw = os.getenv(name, default).strip()
    if not raw:
        return ()
    values: list[int] = []
    for item in raw.split(","):
        stripped = item.strip()
        if not stripped:
            continue
        values.append(max(240, int(stripped)))
    return tuple(values)


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    tg_api_id: int
    tg_api_hash: str
    tg_phone: str
    tg_session_name: str
    tg_2fa_password: str | None
    tg_login_code: str | None
    tg_watch_targets: List[str]
    tg_join_targets: List[str]
    temp_dir: Path
    db_path: Path
    log_level: str
    max_concurrent_downloads: int
    max_concurrent_transcodes: int
    web_max_active_jobs: int
    scan_last_messages: int
    download_chunk_size: int
    delete_after_upload: bool
    poll_interval_seconds: int
    cdn_upload_url: str
    cdn_api_token: str | None
    cdn_timeout_seconds: int
    cdn_source: str
    cdn_notify_url: str | None
    cdn_notify_token: str | None
    cdn_handoff_mode: str
    worker_intake_root: Path | None
    worker_intake_disk: str
    worker_handles_video_prep: bool
    default_category: str | None
    default_language: str | None
    default_vj: str | None
    download_only: bool
    temp_public_url: str
    temp_url_secret: str
    ffmpeg_binary: str | None
    ffprobe_binary: str | None
    video_prep_enabled: bool
    video_prep_max_height: int
    video_prep_crf: int
    video_prep_preset: str
    video_prep_min_size_mb_for_transcode: int
    video_prep_target_max_mb: int
    video_prep_cap_height_ladder: tuple[int, ...]
    video_prep_keep_original_on_success: bool
    video_prep_timeout_seconds: int
    temp_file_ttl_hours: int
    web_recent_job_retention_hours: int

    def should_prepare_video_locally(self, *, download_only: bool = False) -> bool:
        if download_only:
            return False
        if not self.video_prep_enabled:
            return False
        return not self.worker_handles_video_prep

    @classmethod
    def load(cls) -> "Settings":
        api_id = os.getenv("TG_API_ID", "").strip()
        api_hash = os.getenv("TG_API_HASH", "").strip()
        tg_phone = os.getenv("TG_PHONE", "").strip()
        cdn_upload_url = os.getenv("CDN_UPLOAD_URL", "").strip()
        download_only = _bool("DOWNLOAD_ONLY", False)
        handoff_mode = os.getenv("CDN_HANDOFF_MODE", "upload").strip().lower() or "upload"
        temp_public_url = os.getenv("TEMP_PUBLIC_URL", "").strip()
        temp_url_secret = (os.getenv("TEMP_URL_SECRET") or os.getenv("CDN_API_TOKEN") or "").strip()

        missing = []
        if not api_id:
            missing.append("TG_API_ID")
        if not api_hash:
            missing.append("TG_API_HASH")
        if not tg_phone:
            missing.append("TG_PHONE")
        if not download_only and not cdn_upload_url:
            missing.append("CDN_UPLOAD_URL")
        if handoff_mode == "source_url" and not temp_public_url:
            missing.append("TEMP_PUBLIC_URL")
        if handoff_mode == "source_url" and not temp_url_secret:
            missing.append("TEMP_URL_SECRET (or CDN_API_TOKEN)")
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

        temp_dir = Path(os.getenv("TEMP_DIR", "/tmp/telebot")).expanduser().resolve()
        db_path = Path(os.getenv("DB_PATH", "./telebot_state.db")).expanduser().resolve()

        return cls(
            tg_api_id=int(api_id),
            tg_api_hash=api_hash,
            tg_phone=tg_phone,
            tg_session_name=os.getenv("TG_SESSION_NAME", "narabox_data_pipe").strip() or "narabox_data_pipe",
            tg_2fa_password=os.getenv("TG_2FA_PASSWORD") or None,
            tg_login_code=os.getenv("TG_LOGIN_CODE") or None,
            tg_watch_targets=_csv("TG_WATCH_TARGETS"),
            tg_join_targets=_csv("TG_JOIN_TARGETS"),
            temp_dir=temp_dir,
            db_path=db_path,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            max_concurrent_downloads=max(1, int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "1"))),
            max_concurrent_transcodes=max(1, int(os.getenv("MAX_CONCURRENT_TRANSCODES", "1"))),
            web_max_active_jobs=max(1, int(os.getenv("WEB_MAX_ACTIVE_JOBS", "3"))),
            scan_last_messages=max(0, int(os.getenv("SCAN_LAST_MESSAGES", "15"))),
            download_chunk_size=max(65536, int(os.getenv("DOWNLOAD_CHUNK_SIZE", str(1024 * 1024)))),
            delete_after_upload=_bool("DELETE_AFTER_UPLOAD", True),
            poll_interval_seconds=max(10, int(os.getenv("POLL_INTERVAL_SECONDS", "30"))),
            cdn_upload_url=cdn_upload_url,
            cdn_api_token=os.getenv("CDN_API_TOKEN") or None,
            cdn_timeout_seconds=max(60, int(os.getenv("CDN_TIMEOUT_SECONDS", "3600"))),
            cdn_source=os.getenv("CDN_SOURCE", "telegram").strip() or "telegram",
            cdn_notify_url=os.getenv("CDN_NOTIFY_URL") or None,
            cdn_notify_token=os.getenv("CDN_NOTIFY_TOKEN") or None,
            cdn_handoff_mode=handoff_mode,
            worker_intake_root=(
                Path(os.getenv("CDN_SHARED_INTAKE_ROOT", "")).expanduser().resolve()
                if os.getenv("CDN_SHARED_INTAKE_ROOT", "").strip()
                else None
            ),
            worker_intake_disk=(os.getenv("CDN_SHARED_INTAKE_DISK", "telegram-intake").strip() or "telegram-intake"),
            worker_handles_video_prep=_bool(
                "WORKER_HANDLES_VIDEO_PREP",
                handoff_mode in {"path_copy", "source_url"},
            ),
            default_category=os.getenv("DEFAULT_CATEGORY") or None,
            default_language=os.getenv("DEFAULT_LANGUAGE") or None,
            default_vj=os.getenv("DEFAULT_VJ") or None,
            download_only=download_only,
            temp_public_url=temp_public_url,
            temp_url_secret=temp_url_secret,
            ffmpeg_binary=(os.getenv("FFMPEG_BINARY") or "").strip() or None,
            ffprobe_binary=(os.getenv("FFPROBE_BINARY") or "").strip() or None,
            video_prep_enabled=_bool("VIDEO_PREP_ENABLED", True),
            video_prep_max_height=max(240, int(os.getenv("VIDEO_PREP_MAX_HEIGHT", "720"))),
            video_prep_crf=min(35, max(16, int(os.getenv("VIDEO_PREP_CRF", "22")))),
            video_prep_preset=(os.getenv("VIDEO_PREP_PRESET", "superfast").strip() or "superfast"),
            video_prep_min_size_mb_for_transcode=max(1, int(os.getenv("VIDEO_PREP_MIN_SIZE_MB_FOR_TRANSCODE", "50"))),
            video_prep_target_max_mb=max(1, int(os.getenv("VIDEO_PREP_TARGET_MAX_MB", "1024"))),
            video_prep_cap_height_ladder=_csv_ints("VIDEO_PREP_CAP_HEIGHT_LADDER", "480,360"),
            video_prep_keep_original_on_success=_bool("VIDEO_PREP_KEEP_ORIGINAL_ON_SUCCESS", False),
            video_prep_timeout_seconds=max(60, int(os.getenv("VIDEO_PREP_TIMEOUT_SECONDS", "21600"))),
            temp_file_ttl_hours=max(1, int(os.getenv("TEMP_FILE_TTL_HOURS", "24"))),
            web_recent_job_retention_hours=max(1, int(os.getenv("WEB_RECENT_JOB_RETENTION_HOURS", "24"))),
        )
