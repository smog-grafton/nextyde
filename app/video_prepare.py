from __future__ import annotations

import logging
import selectors
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.config import Settings
from app.media_probe import MediaProbeResult, probe_media
from app.media_tools import require_media_tools

LOGGER = logging.getLogger("telebot")
PrepProgressCallback = Callable[[str, dict[str, float | int | str | None]], None]
CancelCheck = Callable[[], bool]
DEFAULT_CAP_HEIGHT_LADDER = (480, 360)
CAP_OVERHEAD_RATIO = 0.97
CAP_AUDIO_BITRATE_KBPS = 96
MIN_VIDEO_BITRATE_KBPS = 150
TRANSCODE_MAX_ATTEMPTS = 3


class VideoPrepCancelledError(RuntimeError):
    """Raised when the active ffmpeg process is cancelled."""


@dataclass(slots=True)
class VideoPrepAttempt:
    target_height: int | None
    video_bitrate_kbps: int | None
    audio_bitrate_kbps: int
    preset: str
    mode: str


@dataclass(slots=True)
class VideoPrepDecision:
    should_transcode: bool
    reason: str
    mode: str
    target_height: int | None
    enforce_size_cap: bool
    target_max_bytes: int | None


@dataclass(slots=True)
class VideoPrepAnalysis:
    source_probe: MediaProbeResult
    decision: VideoPrepDecision
    attempts: tuple[VideoPrepAttempt, ...]


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
    attempts_used: int

    @property
    def changed(self) -> bool:
        return self.optimized_path is not None


def _build_output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}.delivery.mp4")


def _build_height_candidates(
    source_height: int | None,
    max_height: int,
    preferred_heights: tuple[int, ...] | None = None,
) -> tuple[int | None, ...]:
    capped_max_height = max(240, max_height)
    ladder = tuple(height for height in (preferred_heights or DEFAULT_CAP_HEIGHT_LADDER) if height > 0) or DEFAULT_CAP_HEIGHT_LADDER
    candidates = [height for height in ladder if height <= capped_max_height]
    if source_height:
        candidates = [height for height in candidates if height <= source_height]
    if candidates:
        return tuple(candidates)
    if source_height:
        return (source_height,)
    return (capped_max_height,)


def _is_audio_delivery_friendly(probe: MediaProbeResult) -> bool:
    if probe.audio is None:
        return False
    return (probe.audio.codec or "").lower() in {"aac", "mp4a"}


def _is_video_delivery_friendly(probe: MediaProbeResult) -> bool:
    return probe.extension == ".mp4" and (probe.video is not None) and (probe.video.codec or "").lower() in {"h264", "avc1"}


def _build_cap_attempts(settings: Settings, probe: MediaProbeResult, target_max_bytes: int) -> tuple[VideoPrepAttempt, ...]:
    duration_seconds = probe.duration_seconds or 0.0
    if duration_seconds <= 0:
        raise RuntimeError("Cannot enforce the size cap because ffprobe could not determine the video duration.")

    total_target_bitrate_bps = int((target_max_bytes * CAP_OVERHEAD_RATIO * 8) / duration_seconds)
    base_video_bitrate_kbps = max(MIN_VIDEO_BITRATE_KBPS, (total_target_bitrate_bps // 1000) - CAP_AUDIO_BITRATE_KBPS)
    height_candidates = _build_height_candidates(
        probe.video.height if probe.video else None,
        settings.video_prep_max_height,
        settings.video_prep_cap_height_ladder or DEFAULT_CAP_HEIGHT_LADDER,
    )

    attempts: list[VideoPrepAttempt] = []
    for attempt_index in range(TRANSCODE_MAX_ATTEMPTS):
        candidate_height = height_candidates[min(attempt_index, len(height_candidates) - 1)]
        bitrate_multiplier = 0.9 ** attempt_index
        attempts.append(
            VideoPrepAttempt(
                target_height=candidate_height,
                video_bitrate_kbps=max(MIN_VIDEO_BITRATE_KBPS, int(base_video_bitrate_kbps * bitrate_multiplier)),
                audio_bitrate_kbps=CAP_AUDIO_BITRATE_KBPS,
                preset=settings.video_prep_preset,
                mode="cap",
            )
        )
    return tuple(attempts)


def _decide(settings: Settings, probe: MediaProbeResult) -> VideoPrepDecision:
    min_size_bytes = int(settings.video_prep_min_size_mb_for_transcode * 1024 * 1024)
    target_max_bytes = int(settings.video_prep_target_max_mb * 1024 * 1024)
    video_stream = probe.video
    source_is_mp4 = probe.extension == ".mp4"
    source_is_h264 = (video_stream.codec or "").lower() in {"h264", "avc1"} if video_stream else False
    audio_is_aac = _is_audio_delivery_friendly(probe)
    delivery_friendly = source_is_mp4 and source_is_h264 and audio_is_aac
    needs_downscale = bool(video_stream and video_stream.height and video_stream.height > settings.video_prep_max_height and probe.size_bytes >= min_size_bytes)
    needs_cap = probe.size_bytes > target_max_bytes

    if needs_cap:
        return VideoPrepDecision(
            should_transcode=True,
            reason="size_above_cap" if delivery_friendly else "size_above_cap_and_needs_delivery_prep",
            mode="cap",
            target_height=settings.video_prep_max_height if needs_downscale else None,
            enforce_size_cap=True,
            target_max_bytes=target_max_bytes,
        )
    if not source_is_mp4:
        return VideoPrepDecision(True, "container_not_mp4", "crf", settings.video_prep_max_height if needs_downscale else None, False, None)
    if not source_is_h264:
        return VideoPrepDecision(True, "video_codec_not_h264", "crf", settings.video_prep_max_height if needs_downscale else None, False, None)
    if not audio_is_aac:
        return VideoPrepDecision(True, "audio_not_aac_or_missing", "crf", settings.video_prep_max_height if needs_downscale else None, False, None)
    if needs_downscale:
        return VideoPrepDecision(True, "resolution_above_target", "crf", settings.video_prep_max_height, False, None)
    if probe.size_bytes >= min_size_bytes and video_stream and video_stream.height and video_stream.height >= 1080:
        return VideoPrepDecision(True, "oversized_high_resolution", "crf", settings.video_prep_max_height, False, None)
    return VideoPrepDecision(False, "already_delivery_friendly", "copy", None, False, target_max_bytes)


def analyze_video_for_delivery(
    settings: Settings,
    input_path: Path,
    progress_callback: PrepProgressCallback | None = None,
) -> VideoPrepAnalysis:
    if progress_callback:
        progress_callback("probing_started", {"progress_pct": 20})
    tools = require_media_tools()
    source_probe = probe_media(tools.ffprobe.path or "ffprobe", input_path)
    decision = _decide(settings, source_probe)

    attempts: tuple[VideoPrepAttempt, ...] = ()
    if decision.should_transcode and decision.mode == "cap":
        attempts = _build_cap_attempts(settings, source_probe, decision.target_max_bytes or int(settings.video_prep_target_max_mb * 1024 * 1024))
    elif decision.should_transcode:
        attempts = (
            VideoPrepAttempt(
                target_height=decision.target_height,
                video_bitrate_kbps=None,
                audio_bitrate_kbps=128,
                preset=settings.video_prep_preset,
                mode="crf",
            ),
        )

    LOGGER.info(
        "Video prep probe: path=%s ext=%s size=%s duration=%.2fs container=%s video_codec=%s audio_codec=%s width=%s height=%s frame_rate=%s decision=%s mode=%s",
        source_probe.path,
        source_probe.extension,
        source_probe.size_bytes,
        source_probe.duration_seconds or 0.0,
        source_probe.container_format or "n/a",
        source_probe.video.codec if source_probe.video else "n/a",
        source_probe.audio.codec if source_probe.audio else "none",
        source_probe.video.width if source_probe.video else "n/a",
        source_probe.video.height if source_probe.video else "n/a",
        source_probe.video.frame_rate if source_probe.video else "n/a",
        decision.reason,
        decision.mode,
    )

    if progress_callback and not decision.should_transcode:
        progress_callback("prep_skipped", {"progress_pct": 100, "reason": decision.reason})

    return VideoPrepAnalysis(
        source_probe=source_probe,
        decision=decision,
        attempts=attempts,
    )


def _scale_filter(target_height: int | None) -> str | None:
    if not target_height:
        return None
    return "scale='if(gt(ih,{h}),-2,iw)':'if(gt(ih,{h}),{h},ih)':flags=lanczos".format(h=target_height)


def _build_transcode_command(
    ffmpeg_binary: str,
    input_path: Path,
    output_path: Path,
    settings: Settings,
    attempt: VideoPrepAttempt,
) -> list[str]:
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
        attempt.preset,
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        f"{attempt.audio_bitrate_kbps}k",
        "-ac",
        "2",
    ]
    if attempt.video_bitrate_kbps is None:
        cmd += ["-crf", str(settings.video_prep_crf)]
    else:
        cmd += [
            "-b:v",
            f"{attempt.video_bitrate_kbps}k",
            "-maxrate",
            f"{attempt.video_bitrate_kbps}k",
            "-bufsize",
            f"{attempt.video_bitrate_kbps * 2}k",
        ]
    scale_filter = _scale_filter(attempt.target_height)
    if scale_filter:
        cmd += ["-vf", scale_filter]
    cmd += ["-movflags", "+faststart", str(output_path)]
    return cmd


def _terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _run_transcode(
    ffmpeg_binary: str,
    input_path: Path,
    output_path: Path,
    settings: Settings,
    decision: VideoPrepDecision,
    attempt: VideoPrepAttempt,
    source_duration_seconds: float | None,
    progress_callback: PrepProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
    attempt_index: int = 1,
    attempt_count: int = 1,
) -> None:
    cmd = _build_transcode_command(ffmpeg_binary, input_path, output_path, settings, attempt)
    LOGGER.info(
        "Video prep transcode starting: input=%s output=%s reason=%s mode=%s attempt=%s/%s crf=%s video_bitrate=%s audio_bitrate=%s preset=%s target_height=%s",
        input_path,
        output_path,
        decision.reason,
        attempt.mode,
        attempt_index,
        attempt_count,
        settings.video_prep_crf if attempt.video_bitrate_kbps is None else "n/a",
        attempt.video_bitrate_kbps if attempt.video_bitrate_kbps is not None else "crf",
        attempt.audio_bitrate_kbps,
        attempt.preset,
        attempt.target_height,
    )
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise RuntimeError(f"ffmpeg execution failed: {exc}") from exc

    selector: selectors.BaseSelector | None = None
    if proc.stdout is not None:
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)

    deadline = time.monotonic() + settings.video_prep_timeout_seconds
    remaining_output = ""
    try:
        if progress_callback:
            progress_callback(
                "transcoding_started",
                {
                    "progress_pct": 31,
                    "attempt_index": attempt_index,
                    "attempt_count": attempt_count,
                },
            )

        while True:
            if should_cancel and should_cancel():
                _terminate_process(proc)
                raise VideoPrepCancelledError("Video preparation cancelled")

            if time.monotonic() > deadline:
                _terminate_process(proc)
                raise RuntimeError("ffmpeg timed out during transcode")

            if proc.poll() is not None and selector is None:
                break

            events = selector.select(timeout=0.5) if selector is not None else []
            if not events:
                if proc.poll() is not None:
                    break
                continue

            for key, _ in events:
                line = key.fileobj.readline()
                if not line:
                    continue
                remaining_output += line
                row = line.strip()
                if not row or "=" not in row:
                    continue
                progress_key, value = row.split("=", 1)
                if progress_key != "out_time_ms" or not source_duration_seconds or source_duration_seconds <= 0:
                    continue
                try:
                    out_time_ms = int(value)
                except ValueError:
                    continue
                out_seconds = out_time_ms / 1_000_000.0
                raw_pct = int((out_seconds / source_duration_seconds) * 100.0)
                pct = max(31, min(96, raw_pct))
                if progress_callback:
                    progress_callback(
                        "transcoding_progress",
                        {
                            "progress_pct": pct,
                            "attempt_index": attempt_index,
                            "attempt_count": attempt_count,
                        },
                    )

        if selector is not None:
            selector.close()
            selector = None

        tail = (proc.stdout.read() if proc.stdout is not None else "") or ""
        remaining_output += tail
        proc.wait(timeout=max(1, int(deadline - time.monotonic())))
    except subprocess.TimeoutExpired as exc:
        _terminate_process(proc)
        raise RuntimeError("ffmpeg timed out during transcode") from exc
    finally:
        if selector is not None:
            selector.close()

    if proc.returncode != 0:
        stderr_text = (remaining_output or "").strip()
        raise RuntimeError(f"ffmpeg failed with exit {proc.returncode}: {stderr_text}")
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise RuntimeError("ffmpeg finished but no valid output file was produced")


def _probe_output(
    ffprobe_binary: str,
    output_path: Path,
    progress_callback: PrepProgressCallback | None = None,
) -> MediaProbeResult:
    if progress_callback:
        progress_callback("probe_output", {"progress_pct": 97})
    return probe_media(ffprobe_binary, output_path)


def _prepare_with_size_cap(
    settings: Settings,
    input_path: Path,
    output_path: Path,
    analysis: VideoPrepAnalysis,
    progress_callback: PrepProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
) -> tuple[MediaProbeResult, int]:
    tools = require_media_tools()
    target_max_bytes = analysis.decision.target_max_bytes or int(settings.video_prep_target_max_mb * 1024 * 1024)
    last_probe: MediaProbeResult | None = None

    for attempt_offset, attempt in enumerate(analysis.attempts, start=1):
        output_path.unlink(missing_ok=True)
        _run_transcode(
            tools.ffmpeg.path or "ffmpeg",
            input_path,
            output_path,
            settings,
            analysis.decision,
            attempt,
            analysis.source_probe.duration_seconds,
            progress_callback=progress_callback,
            should_cancel=should_cancel,
            attempt_index=attempt_offset,
            attempt_count=len(analysis.attempts),
        )
        last_probe = _probe_output(tools.ffprobe.path or "ffprobe", output_path, progress_callback=progress_callback)
        if last_probe.size_bytes <= target_max_bytes:
            return last_probe, attempt_offset

        LOGGER.warning(
            "Video prep attempt %s/%s exceeded cap: output_size=%s target_max=%s",
            attempt_offset,
            len(analysis.attempts),
            last_probe.size_bytes,
            target_max_bytes,
        )

    if last_probe is None:
        raise RuntimeError("Video preparation did not produce an output file.")

    raise RuntimeError(
        f"Compressed output is still {last_probe.size_bytes} bytes after {len(analysis.attempts)} attempts; "
        f"the {settings.video_prep_target_max_mb} MB cap could not be met."
    )


def prepare_video_for_delivery(
    settings: Settings,
    input_path: Path,
    progress_callback: PrepProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
    analysis: VideoPrepAnalysis | None = None,
) -> VideoPrepResult:
    analysis = analysis or analyze_video_for_delivery(settings, input_path, progress_callback=progress_callback)
    source_probe = analysis.source_probe
    decision = analysis.decision

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
            attempts_used=0,
        )

    tools = require_media_tools()
    output_path = _build_output_path(input_path)

    if decision.mode == "cap":
        output_probe, attempts_used = _prepare_with_size_cap(
            settings,
            input_path,
            output_path,
            analysis,
            progress_callback=progress_callback,
            should_cancel=should_cancel,
        )
    else:
        attempt = analysis.attempts[0]
        _run_transcode(
            tools.ffmpeg.path or "ffmpeg",
            input_path,
            output_path,
            settings,
            decision,
            attempt,
            source_probe.duration_seconds,
            progress_callback=progress_callback,
            should_cancel=should_cancel,
        )
        output_probe = _probe_output(tools.ffprobe.path or "ffprobe", output_path, progress_callback=progress_callback)
        attempts_used = 1

    source_size = source_probe.size_bytes
    output_size = output_probe.size_bytes
    pct = 0.0 if source_size <= 0 else (100.0 * (source_size - output_size) / source_size)
    LOGGER.info(
        "Video prep output ready: output=%s source_size=%s output_size=%s size_reduction_pct=%.2f attempts_used=%s",
        output_path,
        source_size,
        output_size,
        pct,
        attempts_used,
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
        attempts_used=attempts_used,
    )
