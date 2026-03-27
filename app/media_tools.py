from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass

LOGGER = logging.getLogger("telebot")


@dataclass(slots=True)
class BinaryStatus:
    name: str
    path: str | None
    available: bool
    version: str | None
    error: str | None


@dataclass(slots=True)
class MediaToolsStatus:
    ffmpeg: BinaryStatus
    ffprobe: BinaryStatus

    @property
    def ready(self) -> bool:
        return self.ffmpeg.available and self.ffprobe.available


def _version_line(raw_output: str) -> str:
    line = raw_output.splitlines()[0].strip() if raw_output else ""
    return line or "unknown"


def _resolve_binary(name: str, env_var: str) -> str | None:
    env_override = (os.getenv(env_var) or "").strip()
    if env_override:
        return env_override
    return shutil.which(name)


def detect_binary(name: str, env_var: str) -> BinaryStatus:
    path = _resolve_binary(name, env_var)
    if not path:
        return BinaryStatus(
            name=name,
            path=None,
            available=False,
            version=None,
            error=f"{name} not found. Set {env_var} or install {name}.",
        )
    try:
        proc = subprocess.run(
            [path, "-version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
        )
    except OSError as exc:
        return BinaryStatus(
            name=name,
            path=path,
            available=False,
            version=None,
            error=f"Failed to execute {path}: {exc}",
        )
    except subprocess.TimeoutExpired:
        return BinaryStatus(
            name=name,
            path=path,
            available=False,
            version=None,
            error=f"Timed out while checking {path} -version",
        )

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        return BinaryStatus(
            name=name,
            path=path,
            available=False,
            version=None,
            error=f"{path} -version failed with exit {proc.returncode}: {stderr}",
        )
    output = proc.stdout or proc.stderr or ""
    return BinaryStatus(
        name=name,
        path=path,
        available=True,
        version=_version_line(output),
        error=None,
    )


def detect_media_tools() -> MediaToolsStatus:
    ffmpeg = detect_binary("ffmpeg", "FFMPEG_BINARY")
    ffprobe = detect_binary("ffprobe", "FFPROBE_BINARY")
    return MediaToolsStatus(ffmpeg=ffmpeg, ffprobe=ffprobe)


def require_media_tools() -> MediaToolsStatus:
    status = detect_media_tools()
    LOGGER.info(
        "Media tools detection: ffmpeg=%s (%s), ffprobe=%s (%s)",
        "ok" if status.ffmpeg.available else "missing",
        status.ffmpeg.path or "n/a",
        "ok" if status.ffprobe.available else "missing",
        status.ffprobe.path or "n/a",
    )
    if not status.ffmpeg.available:
        raise RuntimeError(status.ffmpeg.error or "ffmpeg unavailable")
    if not status.ffprobe.available:
        raise RuntimeError(status.ffprobe.error or "ffprobe unavailable")
    return status
