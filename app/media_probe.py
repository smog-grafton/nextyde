from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


@dataclass(slots=True)
class VideoStreamInfo:
    codec: str | None
    width: int | None
    height: int | None
    pix_fmt: str | None
    frame_rate: float | None


@dataclass(slots=True)
class AudioStreamInfo:
    codec: str | None
    channels: int | None
    sample_rate: int | None


@dataclass(slots=True)
class MediaProbeResult:
    path: Path
    extension: str
    size_bytes: int
    duration_seconds: float | None
    container_format: str | None
    video: VideoStreamInfo | None
    audio: AudioStreamInfo | None


def _to_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_frame_rate(value: object) -> float | None:
    if value in (None, "", "0/0"):
        return None
    try:
        return float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return None


def probe_media(ffprobe_binary: str, input_path: Path) -> MediaProbeResult:
    if not input_path.is_file():
        raise RuntimeError(f"Cannot probe media; file not found: {input_path}")

    cmd = [
        ffprobe_binary,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(input_path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=45,
        )
    except OSError as exc:
        raise RuntimeError(f"ffprobe execution failed: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ffprobe timed out while probing media") from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise RuntimeError(f"ffprobe failed with exit {proc.returncode}: {stderr}")

    try:
        parsed = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe returned invalid JSON output") from exc

    streams = parsed.get("streams") if isinstance(parsed, dict) else None
    streams = streams if isinstance(streams, list) else []
    format_info = parsed.get("format") if isinstance(parsed, dict) else {}
    format_info = format_info if isinstance(format_info, dict) else {}

    video_stream: VideoStreamInfo | None = None
    audio_stream: AudioStreamInfo | None = None

    for stream in streams:
        if not isinstance(stream, dict):
            continue
        codec_type = str(stream.get("codec_type") or "")
        if codec_type == "video" and video_stream is None:
            video_stream = VideoStreamInfo(
                codec=str(stream.get("codec_name")) if stream.get("codec_name") else None,
                width=_to_int(stream.get("width")),
                height=_to_int(stream.get("height")),
                pix_fmt=str(stream.get("pix_fmt")) if stream.get("pix_fmt") else None,
                frame_rate=_to_frame_rate(stream.get("avg_frame_rate") or stream.get("r_frame_rate")),
            )
        elif codec_type == "audio" and audio_stream is None:
            audio_stream = AudioStreamInfo(
                codec=str(stream.get("codec_name")) if stream.get("codec_name") else None,
                channels=_to_int(stream.get("channels")),
                sample_rate=_to_int(stream.get("sample_rate")),
            )
        if video_stream is not None and audio_stream is not None:
            break

    size_bytes = input_path.stat().st_size
    return MediaProbeResult(
        path=input_path,
        extension=input_path.suffix.lower(),
        size_bytes=size_bytes,
        duration_seconds=_to_float(format_info.get("duration")),
        container_format=str(format_info.get("format_name")) if format_info.get("format_name") else None,
        video=video_stream,
        audio=audio_stream,
    )
