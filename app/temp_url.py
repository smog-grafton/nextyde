from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import quote

from app.config import Settings


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def _payload_bytes(relative_path: str, expires_at: int) -> bytes:
    payload = {
        "path": relative_path,
        "exp": int(expires_at),
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def create_signed_temp_token(secret: str, relative_path: str, expires_at: int) -> str:
    payload = _payload_bytes(relative_path, expires_at)
    signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"{_b64url_encode(payload)}.{signature}"


def decode_signed_temp_token(token: str, secret: str) -> dict[str, str | int]:
    try:
        encoded_payload, provided_signature = token.split(".", 1)
    except ValueError as exc:
        raise ValueError("Malformed temp token.") from exc

    payload = _b64url_decode(encoded_payload)
    expected_signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(provided_signature, expected_signature):
        raise ValueError("Invalid temp token signature.")

    decoded = json.loads(payload.decode("utf-8"))
    relative_path = str(decoded.get("path") or "").strip().lstrip("/")
    expires_at = int(decoded.get("exp") or 0)

    if not relative_path or expires_at <= 0:
        raise ValueError("Temp token payload is invalid.")

    if expires_at < int(time.time()):
        raise ValueError("Temp token has expired.")

    return {
        "path": relative_path,
        "exp": expires_at,
    }


def build_signed_temp_url(settings: Settings, file_path: Path, *, expires_at: int | None = None) -> str:
    base_url = (settings.temp_public_url or "").strip()
    if not base_url:
        raise RuntimeError("TEMP_PUBLIC_URL is required when CDN_HANDOFF_MODE=source_url.")

    secret = (settings.temp_url_secret or "").strip()
    if not secret:
        raise RuntimeError("TEMP_URL_SECRET or CDN_API_TOKEN is required when CDN_HANDOFF_MODE=source_url.")

    resolved_path = file_path.resolve()
    temp_root = settings.temp_dir.resolve()

    try:
        relative_path = resolved_path.relative_to(temp_root).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"Temp file {resolved_path} is not inside TEMP_DIR {temp_root}.") from exc

    expiry = expires_at if expires_at is not None else int(time.time()) + (settings.temp_file_ttl_hours * 3600)
    token = create_signed_temp_token(secret, relative_path, expiry)

    return f"{base_url.rstrip('/')}/api/fetch/{quote(token, safe='')}/{quote(resolved_path.name, safe='')}"


def resolve_signed_temp_path(settings: Settings, token: str, filename: str) -> Path:
    secret = (settings.temp_url_secret or "").strip()
    if not secret:
        raise RuntimeError("TEMP_URL_SECRET or CDN_API_TOKEN is required to serve signed temp URLs.")

    payload = decode_signed_temp_token(token, secret)
    temp_root = settings.temp_dir.resolve()
    candidate = (temp_root / str(payload["path"])).resolve()

    try:
        candidate.relative_to(temp_root)
    except ValueError as exc:
        raise ValueError("Resolved temp path is outside TEMP_DIR.") from exc

    if candidate.name != filename:
        raise ValueError("Filename does not match token payload.")

    return candidate
