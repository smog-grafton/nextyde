from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache

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
    handbrake: BinaryStatus | None = None

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
            [path, "--version" if name == "HandBrakeCLI" else "-version"],
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
    handbrake = detect_binary("HandBrakeCLI", "HANDBRAKE_BINARY")
    return MediaToolsStatus(ffmpeg=ffmpeg, ffprobe=ffprobe, handbrake=handbrake)


def require_media_tools(*, require_handbrake: bool = False) -> MediaToolsStatus:
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
    if require_handbrake and (not status.handbrake or not status.handbrake.available):
        raise RuntimeError(status.handbrake.error if status.handbrake else "HandBrakeCLI unavailable")
    return status


@lru_cache(maxsize=8)
def inspect_handbrake(binary: str) -> dict:
    """Discover local presets/encoders; do not assume platform-specific AAC names."""
    def output(flag: str) -> str:
        try:
            proc = subprocess.run([binary, flag], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"HandBrakeCLI capability detection failed: {exc}") from exc
        if proc.returncode:
            raise RuntimeError(f"HandBrakeCLI {flag} failed: {proc.stderr[-2000:]}")
        return proc.stdout + proc.stderr

    presets, help_text = output("--preset-list"), output("--help")
    aac_encoder = next((codec for codec in ("av_aac", "ca_aac", "fdk_aac") if codec in help_text), None)
    if aac_encoder is None:
        raise RuntimeError("HandBrakeCLI does not provide a supported AAC encoder")
    return {"aac_encoder": aac_encoder, "presets": [name for name in (
        "Very Fast 720p30", "Fast 720p30", "Very Fast 1080p30", "Fast 1080p30",
    ) if name in presets]}


def inspect_hardware_encoders(ffmpeg_binary: str) -> list[str]:
    """Report compiled capabilities only; their presence does not prove a GPU exists."""
    proc = subprocess.run([ffmpeg_binary, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=15)
    return [name for name in ("h264_nvenc", "h264_qsv", "h264_vaapi", "h264_videotoolbox") if name in proc.stdout]
