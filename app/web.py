"""
Simple web UI: paste a t.me link, run download → CDN → notify → delete.
Requires a valid Telethon session (run `python main.py` once to log in).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

from app.config import Settings
from app.media_tools import detect_media_tools
from app.telegram_worker import TelegramPipeWorker, JobCancelledError, AlreadyProcessedError

LOG = logging.getLogger("telebot.web")
jobs: dict[str, dict] = {}
worker: TelegramPipeWorker | None = None
authorized = False


def _job_with_temp_url(job: dict, current_worker: TelegramPipeWorker | None) -> dict:
    out = dict(job)
    if not current_worker:
        return out
    temp_path = out.get("temp_path")
    if not temp_path:
        return out
    base = (current_worker.settings.temp_public_url or "").strip()
    if base:
        out["temp_url"] = f"{base.rstrip('/')}/api/temp/{quote(str(out.get('job_id', '')))}" + "/file"
    return out


def _recover_download_only_jobs(current_worker: TelegramPipeWorker) -> int:
    recovered = 0
    for child in current_worker.settings.temp_dir.iterdir():
        if not child.is_dir():
            continue
        job_id = child.name
        if job_id in jobs:
            continue
        files = [p for p in child.iterdir() if p.is_file()]
        if not files:
            continue
        latest = max(files, key=lambda p: p.stat().st_mtime)
        jobs[job_id] = {
            "job_id": job_id,
            "status": "downloaded",
            "progress_pct": 100,
            "message": "Recovered after restart. Ready for CDN fetch or destroy.",
            "file_name": latest.name,
            "result": None,
            "error": None,
            "temp_path": str(latest),
            "download_only": True,
            "_ts": latest.stat().st_mtime,
        }
        recovered += 1
    return recovered


@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker, authorized
    try:
        settings = Settings.load()
        worker = TelegramPipeWorker(settings)
        await worker.store.init()
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
    except Exception as e:
        LOG.exception("Startup failed: %s", e)
        authorized = False
    yield
    if worker:
        await worker.cdn.close()
        await worker.client.disconnect()


app = FastAPI(title="Narabox Telebot", lifespan=lifespan)


class ProcessRequest(BaseModel):
    link: str
    download_only: bool = False

    @property
    def link_str(self) -> str:
        return self.link.strip()


@app.post("/api/process")
async def api_process(req: ProcessRequest):
    if not worker or not authorized:
        raise HTTPException(
            503,
            detail="Telegram not logged in. Run 'python main.py' once, enter the code, then restart the web app.",
        )
    from app.link_parser import parse_telegram_link
    if not parse_telegram_link(req.link_str):
        raise HTTPException(422, detail="Invalid t.me URL. Use e.g. https://t.me/jozzmovies/45")
    job_id = str(uuid.uuid4())
    download_only = req.download_only or (worker.settings.download_only if worker else False)
    jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "progress_pct": 0,
        "message": "Queued…",
        "file_name": "",
        "result": None,
        "error": None,
        "temp_path": None,
        "download_only": download_only,
        "_ts": time.time(),
    }

    def _job_message(e: Exception) -> str:
        s = str(e).lower()
        if "authkeyduplicated" in s or "authorization key" in s:
            return "Telegram session in use elsewhere or invalid. Use this session only on this server and restart the app."
        if "disconnected" in s or "cannot send requests while disconnected" in s:
            return "Telegram disconnected. Reload the page and try again; if it persists, restart the app."
        if "not logged in" in s:
            return "Telegram not logged in. Restart the app after logging in with 'python main.py' once."
        if "readerror" in s or "read error" in s:
            return "Upload to CDN failed (connection closed). Try again."
        return str(e)

    async def run():
        try:
            await worker.process_link(req.link_str, job=jobs[job_id])
        except AlreadyProcessedError as e:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = str(e)
            jobs[job_id]["message"] = str(e)
            LOG.info("Job %s skipped (already processed): %s", job_id, e)
        except Exception as e:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = str(e)
            jobs[job_id]["message"] = _job_message(e)
            LOG.exception("Job %s failed: %s", job_id, e)

    asyncio.create_task(run())
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/status")
async def api_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, detail="Job not found")
    return _job_with_temp_url(jobs[job_id], worker)


@app.post("/api/cancel")
async def api_cancel(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, detail="Job not found")
    j = jobs[job_id]
    if j["status"] in ("done", "failed", "cancelled"):
        return {"ok": True, "message": "Job already finished"}
    j["cancelled"] = True
    return {"ok": True, "message": "Cancel requested"}


@app.get("/api/temp/{job_id}/file")
async def api_temp_file(job_id: str):
    """Serve the downloaded file so the CDN (or you) can fetch it by URL. Only for jobs in 'downloaded' state."""
    if job_id not in jobs:
        raise HTTPException(404, detail="Job not found")
    j = jobs[job_id]
    if not j.get("temp_path"):
        raise HTTPException(404, detail="File not available for this job")
    if j.get("status") not in {"downloaded", "failed"}:
        raise HTTPException(404, detail="File not available (wrong status)")
    path = j.get("temp_path")
    if not path or not Path(path).is_file():
        raise HTTPException(404, detail="File not found or already destroyed")
    return FileResponse(path, filename=j.get("file_name") or Path(path).name, media_type="application/octet-stream")


@app.post("/api/destroy/{job_id}")
async def api_destroy(job_id: str):
    """Delete the temp file for this job after you have imported it to the CDN. Call once CDN has fetched the file."""
    if job_id not in jobs:
        raise HTTPException(404, detail="Job not found")
    j = jobs[job_id]
    if j.get("status") == "destroyed":
        return {"ok": True, "message": "Already destroyed"}
    if not j.get("temp_path"):
        raise HTTPException(400, detail="No temp file recorded for this job")
    path = Path(j.get("temp_path") or "")
    if path.is_file():
        try:
            path.unlink()
        except OSError as e:
            raise HTTPException(500, detail=f"Could not delete file: {e}") from e
    parent = path.parent
    if parent.is_dir() and str(parent) != ".":
        try:
            parent.rmdir()
        except OSError:
            pass
    j["status"] = "destroyed"
    j["message"] = "File deleted."
    j["temp_path"] = None
    j["temp_url"] = None
    return {"ok": True, "message": "File deleted"}


@app.get("/api/jobs")
async def api_jobs(limit: int = 20):
    """List recent jobs (newest first)."""
    order = sorted(jobs.items(), key=lambda x: x[1].get("_ts", 0), reverse=True)
    return [_job_with_temp_url(v, worker) for _, v in order[:limit]]


@app.get("/api/health")
async def api_health():
    tools = detect_media_tools()
    return {
        "telegram": "connected" if authorized else "not_logged_in",
        "worker": worker is not None,
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
    """Simple 200 for proxy/load-balancer health checks. Use /api/health for full status."""
    return {"status": "ok"}


def _html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Narabox Telebot – Ingest from Telegram</title>
  <style>
    * { box-sizing: border-box; }
    body {
      font-family: system-ui, -apple-system, sans-serif;
      max-width: 560px;
      margin: 2rem auto;
      padding: 0 1rem;
      color: #1a1a1a;
      background: #f5f5f5;
    }
    h1 { font-size: 1.5rem; margin-bottom: 0.5rem; }
    .muted { color: #666; font-size: 0.9rem; margin-bottom: 1.5rem; }
    label { display: block; font-weight: 600; margin-bottom: 0.35rem; }
    input[type="text"], input[type="url"] {
      width: 100%;
      padding: 0.6rem 0.75rem;
      border: 1px solid #ccc;
      border-radius: 6px;
      font-size: 1rem;
      margin-bottom: 1rem;
    }
    input:focus { outline: none; border-color: #0d6efd; }
    button {
      background: #0d6efd;
      color: #fff;
      border: none;
      padding: 0.65rem 1.25rem;
      border-radius: 6px;
      font-size: 1rem;
      cursor: pointer;
      width: 100%;
    }
    button:hover { background: #0b5ed7; }
    button:disabled { background: #6c757d; cursor: not-allowed; }
    #status {
      margin-top: 1.5rem;
      padding: 1rem;
      border-radius: 8px;
      background: #fff;
      border: 1px solid #dee2e6;
      min-height: 80px;
    }
    #status:empty { display: none; }
    .bar {
      height: 8px;
      background: #e9ecef;
      border-radius: 4px;
      overflow: hidden;
      margin-top: 0.5rem;
    }
    .bar-fill {
      height: 100%;
      background: #0d6efd;
      transition: width 0.3s ease;
    }
    .done { border-color: #198754; background: #d1e7dd; }
    .failed { border-color: #dc3545; background: #f8d7da; }
    .cancelled { border-color: #6c757d; background: #e9ecef; }
    .channel-hint { font-size: 0.85rem; color: #666; margin-top: 0.25rem; }
    .status-actions { margin-top: 0.75rem; display: flex; gap: 0.5rem; flex-wrap: wrap; }
    .btn-secondary { background: #6c757d; }
    .btn-secondary:hover { background: #5a6268; }
    .btn-danger { background: #dc3545; }
    .btn-danger:hover { background: #c82333; }
    .btn-sm { padding: 0.4rem 0.8rem; font-size: 0.9rem; width: auto; }
    .checkbox-label { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 1rem; font-weight: normal; }
    .checkbox-label input { width: auto; margin: 0; }
    .temp-url-box { margin: 0.5rem 0; padding: 0.5rem; background: #f0f0f0; border-radius: 4px; font-size: 0.85rem; word-break: break-all; }
    #serverStatus { font-size: 0.85rem; color: #666; margin-bottom: 1rem; }
    #serverStatus.connected { color: #198754; }
    #serverStatus.disconnected { color: #dc3545; }
    .jobs-list { margin-top: 1.5rem; }
    .jobs-list h3 { font-size: 1rem; margin-bottom: 0.5rem; }
    .jobs-list ul { list-style: none; padding: 0; margin: 0; }
    .jobs-list li { padding: 0.4rem 0; border-bottom: 1px solid #eee; font-size: 0.9rem; display: flex; justify-content: space-between; align-items: center; }
    .jobs-list .badge { font-size: 0.75rem; padding: 0.2rem 0.5rem; border-radius: 4px; }
    .jobs-list .badge.done { background: #d1e7dd; color: #0f5132; }
    .jobs-list .badge.failed { background: #f8d7da; color: #842029; }
    .jobs-list .badge.cancelled { background: #e9ecef; color: #495057; }
    .jobs-list .badge.running { background: #cce5ff; color: #004085; }
    .jobs-list .badge.downloaded { background: #cce5ff; color: #004085; }
    .jobs-list .badge.destroyed { background: #e9ecef; color: #495057; }
  </style>
</head>
<body>
  <h1>Narabox Telebot</h1>
  <p id="serverStatus" class="muted">Checking…</p>
  <p class="muted">Paste a Telegram message link. With <strong>Download only</strong>: get a link to the file, paste it in the CDN import, then click Destroy.</p>
  <form id="form">
    <label for="channel">Channel (saved in browser)</label>
    <input type="text" id="channel" name="channel" placeholder="@jozzmovies">
    <p class="channel-hint">Optional. Paste a channel to remember it; the link below is what gets processed.</p>
    <label for="link">Message link</label>
    <input type="url" id="link" name="link" placeholder="https://t.me/jozzmovies/45" required>
    <p class="channel-hint">Paste a t.me link like <code>https://t.me/channelname/123</code> (message must contain a video).</p>
    <label class="checkbox-label"><input type="checkbox" id="downloadOnly" name="download_only"> Download only (get link → paste in CDN → Destroy)</label>
    <button type="submit" id="btn">Download &amp; ingest</button>
  </form>
  <div id="status"></div>
  <div class="jobs-list">
    <h3>Recent jobs</h3>
    <ul id="jobsList"></ul>
  </div>
  <script>
    const form = document.getElementById('form');
    const channelInput = document.getElementById('channel');
    const linkInput = document.getElementById('link');
    const btn = document.getElementById('btn');
    const statusEl = document.getElementById('status');
    const serverStatusEl = document.getElementById('serverStatus');
    const jobsListEl = document.getElementById('jobsList');
    let currentJobId = null;
    let pollInterval = null;
    try {
      var saved = localStorage.getItem('telebot_channel');
      if (saved) channelInput.value = saved;
    } catch (_) {}
    channelInput.addEventListener('change', function() { try { localStorage.setItem('telebot_channel', channelInput.value); } catch (_) {} });
    channelInput.addEventListener('blur', function() { try { localStorage.setItem('telebot_channel', channelInput.value); } catch (_) {} });

    async function refreshHealth() {
      try {
        const r = await fetch('/api/health');
        const h = await r.json();
        serverStatusEl.textContent = h.telegram === 'connected' ? 'Telegram: connected' : 'Telegram: not logged in (run python main.py once to log in)';
        serverStatusEl.className = h.telegram === 'connected' ? 'connected' : 'disconnected';
      } catch (_) {
        serverStatusEl.textContent = 'Server: unreachable';
        serverStatusEl.className = 'disconnected';
      }
    }
    refreshHealth();
    setInterval(refreshHealth, 60000);

    async function refreshJobs() {
      try {
        const r = await fetch('/api/jobs?limit=10');
        const list = await r.json();
        jobsListEl.innerHTML = list.length === 0 ? '<li class="muted">No jobs yet</li>' : list.map(function(j) {
          var label = j.file_name || j.message || j.job_id.slice(0, 8);
          var badgeClass = j.status === 'done' ? 'done' : j.status === 'failed' ? 'failed' : j.status === 'cancelled' ? 'cancelled' : j.status === 'downloaded' ? 'downloaded' : j.status === 'destroyed' ? 'destroyed' : 'running';
          var badge = '<span class="badge ' + badgeClass + '">' + j.status + '</span>';
          var actions = '';
          if (j.temp_path) {
            if (j.temp_url) {
              actions += '<button type="button" class="btn-secondary btn-sm" data-action="copy-temp-url" data-temp-url="' + escapeHtml(j.temp_url) + '">Copy URL</button>';
            }
            actions += '<button type="button" class="btn-danger btn-sm" data-action="destroy" data-job-id="' + escapeHtml(j.job_id) + '">Destroy</button>';
          }
          return '<li><span title="' + escapeHtml(j.job_id) + '">' + escapeHtml(String(label).slice(0, 50)) + '</span><span>' + badge + actions + '</span></li>';
        }).join('');
      } catch (_) {}
    }
    setInterval(refreshJobs, 5000);
    refreshJobs();

    function showStatus(html, className = '') {
      statusEl.innerHTML = html;
      statusEl.className = className;
      statusEl.style.display = 'block';
    }

    function stopPolling() {
      if (pollInterval) clearInterval(pollInterval);
      pollInterval = null;
      currentJobId = null;
      btn.disabled = false;
      refreshJobs();
    }

    function poll(jobId) {
      currentJobId = jobId;
      pollInterval = setInterval(async () => {
        try {
          const r = await fetch('/api/status?job_id=' + encodeURIComponent(jobId));
          const j = await r.json();
          const pct = j.progress_pct ?? 0;
          const canCancel = ['queued', 'starting', 'downloading', 'preparing', 'uploading'].indexOf(j.status) >= 0;
          let html = '<p><strong>' + (j.message || j.status) + '</strong></p>';
          if (j.file_name) html += '<p class="muted">' + escapeHtml(j.file_name) + '</p>';
          html += '<div class="bar"><div class="bar-fill" style="width:' + pct + '%"></div></div>';
          if (j.status === 'downloaded') {
            if (j.temp_url) {
              html += '<p class="muted">Paste this URL in CDN import (source URL), then click Destroy after the CDN has fetched it.</p>';
              html += '<div class="temp-url-box"><code id="tempUrlRef">' + escapeHtml(j.temp_url) + '</code></div>';
              html += '<div class="status-actions"><button type="button" class="btn-secondary btn-sm" data-action="copy-temp" data-job-id="' + escapeHtml(jobId) + '">Copy link</button><button type="button" class="btn-danger btn-sm" data-action="destroy" data-job-id="' + escapeHtml(jobId) + '">Destroy</button><button type="button" class="btn-secondary btn-sm" data-action="clear">Clear</button></div>';
            } else {
              html += '<p class="muted">Set TEMP_PUBLIC_URL on the server to get the link. Path: ' + escapeHtml(j.temp_path || '') + '</p>';
              html += '<div class="status-actions"><button type="button" class="btn-danger btn-sm" data-action="destroy" data-job-id="' + escapeHtml(jobId) + '">Destroy</button><button type="button" class="btn-secondary btn-sm" data-action="clear">Clear</button></div>';
            }
          } else {
            html += '<div class="status-actions">';
            if (canCancel) html += '<button type="button" class="btn-danger btn-sm" data-action="cancel" data-job-id="' + escapeHtml(jobId) + '">Cancel</button>';
            html += '<button type="button" class="btn-secondary btn-sm" data-action="clear">Clear</button></div>';
          }
          var statusClass = j.status === 'failed' ? 'failed' : j.status === 'done' ? 'done' : j.status === 'cancelled' ? 'cancelled' : j.status === 'downloaded' ? 'done' : '';
          showStatus(html, statusClass);
          if (j.status === 'done') {
            if (j.result && j.result.asset_id) html += '<p class="muted">Asset: ' + escapeHtml(j.result.asset_id) + '</p>';
            showStatus(statusEl.innerHTML, 'done');
            stopPolling();
          }
          if (j.status === 'downloaded') {
            stopPolling();
          }
          if (j.status === 'failed') {
            if (j.error) html += '<p class="muted">' + escapeHtml(j.error) + '</p>';
            showStatus(statusEl.innerHTML, 'failed');
            stopPolling();
          }
          if (j.status === 'cancelled') {
            stopPolling();
            showStatus(statusEl.innerHTML, 'cancelled');
          }
        } catch (e) {
          stopPolling();
          showStatus('<p class="failed">Status check failed: ' + escapeHtml(e.message) + '</p>', 'failed');
        }
      }, 1500);
    }

    function escapeHtml(s) {
      if (s == null) return '';
      const div = document.createElement('div');
      div.textContent = s;
      return div.innerHTML;
    }

    statusEl.addEventListener('click', function(e) {
      var target = e.target;
      if (target.dataset.action === 'cancel' && target.dataset.jobId) {
        fetch('/api/cancel?job_id=' + encodeURIComponent(target.dataset.jobId), { method: 'POST' }).then(function() {});
      }
      if (target.dataset.action === 'copy-temp') {
        var code = document.getElementById('tempUrlRef');
        if (code) {
          navigator.clipboard.writeText(code.textContent).then(function() { target.textContent = 'Copied!'; });
        }
      }
      if (target.dataset.action === 'copy-temp-url' && target.dataset.tempUrl) {
        navigator.clipboard.writeText(target.dataset.tempUrl).then(function() { target.textContent = 'Copied!'; });
      }
      if (target.dataset.action === 'destroy' && target.dataset.jobId) {
        fetch('/api/destroy/' + encodeURIComponent(target.dataset.jobId), { method: 'POST' }).then(async function(r) {
          var data = await r.json().catch(function() { return { ok: false, message: r.statusText || 'Destroy failed' }; });
          return { ok: r.ok && !!data.ok, message: data.message || data.detail || 'Destroy failed' };
        }).then(function(data) {
          if (data.ok) {
            showStatus('<p><strong>File deleted.</strong></p><div class="status-actions"><button type="button" class="btn-secondary btn-sm" data-action="clear">Clear</button></div>', '');
            refreshJobs();
          } else {
            showStatus('<p class="failed">' + escapeHtml(data.message) + '</p>', 'failed');
          }
        });
      }
      if (target.dataset.action === 'clear') {
        showStatus('', '');
        statusEl.style.display = 'none';
      }
    });

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const link = linkInput.value.trim();
      if (!link) return;
      btn.disabled = true;
      showStatus('<p>Starting…</p>', '');
      try {
        const downloadOnly = document.getElementById('downloadOnly').checked;
        const r = await fetch('/api/process', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ link: link, download_only: downloadOnly })
        });
        if (!r.ok) {
          const err = await r.json().catch(() => ({ detail: r.statusText }));
          throw new Error(err.detail || err.message || 'Request failed');
        }
        const data = await r.json();
        poll(data.job_id);
      } catch (err) {
        showStatus('<p class="failed">' + escapeHtml(err.message) + '</p>', 'failed');
        btn.disabled = false;
      }
    });
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
