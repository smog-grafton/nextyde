from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.config import Settings
from app.media_probe import MediaProbeResult, probe_media
from app.media_tools import require_media_tools

LOGGER = logging.getLogger("telebot")
PrepProgressCallback = Callable[[str, dict[str, float | int | str]], None]


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
    source_duration_seconds: float | None,
    progress_callback: PrepProgressCallback | None = None,
) -> None:
    vf = "scale='if(gt(ih,{h}),-2,iw)':'if(gt(ih,{h}),{h},ih)':flags=lanczos".format(
        h=settings.video_prep_max_height
    )
    cmd = [
        ffmpeg_binary,
        "-y",
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "warning",
        "-progress",
        "pipe:1",
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
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as exc:
        raise RuntimeError(f"ffmpeg execution failed: {exc}") from exc
    remaining_output = ""
    try:
        if progress_callback:
            progress_callback("transcoding_started", {"progress_pct": 31})
        while True:
            if proc.stdout is None:
                break
            line = proc.stdout.readline()
            if not line:
                break
            row = line.strip()
            if not row or "=" not in row:
                continue
            key, value = row.split("=", 1)
            if key != "out_time_ms":
                continue
            if not source_duration_seconds or source_duration_seconds <= 0:
                continue
            try:
                out_time_ms = int(value)
            except ValueError:
                continue
            out_seconds = out_time_ms / 1_000_000.0
            raw_pct = int((out_seconds / source_duration_seconds) * 100.0)
            pct = max(31, min(96, raw_pct))
            if progress_callback:
                progress_callback("transcoding_progress", {"progress_pct": pct})
        remaining_output, _ = proc.communicate(timeout=settings.video_prep_timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.communicate()
        raise RuntimeError("ffmpeg timed out during transcode") from exc

    if proc.returncode != 0:
        stderr_text = (remaining_output or "").strip()
        raise RuntimeError(f"ffmpeg failed with exit {proc.returncode}: {stderr_text}")
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise RuntimeError("ffmpeg finished but no valid output file was produced")


def prepare_video_for_delivery(
    settings: Settings,
    input_path: Path,
    progress_callback: PrepProgressCallback | None = None,
) -> VideoPrepResult:
    if progress_callback:
        progress_callback("probing_started", {"progress_pct": 20})
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
        if progress_callback:
            progress_callback("prep_skipped", {"progress_pct": 100, "reason": decision.reason})
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
    _run_transcode(
        tools.ffmpeg.path or "ffmpeg",
        input_path,
        output_path,
        settings,
        decision,
        source_probe.duration_seconds,
        progress_callback=progress_callback,
    )
    if progress_callback:
        progress_callback("probe_output", {"progress_pct": 97})
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
    if progress_callback:
        progress_callback("prep_done", {"progress_pct": 100})
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
