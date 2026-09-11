from __future__ import annotations

import json
import math
import struct
import subprocess
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path


@dataclass(slots=True)
class VideoStreamInfo:
    codec: str | None
    width: int | None
    height: int | None
    pix_fmt: str | None
    frame_rate: float | None
    index: int = 0
    profile: str | None = None
    codec_tag: str | None = None
    level: int | None = None
    display_aspect_ratio: str | None = None
    sample_aspect_ratio: str | None = None
    bitrate: int | None = None
    duration_seconds: float | None = None
    start_time: float | None = None
    field_order: str | None = None
    color_transfer: str | None = None
    rotation: int = 0


@dataclass(slots=True)
class AudioStreamInfo:
    codec: str | None
    channels: int | None
    sample_rate: int | None
    index: int = 1
    profile: str | None = None
    channel_layout: str | None = None
    language: str | None = None
    title: str | None = None
    default: bool = False
    commentary: bool = False
    bitrate: int | None = None
    duration_seconds: float | None = None
    start_time: float | None = None


@dataclass(slots=True)
class SubtitleStreamInfo:
    index: int
    codec: str | None
    language: str | None
    title: str | None


@dataclass(slots=True)
class MediaProbeResult:
    path: Path
    extension: str
    size_bytes: int
    duration_seconds: float | None
    container_format: str | None
    video: VideoStreamInfo | None
    audio: AudioStreamInfo | None
    audio_streams: tuple[AudioStreamInfo, ...] = ()
    subtitle_streams: tuple[SubtitleStreamInfo, ...] = ()
    video_stream_count: int = 1
    major_brand: str | None = None
    bitrate: int | None = None
    start_time: float | None = None
    fast_start: bool | None = None
    timestamps_valid: bool = True
    probe_warnings: str | None = None

    @property
    def is_mp4(self) -> bool:
        # MOV/MP4 share a demuxer: actual ftyp brand distinguishes them.
        return "mp4" in (self.container_format or "").split(",") and (self.major_brand or "").strip() in {
            "isom", "iso2", "iso3", "iso4", "iso5", "iso6", "iso7", "iso8", "iso9",
            "mp41", "mp42", "avc1", "M4V", "MSNV", "dash",
        }

    def metadata(self) -> dict:
        data = asdict(self)
        data.pop("path")  # Do not expose private temporary paths in diagnostics.
        return data


def _to_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _to_float(value: object) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _to_frame_rate(value: object) -> float | None:
    try:
        number = float(Fraction(str(value)))
        return number if math.isfinite(number) and number > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def mp4_has_fast_start(path: Path) -> bool:
    """Read top-level box headers with seeks, never load the movie into RAM."""
    size = path.stat().st_size
    moov_offset = mdat_offset = None
    offset = 0
    with path.open("rb") as handle:
        while offset + 8 <= size:
            handle.seek(offset)
            box_size, box_type = struct.unpack(">I4s", handle.read(8))
            header_size = 8
            if box_size == 1:
                raw = handle.read(8)
                if len(raw) != 8:
                    return False
                box_size = struct.unpack(">Q", raw)[0]
                header_size = 16
            elif box_size == 0:
                box_size = size - offset
            if box_size < header_size or offset + box_size > size:
                return False
            if box_type == b"moov" and moov_offset is None:
                moov_offset = offset
            elif box_type == b"mdat" and mdat_offset is None:
                mdat_offset = offset
            offset += box_size
    return offset == size and moov_offset is not None and mdat_offset is not None and moov_offset < mdat_offset


def probe_media(ffprobe_binary: str, input_path: Path) -> MediaProbeResult:
    if not input_path.is_file() or input_path.is_symlink():
        raise RuntimeError(f"Cannot probe media; regular file required: {input_path}")
    # A bounded packet sample checks timestamps without scanning a multi-GB movie.
    # This is deliberately reported as sampled validation, not a full decode scan.
    cmd = [ffprobe_binary, "-v", "error", "-protocol_whitelist", "file,pipe",
           "-format_whitelist", "mov,matroska,webm,avi,mpeg,mpegts,flv,asf,ogg",
           "-print_format", "json", "-show_format", "-show_streams", "-show_packets",
           "-read_intervals", "%+3", "-show_entries", "packet=stream_index,pts_time,dts_time", str(input_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=90)
    except OSError as exc:
        raise RuntimeError(f"ffprobe execution failed: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ffprobe timed out while probing media") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed with exit {proc.returncode}: {(proc.stderr or '').strip()[-4000:]}")
    try:
        parsed = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe returned invalid JSON output") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("ffprobe returned invalid media data")
    videos: list[VideoStreamInfo] = []
    audios: list[AudioStreamInfo] = []
    subtitles: list[SubtitleStreamInfo] = []
    for stream in parsed.get("streams") or []:
        tags, disposition = stream.get("tags") or {}, stream.get("disposition") or {}
        index = _to_int(stream.get("index"))
        if index is None:
            continue
        codec = stream.get("codec_name")
        if stream.get("codec_type") == "video" and not disposition.get("attached_pic"):
            rotation = next((_to_int(item.get("rotation")) for item in stream.get("side_data_list") or [] if "rotation" in item), None)
            videos.append(VideoStreamInfo(
                codec=codec, width=_to_int(stream.get("width")), height=_to_int(stream.get("height")),
                pix_fmt=stream.get("pix_fmt"), frame_rate=_to_frame_rate(stream.get("avg_frame_rate")) or _to_frame_rate(stream.get("r_frame_rate")),
                index=index, profile=stream.get("profile"), codec_tag=stream.get("codec_tag_string"),
                level=_to_int(stream.get("level")), display_aspect_ratio=stream.get("display_aspect_ratio"),
                sample_aspect_ratio=stream.get("sample_aspect_ratio"), bitrate=_to_int(stream.get("bit_rate")),
                duration_seconds=_to_float(stream.get("duration")), start_time=_to_float(stream.get("start_time")),
                field_order=stream.get("field_order"), color_transfer=stream.get("color_transfer"),
                rotation=rotation or _to_int(tags.get("rotate")) or 0,
            ))
        elif stream.get("codec_type") == "audio":
            audios.append(AudioStreamInfo(
                codec=codec, channels=_to_int(stream.get("channels")), sample_rate=_to_int(stream.get("sample_rate")),
                index=index, profile=stream.get("profile"), channel_layout=stream.get("channel_layout"),
                language=tags.get("language"), title=tags.get("title"), default=bool(disposition.get("default")),
                commentary=bool(disposition.get("comment") or disposition.get("visual_impaired")),
                bitrate=_to_int(stream.get("bit_rate")), duration_seconds=_to_float(stream.get("duration")),
                start_time=_to_float(stream.get("start_time")),
            ))
        elif stream.get("codec_type") == "subtitle":
            subtitles.append(SubtitleStreamInfo(index, codec, tags.get("language"), tags.get("title")))
    selected_video = videos[0] if videos else None
    format_info = parsed.get("format") or {}
    duration = _to_float(format_info.get("duration")) or (selected_video.duration_seconds if selected_video else None)
    relevant_indices = {stream.index for stream in [*videos, *audios]}
    last_dts: dict[int, float] = {}
    seen_indices: set[int] = set()
    timestamps_valid = True
    for packet in parsed.get("packets") or []:
        index = _to_int(packet.get("stream_index"))
        if index not in relevant_indices:
            continue
        pts, dts = _to_float(packet.get("pts_time")), _to_float(packet.get("dts_time"))
        if pts is None:
            timestamps_valid = False
        # MKV B-frames can omit initial DTS. PTS legitimately reorders.
        if dts is not None:
            if index in last_dts and dts < last_dts[index] - 0.001:
                timestamps_valid = False
            last_dts[index] = dts
        seen_indices.add(index)
    if selected_video is not None and selected_video.index not in seen_indices:
        timestamps_valid = False
    result = MediaProbeResult(
        path=input_path, extension=input_path.suffix.lower(), size_bytes=input_path.stat().st_size,
        duration_seconds=duration, container_format=format_info.get("format_name"), video=selected_video,
        audio=audios[0] if audios else None, audio_streams=tuple(audios), subtitle_streams=tuple(subtitles),
        video_stream_count=len(videos), major_brand=(format_info.get("tags") or {}).get("major_brand"),
        bitrate=_to_int(format_info.get("bit_rate")), start_time=_to_float(format_info.get("start_time")),
        timestamps_valid=timestamps_valid, probe_warnings=(proc.stderr or "").strip()[-4000:] or None,
    )
    if "mp4" in (result.container_format or "").split(","):
        result.fast_start = mp4_has_fast_start(input_path)
    return result
