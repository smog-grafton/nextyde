# Strategy 2: Cross-Server Pull Handoff With Signed Source URLs

Use this when telebot and the Laravel worker are on different servers. Telebot exposes a signed temporary fetch URL, then the worker downloads it remotely through `source_url`.

## Flow

1. Telebot downloads the Telegram file.
2. Telebot creates a signed URL like `/api/fetch/.../movie-name.mkv`.
3. Telebot sends that URL to the worker.
4. The worker fetches, probes, and transcodes the file.

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

## Important notes

- `TEMP_PUBLIC_URL` must be reachable by the worker.
- `TEMP_URL_SECRET` should be strong and private. If left blank, telebot reuses `CDN_API_TOKEN`.
- `DELETE_AFTER_UPLOAD=true` is safe here because `source_url` mode keeps the temp file until cleanup removes it.
- `TEMP_FILE_TTL_HOURS` must be long enough for the worker to finish fetching slow large files.
- The telebot web app needs HTTPS if you do not want signed temp URLs traveling over plain HTTP.
