from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.media_probe import MediaProbeResult, probe_media
from app.media_tools import require_media_tools

LOGGER = logging.getLogger("telebot")


@dataclass(slots=True)
class VideoPrepDecision:
    should_transcode: bool
    reason: str
    target_height: int | None


@dataclass(slots=True)
class VideoPrepResult:
    delivery_path: Path
    source_path: Path
    optimized_path: Path | None
    source_probe: MediaProbeResult
    output_probe: MediaProbeResult | None
    decision: VideoPrepDecision
    source_size_bytes: int
    output_size_bytes: int

    @property
    def changed(self) -> bool:
        return self.optimized_path is not None


def _decide(settings: Settings, probe: MediaProbeResult) -> VideoPrepDecision:
    min_size_bytes = int(settings.video_prep_min_size_mb_for_transcode * 1024 * 1024)
    ext = probe.extension
    v = probe.video
    source_is_mp4 = ext == ".mp4"
    source_is_h264 = (v.codec or "").lower() in {"h264", "avc1"} if v else False
    too_large = probe.size_bytes >= min_size_bytes
    needs_downscale = False
    if v and v.height and v.height > settings.video_prep_max_height and too_large:
        needs_downscale = True

    if not source_is_mp4:
        return VideoPrepDecision(True, "container_not_mp4", settings.video_prep_max_height if needs_downscale else None)
    if not source_is_h264:
        return VideoPrepDecision(True, "video_codec_not_h264", settings.video_prep_max_height if needs_downscale else None)
    if (probe.audio and (probe.audio.codec or "").lower() not in {"aac", "mp4a"}) or (probe.audio is None):
        return VideoPrepDecision(True, "audio_not_aac_or_missing", settings.video_prep_max_height if needs_downscale else None)
    if needs_downscale:
        return VideoPrepDecision(True, "resolution_above_target", settings.video_prep_max_height)
    if too_large and v and v.height and v.height >= 1080:
        return VideoPrepDecision(True, "oversized_high_resolution", settings.video_prep_max_height)
    return VideoPrepDecision(False, "already_delivery_friendly", None)


def _build_output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}.delivery.mp4")


def _run_transcode(
    ffmpeg_binary: str,
    input_path: Path,
    output_path: Path,
    settings: Settings,
    decision: VideoPrepDecision,
) -> None:
    vf = "scale='if(gt(ih,{h}),-2,iw)':'if(gt(ih,{h}),{h},ih)':flags=lanczos".format(
        h=settings.video_prep_max_height
    )
    cmd = [
        ffmpeg_binary,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        settings.video_prep_preset,
        "-crf",
        str(settings.video_prep_crf),
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ac",
        "2",
    ]
    if decision.target_height:
        cmd += ["-vf", vf]
    cmd += ["-movflags", "+faststart", str(output_path)]
    LOGGER.info(
        "Video prep transcode starting: input=%s output=%s reason=%s crf=%s preset=%s target_height=%s",
        input_path,
        output_path,
        decision.reason,
        settings.video_prep_crf,
        settings.video_prep_preset,
        decision.target_height,
    )
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=settings.video_prep_timeout_seconds,
        )
    except OSError as exc:
        raise RuntimeError(f"ffmpeg execution failed: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ffmpeg timed out during transcode") from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise RuntimeError(f"ffmpeg failed with exit {proc.returncode}: {stderr}")
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise RuntimeError("ffmpeg finished but no valid output file was produced")


def prepare_video_for_delivery(settings: Settings, input_path: Path) -> VideoPrepResult:
    tools = require_media_tools()
    source_probe = probe_media(tools.ffprobe.path or "ffprobe", input_path)
    decision = _decide(settings, source_probe)
    LOGGER.info(
        "Video prep probe: path=%s ext=%s size=%s duration=%.2fs container=%s video_codec=%s audio_codec=%s width=%s height=%s decision=%s",
        source_probe.path,
        source_probe.extension,
        source_probe.size_bytes,
        source_probe.duration_seconds or 0.0,
        source_probe.container_format or "n/a",
        source_probe.video.codec if source_probe.video else "n/a",
        source_probe.audio.codec if source_probe.audio else "none",
        source_probe.video.width if source_probe.video else "n/a",
        source_probe.video.height if source_probe.video else "n/a",
        decision.reason,
    )
    if not decision.should_transcode:
        return VideoPrepResult(
            delivery_path=input_path,
            source_path=input_path,
            optimized_path=None,
            source_probe=source_probe,
            output_probe=None,
            decision=decision,
            source_size_bytes=source_probe.size_bytes,
            output_size_bytes=source_probe.size_bytes,
        )

    output_path = _build_output_path(input_path)
    _run_transcode(tools.ffmpeg.path or "ffmpeg", input_path, output_path, settings, decision)
    output_probe = probe_media(tools.ffprobe.path or "ffprobe", output_path)
    source_size = source_probe.size_bytes
    output_size = output_probe.size_bytes
    pct = 0.0 if source_size <= 0 else (100.0 * (source_size - output_size) / source_size)
    LOGGER.info(
        "Video prep output ready: output=%s source_size=%s output_size=%s size_reduction_pct=%.2f",
        output_path,
        source_size,
        output_size,
        pct,
    )
    return VideoPrepResult(
        delivery_path=output_path,
        source_path=input_path,
        optimized_path=output_path,
        source_probe=source_probe,
        output_probe=output_probe,
        decision=decision,
        source_size_bytes=source_size,
        output_size_bytes=output_size,
    )
