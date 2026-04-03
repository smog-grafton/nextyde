# Strategy 3: Coolify And Docker Deployment

`telebot` already includes its own Dockerfile. In Coolify you can deploy it as a web UI service, a watcher service, or both. The default container command runs the web UI on port `8765`.

## Existing production resource: add this delta

If your telebot Coolify resource is already running with the usual Telegram and web variables, the important worker-integration delta is:

```env
# Change this away from the old CDN intake if Laravel worker is now the intake target.
CDN_UPLOAD_URL=https://worker.example.com/api/v1/media/telegram-intake

# Recommended for separate Coolify containers, even on the same host.
CDN_HANDOFF_MODE=source_url
WORKER_HANDLES_VIDEO_PREP=true

# Required for source_url handoff.
TEMP_PUBLIC_URL=https://telebot.example.com
TEMP_URL_SECRET=replace_me

# Keep the web app attached to Telegram in the main service.
WEB_DISABLE_TELEGRAM=false
DOWNLOAD_ONLY=false
```

Notes:

- `MAX_CONCURRENT_UPLOADS` is a legacy variable from older deployments and the current telebot code does not read it. You can leave it in Coolify, but it has no effect.
- If you want `path_copy` instead of `source_url`, both containers must mount the same shared intake volume. Being on the same server is not enough by itself.
- Keep `VIDEO_PREP_ENABLED=true` if you still want download-only jobs to optimize locally, but normal worker handoffs will skip local prep when `WORKER_HANDLES_VIDEO_PREP=true`.

## Recommended service split

### 1. Web UI service

Keep the default container command and set:

```env
TG_API_ID=replace_me
TG_API_HASH=replace_me
TG_PHONE=+2567xxxxxxx
TG_SESSION_NAME=/data/narabox_telebot
TG_2FA_PASSWORD=
TG_LOGIN_CODE=

TG_WATCH_TARGETS=@jozzmovies
TG_JOIN_TARGETS=

TEMP_DIR=/data/temp
DB_PATH=/data/telebot_state.db
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
TEMP_URL_SECRET=replace_me

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
CDN_HANDOFF_MODE=source_url
WORKER_HANDLES_VIDEO_PREP=true
WEB_DISABLE_TELEGRAM=false

CDN_NOTIFY_URL=https://portal.naraboxtv.com/api/telegram/ingest-notify
CDN_NOTIFY_TOKEN=

DEFAULT_CATEGORY=movies
DEFAULT_LANGUAGE=
DEFAULT_VJ=
```

Mount a persistent volume at `/data` so the Telethon session and SQLite DB survive redeploys.

### 2. Watcher service

Create a second service from the same repo and override the container command to:

```bash
python main.py
```

Use the same environment variables and the same `/data` volume.

### 3. Temp-file-only service

If you want a dedicated signed-temp-URL service for `source_url` handoff, create a third service with:

```env
WEB_DISABLE_TELEGRAM=true
PORT=8765
TEMP_PUBLIC_URL=https://telebot-fetch.example.com
TEMP_URL_SECRET=replace_me
TEMP_DIR=/data/temp
DB_PATH=/data/telebot_state.db
```

This service does not log in to Telegram. It only serves temp files already produced by the main service.

## Important notes

- The image already installs `ffmpeg`, so you usually leave `FFMPEG_BINARY` and `FFPROBE_BINARY` blank.
- The health endpoint is `/health`.
- The default Docker command is the web UI. Only override the command for the watcher service.
- If you use `TG_SESSION_NAME=/data/narabox_telebot`, Telethon will create `/data/narabox_telebot.session`.
