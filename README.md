# Narabox Telegram Pipe

A Telegram worker for Narabox that watches channels, downloads movie files temporarily, uploads them to your CDN, then deletes the local temp file.

## Why this uses Telethon instead of a regular Telegram bot

For large movie files, a normal Bot API bot is a bad fit. The Bot API download limit is still 20 MB unless you run Telegram's local Bot API server. Telethon uses Telegram's client API (MTProto), which is a better fit for channel monitoring and large media workflows.

## What this project does

- Watches one or more Telegram channels or invite links
- Optionally auto-joins channels on startup
- Catches up on the last N messages on boot
- Downloads supported video files to a temp directory
- Extracts basic metadata from filenames like `28 YEARS LATER=1 VJ JOZZ UG.mkv`
- Uploads the file to your CDN over HTTP
- Records processed Telegram message IDs in SQLite so duplicates are skipped
- Deletes the temp file after upload

## Supported video extensions

- `.mp4`
- `.mkv`
- `.avi`
- `.mov`
- `.m4v`
- `.webm`

## Expected CDN contract

Set `CDN_UPLOAD_URL` to an endpoint in `naraboxtv-cdn` that accepts a multipart upload with:

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

If your CDN already has a manual remote-fetch endpoint but no direct upload endpoint, add a small authenticated upload route there first. This worker is already prepared for that contract.

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
SCAN_LAST_MESSAGES=15
CDN_UPLOAD_URL=https://cdn.naraboxtv.com/api/v1/media/telegram-intake
CDN_API_TOKEN=replace_me
CDN_NOTIFY_URL=https://portal.naraboxtv.com/api/telegram/ingest-notify
CDN_NOTIFY_TOKEN=replace_me
DEFAULT_CATEGORY=movies
```

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
   - **Port:** For Web UI use `8765` (the app listens on `0.0.0.0:8765`). For channel watcher only, you don’t need to expose a port.
   - **Start command (optional):** To run **only the Web UI** in production, override the start command to:
     ```bash
     python -m app.web
     ```
     Otherwise the default `CMD` in the Dockerfile runs `main.py` (channel watcher).

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
| `CDN_NOTIFY_URL` | No | https://portal.naraboxtv.com/api/telegram/ingest-notify |
| `CDN_NOTIFY_TOKEN` | No | TELEGRAM_INGEST_NOTIFY_TOKEN from portal |
| `TEMP_DIR` | No | /tmp/telebot |
| `DB_PATH` | No | /data/telebot_state.db (use a path on a persistent volume) |
| `PORT` | No (default 8765) | Set by Coolify for Web UI; use when overriding start to `python -m app.web` |

For **Web UI only**, you can leave `TG_WATCH_TARGETS` and `TG_JOIN_TARGETS` empty.

### 6. Persistent storage (Coolify volumes)

Mount a persistent volume so the session and DB survive restarts:

- **Session:** Mount a directory (e.g. `/data`) and put the session file there; set `TG_SESSION_NAME` to the full path or ensure the app’s working dir is that directory. Or mount the session file itself into `/app/narabox_telebot.session`.
- **SQLite DB:** Set `DB_PATH=/data/telebot_state.db` and mount the same volume so `/data` is persistent.

### 7. Expose the Web UI (if you use it)

In Coolify, add a **Domain** or **Proxy** for the service and point it to port **8765**. Then open `https://your-domain` and use the Web UI to paste t.me links.

### Summary

- **Web UI:** Works with almost any channel your Telegram account can access; you just paste the message link.
- **Coolify:** Use the repo’s Dockerfile, create the session once locally and upload/mount it on the server, set env vars, optionally override start command to `python -m app.web`, expose port 8765, and use a persistent volume for session + DB.

## Web UI

A simple local website to paste a t.me link and run download → CDN → delete without the CLI.

1. Log in once (so the session exists): run `python main.py`, enter the Telegram code, then stop it (Ctrl+C).
2. Start the web app:

```bash
source .venv/bin/activate
python -m app.web
```

3. Open **http://127.0.0.1:8765** in your browser. Paste the message link (e.g. `https://t.me/jozzmovies/45`), click **Download & ingest**. Progress is shown; when done, the file is removed locally.

The optional **Channel** field is stored in your browser only (e.g. `@jozzmovies`); the **Message link** is what gets processed.

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

