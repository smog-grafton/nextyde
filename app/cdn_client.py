from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx
import orjson

from app.config import Settings

LOGGER = logging.getLogger("telebot")

CDN_UPLOAD_MAX_ATTEMPTS = 3
CDN_UPLOAD_BACKOFF_SECONDS = (5, 15, 30)


class CdnClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(settings.cdn_timeout_seconds))

    async def close(self) -> None:
        await self._client.aclose()

    async def upload_file(self, file_path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "narabox-telebot/1.0",
        }
        if self.settings.cdn_api_token:
            headers["Authorization"] = f"Bearer {self.settings.cdn_api_token}"

        data = {
            "source": self.settings.cdn_source,
            "metadata": orjson.dumps(metadata).decode("utf-8"),
            "title": str(metadata.get("title_guess") or ""),
            "original_filename": str(metadata.get("original_filename") or file_path.name),
            "episode": "" if metadata.get("episode_guess") is None else str(metadata.get("episode_guess")),
            "vj": str(metadata.get("vj_guess") or self.settings.default_vj or ""),
            "category": str(self.settings.default_category or ""),
            "language": str(self.settings.default_language or ""),
            "telegram_chat_id": str(metadata.get("telegram_chat_id") or ""),
            "telegram_message_id": str(metadata.get("telegram_message_id") or ""),
            "telegram_channel": str(metadata.get("telegram_channel") or ""),
        }

        last_error: Exception | None = None
        for attempt in range(1, CDN_UPLOAD_MAX_ATTEMPTS + 1):
            try:
                with file_path.open("rb") as handle:
                    files = {
                        "file": (file_path.name, handle, "application/octet-stream"),
                    }
                    response = await self._client.post(
                        self.settings.cdn_upload_url,
                        headers=headers,
                        data=data,
                        files=files,
                    )
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if "application/json" in content_type:
                    return response.json()
                return {"status": "ok", "raw": response.text}
            except (httpx.HTTPStatusError, httpx.ConnectError, httpx.ReadTimeout, httpx.ReadError, httpx.WriteError) as e:
                last_error = e
                if attempt < CDN_UPLOAD_MAX_ATTEMPTS:
                    delay = CDN_UPLOAD_BACKOFF_SECONDS[min(attempt - 1, len(CDN_UPLOAD_BACKOFF_SECONDS) - 1)]
                    LOGGER.warning(
                        "CDN upload attempt %s failed (%s), retrying in %ss",
                        attempt,
                        type(e).__name__,
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    LOGGER.exception("CDN upload failed after %s attempts", CDN_UPLOAD_MAX_ATTEMPTS)
        raise last_error  # type: ignore[misc]

    async def notify(self, payload: dict[str, Any]) -> None:
        if not self.settings.cdn_notify_url:
            return

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "narabox-telebot/1.0",
        }
        if self.settings.cdn_notify_token:
            headers["Authorization"] = f"Bearer {self.settings.cdn_notify_token}"

        try:
            response = await self._client.post(
                self.settings.cdn_notify_url,
                headers=headers,
                content=orjson.dumps(payload),
            )
            response.raise_for_status()
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ReadError) as e:
            LOGGER.warning("Notify request failed (non-fatal): %s", e)
