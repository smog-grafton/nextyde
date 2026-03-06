from __future__ import annotations

import aiosqlite


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
"""


class StateStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(SCHEMA)
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
