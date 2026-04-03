# Strategy 1: Same Server With Shared Intake Path

Use this when `telebot` and `ffmpeg-worker` run on the same machine or can share one writable volume. This is the most efficient setup for big Telegram movies because telebot does not upload the file through PHP.

## Flow

1. Telebot downloads the original Telegram file.
2. Telebot copies it into the worker intake path.
3. Telebot sends metadata plus `source_path`.
4. Laravel handles probing, compression, MP4, HLS, and posters.

## Telebot environment variables

```env
TG_API_ID=replace_me
TG_API_HASH=replace_me
TG_PHONE=+2567xxxxxxx
TG_SESSION_NAME=narabox_telebot
TG_2FA_PASSWORD=
TG_LOGIN_CODE=

TG_WATCH_TARGETS=@jozzmovies
TG_JOIN_TARGETS=

TEMP_DIR=/srv/narabox/telebot/temp
DB_PATH=/srv/narabox/telebot/telebot_state.db
PORT=8765

LOG_LEVEL=INFO
MAX_CONCURRENT_DOWNLOADS=1
MAX_CONCURRENT_TRANSCODES=1
WEB_MAX_ACTIVE_JOBS=3
WEB_RECENT_JOB_RETENTION_HOURS=24
SCAN_LAST_MESSAGES=15
DOWNLOAD_CHUNK_SIZE=1048576
POLL_INTERVAL_SECONDS=30

DELETE_AFTER_UPLOAD=true
TEMP_FILE_TTL_HOURS=24
DOWNLOAD_ONLY=false

TEMP_PUBLIC_URL=https://telebot.example.com
TEMP_URL_SECRET=

VIDEO_PREP_ENABLED=true
VIDEO_PREP_MAX_HEIGHT=720
VIDEO_PREP_CRF=22
VIDEO_PREP_PRESET=superfast
VIDEO_PREP_MIN_SIZE_MB_FOR_TRANSCODE=50
VIDEO_PREP_TARGET_MAX_MB=1024
VIDEO_PREP_CAP_HEIGHT_LADDER=480,360
VIDEO_PREP_KEEP_ORIGINAL_ON_SUCCESS=false
VIDEO_PREP_TIMEOUT_SECONDS=21600
FFMPEG_BINARY=
FFPROBE_BINARY=

CDN_UPLOAD_URL=https://worker.example.com/api/v1/media/telegram-intake
CDN_API_TOKEN=replace_me
CDN_TIMEOUT_SECONDS=3600
CDN_SOURCE=telegram
CDN_HANDOFF_MODE=path_copy
CDN_SHARED_INTAKE_ROOT=/srv/narabox/ffmpeg-worker/intake
CDN_SHARED_INTAKE_DISK=telegram-intake
WORKER_HANDLES_VIDEO_PREP=true
WEB_DISABLE_TELEGRAM=false

CDN_NOTIFY_URL=https://portal.naraboxtv.com/api/telegram/ingest-notify
CDN_NOTIFY_TOKEN=

DEFAULT_CATEGORY=movies
DEFAULT_LANGUAGE=
DEFAULT_VJ=
```

## Important notes

- `CDN_SHARED_INTAKE_ROOT` must match worker `FFMPEG_WORKER_INTAKE_ROOT`.
- Keep `WORKER_HANDLES_VIDEO_PREP=true` so telebot skips duplicate compression work.
- `TEMP_PUBLIC_URL` is still useful for download-only mode and operator copy links.
- Persist the session file and SQLite DB path so redeploys do not force a fresh Telegram login.
