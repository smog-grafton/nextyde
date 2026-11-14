from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import os
from pathlib import PurePosixPath
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError


ProgressCallback = Callable[[int, int, str, list[dict[str, Any]]], Awaitable[None]]
VerifyCallback = Callable[[], Awaitable[None]]
LOGGER = logging.getLogger("telebot.object_storage")


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _integer(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)).strip() or default)


@dataclass(frozen=True, slots=True)
class ObjectStorageTarget:
    key: str
    label: str
    provider: str
    endpoint: str
    region: str
    bucket: str
    public_url: str
    path_prefix: str
    access_key_id: str
    secret_access_key: str
    use_path_style_endpoint: bool
    enabled: bool
    writable: bool
    priority: int
    capacity_bytes: int
    reserve_percent: float
    known_used_bytes: int

    @property
    def configured(self) -> bool:
        return bool(
            self.enabled
            and self.writable
            and self.endpoint
            and self.bucket
            and self.access_key_id
            and self.secret_access_key
        )

    def has_safe_capacity_for(self, expected_bytes: int) -> bool:
        if self.capacity_bytes <= 0:
            return True
        reserve = int(self.capacity_bytes * max(0.0, self.reserve_percent) / 100)
        return self.known_used_bytes + max(0, expected_bytes) <= self.capacity_bytes - reserve

    def object_url(self, object_key: str) -> str:
        encoded = "/".join(quote(part, safe="") for part in object_key.split("/"))
        if self.public_url:
            return f"{self.public_url.rstrip('/')}/{encoded}"
        return f"{self.endpoint.rstrip('/')}/{quote(self.bucket, safe='')}/{encoded}"


def _target(prefix: str, key: str, label: str, provider: str, *, legacy: bool = False) -> ObjectStorageTarget:
    legacy_access = os.getenv("CONTABO_OBJECT_STORAGE_ACCESS_KEY", "") if legacy else ""
    legacy_secret = os.getenv("CONTABO_OBJECT_STORAGE_SECRET_KEY", "") if legacy else ""
    legacy_endpoint = os.getenv("CONTABO_OBJECT_STORAGE_ENDPOINT", "") if legacy else ""
    legacy_bucket = os.getenv("CONTABO_OBJECT_STORAGE_BUCKET", "") if legacy else ""
    legacy_region = os.getenv("CONTABO_OBJECT_STORAGE_REGION", "") if legacy else ""
    legacy_public_url = os.getenv("CONTABO_OBJECT_STORAGE_PUBLIC_URL", "") if legacy else ""
    legacy_prefix = os.getenv("CONTABO_OBJECT_STORAGE_PATH_PREFIX", "") if legacy else ""

    return ObjectStorageTarget(
        key=key,
        label=label,
        provider=provider,
        endpoint=(os.getenv(f"{prefix}_ENDPOINT") or legacy_endpoint).strip(),
        region=(os.getenv(f"{prefix}_REGION") or legacy_region or "us-east-1").strip(),
        bucket=(os.getenv(f"{prefix}_BUCKET") or legacy_bucket).strip(),
        public_url=(os.getenv(f"{prefix}_PUBLIC_URL") or legacy_public_url).strip(),
        path_prefix=(os.getenv(f"{prefix}_PATH_PREFIX") or legacy_prefix or "videos").strip("/"),
        access_key_id=(os.getenv(f"{prefix}_ACCESS_KEY_ID") or legacy_access).strip(),
        secret_access_key=(os.getenv(f"{prefix}_SECRET_ACCESS_KEY") or legacy_secret).strip(),
        use_path_style_endpoint=_bool(f"{prefix}_USE_PATH_STYLE_ENDPOINT", provider == "contabo"),
        enabled=_bool(f"{prefix}_ENABLED", False),
        writable=_bool(f"{prefix}_WRITABLE", True),
        priority=_integer(f"{prefix}_PRIORITY", 0),
        capacity_bytes=_integer(f"{prefix}_CAPACITY_BYTES", 0),
        reserve_percent=float(os.getenv(f"{prefix}_RESERVE_PERCENT", "0") or 0),
        known_used_bytes=_integer(f"{prefix}_KNOWN_USED_BYTES", 0),
    )


def load_storage_targets() -> dict[str, ObjectStorageTarget]:
    return {
        "contabo_nbx": _target(
            "CONTABO_NBX",
            "contabo_nbx",
            "NaraBox Legacy Storage — nbx",
            "contabo",
            legacy=True,
        ),
        "contabo_nb_nbx": _target(
            "CONTABO_NB_NBX",
            "contabo_nb_nbx",
            "NaraBox Storage 2 — nb-nbx",
            "contabo",
        ),
        "r2_nbx": _target(
            "CLOUDFLARE_R2",
            "r2_nbx",
            "Cloudflare R2 — nbx",
            "cloudflare_r2",
        ),
    }


def _managed_target_from_payload(payload: Any) -> ObjectStorageTarget | None:
    if not isinstance(payload, dict):
        return None

    required = ("key", "endpoint", "region", "bucket", "access_key_id", "secret_access_key")
    if any(not str(payload.get(field) or "").strip() for field in required):
        return None

    return ObjectStorageTarget(
        key=str(payload["key"]).strip(),
        label=str(payload.get("label") or payload["key"]).strip(),
        provider=str(payload.get("provider") or "s3_compatible").strip(),
        endpoint=str(payload["endpoint"]).strip(),
        region=str(payload["region"]).strip(),
        bucket=str(payload["bucket"]).strip(),
        public_url=str(payload.get("public_url") or "").strip(),
        path_prefix=str(payload.get("path_prefix") or "videos").strip("/"),
        access_key_id=str(payload["access_key_id"]).strip(),
        secret_access_key=str(payload["secret_access_key"]).strip(),
        use_path_style_endpoint=bool(payload.get("use_path_style_endpoint", True)),
        enabled=bool(payload.get("enabled", False)),
        writable=bool(payload.get("writable", False)),
        priority=int(payload.get("priority") or 0),
        capacity_bytes=int(payload.get("capacity_bytes") or 0),
        reserve_percent=float(payload.get("reserve_percent") or 0),
        known_used_bytes=int(payload.get("known_used_bytes") or 0),
    )


def load_portal_managed_target(key: str) -> ObjectStorageTarget | None:
    """Fetch a Filament-managed target only over the worker-only portal API.

    Static Contabo/R2 targets still come from Teletyde's own environment. This
    path is exclusively for a target added later in Portal, and keeps its
    credentials in memory rather than copying them into Teletyde's `.env` or
    SQLite job state.
    """
    base_url = os.getenv("PORTAL_STORAGE_TARGETS_URL", "").strip().rstrip("/")
    token = os.getenv("PORTAL_STORAGE_TARGETS_TOKEN", "").strip()
    if not base_url or not token:
        return None

    request = Request(
        f"{base_url}/{quote(key, safe='')}",
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - URL is trusted worker env configuration.
            data = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        LOGGER.warning("Unable to load managed storage target %s from Portal: %s", key, exc)
        return None

    target = _managed_target_from_payload(data.get("target") if isinstance(data, dict) else None)
    if target is None or target.key != key:
        LOGGER.warning("Portal returned an invalid managed storage target for %s", key)
        return None

    return target


def load_portal_managed_targets() -> dict[str, ObjectStorageTarget]:
    """Load all enabled Portal-managed targets for automatic selection."""
    base_url = os.getenv("PORTAL_STORAGE_TARGETS_URL", "").strip().rstrip("/")
    token = os.getenv("PORTAL_STORAGE_TARGETS_TOKEN", "").strip()
    if not base_url or not token:
        return {}

    request = Request(
        base_url,
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - URL is trusted worker env configuration.
            data = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        LOGGER.warning("Unable to load managed storage targets from Portal: %s", exc)
        return {}

    raw_targets = data.get("targets") if isinstance(data, dict) else []
    if not isinstance(raw_targets, list):
        return {}

    targets: dict[str, ObjectStorageTarget] = {}
    for raw_target in raw_targets:
        target = _managed_target_from_payload(raw_target)
        if target is not None:
            targets[target.key] = target
    return targets


class ObjectStorageManager:
    def __init__(
        self,
        targets: dict[str, ObjectStorageTarget] | None = None,
        managed_target_loader: Callable[[str], ObjectStorageTarget | None] | None = None,
        managed_targets_loader: Callable[[], dict[str, ObjectStorageTarget]] | None = None,
    ) -> None:
        self.targets = targets if targets is not None else load_storage_targets()
        self.managed_target_loader = managed_target_loader or load_portal_managed_target
        self.managed_targets_loader = managed_targets_loader or load_portal_managed_targets
        self._managed_targets_loaded = False
        self._reservations: dict[str, tuple[str, int]] = {}
        self._committed: dict[str, int] = {}

    def reserve(self, job_id: str, requested: str | None, expected_bytes: int) -> ObjectStorageTarget:
        """Account for this process's concurrent uploads in configured capacity."""
        prior = self._reservations.pop(job_id, None)
        try:
            target = self.resolve(requested, expected_bytes)
            self._reservations[job_id] = (target.key, expected_bytes)
            return target
        except Exception:
            if prior:
                self._reservations[job_id] = prior
            raise

    def release(self, job_id: str, *, committed: bool = False) -> None:
        reserved = self._reservations.pop(job_id, None)
        if committed and reserved:
            key, size = reserved
            self._committed[key] = self._committed.get(key, 0) + size

    def _available(self, target: ObjectStorageTarget, expected_bytes: int) -> bool:
        in_flight = sum(size for key, size in self._reservations.values() if key == target.key)
        return target.has_safe_capacity_for(expected_bytes + in_flight + self._committed.get(target.key, 0))

    def resolve(self, requested: str | None, expected_bytes: int) -> ObjectStorageTarget:
        key = (requested or "auto").strip().lower()
        aliases = {"r2": "r2_nbx", "contabo": "contabo_nbx"}
        key = aliases.get(key, key)
        if key != "auto":
            target = self.targets.get(key)
            if target is None:
                target = self.managed_target_loader(key)
                if target is not None:
                    self.targets[key] = target
            if target is None:
                raise RuntimeError(f"Unknown Telescope storage target: {key}")
            self._assert_usable(target, expected_bytes)
            if not self._available(target, expected_bytes):
                raise RuntimeError(f"Telescope storage target [{key}] has insufficient safe capacity after active uploads.")
            return target

        self._load_managed_targets_once()

        candidates = [
            target
            for target in self.targets.values()
            if target.configured and self._available(target, expected_bytes)
        ]
        if not candidates:
            raise RuntimeError(
                "No configured Telescope storage target has enough safe capacity. "
                "Check enabled/writable flags, credentials, capacity, and reserve settings."
            )
        return sorted(
            candidates,
            key=lambda target: (target.priority, target.capacity_bytes - target.known_used_bytes),
            reverse=True,
        )[0]

    def _load_managed_targets_once(self) -> None:
        if self._managed_targets_loaded:
            return

        self._managed_targets_loaded = True
        for key, target in self.managed_targets_loader().items():
            if key not in self.targets:
                self.targets[key] = target

    @staticmethod
    def _assert_usable(target: ObjectStorageTarget, expected_bytes: int) -> None:
        if not target.configured:
            raise RuntimeError(f"Telescope storage target [{target.key}] is disabled, read-only, or missing credentials.")
        if not target.has_safe_capacity_for(expected_bytes):
            raise RuntimeError(f"Telescope storage target [{target.key}] does not have enough safe capacity.")

    @staticmethod
    def build_object_key(target: ObjectStorageTarget, job_id: str, file_name: str, date_path: str) -> str:
        return str(PurePosixPath(target.path_prefix) / "telescope" / date_path / job_id / file_name)


async def _s3_call(function, *args, **kwargs):
    """Do not release a job's ownership while a boto3 thread still mutates S3."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


class S3MultipartUploader:
    """Streams bounded Telegram chunks into an S3-compatible multipart upload."""

    def __init__(self, target: ObjectStorageTarget, part_size: int, max_attempts: int, retry_base_ms: int) -> None:
        self.target = target
        self.part_size = max(5 * 1024 * 1024, part_size)
        self.max_attempts = max(1, max_attempts)
        self.retry_base_ms = max(100, retry_base_ms)
        self.client = boto3.client(
            "s3",
            endpoint_url=target.endpoint,
            region_name=target.region,
            aws_access_key_id=target.access_key_id,
            aws_secret_access_key=target.secret_access_key,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path" if target.use_path_style_endpoint else "virtual"},
                connect_timeout=30,
                read_timeout=300,
                retries={"max_attempts": 1},
            ),
        )

    async def create_upload(self, object_key: str, mime_type: str) -> str:
        started = asyncio.get_running_loop().time()
        LOGGER.info(
            "Starting S3 multipart upload: target=%s bucket=%s key=%s",
            self.target.key,
            self.target.bucket,
            object_key,
        )
        response = await _s3_call(
            self.client.create_multipart_upload,
            Bucket=self.target.bucket,
            Key=object_key,
            ContentType=mime_type or "application/octet-stream",
        )
        LOGGER.info(
            "S3 multipart upload created: target=%s key=%s elapsed=%.2fs",
            self.target.key,
            object_key,
            asyncio.get_running_loop().time() - started,
        )
        return str(response["UploadId"])

    async def upload(
        self,
        chunks: AsyncIterator[bytes],
        *,
        object_key: str,
        mime_type: str,
        total_bytes: int,
        upload_id: str | None = None,
        completed_parts: list[dict[str, Any]] | None = None,
        bytes_transferred: int = 0,
        progress_callback: ProgressCallback | None = None,
        verify_callback: VerifyCallback | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        parts = list(completed_parts or [])
        current_upload_id = upload_id or await self.create_upload(object_key, mime_type)
        part_number = len(parts) + 1
        transferred = max(0, bytes_transferred)
        buffer = bytearray()

        # A previous process may have completed the multipart request but
        # exited before persisting its final result. The unique object key lets
        # us safely recognize that state without downloading or duplicating it.
        if upload_id and transferred == total_bytes:
            recovered = await self._verified_result(object_key, mime_type, total_bytes, missing_ok=True)
            if recovered is not None:
                return recovered

        if progress_callback and not upload_id:
            await progress_callback(transferred, total_bytes, current_upload_id, parts)

        async def flush() -> None:
            nonlocal part_number, transferred
            if not buffer:
                return
            body = bytes(buffer)
            response = await self._upload_part_with_retry(
                object_key,
                current_upload_id,
                part_number,
                body,
            )
            transferred += len(body)
            parts.append({"PartNumber": part_number, "ETag": str(response["ETag"])})
            part_number += 1
            buffer.clear()
            if progress_callback:
                await progress_callback(transferred, total_bytes, current_upload_id, parts)

        async for chunk in chunks:
            if cancelled and cancelled():
                await self.abort(object_key, current_upload_id)
                raise asyncio.CancelledError("Telescope import cancelled")
            buffer.extend(chunk)
            while len(buffer) >= self.part_size:
                part = bytes(buffer[: self.part_size])
                del buffer[: self.part_size]
                response = await self._upload_part_with_retry(
                    object_key,
                    current_upload_id,
                    part_number,
                    part,
                )
                transferred += len(part)
                parts.append({"PartNumber": part_number, "ETag": str(response["ETag"])})
                part_number += 1
                if progress_callback:
                    await progress_callback(transferred, total_bytes, current_upload_id, parts)

        await flush()
        if transferred != total_bytes:
            raise RuntimeError(
                f"Telegram stream ended after {transferred} bytes; expected {total_bytes}. Multipart upload remains resumable."
            )
        if cancelled and cancelled():
            await self.abort(object_key, current_upload_id)
            raise asyncio.CancelledError("Telescope import cancelled")
        if verify_callback:
            await verify_callback()
        try:
            await _s3_call(
                self.client.complete_multipart_upload,
                Bucket=self.target.bucket,
                Key=object_key,
                UploadId=current_upload_id,
                MultipartUpload={"Parts": parts},
            )
        except Exception:
            recovered = await self._verified_result(object_key, mime_type, total_bytes, missing_ok=True)
            if recovered is not None:
                return recovered
            raise
        result = await self._verified_result(object_key, mime_type, total_bytes)
        if result is None:  # pragma: no cover - missing_ok is false
            raise RuntimeError("Stored object verification failed.")
        return result

    async def _verified_result(
        self,
        object_key: str,
        mime_type: str,
        total_bytes: int,
        *,
        missing_ok: bool = False,
    ) -> dict[str, Any] | None:
        try:
            head = await _s3_call(self.client.head_object, Bucket=self.target.bucket, Key=object_key)
        except ClientError as exc:
            if missing_ok and str(exc.response.get("Error", {}).get("Code")) in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        stored_bytes = int(head.get("ContentLength") or 0)
        if stored_bytes != total_bytes:
            raise RuntimeError(f"Stored object verification failed: expected {total_bytes} bytes, found {stored_bytes}.")
        if head.get("ContentEncoding"):
            raise RuntimeError("Stored video has an unexpected Content-Encoding.")
        if mime_type == "video/mp4" and head.get("ContentType") != "video/mp4":
            raise RuntimeError("Stored MP4 has an incorrect Content-Type.")
        return {
            "storage_target": self.target.key,
            "provider": self.target.provider,
            "bucket": self.target.bucket,
            "object_key": object_key,
            "public_url": self.target.object_url(object_key),
            "bytes": stored_bytes,
            "mime_type": head.get("ContentType") or mime_type,
            "etag": str(head.get("ETag") or "").strip('"'),
        }

    async def _upload_part_with_retry(
        self,
        object_key: str,
        upload_id: str,
        part_number: int,
        body: bytes,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                started = asyncio.get_running_loop().time()
                LOGGER.info(
                    "Uploading S3 part: target=%s key=%s part=%s bytes=%s attempt=%s/%s",
                    self.target.key,
                    object_key,
                    part_number,
                    len(body),
                    attempt,
                    self.max_attempts,
                )
                response = await _s3_call(
                    self.client.upload_part,
                    Bucket=self.target.bucket,
                    Key=object_key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=body,
                )
                LOGGER.info(
                    "S3 part uploaded: target=%s key=%s part=%s elapsed=%.2fs",
                    self.target.key,
                    object_key,
                    part_number,
                    asyncio.get_running_loop().time() - started,
                )
                return response
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < self.max_attempts:
                    await asyncio.sleep((self.retry_base_ms / 1000) * (2 ** (attempt - 1)))
        raise RuntimeError(f"S3 multipart part {part_number} failed after {self.max_attempts} attempts") from last_error

    async def abort(self, object_key: str, upload_id: str) -> None:
        await _s3_call(
            self.client.abort_multipart_upload,
            Bucket=self.target.bucket,
            Key=object_key,
            UploadId=upload_id,
        )
