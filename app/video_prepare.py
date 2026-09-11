"""Backward-compatible entrypoints for the shared stream-aware processing engine.

The former extension/size-cap decision tree has been removed. Large compatible
media is copied unless an administrator explicitly selects a compression profile.
"""
from dataclasses import dataclass
from pathlib import Path
from app.media_probe import MediaProbeResult, probe_media
from app.media_tools import require_media_tools
from app.media_processing import (MediaProcessingCancelled, MediaProcessingAnalysis,
                                  decide, prepare_media_for_processing)
from app.processing_profiles import normalize_processing_options

VideoPrepCancelledError = MediaProcessingCancelled


@dataclass
class VideoPrepDecision:
    should_transcode: bool
    reason: str
    mode: str
    target_height: int | None = None
    enforce_size_cap: bool = False
    target_max_bytes: int | None = None


@dataclass
class VideoPrepAnalysis:
    source_probe: MediaProbeResult
    decision: VideoPrepDecision
    attempts: tuple = ()
    processing_analysis: MediaProcessingAnalysis | None = None


@dataclass
class VideoPrepResult:
    delivery_path: Path
    source_path: Path
    optimized_path: Path | None
    source_probe: MediaProbeResult
    output_probe: MediaProbeResult
    decision: VideoPrepDecision
    source_size_bytes: int
    output_size_bytes: int
    attempts_used: int

    @property
    def changed(self):
        return self.optimized_path is not None


def analyze_video_for_delivery(settings, input_path, progress_callback=None):
    if progress_callback:
        progress_callback('inspecting', {'progress_pct': 0})
    tools = require_media_tools()
    source = probe_media(settings.ffprobe_binary or tools.ffprobe.path, input_path)
    options = normalize_processing_options()
    decision = decide(source, options)
    return VideoPrepAnalysis(source, VideoPrepDecision(decision.method != 'passthrough', decision.reason, decision.method),
                             (), MediaProcessingAnalysis(source, options, decision))


def prepare_video_for_delivery(settings, input_path, progress_callback=None, should_cancel=None, analysis=None):
    analysis = analysis or analyze_video_for_delivery(settings, input_path, progress_callback)
    result = prepare_media_for_processing(settings, input_path, analysis=analysis.processing_analysis,
                                         progress_callback=progress_callback, should_cancel=should_cancel)
    return VideoPrepResult(result.delivery_path, input_path,
                           result.delivery_path if result.delivery_path != input_path else None,
                           result.source_probe, result.output_probe, analysis.decision,
                           result.source_size_bytes, result.output_size_bytes, int(result.method != 'passthrough'))
