from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

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

    async def upload_file(self, file_path: Path, metadata: dict[str, Any], source_url: str | None = None) -> dict[str, Any]:
        if self.settings.cdn_handoff_mode == "path_copy":
            return await self._handoff_by_path(file_path, metadata)
        if self.settings.cdn_handoff_mode == "source_url":
            return await self._handoff_by_source_url(file_path, metadata, source_url)

        headers = self._headers()
        data = self._intake_payload(metadata, original_filename=file_path.name)

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

    async def _handoff_by_path(self, file_path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
        if self.settings.worker_intake_root is None:
            raise RuntimeError("CDN_SHARED_INTAKE_ROOT is required when CDN_HANDOFF_MODE=path_copy.")

        headers = self._headers()

        date_path = datetime.utcnow().strftime("%Y/%m/%d")
        target_dir = self.settings.worker_intake_root / date_path
        target_dir.mkdir(parents=True, exist_ok=True)
        staged_name = f"{uuid4().hex[:12]}-{file_path.name}"
        staged_path = target_dir / staged_name

        LOGGER.info("Staging %s into Laravel worker intake at %s", file_path.name, staged_path)
        await asyncio.to_thread(shutil.copy2, file_path, staged_path)

        data = self._intake_payload(
            metadata,
            original_filename=file_path.name,
            source_type="telegram",
            source_disk=self.settings.worker_intake_disk,
            source_path=f"{date_path}/{staged_name}",
        )

        response = await self._client.post(
            self.settings.cdn_upload_url,
            headers=headers,
            data=data,
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return {"status": "ok", "raw": response.text}

    async def _handoff_by_source_url(self, file_path: Path, metadata: dict[str, Any], source_url: str | None) -> dict[str, Any]:
        if not source_url:
            raise RuntimeError("A signed temp source URL is required when CDN_HANDOFF_MODE=source_url.")

        headers = self._headers()
        data = self._intake_payload(
            metadata,
            original_filename=file_path.name,
            source_type="telegram",
            source_url=source_url,
        )

        response = await self._client.post(
            self.settings.cdn_upload_url,
            headers=headers,
            data=data,
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return {"status": "ok", "raw": response.text}

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "narabox-telebot/1.0",
        }
        if self.settings.cdn_api_token:
            headers["Authorization"] = f"Bearer {self.settings.cdn_api_token}"

        return headers

    def _intake_payload(
        self,
        metadata: dict[str, Any],
        *,
        original_filename: str,
        source_type: str | None = None,
        source_disk: str | None = None,
        source_path: str | None = None,
        source_url: str | None = None,
    ) -> dict[str, str]:
        data = {
            "source": self.settings.cdn_source,
            "metadata": orjson.dumps(metadata).decode("utf-8"),
            "title": str(metadata.get("title_guess") or ""),
            "original_filename": str(metadata.get("original_filename") or original_filename),
            "episode": "" if metadata.get("episode_guess") is None else str(metadata.get("episode_guess")),
            "vj": str(metadata.get("vj_guess") or self.settings.default_vj or ""),
            "category": str(self.settings.default_category or ""),
            "language": str(self.settings.default_language or ""),
            "telegram_chat_id": str(metadata.get("telegram_chat_id") or ""),
            "telegram_message_id": str(metadata.get("telegram_message_id") or ""),
            "telegram_channel": str(metadata.get("telegram_channel") or ""),
        }

        if source_type:
            data["source_type"] = source_type
        if source_disk:
            data["source_disk"] = source_disk
        if source_path:
            data["source_path"] = source_path
        if source_url:
            data["source_url"] = source_url

        return data

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
