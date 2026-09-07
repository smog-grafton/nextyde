from __future__ import annotations

import aiosqlite
import json
import time
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_messages (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    file_name TEXT,
    status TEXT NOT NULL,
    cdn_response TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS telescope_jobs (
    job_id TEXT PRIMARY KEY,
    telegram_url TEXT NOT NULL,
    reference_json TEXT NOT NULL,
    portal_source_id TEXT,
    storage_target_requested TEXT NOT NULL DEFAULT 'auto',
    storage_target TEXT,
    object_key TEXT,
    file_name TEXT,
    mime_type TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    progress INTEGER NOT NULL DEFAULT 0,
    bytes_total INTEGER NOT NULL DEFAULT 0,
    bytes_transferred INTEGER NOT NULL DEFAULT 0,
    multipart_upload_id TEXT,
    multipart_parts_json TEXT NOT NULL DEFAULT '[]',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    result_json TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    callback_status TEXT NOT NULL DEFAULT 'pending',
    callback_attempts INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    started_at REAL,
    updated_at REAL NOT NULL,
    completed_at REAL
);
CREATE INDEX IF NOT EXISTS telescope_jobs_status_created_idx ON telescope_jobs(status, created_at);

CREATE TABLE IF NOT EXISTS telescope_callbacks (
    event_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    last_error TEXT,
    created_at REAL NOT NULL,
    delivered_at REAL,
    FOREIGN KEY(job_id) REFERENCES telescope_jobs(job_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS telescope_callbacks_due_idx ON telescope_callbacks(status, next_attempt_at);
"""

TELESCOPE_JOB_COLUMNS = {
    "portal_source_id",
    "storage_target_requested",
    "storage_target",
    "object_key",
    "file_name",
    "mime_type",
    "status",
    "progress",
    "bytes_total",
    "bytes_transferred",
    "multipart_upload_id",
    "multipart_parts_json",
    "attempts",
    "last_error",
    "result_json",
    "metadata_json",
    "callback_status",
    "callback_attempts",
    "cancel_requested",
    "started_at",
    "updated_at",
    "completed_at",
}


class StateStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.executescript(SCHEMA)
            await db.commit()

    async def is_processed(self, chat_id: int, message_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM processed_messages WHERE chat_id = ? AND message_id = ? LIMIT 1",
                (chat_id, message_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
            return row is not None

    async def get_processed(self, chat_id: int, message_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT chat_id, message_id, file_name, status, cdn_response, created_at, updated_at "
                "FROM processed_messages WHERE chat_id = ? AND message_id = ? LIMIT 1",
                (chat_id, message_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
            return dict(row) if row is not None else None

    async def mark_processed(
        self,
        chat_id: int,
        message_id: int,
        file_name: str | None,
        status: str,
        cdn_response: str | None,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO processed_messages (chat_id, message_id, file_name, status, cdn_response)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id)
                DO UPDATE SET
                    file_name = excluded.file_name,
                    status = excluded.status,
                    cdn_response = excluded.cdn_response,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (chat_id, message_id, file_name, status, cdn_response),
            )
            await db.commit()

    async def create_telescope_job(self, job: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO telescope_jobs (
                    job_id, telegram_url, reference_json, portal_source_id,
                    storage_target_requested, status, metadata_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    job["job_id"],
                    job["telegram_url"],
                    json.dumps(job["reference"], separators=(",", ":")),
                    str(job.get("portal_source_id") or "") or None,
                    str(job.get("storage_target") or "auto"),
                    json.dumps(job.get("metadata") or {}, separators=(",", ":")),
                    now,
                    now,
                ),
            )
            await db.commit()
        created = await self.get_telescope_job(job["job_id"])
        if created is None:
            raise RuntimeError("Telescope job was not persisted")
        return created

    async def get_telescope_job(self, job_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM telescope_jobs WHERE job_id = ?", (job_id,))
            row = await cursor.fetchone()
            await cursor.close()
        return self._decode_telescope_job(dict(row)) if row is not None else None

    async def list_telescope_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM telescope_jobs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(500, limit)),),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._decode_telescope_job(dict(row)) for row in rows]

    async def next_queued_telescope_jobs(self, limit: int) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT * FROM telescope_jobs WHERE status = 'queued' AND cancel_requested = 0 ORDER BY created_at LIMIT ?",
                (max(1, limit),),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            job_ids = [str(row["job_id"]) for row in rows]
            if job_ids:
                placeholders = ",".join("?" for _ in job_ids)
                await db.execute(
                    f"UPDATE telescope_jobs SET status = 'resolving', started_at = COALESCE(started_at, ?), "
                    f"updated_at = ?, attempts = attempts + 1 WHERE job_id IN ({placeholders})",
                    (time.time(), time.time(), *job_ids),
                )
            await db.commit()
        claimed: list[dict[str, Any]] = []
        for row in rows:
            decoded = self._decode_telescope_job(dict(row))
            decoded["status"] = "resolving"
            decoded["attempts"] = int(decoded.get("attempts") or 0) + 1
            claimed.append(decoded)
        return claimed

    async def update_telescope_job(self, job_id: str, **changes: Any) -> None:
        invalid = set(changes) - TELESCOPE_JOB_COLUMNS
        if invalid:
            raise ValueError(f"Unsupported Telescope job fields: {', '.join(sorted(invalid))}")
        if not changes:
            return
        changes["updated_at"] = time.time()
        assignments = ", ".join(f"{column} = ?" for column in changes)
        values = [self._encode_job_value(column, value) for column, value in changes.items()]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE telescope_jobs SET {assignments} WHERE job_id = ?",
                (*values, job_id),
            )
            await db.commit()

    async def request_telescope_cancel(self, job_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE telescope_jobs SET cancel_requested = 1, updated_at = ? "
                "WHERE job_id = ? AND status NOT IN ('ready', 'failed', 'cancelled')",
                (time.time(), job_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def requeue_telescope_job(self, job_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE telescope_jobs SET status = 'queued', cancel_requested = 0, last_error = NULL, "
                "completed_at = NULL, updated_at = ? WHERE job_id = ? AND status IN ('failed', 'cancelled')",
                (time.time(), job_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def recover_telescope_jobs(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE telescope_jobs SET status = 'queued', updated_at = ? "
                "WHERE status IN ('resolving', 'waiting_for_slot', 'transferring', 'uploading', 'verifying')",
                (time.time(),),
            )
            await db.commit()
            return cursor.rowcount

    async def enqueue_telescope_callback(self, event: dict[str, Any]) -> None:
        now = time.time()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO telescope_callbacks (
                    event_id, job_id, event_type, payload_json, status,
                    attempts, next_attempt_at, created_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (
                    event["event_id"],
                    event["job_id"],
                    event["event_type"],
                    json.dumps(event["payload"], separators=(",", ":")),
                    now,
                    now,
                ),
            )
            await db.commit()

    async def due_telescope_callbacks(self, limit: int = 20) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM telescope_callbacks WHERE status = 'pending' AND next_attempt_at <= ? "
                "ORDER BY created_at LIMIT ?",
                (time.time(), max(1, limit)),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        decoded = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            decoded.append(item)
        return decoded

    async def mark_telescope_callback_delivered(self, event_id: str, job_id: str, event_type: str) -> None:
        now = time.time()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE telescope_callbacks SET status = 'delivered', delivered_at = ?, attempts = attempts + 1 WHERE event_id = ?",
                (now, event_id),
            )
            if event_type == "ready":
                await db.execute(
                    "UPDATE telescope_jobs SET status = 'ready', callback_status = 'delivered', completed_at = ?, updated_at = ? WHERE job_id = ?",
                    (now, now, job_id),
                )
            await db.commit()

    async def mark_telescope_callback_failed(self, event_id: str, job_id: str, error: str, attempts: int) -> None:
        delay = min(300, 5 * (2 ** min(6, max(0, attempts))))
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE telescope_callbacks SET attempts = attempts + 1, last_error = ?, next_attempt_at = ? WHERE event_id = ?",
                (error[:2000], time.time() + delay, event_id),
            )
            await db.execute(
                "UPDATE telescope_jobs SET callback_status = 'pending', callback_attempts = callback_attempts + 1, updated_at = ? WHERE job_id = ?",
                (time.time(), job_id),
            )
            await db.commit()

    @staticmethod
    def _encode_job_value(column: str, value: Any) -> Any:
        if column in {"reference_json", "multipart_parts_json", "result_json", "metadata_json"} and not isinstance(value, str):
            return json.dumps(value, separators=(",", ":"))
        if column in {"cancel_requested"}:
            return int(bool(value))
        return value

    @staticmethod
    def _decode_telescope_job(row: dict[str, Any]) -> dict[str, Any]:
        row["reference"] = json.loads(row.pop("reference_json") or "{}")
        row["multipart_parts"] = json.loads(row.pop("multipart_parts_json") or "[]")
        row["result"] = json.loads(row.pop("result_json") or "null")
        row["metadata"] = json.loads(row.pop("metadata_json") or "{}")
        row["cancel_requested"] = bool(row.get("cancel_requested"))
        return row
