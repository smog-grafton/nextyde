"""Version 1 of the structured Laravel/TeleTyde processing contract.

RF is constant quality, never a promise of a target file size.
"""
from __future__ import annotations

PROCESSING_MODES = ("auto", "fast_mp4", "720p_balanced", "720p_compact", "720p_extra_compact", "1080p_balanced", "keep_original", "custom")
ENCODER_PRESETS = ("veryfast", "faster", "fast")
TARGET_HEIGHTS = (480, 720, 1080)
MAX_FRAME_RATES = (24, 25, 30, 50, 60)
RF_MIN, RF_MAX = 18, 30
_PROFILE_DEFAULTS = {
    "auto": (None, 23, None), "fast_mp4": (None, 23, None), "keep_original": (None, 23, None),
    "720p_balanced": (720, 23, 30), "720p_compact": (720, 26, 30),
    "720p_extra_compact": (720, 27, 30), "1080p_balanced": (1080, 23, 30), "custom": (None, 23, None),
}


def normalize_processing_options(value: dict | None = None) -> dict:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("Processing instructions must be an object")
    allowed = {"mode", "target_height", "rf", "max_frame_rate", "fast_start", "encoder_preset", "require_compatible", "audio_stream_index"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown processing option: {', '.join(sorted(unknown))}")
    mode = value.get("mode", "auto")
    if not isinstance(mode, str) or mode not in PROCESSING_MODES:
        raise ValueError("Unknown processing mode")
    height, rf, fps = _PROFILE_DEFAULTS[mode]
    options = {"mode": mode, "target_height": height, "rf": rf, "max_frame_rate": fps,
               "fast_start": mode != "keep_original", "encoder_preset": "veryfast",
               "require_compatible": mode != "keep_original", "audio_stream_index": None}
    options.update(value)
    for key in ("target_height", "rf", "max_frame_rate", "audio_stream_index"):
        if options[key] is not None and type(options[key]) is not int:
            raise ValueError(f"{key} must be an integer")
    if options["target_height"] is not None and options["target_height"] not in TARGET_HEIGHTS:
        raise ValueError("Unsupported target height")
    if options["rf"] is None or not RF_MIN <= options["rf"] <= RF_MAX:
        raise ValueError(f"RF must be between {RF_MIN} and {RF_MAX}")
    if options["max_frame_rate"] is not None and options["max_frame_rate"] not in MAX_FRAME_RATES:
        raise ValueError("Unsupported maximum frame rate")
    if not isinstance(options["encoder_preset"], str) or options["encoder_preset"] not in ENCODER_PRESETS:
        raise ValueError("Unsupported encoder speed preset")
    for key in ("fast_start", "require_compatible"):
        if type(options[key]) is not bool:
            raise ValueError(f"{key} must be a boolean")
    if options["audio_stream_index"] is not None and not 0 <= options["audio_stream_index"] <= 255:
        raise ValueError("audio_stream_index must be between 0 and 255")
    if mode != "custom":
        options["target_height"], options["max_frame_rate"] = height, fps
        options["fast_start"] = mode != "keep_original"
    options["require_compatible"] = mode != "keep_original"
    if mode in {"auto", "fast_mp4", "keep_original"}:
        options["rf"] = rf
    return options
