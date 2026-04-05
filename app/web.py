"""
Simple web UI: paste one or more t.me links, then run download -> worker/CDN handoff
or expose a temp URL for manual fetching.
Requires a valid Telethon session (run `python main.py` once to log in).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

from app.config import Settings
from app.link_parser import parse_telegram_link
from app.media_tools import detect_media_tools
from app.temp_url import resolve_signed_temp_path
from app.telegram_worker import AlreadyProcessedError, JobCancelledError, TelegramPipeWorker

LOG = logging.getLogger("telebot.web")
jobs: dict[str, dict] = {}
worker: TelegramPipeWorker | None = None
app_settings: Settings | None = None
authorized = False

TERMINAL_STATUSES = {"done", "failed", "cancelled", "destroyed", "expired"}
CANCELLABLE_STATUSES = {"queued", "downloading", "waiting_to_prepare", "preparing", "uploading"}


def _touch_job(job: dict) -> None:
    job["updated_ts"] = time.time()


def _is_terminal_status(status: str | None) -> bool:
    return (status or "") in TERMINAL_STATUSES


def _active_job_count() -> int:
    return sum(1 for job in jobs.values() if not _is_terminal_status(job.get("status")))


def _prune_recent_jobs(current_worker: TelegramPipeWorker | None, now: float | None = None) -> int:
    if current_worker is None:
        return 0
    retention_seconds = current_worker.settings.web_recent_job_retention_hours * 3600
    current_time = now if now is not None else time.time()
    removable_ids = [
        job_id
        for job_id, job in jobs.items()
        if _is_terminal_status(job.get("status"))
        and (current_time - float(job.get("updated_ts", job.get("_ts", current_time)))) > retention_seconds
    ]
    for job_id in removable_ids:
        jobs.pop(job_id, None)
    return len(removable_ids)


def _build_temp_path(job: dict) -> str | None:
    temp_path = job.get("temp_path")
    job_id = str(job.get("job_id", "")).strip()
    if not temp_path or not job_id:
        return None
    file_name = Path(temp_path).name
    return f"/api/temp/{quote(job_id, safe='')}/{quote(file_name, safe='')}"


def _build_temp_url(current_worker: TelegramPipeWorker | None, job: dict) -> str | None:
    temp_path = _build_temp_path(job)
    if not temp_path:
        return None
    if not current_worker:
        return temp_path
    base = (current_worker.settings.temp_public_url or "").strip()
    if not base:
        return temp_path
    return f"{base.rstrip('/')}{temp_path}"


def _job_with_temp_url(job: dict, current_worker: TelegramPipeWorker | None) -> dict:
    out = dict(job)
    out["temp_url"] = _build_temp_url(current_worker, out)
    return out


def _pick_recovered_file(files: list[Path]) -> Path:
    delivery_files = [path for path in files if path.name.endswith(".delivery.mp4")]
    if delivery_files:
        return max(delivery_files, key=lambda path: path.stat().st_mtime)
    return max(files, key=lambda path: path.stat().st_mtime)


def _recover_download_only_jobs(current_worker: TelegramPipeWorker) -> int:
    recovered = 0
    if not current_worker.settings.temp_dir.exists():
        return recovered

    for child in current_worker.settings.temp_dir.iterdir():
        if not child.is_dir():
            continue
        job_id = child.name
        if job_id in jobs:
            continue
        files = [path for path in child.rglob("*") if path.is_file()]
        if not files:
            continue
        latest = _pick_recovered_file(files)
        jobs[job_id] = {
            "job_id": job_id,
            "status": "downloaded",
            "progress_pct": 100,
            "message": "Recovered after restart. Temp URL ready to copy or destroy.",
            "file_name": latest.name,
            "link": "",
            "link_key": "",
            "result": None,
            "error": None,
            "temp_path": str(latest),
            "download_only": True,
            "_ts": latest.stat().st_mtime,
            "updated_ts": time.time(),
        }
        recovered += 1
    return recovered


def _split_link_inputs(*values: str | None) -> list[str]:
    links: list[str] = []
    for value in values:
        if not value:
            continue
        for part in value.splitlines():
            stripped = part.strip()
            if stripped:
                links.append(stripped)
    return links


def _normalize_links(link: str | None, links: list[str] | None) -> list[dict[str, str]]:
    raw_links = _split_link_inputs(link, *(links or []))
    if not raw_links:
        raise HTTPException(422, detail="Paste at least one valid Telegram message link.")

    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_link in raw_links:
        parsed = parse_telegram_link(raw_link)
        if not parsed:
            raise HTTPException(422, detail=f"Invalid t.me URL: {raw_link}")
        channel_ref, message_id = parsed
        link_key = f"{channel_ref.lower()}:{message_id}"
        if link_key in seen:
            continue
        seen.add(link_key)
        normalized.append({"link": raw_link, "link_key": link_key})

    if len(normalized) > 3:
        raise HTTPException(422, detail="Paste at most 3 Telegram message links at a time.")
    return normalized


def _find_active_job_by_key(link_key: str) -> dict | None:
    for job in jobs.values():
        if job.get("link_key") == link_key and not _is_terminal_status(job.get("status")):
            return job
    return None


def _build_job(link: str, link_key: str, download_only: bool) -> dict:
    now = time.time()
    return {
        "job_id": str(uuid.uuid4()),
        "status": "queued",
        "progress_pct": 0,
        "message": "Queued…",
        "file_name": "",
        "link": link,
        "link_key": link_key,
        "result": None,
        "error": None,
        "temp_path": None,
        "download_only": download_only,
        "_ts": now,
        "updated_ts": now,
    }


def _job_summary(job: dict) -> dict:
    return {
        "job_id": job["job_id"],
        "status": job.get("status"),
        "link": job.get("link", ""),
    }


def _job_message(error: Exception) -> str:
    text = str(error).lower()
    if "authkeyduplicated" in text or "authorization key" in text:
        return "Telegram session in use elsewhere or invalid. Use this session only on this server and restart the app."
    if "disconnected" in text or "cannot send requests while disconnected" in text:
        return "Telegram disconnected. Reload the page and try again; if it persists, restart the app."
    if "not logged in" in text:
        return "Telegram not logged in. Restart the app after logging in with 'python main.py' once."
    if "readerror" in text or "read error" in text:
        return "Upload to CDN failed (connection closed). Try again."
    return str(error)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker, authorized, app_settings
    try:
        settings = Settings.load()
        app_settings = settings
        if os.getenv("WEB_DISABLE_TELEGRAM", "").strip().lower() in {"1", "true", "yes", "on"}:
            settings.temp_dir.mkdir(parents=True, exist_ok=True)
            worker = None
            authorized = False
            LOG.info("Web app running in temp-file-only mode. Telegram processing endpoints stay disabled.")
        else:
            worker = TelegramPipeWorker(settings)
            await worker.store.init()
            worker.bind_job_registry(jobs)
            worker.settings.temp_dir.mkdir(parents=True, exist_ok=True)
            await worker.client.connect()
            authorized = await worker.client.is_user_authorized()
            if not authorized:
                LOG.warning("Telegram not authorized. Run 'python main.py' once to log in.")
            else:
                me = await worker.client.get_me()
                LOG.info("Web app ready. Signed in as %s", getattr(me, "username", me.id))
            recovered = _recover_download_only_jobs(worker)
            if recovered:
                LOG.info("Recovered %s download-only job(s) from temp storage", recovered)
            await worker.start_housekeeping(jobs)
    except Exception as exc:
        LOG.exception("Startup failed: %s", exc)
        authorized = False
    yield
    if worker:
        await worker.stop()


app = FastAPI(title="Narabox Telebot", lifespan=lifespan)


class ProcessRequest(BaseModel):
    link: str | None = None
    links: list[str] | None = None
    download_only: bool = False


@app.post("/api/process")
async def api_process(req: ProcessRequest):
    if not worker or not authorized:
        raise HTTPException(
            503,
            detail="Telegram not logged in. Run 'python main.py' once, enter the code, then restart the web app.",
        )

    _prune_recent_jobs(worker)
    normalized_links = _normalize_links(req.link, req.links)
    download_only = req.download_only or worker.settings.download_only

    existing_jobs: list[dict] = []
    new_jobs: list[dict] = []
    for entry in normalized_links:
        existing = _find_active_job_by_key(entry["link_key"])
        if existing is not None:
            existing_jobs.append(existing)
            continue
        new_jobs.append(_build_job(entry["link"], entry["link_key"], download_only))

    active_limit = worker.settings.web_max_active_jobs
    if _active_job_count() + len(new_jobs) > active_limit:
        raise HTTPException(429, detail=f"There are already {active_limit} active jobs. Wait for one to finish or destroy a downloaded file first.")

    async def run_job(job_id: str) -> None:
        job = jobs[job_id]
        try:
            await worker.process_link(job["link"], job=job)
        except AlreadyProcessedError as exc:
            job["status"] = "failed"
            job["error"] = str(exc)
            job["message"] = str(exc)
            _touch_job(job)
            LOG.info("Job %s skipped (already processed): %s", job_id, exc)
        except JobCancelledError:
            job["status"] = "cancelled"
            job["message"] = "Cancelled."
            job["error"] = None
            _touch_job(job)
            LOG.info("Job %s cancelled", job_id)
        except Exception as exc:  # noqa: BLE001
            job["status"] = "failed"
            job["error"] = str(exc)
            job["message"] = _job_message(exc)
            _touch_job(job)
            LOG.exception("Job %s failed: %s", job_id, exc)

    for job in new_jobs:
        jobs[job["job_id"]] = job
        asyncio.create_task(run_job(job["job_id"]))

    response_jobs = [_job_summary(job) for job in existing_jobs + new_jobs]
    payload: dict[str, object] = {"jobs": response_jobs}
    if len(response_jobs) == 1:
        payload["job_id"] = response_jobs[0]["job_id"]
        payload["status"] = response_jobs[0]["status"]
    return payload


@app.get("/api/status")
async def api_status(job_id: str):
    _prune_recent_jobs(worker)
    if job_id not in jobs:
        raise HTTPException(404, detail="Job not found")
    return _job_with_temp_url(jobs[job_id], worker)


@app.post("/api/cancel")
async def api_cancel(job_id: str):
    _prune_recent_jobs(worker)
    if job_id not in jobs:
        raise HTTPException(404, detail="Job not found")
    job = jobs[job_id]
    if job.get("status") not in CANCELLABLE_STATUSES:
        return {"ok": True, "message": "Job already finished"}
    job["cancelled"] = True
    job["message"] = "Cancellation requested…"
    _touch_job(job)
    return {"ok": True, "message": "Cancel requested"}


def _resolve_temp_file(job_id: str) -> tuple[dict, Path]:
    if job_id not in jobs:
        raise HTTPException(404, detail="Job not found")
    job = jobs[job_id]
    if not job.get("temp_path"):
        raise HTTPException(404, detail="File not available for this job")
    if job.get("status") not in {"downloaded", "failed"}:
        raise HTTPException(404, detail="File not available (wrong status)")
    path = Path(job["temp_path"])
    if not path.is_file():
        raise HTTPException(404, detail="File not found or already destroyed")
    return job, path


@app.get("/api/temp/{job_id}/{filename}")
async def api_temp_file(job_id: str, filename: str):
    _, path = _resolve_temp_file(job_id)
    expected_name = path.name
    if filename != expected_name:
        raise HTTPException(404, detail="File not available for this URL")
    return FileResponse(path, filename=expected_name, media_type="application/octet-stream")


@app.get("/api/temp/{job_id}/file")
async def api_temp_file_legacy(job_id: str):
    _, path = _resolve_temp_file(job_id)
    expected_name = path.name
    return RedirectResponse(
        url=f"/api/temp/{quote(job_id, safe='')}/{quote(expected_name, safe='')}",
        status_code=307,
    )


@app.get("/api/fetch/{token}/{filename}")
async def api_fetch_file(token: str, filename: str):
    settings = app_settings if app_settings is not None else (worker.settings if worker is not None else None)
    if settings is None:
        raise HTTPException(503, detail="Temp file server is not ready.")

    try:
        path = resolve_signed_temp_path(settings, token, filename)
    except ValueError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(500, detail=str(exc)) from exc

    if not path.is_file():
        raise HTTPException(404, detail="File not found or already expired.")

    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@app.post("/api/destroy/{job_id}")
async def api_destroy(job_id: str):
    _prune_recent_jobs(worker)
    if job_id not in jobs:
        raise HTTPException(404, detail="Job not found")

    job = jobs[job_id]
    if job.get("status") == "destroyed":
        return {"ok": True, "message": "Already destroyed"}
    if not job.get("temp_path"):
        raise HTTPException(400, detail="No temp file recorded for this job")

    path = Path(job["temp_path"])
    if path.is_file():
        try:
            path.unlink()
        except OSError as exc:
            raise HTTPException(500, detail=f"Could not delete file: {exc}") from exc

    root_temp_dir = worker.settings.temp_dir if worker is not None else path.parent
    parent = path.parent
    while parent != root_temp_dir and parent.exists():
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent

    job["status"] = "destroyed"
    job["message"] = "File deleted."
    job["temp_path"] = None
    _touch_job(job)
    return {"ok": True, "message": "File deleted"}


@app.get("/api/jobs")
async def api_jobs(limit: int = 30):
    _prune_recent_jobs(worker)
    order = sorted(
        jobs.items(),
        key=lambda item: (item[1].get("updated_ts", item[1].get("_ts", 0)), item[1].get("_ts", 0)),
        reverse=True,
    )
    return [_job_with_temp_url(job, worker) for _, job in order[:limit]]


@app.get("/api/health")
async def api_health():
    tools = detect_media_tools()
    return {
        "telegram": "connected" if authorized else ("disabled" if worker is None and app_settings is not None else "not_logged_in"),
        "worker": worker is not None,
        "active_jobs": _active_job_count(),
        "ffmpeg_available": tools.ffmpeg.available,
        "ffprobe_available": tools.ffprobe.available,
        "ffmpeg_path": tools.ffmpeg.path,
        "ffprobe_path": tools.ffprobe.path,
        "ffmpeg_version": tools.ffmpeg.version,
        "ffprobe_version": tools.ffprobe.version,
        "ffmpeg_error": tools.ffmpeg.error,
        "ffprobe_error": tools.ffprobe.error,
    }


@app.get("/health")
async def root_health():
    return {"status": "ok"}


def _html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Narabox Telebot - Batch Telegram Intake</title>
  <style>
    :root {
      --bg: #f6f7f3;
      --panel: #ffffff;
      --ink: #15211d;
      --muted: #5c6d66;
      --line: #d7dfd9;
      --accent: #0d7a5f;
      --accent-strong: #095842;
      --warning: #f4b400;
      --danger: #c0392b;
      --success: #1f7a44;
      --shadow: 0 18px 44px rgba(21, 33, 29, 0.08);
      --radius: 18px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(13, 122, 95, 0.12), transparent 28%),
        linear-gradient(180deg, #fbfcfa 0%, var(--bg) 100%);
      color: var(--ink);
    }
    .shell {
      max-width: 1100px;
      margin: 0 auto;
      padding: 2rem 1rem 3rem;
    }
    .hero {
      display: grid;
      gap: 1rem;
      margin-bottom: 1.5rem;
    }
    .hero-card {
      background: var(--panel);
      border: 1px solid rgba(13, 122, 95, 0.18);
      border-radius: 26px;
      box-shadow: var(--shadow);
      padding: 1.5rem;
    }
    .eyebrow {
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      background: rgba(13, 122, 95, 0.08);
      color: var(--accent-strong);
      border-radius: 999px;
      padding: 0.35rem 0.75rem;
      font-size: 0.82rem;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    h1 {
      margin: 0.7rem 0 0.5rem;
      font-size: clamp(2rem, 4vw, 3.4rem);
      line-height: 0.95;
      letter-spacing: -0.03em;
    }
    .hero-copy {
      max-width: 760px;
      color: var(--muted);
      font-size: 1.02rem;
      line-height: 1.6;
    }
    .layout {
      display: grid;
      gap: 1.25rem;
      grid-template-columns: minmax(0, 360px) minmax(0, 1fr);
      align-items: start;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 1.25rem;
    }
    .panel h2 {
      margin: 0 0 0.6rem;
      font-size: 1.15rem;
    }
    .panel p {
      margin: 0 0 1rem;
      color: var(--muted);
      line-height: 1.55;
      font-size: 0.95rem;
    }
    textarea, input[type="text"] {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 0.9rem 1rem;
      font: inherit;
      background: #fbfcfa;
      color: var(--ink);
      transition: border-color 0.2s ease, box-shadow 0.2s ease;
    }
    textarea {
      min-height: 170px;
      resize: vertical;
    }
    textarea:focus, input[type="text"]:focus {
      outline: none;
      border-color: rgba(13, 122, 95, 0.75);
      box-shadow: 0 0 0 4px rgba(13, 122, 95, 0.14);
    }
    .hint {
      font-size: 0.88rem;
      color: var(--muted);
      margin-top: 0.55rem;
    }
    .checkbox-row {
      display: flex;
      gap: 0.7rem;
      align-items: start;
      margin: 1rem 0 1.1rem;
      font-size: 0.95rem;
    }
    .checkbox-row input {
      margin-top: 0.18rem;
    }
    button {
      border: none;
      border-radius: 999px;
      cursor: pointer;
      font: inherit;
      transition: transform 0.14s ease, opacity 0.14s ease, background 0.14s ease;
    }
    button:hover { transform: translateY(-1px); }
    button:disabled { opacity: 0.65; cursor: not-allowed; transform: none; }
    .btn-primary {
      width: 100%;
      padding: 0.9rem 1.1rem;
      background: linear-gradient(135deg, var(--accent) 0%, var(--accent-strong) 100%);
      color: white;
      font-weight: 600;
    }
    .btn-secondary {
      background: #eaf1ed;
      color: var(--ink);
      padding: 0.55rem 0.9rem;
    }
    .btn-danger {
      background: #f9e5e1;
      color: var(--danger);
      padding: 0.55rem 0.9rem;
    }
    .notice {
      min-height: 52px;
      padding: 0.85rem 1rem;
      border-radius: 14px;
      background: #edf6f2;
      color: var(--accent-strong);
      border: 1px solid rgba(13, 122, 95, 0.18);
      display: none;
      margin-top: 1rem;
      line-height: 1.5;
    }
    .notice.error {
      background: #faece9;
      color: var(--danger);
      border-color: rgba(192, 57, 43, 0.18);
    }
    .server-status {
      display: flex;
      flex-wrap: wrap;
      gap: 0.6rem;
      margin-top: 0.9rem;
    }
    .pill {
      border-radius: 999px;
      padding: 0.4rem 0.75rem;
      background: #eff3f1;
      color: var(--muted);
      font-size: 0.86rem;
    }
    .pill.good {
      background: #e1f3e6;
      color: var(--success);
    }
    .pill.bad {
      background: #faece9;
      color: var(--danger);
    }
    .dashboard {
      display: grid;
      gap: 1.25rem;
    }
    .section-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 1rem;
      margin-bottom: 0.85rem;
    }
    .section-head h3 {
      margin: 0;
      font-size: 1rem;
    }
    .section-head span {
      color: var(--muted);
      font-size: 0.88rem;
    }
    .job-grid {
      display: grid;
      gap: 0.95rem;
    }
    .empty {
      border: 1px dashed var(--line);
      border-radius: 16px;
      padding: 1rem;
      color: var(--muted);
      background: rgba(255, 255, 255, 0.65);
    }
    .job-card {
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 1rem;
      background: #fbfcfa;
      display: grid;
      gap: 0.75rem;
    }
    .job-card[data-status="downloaded"] {
      border-color: rgba(13, 122, 95, 0.32);
      background: #f4fbf8;
    }
    .job-card[data-status="failed"] {
      border-color: rgba(192, 57, 43, 0.22);
      background: #fff7f6;
    }
    .job-card[data-status="expired"],
    .job-card[data-status="destroyed"] {
      background: #f4f5f3;
    }
    .job-head {
      display: flex;
      justify-content: space-between;
      align-items: start;
      gap: 1rem;
    }
    .job-title {
      margin: 0;
      font-size: 1rem;
      line-height: 1.35;
    }
    .job-link {
      color: var(--muted);
      font-size: 0.86rem;
      word-break: break-all;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 999px;
      padding: 0.35rem 0.7rem;
      background: #eef4f0;
      color: var(--accent-strong);
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      white-space: nowrap;
    }
    .badge.running { background: #e4f0ff; color: #0c4f99; }
    .badge.done { background: #e1f3e6; color: var(--success); }
    .badge.failed { background: #faece9; color: var(--danger); }
    .badge.cancelled, .badge.destroyed, .badge.expired { background: #ecefe9; color: #5f6b65; }
    .badge.downloaded { background: #e3f7ef; color: var(--accent-strong); }
    .job-message {
      color: var(--ink);
      font-size: 0.94rem;
    }
    .bar {
      height: 9px;
      border-radius: 999px;
      background: #e8ece8;
      overflow: hidden;
    }
    .bar-fill {
      height: 100%;
      background: linear-gradient(90deg, var(--accent) 0%, #1ab588 100%);
      transition: width 0.28s ease;
    }
    .meta-row {
      display: flex;
      flex-wrap: wrap;
      gap: 0.55rem;
      color: var(--muted);
      font-size: 0.82rem;
    }
    .temp-url {
      padding: 0.8rem 0.9rem;
      border-radius: 14px;
      background: #eef4f0;
      color: var(--ink);
      font-size: 0.85rem;
      overflow-wrap: anywhere;
    }
    .temp-url a {
      color: var(--accent-strong);
      text-decoration: none;
    }
    .temp-url a:hover {
      text-decoration: underline;
    }
    .job-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 0.65rem;
    }
    @media (max-width: 880px) {
      .layout { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="hero-card">
        <div class="eyebrow">Telegram intake dashboard</div>
        <h1>Queue up to 3 movie links and keep every job under control.</h1>
        <p class="hero-copy">Each job follows a simple flow: Telegram download, then either worker/CDN handoff or a temporary fetch URL with the original extension and a clean filename.</p>
        <div class="server-status" id="serverStatus"></div>
      </div>
    </section>

    <div class="layout">
      <section class="panel">
        <h2>Start jobs</h2>
        <p>Paste 1 to 3 Telegram message links, one per line. Duplicate links are reused instead of spawning another heavy download or worker handoff.</p>
        <form id="form">
          <label for="linksInput"><strong>Telegram message links</strong></label>
          <textarea id="linksInput" name="links" placeholder="https://t.me/channelname/123&#10;https://t.me/channelname/124&#10;https://t.me/c/1234567890/45" required></textarea>
          <div class="hint">The app trims blank lines and only accepts up to 3 links at a time.</div>
          <label class="checkbox-row">
            <input type="checkbox" id="downloadOnly" name="download_only">
            <span>Download only. Fetch the Telegram file, expose a temporary URL in this UI, then wait for Destroy after your other tool finishes fetching it.</span>
          </label>
          <button class="btn-primary" type="submit" id="submitBtn">Queue jobs</button>
        </form>
        <div class="notice" id="notice"></div>
      </section>

      <section class="dashboard" id="jobsPanel">
        <div class="panel">
          <div class="section-head">
            <h3>Active jobs</h3>
            <span id="activeCount">0 active</span>
          </div>
          <div class="job-grid" id="activeJobs"></div>
        </div>
        <div class="panel">
          <div class="section-head">
            <h3>Recent jobs</h3>
            <span>Refresh-safe actions stay here too, and old finished entries are pruned automatically.</span>
          </div>
          <div class="job-grid" id="recentJobs"></div>
        </div>
      </section>
    </div>
  </div>

  <script>
    const TERMINAL_STATUSES = new Set(['done', 'failed', 'cancelled', 'destroyed', 'expired']);
    const CANCELLABLE_STATUSES = new Set(['queued', 'downloading', 'waiting_to_prepare', 'preparing', 'uploading']);
    const form = document.getElementById('form');
    const linksInput = document.getElementById('linksInput');
    const submitBtn = document.getElementById('submitBtn');
    const noticeEl = document.getElementById('notice');
    const jobsPanel = document.getElementById('jobsPanel');
    const activeJobsEl = document.getElementById('activeJobs');
    const recentJobsEl = document.getElementById('recentJobs');
    const activeCountEl = document.getElementById('activeCount');
    const serverStatusEl = document.getElementById('serverStatus');

    async function apiJson(url, options) {
      const response = await fetch(url, options || {});
      const text = await response.text();
      let data = {};
      try {
        data = text ? JSON.parse(text) : {};
      } catch (_) {
        const snippet = (text || '').slice(0, 120);
        throw new Error((response.ok ? 'Invalid JSON response' : 'HTTP ' + response.status) + ': ' + (snippet || response.statusText || 'empty response'));
      }
      if (!response.ok) {
        const detail = data && (data.detail || data.message || data.error);
        throw new Error(detail || ('HTTP ' + response.status));
      }
      return data;
    }

    function showNotice(message, kind) {
      if (!message) {
        noticeEl.style.display = 'none';
        noticeEl.className = 'notice';
        noticeEl.textContent = '';
        return;
      }
      noticeEl.textContent = message;
      noticeEl.className = kind === 'error' ? 'notice error' : 'notice';
      noticeEl.style.display = 'block';
    }

    function splitLinks(raw) {
      return raw.split(/\\n+/).map((item) => item.trim()).filter(Boolean);
    }

    function badgeClass(status) {
      if (status === 'done') return 'badge done';
      if (status === 'downloaded') return 'badge downloaded';
      if (status === 'failed') return 'badge failed';
      if (status === 'cancelled' || status === 'destroyed' || status === 'expired') return 'badge ' + status;
      return 'badge running';
    }

    function escapeHtml(value) {
      if (value == null) return '';
      const div = document.createElement('div');
      div.textContent = String(value);
      return div.innerHTML;
    }

    function toAbsoluteUrl(value) {
      if (!value) return '';
      try {
        return new URL(value, window.location.origin).toString();
      } catch (_) {
        return String(value);
      }
    }

    function renderJob(job) {
      const label = job.file_name || job.link || job.message || job.job_id.slice(0, 8);
      const pct = Math.max(0, Math.min(100, Number(job.progress_pct || 0)));
      const resolvedTempUrl = job.temp_url ? toAbsoluteUrl(job.temp_url) : '';
      let actions = '';
      if (CANCELLABLE_STATUSES.has(job.status)) {
        actions += '<button type="button" class="btn-danger" data-action="cancel" data-job-id="' + escapeHtml(job.job_id) + '">Cancel</button>';
      }
      if (resolvedTempUrl && job.temp_path && job.status !== 'destroyed' && job.status !== 'expired') {
        actions += '<button type="button" class="btn-secondary" data-action="copy-temp-url" data-temp-url="' + escapeHtml(resolvedTempUrl) + '">Copy URL</button>';
      }
      if (job.temp_path && job.status !== 'destroyed' && job.status !== 'expired') {
        actions += '<button type="button" class="btn-danger" data-action="destroy" data-job-id="' + escapeHtml(job.job_id) + '">Destroy</button>';
      }

      let tempUrlPanel = '';
      if (resolvedTempUrl && job.temp_path) {
        tempUrlPanel = '<div class="temp-url"><strong>Temp URL</strong><br><a href="' + escapeHtml(resolvedTempUrl) + '" target="_blank" rel="noreferrer">' + escapeHtml(resolvedTempUrl) + '</a></div>';
      }

      const progress = TERMINAL_STATUSES.has(job.status) && job.status !== 'downloaded'
        ? ''
        : '<div class="bar"><div class="bar-fill" style="width:' + pct + '%"></div></div>';

      const meta = []
        .concat(job.download_only ? ['download only'] : [])
        .concat(job.file_name ? [job.file_name] : [])
        .concat(job.updated_ts ? ['updated ' + new Date(job.updated_ts * 1000).toLocaleTimeString()] : []);

      return ''
        + '<article class="job-card" data-status="' + escapeHtml(job.status) + '">'
        +   '<div class="job-head">'
        +     '<div>'
        +       '<h4 class="job-title">' + escapeHtml(label) + '</h4>'
        +       (job.link ? '<div class="job-link">' + escapeHtml(job.link) + '</div>' : '')
        +     '</div>'
        +     '<span class="' + badgeClass(job.status) + '">' + escapeHtml(job.status) + '</span>'
        +   '</div>'
        +   '<div class="job-message">' + escapeHtml(job.message || job.status) + '</div>'
        +   progress
        +   (meta.length ? '<div class="meta-row">' + meta.map((item) => '<span>' + escapeHtml(item) + '</span>').join('') + '</div>' : '')
        +   tempUrlPanel
        +   (actions ? '<div class="job-actions">' + actions + '</div>' : '')
        + '</article>';
    }

    async function refreshHealth() {
      try {
        const health = await apiJson('/api/health');
        serverStatusEl.innerHTML = ''
          + '<span class="pill ' + (health.telegram === 'connected' ? 'good' : 'bad') + '">'
          + (health.telegram === 'connected' ? 'Telegram connected' : 'Telegram not logged in')
          + '</span>'
          + '<span class="pill">' + escapeHtml(String(health.active_jobs)) + ' active jobs</span>'
          + '<span class="pill ' + (health.ffmpeg_available ? 'good' : 'bad') + '">'
          + (health.ffmpeg_available ? 'ffmpeg ready' : 'ffmpeg missing')
          + '</span>';
      } catch (_) {
        serverStatusEl.innerHTML = '<span class="pill bad">Server unreachable</span>';
      }
    }

    async function refreshJobs() {
      try {
        const list = await apiJson('/api/jobs?limit=50');
        const active = list.filter((job) => !TERMINAL_STATUSES.has(job.status));
        const recent = list.filter((job) => TERMINAL_STATUSES.has(job.status));
        activeCountEl.textContent = active.length + ' active';
        activeJobsEl.innerHTML = active.length ? active.map(renderJob).join('') : '<div class="empty">No active jobs right now. Queue up to 3 Telegram links and they will appear here immediately.</div>';
        recentJobsEl.innerHTML = recent.length ? recent.map(renderJob).join('') : '<div class="empty">Finished, failed, destroyed, and expired jobs will collect here.</div>';
      } catch (error) {
        activeJobsEl.innerHTML = '<div class="empty">Could not load jobs: ' + escapeHtml(error.message || 'unknown error') + '</div>';
      }
    }

    jobsPanel.addEventListener('click', async (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;

      if (target.dataset.action === 'copy-temp-url' && target.dataset.tempUrl) {
        try {
          await navigator.clipboard.writeText(target.dataset.tempUrl);
          showNotice('Temporary URL copied.', 'ok');
        } catch (_) {
          showNotice('Clipboard access failed. Copy the URL manually.', 'error');
        }
        return;
      }

      if (target.dataset.action === 'cancel' && target.dataset.jobId) {
        try {
          await apiJson('/api/cancel?job_id=' + encodeURIComponent(target.dataset.jobId), { method: 'POST' });
          showNotice('Cancellation requested.', 'ok');
          await refreshJobs();
        } catch (error) {
          showNotice(error.message || 'Cancel failed.', 'error');
        }
        return;
      }

      if (target.dataset.action === 'destroy' && target.dataset.jobId) {
        try {
          await apiJson('/api/destroy/' + encodeURIComponent(target.dataset.jobId), { method: 'POST' });
          showNotice('Temporary file destroyed.', 'ok');
          await refreshJobs();
        } catch (error) {
          showNotice(error.message || 'Destroy failed.', 'error');
        }
      }
    });

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const raw = linksInput.value.trim();
      const links = splitLinks(raw);
      if (!links.length) {
        showNotice('Paste at least one Telegram message link.', 'error');
        return;
      }

      submitBtn.disabled = true;
      showNotice('Submitting jobs…', 'ok');
      try {
        const payload = {
          links: links,
          download_only: document.getElementById('downloadOnly').checked
        };
        const data = await apiJson('/api/process', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const count = Array.isArray(data.jobs) ? data.jobs.length : 0;
        showNotice('Queued ' + count + ' job' + (count === 1 ? '' : 's') + '.', 'ok');
        linksInput.value = '';
        await refreshJobs();
      } catch (error) {
        showNotice(error.message || 'Could not queue jobs.', 'error');
      } finally {
        submitBtn.disabled = false;
      }
    });

    refreshHealth();
    refreshJobs();
    setInterval(refreshHealth, 60000);
    setInterval(refreshJobs, 2000);
  </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return _html()


if __name__ == "__main__":
    import os
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    port = int(os.environ.get("PORT", "8765"))
    uvicorn.run("app.web:app", host="0.0.0.0", port=port, reload=False)
