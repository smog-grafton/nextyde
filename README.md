# Narabox Telegram Pipe

A Telegram worker for Narabox that watches channels, downloads movie files temporarily, uploads them to your CDN, then deletes the local temp file.

## Why this uses Telethon instead of a regular Telegram bot

For large movie files, a normal Bot API bot is a bad fit. The Bot API download limit is still 20 MB unless you run Telegram's local Bot API server. Telethon uses Telegram's client API (MTProto), which is a better fit for channel monitoring and large media workflows.

## What this project does

- Watches one or more Telegram channels or invite links
- Optionally auto-joins channels on startup
- Catches up on the last N messages on boot
- Downloads supported video files to a temp directory
- Hands the original Telegram file to the Laravel worker for probing, transcoding, and compression
- Extracts basic metadata from filenames like `28 YEARS LATER=1 VJ JOZZ UG.mkv`
- Hands the file off to your worker/CDN over HTTP
- Records processed Telegram message IDs in SQLite so duplicates are skipped
- Deletes the temp file after upload

## Supported video extensions

- `.mp4`
- `.mkv`
- `.avi`
- `.mov`
- `.m4v`
- `.webm`

## Deployment guides

- [Same server with shared intake path](docs/deployment/01-same-server-path-copy.md)
- [Cross-server pull handoff with signed source URLs](docs/deployment/02-cross-server-source-url.md)
- [Coolify and Docker deployment](docs/deployment/03-coolify-docker.md)

## Expected CDN contract

Set `CDN_UPLOAD_URL` to the destination intake endpoint. It can still be a CDN endpoint, but it can now also point to the Laravel FFmpeg worker intake endpoint.

- `file` → uploaded file
- `source` → `telegram`
- `metadata` → JSON string
- `title`
- `original_filename`
- `episode`
- `vj`
- `category`
- `language`
- `telegram_chat_id`
- `telegram_message_id`
- `telegram_channel`

For the new Laravel worker flow on the same server, prefer path handoff instead of multipart upload:

- `CDN_UPLOAD_URL=https://your-worker.example.com/api/v1/media/telegram-intake`
- `CDN_API_TOKEN=<same as TELEGRAM_INGEST_TOKEN on the worker>`
- `CDN_HANDOFF_MODE=path_copy`
- `CDN_SHARED_INTAKE_ROOT=/Applications/XAMPP/xamppfiles/htdocs/ffmpeg-worker/storage/app/telegram-intake`
- `CDN_SHARED_INTAKE_DISK=telegram-intake`
- `WORKER_HANDLES_VIDEO_PREP=true`

That mode copies the original downloaded file into the worker intake disk and only POSTs metadata + `source_path`, which avoids large HTTP uploads timing out.

If telebot and the Laravel worker are on different servers, you can also use a signed temp URL handoff:

- `CDN_HANDOFF_MODE=source_url`
- `TEMP_PUBLIC_URL=https://telebot.example.com`
- `TEMP_URL_SECRET=<optional, defaults to CDN_API_TOKEN>`

That mode keeps the downloaded file in telebot temp storage, generates a signed direct file URL with the real extension, and POSTs that URL to Laravel as `source_url`.

## Local setup

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/telebot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

On first run, Telethon will ask for the login code sent to your Telegram account. If that account has 2FA enabled, set `TG_2FA_PASSWORD` in `.env`.

## Suggested `.env`

```env
TG_API_ID=123456
TG_API_HASH=your_api_hash
TG_PHONE=+2567xxxxxxx
TG_SESSION_NAME=narabox_data_pipe
TG_WATCH_TARGETS=@vjjozzchannel
TG_JOIN_TARGETS=https://t.me/+abcdefg12345
TEMP_DIR=/tmp/telebot
DB_PATH=./telebot_state.db
MAX_CONCURRENT_DOWNLOADS=1
MAX_CONCURRENT_TRANSCODES=1
WEB_MAX_ACTIVE_JOBS=3
WEB_RECENT_JOB_RETENTION_HOURS=24
SCAN_LAST_MESSAGES=15
CDN_UPLOAD_URL=https://cdn.naraboxtv.com/api/v1/media/telegram-intake
CDN_API_TOKEN=replace_me
CDN_HANDOFF_MODE=path_copy
CDN_SHARED_INTAKE_ROOT=/Applications/XAMPP/xamppfiles/htdocs/ffmpeg-worker/storage/app/telegram-intake
CDN_SHARED_INTAKE_DISK=telegram-intake
WORKER_HANDLES_VIDEO_PREP=true
TEMP_PUBLIC_URL=https://telebot.example.com
TEMP_URL_SECRET=replace_me
CDN_NOTIFY_URL=https://portal.naraboxtv.com/api/telegram/ingest-notify
CDN_NOTIFY_TOKEN=replace_me
DEFAULT_CATEGORY=movies
VIDEO_PREP_ENABLED=true
VIDEO_PREP_MAX_HEIGHT=720
VIDEO_PREP_CRF=22
VIDEO_PREP_PRESET=superfast
VIDEO_PREP_MIN_SIZE_MB_FOR_TRANSCODE=50
VIDEO_PREP_TARGET_MAX_MB=1024
VIDEO_PREP_CAP_HEIGHT_LADDER=480,360
VIDEO_PREP_KEEP_ORIGINAL_ON_SUCCESS=false
VIDEO_PREP_TIMEOUT_SECONDS=21600
TEMP_FILE_TTL_HOURS=24
FFMPEG_BINARY=
FFPROBE_BINARY=
```

### Video preparation behavior

When `WORKER_HANDLES_VIDEO_PREP=true`, telebot skips local transcoding/compression for normal worker handoffs and sends the original download to Laravel instead. The local video prep settings below only apply when you disable that delegation for normal upload/handoff jobs.

If local video prep is active, telebot:

- Detects `ffmpeg`/`ffprobe` at runtime (env override first, then `PATH`, with `-version` validation)
- Probes source media and decides whether to keep source or transcode
- Transcodes to MP4 with `libx264` + `aac` + `+faststart` when needed
- Enforces a hard size cap for oversized videos so exposed/uploaded files stay at or below `VIDEO_PREP_TARGET_MAX_MB`
- Starts capped jobs from the lower `VIDEO_PREP_CAP_HEIGHT_LADDER` so weaker VPS instances do not waste hours trying a too-heavy first pass
- Keeps only primary video and first audio stream (`-map 0:v:0 -map 0:a:0?`)
- Downscales oversized sources to a max height (default `720`) without upscaling

If video prep is enabled but media tools are missing or transcoding fails, the job fails with a clear error so issues are visible in production logs/UI.

## Deploy on VPS (Coolify)

You can run either the **Web UI only** (recommended for “paste a link and ingest”) or the **channel watcher** (`main.py`). Only one process should use the same Telethon session at a time.

### 1. Create the Telegram session once (on your machine)

Telethon needs a logged-in session. Create it locally, then reuse it on the server:

```bash
# On your Mac/PC: activate venv, run once, enter code when asked, then Ctrl+C
source .venv/bin/activate
python main.py
# Enter the code from Telegram, wait until it says "Watching ... targets", then Ctrl+C
```

This creates a session file (e.g. `narabox_telebot.session` in the project directory). You’ll upload this to the VPS or mount it in Coolify.

### 2. Push the project to Git

Coolify usually builds from a Git repo. Commit and push the telebot project (add `.env` to `.gitignore` and **do not** commit the session file if it contains secrets; see step 3).

### 3. Add the session file on the server

- **Option A:** Upload the session file to the server (e.g. via Coolify “Secrets” or a persistent volume), and mount it so the app sees it at `/app/narabox_telebot.session` (or whatever path matches `TG_SESSION_NAME`; the default session name is from `.env`).
- **Option B:** Bake the session into the image in a private build step (not recommended if the repo is public).

Set `TG_SESSION_NAME` in Coolify env to the same name you used locally (e.g. `narabox_telebot`) so the app finds the session file.

### 4. Create the application in Coolify

1. **New Resource** → **Application** (or **Docker Compose** if you prefer).
2. **Source:** Connect your Git repo and select the branch (e.g. `main`).
3. **Build:**
   - **Build Pack:** Dockerfile (use the repo’s `Dockerfile`).
   - Or leave Coolify to detect the Dockerfile.
4. **Deploy:**
   - **Port:** Set to `8765`. The Dockerfile **default is the Web UI** (`python -m app.web`), so the app will listen on `0.0.0.0:8765` with no start-command override.
   - **Start command:** Leave empty. The image already runs the Web UI by default. If you ever want the **channel watcher** instead (no browser), use Coolify’s **Custom Docker Options** (General tab) to override the command (e.g. `--entrypoint python` and container command `main.py`; syntax depends on your Coolify version).

### 5. Environment variables in Coolify

Add all required env vars in Coolify’s **Environment Variables** for this service:

| Variable | Required | Example |
|----------|----------|--------|
| `TG_API_ID` | Yes | From my.telegram.org |
| `TG_API_HASH` | Yes | From my.telegram.org |
| `TG_PHONE` | Yes | +2567xxxxxxxx |
| `TG_SESSION_NAME` | No (default used) | narabox_telebot |
| `TG_2FA_PASSWORD` | If 2FA enabled | your password |
| `CDN_UPLOAD_URL` | Yes | https://cdn.naraboxtv.com/api/v1/media/telegram-intake |
| `CDN_API_TOKEN` | Yes | Same token as portal uses for CDN |
| `CDN_HANDOFF_MODE` | No | `upload`, `path_copy`, or `source_url` |
| `CDN_SHARED_INTAKE_ROOT` | If `CDN_HANDOFF_MODE=path_copy` | `/Applications/XAMPP/xamppfiles/htdocs/ffmpeg-worker/storage/app/telegram-intake` |
| `CDN_SHARED_INTAKE_DISK` | No | `telegram-intake` |
| `TEMP_PUBLIC_URL` | If `CDN_HANDOFF_MODE=source_url` or Download only | `https://telebot.example.com` |
| `TEMP_URL_SECRET` | No | Reuses `CDN_API_TOKEN` if blank |
| `CDN_NOTIFY_URL` | No | https://portal.naraboxtv.com/api/telegram/ingest-notify |
| `CDN_NOTIFY_TOKEN` | No | TELEGRAM_INGEST_NOTIFY_TOKEN from portal |
| `TEMP_DIR` | No | /tmp/telebot |
| `DB_PATH` | No | /data/telebot_state.db (use a path on a persistent volume) |
| `PORT` | No (default 8765) | Web UI port; set in Coolify if your host port mapping differs |
| `WEB_DISABLE_TELEGRAM` | No | `true` to run the web app only as a signed temp-file server |

For **Web UI only**, you can leave `TG_WATCH_TARGETS` and `TG_JOIN_TARGETS` empty.

### 6. Persistent storage (Coolify volumes)

Mount a persistent volume so the session and DB survive restarts:

- **Session:** Mount a directory (e.g. `/data`) and put the session file there; set `TG_SESSION_NAME` to the full path or ensure the app’s working dir is that directory. Or mount the session file itself into `/app/narabox_telebot.session`.
- **SQLite DB:** Set `DB_PATH=/data/telebot_state.db` and mount the same volume so `/data` is persistent.

### 7. Expose the Web UI (if you use it)

In Coolify, add a **Domain** or **Proxy** for the service and point it to port **8765**. Then open `https://your-domain` and use the Web UI to paste t.me links.

### 8. "No available server" but container logs show Uvicorn running

Coolify’s docs say this usually means **the proxy’s health check failed** – the container is marked unhealthy so the proxy doesn’t route to it. The health check often uses **curl** or **wget**; the image now includes **curl** so that check can succeed. The health check runs **inside the container** (e.g. `curl -f http://localhost:8765/health`), so the image must include **curl**. This Dockerfile installs curl and defines a **HEALTHCHECK**. If you deployed before that change, **force a full rebuild** (e.g. Clear build cache + Redeploy) so the new image is used—a plain Redeploy reuses the old image.

If the app logs show `Uvicorn running on http://0.0.0.0:8765` and deployment succeeded, but the browser still shows **no available server**:

- **Port:** In the application’s **General** or **Deploy** settings, set the **application port** (or “Port Exposes”) to **8765** — the port the app listens on. The proxy must forward to this port.
- **Domain / FQDN:** Ensure a **Domain** or **FQDN** is set for this application and matches the URL you use (e.g. the sslip.io URL). The proxy routes by hostname.
- **Restart proxy:** After changing port or domain, **Restart** the application (or the proxy) so the proxy picks up the new config.
- **From the server:** On the VPS, run `curl -s http://127.0.0.1:8765/health` (or the container IP and port). If that returns `{"status":"ok"}`, the app is reachable; the issue is proxy configuration. (If `curl` isn’t installed in the container, use `python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8765/health').read())"`.)
- **Custom Docker Options:** Leave **Custom Docker Options** empty for this app. Options copied from other apps (e.g. `--cap-add SYS_ADMIN`, `--device=/dev/fuse`) can prevent the container from starting or joining the proxy network; clear them, Save, and Redeploy.
- **Access without the domain:** To use the app without the proxy domain, expose the container port to the host. In Coolify, set **Port Mappings** (or equivalent) so the host publishes port 8765, e.g. `8765:8765`. Then open `http://YOUR_SERVER_IP:8765` (e.g. `http://157.173.104.218:8765`). Ensure the server firewall allows inbound TCP on 8765.

### Summary

- **Web UI:** Works with almost any channel your Telegram account can access; you just paste the message link.
- **Coolify:** Use the repo’s Dockerfile, create the session once locally and upload/mount it on the server, set env vars, optionally override start command to `python -m app.web`, expose port 8765, and use a persistent volume for session + DB.

## Web UI

A local dashboard to paste 1 to 3 Telegram message links at once and run download -> worker handoff or expose a temporary fetch URL.

1. Log in once (so the session exists): run `python main.py`, enter the Telegram code, then stop it (Ctrl+C).
2. Start the web app:

```bash
source .venv/bin/activate
python -m app.web
```

3. Open **http://127.0.0.1:8765** in your browser. Paste up to 3 message links (one per line), then queue the jobs. The dashboard shows active jobs and recent jobs separately, so refresh-safe actions like **Copy URL** and **Destroy** still work after the page reloads. Terminal jobs are pruned from recent history automatically after `WEB_RECENT_JOB_RETENTION_HOURS`.

**Download only:** Check **Download only** to skip the worker/CDN handoff completely. Telebot now exposes the original downloaded file at a temporary URL without local prep/transcoding, so the URL keeps the real file extension and uses a normalized filename with no spaces in the path. The dashboard shows that URL directly and lets you copy it. Paste it into any tool that can fetch from URL, then click **Destroy** after the fetch is finished. If `TEMP_PUBLIC_URL` is set, telebot uses it; otherwise the web UI falls back to the current browser origin when building the visible temp URL. Forgotten download-only files are also cleaned up automatically after `TEMP_FILE_TTL_HOURS`.

**Automatic remote-worker handoff:** Set `CDN_HANDOFF_MODE=source_url` when telebot and the Laravel worker do not share a filesystem. Telebot will generate a signed temp file URL like `https://telebot.example.com/api/fetch/.../movie-final-cut.mkv` and pass it to the Laravel worker automatically as `source_url`, so there is nothing to copy by hand.

### Does the Web UI work with any channel?

Yes, with two conditions:

1. **Your Telegram account (the one you logged in with) must have access** to that channel:
   - **Public channels:** Usually fine as long as you’ve opened or joined them (or the message is public).
   - **Private channels:** Your account must be a member (you were added or joined via invite link).
2. **The message at the link must contain a supported video file** (e.g. `.mp4`, `.mkv`). Text-only or photo-only posts won’t be processed.

You don’t configure a fixed list of channels in the Web UI. You paste **any** valid `https://t.me/channelname/123` link; if your account can see that message and it has a video, it will be processed.

## Process a single t.me link (CLI)

To ingest one message by URL from the command line:

```bash
python -m app.cli process-link "https://t.me/jozzmovies/45"
```

You can also paste a t.me message link in any watched channel; the worker will fetch that message and process it if it contains a supported video file.

## CDN and portal

- **CDN:** Use `POST /api/v1/media/telegram-intake` (Bearer token). The telebot sends the multipart upload there.
- **Portal:** Set `CDN_NOTIFY_URL` to your portal’s `POST /api/telegram/ingest-notify` and `CDN_NOTIFY_TOKEN` to `TELEGRAM_INGEST_NOTIFY_TOKEN` so the portal records each ingest for the admin.
