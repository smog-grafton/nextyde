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

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.config import Settings
from app.telegram_worker import TelegramPipeWorker, JobCancelledError

LOG = logging.getLogger("telebot.web")
jobs: dict[str, dict] = {}
worker: TelegramPipeWorker | None = None
authorized = False


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
    jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "progress_pct": 0,
        "message": "Queued…",
        "file_name": "",
        "result": None,
        "error": None,
        "_ts": time.time(),
    }

    async def run():
        try:
            await worker.process_link(req.link_str, job=jobs[job_id])
        except Exception as e:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = str(e)
            jobs[job_id]["message"] = str(e)
            LOG.exception("Job %s failed: %s", job_id, e)

    asyncio.create_task(run())
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/status")
async def api_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, detail="Job not found")
    return jobs[job_id]


@app.post("/api/cancel")
async def api_cancel(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, detail="Job not found")
    j = jobs[job_id]
    if j["status"] in ("done", "failed", "cancelled"):
        return {"ok": True, "message": "Job already finished"}
    j["cancelled"] = True
    return {"ok": True, "message": "Cancel requested"}


@app.get("/api/jobs")
async def api_jobs(limit: int = 20):
    """List recent jobs (newest first)."""
    order = sorted(jobs.items(), key=lambda x: x[1].get("_ts", 0), reverse=True)
    return [v for _, v in order[:limit]]


@app.get("/api/health")
async def api_health():
    return {"telegram": "connected" if authorized else "not_logged_in", "worker": worker is not None}


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
  </style>
</head>
<body>
  <h1>Narabox Telebot</h1>
  <p id="serverStatus" class="muted">Checking…</p>
  <p class="muted">Paste a Telegram message link. The file will be downloaded, sent to your CDN, then deleted locally.</p>
  <form id="form">
    <label for="channel">Channel (saved in browser)</label>
    <input type="text" id="channel" name="channel" placeholder="@jozzmovies">
    <p class="channel-hint">Optional. Paste a channel to remember it; the link below is what gets processed.</p>
    <label for="link">Message link</label>
    <input type="url" id="link" name="link" placeholder="https://t.me/jozzmovies/45" required>
    <p class="channel-hint">Paste a t.me link like <code>https://t.me/channelname/123</code> (message must contain a video).</p>
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
          var badge = '<span class="badge ' + (j.status === 'done' ? 'done' : j.status === 'failed' ? 'failed' : j.status === 'cancelled' ? 'cancelled' : 'running') + '">' + j.status + '</span>';
          return '<li><span title="' + escapeHtml(j.job_id) + '">' + escapeHtml(String(label).slice(0, 50)) + '</span>' + badge + '</li>';
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
          const canCancel = ['queued', 'starting', 'downloading', 'uploading'].indexOf(j.status) >= 0;
          let html = '<p><strong>' + (j.message || j.status) + '</strong></p>';
          if (j.file_name) html += '<p class="muted">' + escapeHtml(j.file_name) + '</p>';
          html += '<div class="bar"><div class="bar-fill" style="width:' + pct + '%"></div></div>';
          html += '<div class="status-actions">';
          if (canCancel) html += '<button type="button" class="btn-danger btn-sm" data-action="cancel" data-job-id="' + escapeHtml(jobId) + '">Cancel</button>';
          html += '<button type="button" class="btn-secondary btn-sm" data-action="clear">Clear</button></div>';
          var statusClass = j.status === 'failed' ? 'failed' : j.status === 'done' ? 'done' : j.status === 'cancelled' ? 'cancelled' : '';
          showStatus(html, statusClass);
          if (j.status === 'done') {
            if (j.result && j.result.asset_id) html += '<p class="muted">Asset: ' + escapeHtml(j.result.asset_id) + '</p>';
            showStatus(statusEl.innerHTML, 'done');
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
        const r = await fetch('/api/process', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ link })
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
