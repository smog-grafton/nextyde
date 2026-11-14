"""Stream-aware media normalization shared by Telescope and legacy local prep."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from fractions import Fraction
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import time

from app.media_probe import MediaProbeResult, AudioStreamInfo, probe_media
from app.media_tools import require_media_tools, inspect_handbrake
from app.processing_profiles import normalize_processing_options

LOG = logging.getLogger('telebot.media')


class MediaProcessingCancelled(RuntimeError):
    pass


class MediaVideoRepairRequired(RuntimeError):
    """Caller must acquire its CPU slot before retrying a failed copy operation."""
    pass


@dataclass
class ProcessingDecision:
    method: str
    reason: str
    encodes_video: bool
    encodes_audio: bool
    selected_audio_stream_index: int | None
    # Use decoded/coded frame dimensions when source display metadata is
    # internally inconsistent. This never relaxes codec, duration or stream
    # validation; it only avoids trusting a broken declared display ratio.
    ignore_source_dimensions: bool = False


@dataclass
class MediaProcessingAnalysis:
    source_probe: MediaProbeResult
    processing: dict
    decision: ProcessingDecision


@dataclass
class MediaProcessingResult:
    delivery_path: Path
    source_probe: MediaProbeResult
    output_probe: MediaProbeResult
    decision: ProcessingDecision
    processing: dict
    processing_duration_seconds: float
    reused: bool = False

    @property
    def method(self):
        return self.decision.method

    @property
    def source_size_bytes(self):
        return self.source_probe.size_bytes

    @property
    def output_size_bytes(self):
        return self.output_probe.size_bytes

    def metadata(self):
        saved = self.source_size_bytes - self.output_size_bytes
        return {'method': self.method, 'profile': self.processing['mode'],
                'source_size': self.source_size_bytes, 'output_size': self.output_size_bytes,
                'bytes_saved': saved, 'percentage_saved': round(saved * 100 / self.source_size_bytes, 2),
                'duration_seconds': round(self.processing_duration_seconds, 3),
                'source_probe': self.source_probe.metadata(), 'output_probe': self.output_probe.metadata(),
                'selected_audio_stream_index': self.decision.selected_audio_stream_index,
                'video_copied': not self.decision.encodes_video, 'audio_copied': not self.decision.encodes_audio,
                'reason': self.decision.reason, 'reused_output': self.reused,
                'source_dimensions_ignored': self.decision.ignore_source_dimensions,
                'subtitle_policy': 'preserved_original' if self.processing['mode'] == 'keep_original' else 'external_tracks_only',
                'validation': {'passed': True, 'timestamp_check': 'first_3_seconds',
                               'duration_compared': True, 'fast_start': self.output_probe.fast_start,
                               'require_compatible': self.processing['require_compatible']}}


def _healthy(probe):
    if not probe.video or not probe.video.width or not probe.video.height:
        raise RuntimeError('Media has no readable video stream with valid dimensions.')
    if not probe.duration_seconds or not math.isfinite(probe.duration_seconds) or probe.duration_seconds <= 0:
        raise RuntimeError('Media has no valid duration.')
    if not probe.container_format or probe.size_bytes < 1024:
        raise RuntimeError('Media container is unreadable or file is implausibly small.')


def select_audio(probe, index=None):
    tracks = probe.audio_streams or ((probe.audio,) if probe.audio else ())
    if index is not None:
        chosen = next((t for t in tracks if t.index == index), None)
        if chosen is None:
            raise RuntimeError(f'Requested audio stream {index} is absent; available indices: {[t.index for t in tracks]}')
        return chosen
    if len(tracks) <= 1:
        return tracks[0] if tracks else None
    defaults = [t for t in tracks if t.default and not t.commentary and 'commentary' not in (t.title or '').lower()]
    if len(defaults) == 1:
        return defaults[0]
    available = '; '.join(f'{t.index}: {t.language or "und"} {t.title or "untitled"}' for t in tracks)
    raise RuntimeError(f'Multiple audio tracks have no unique non-commentary default. Select the intended VJ audio stream index. Available: {available}')


def _audio_compatible(audio):
    return audio is None or (audio.codec == 'aac' and audio.profile == 'LC'
                            and audio.channels in (1, 2) and audio.sample_rate in (44100, 48000))


def _video_compatible(probe):
    v = probe.video
    return bool(v and v.codec == 'h264' and v.profile in ('Baseline', 'Constrained Baseline', 'Main', 'High')
                and v.pix_fmt == 'yuv420p' and v.level is not None and 0 < v.level <= 42
                and v.width <= 1920 and v.height <= 1080
                and v.frame_rate and 0 < v.frame_rate <= 60.01
                and v.field_order in (None, 'unknown', 'progressive')
                and v.color_transfer not in ('smpte2084', 'arib-std-b67') and v.rotation == 0)


def decide(probe, options):
    _healthy(probe)
    if options['mode'] == 'keep_original':
        if probe.probe_warnings or not probe.timestamps_valid:
            raise RuntimeError('Keep Original cannot repair a source with probe errors or invalid sampled timestamps.')
        return ProcessingDecision('passthrough', 'Administrator requested original streams and container.', False, False, None)
    audio = select_audio(probe, options['audio_stream_index'])
    index = audio.index if audio else None
    compress = options['mode'] not in ('auto', 'fast_mp4')
    video_encode = compress or not _video_compatible(probe)
    audio_encode = not _audio_compatible(audio)
    if video_encode:
        return ProcessingDecision('full_transcode' if audio_encode else 'video_transcode',
                                  'Explicit constant-quality compression.' if compress else 'Video outside universal compatibility limits.',
                                  True, audio_encode, index)
    if audio_encode:
        return ProcessingDecision('audio_transcode', 'Preserve compatible video; normalize selected audio to AAC stereo.', False, True, index)
    extra = len(probe.audio_streams) > 1 or probe.video_stream_count != 1 or bool(probe.subtitle_streams)
    if probe.is_mp4 and not extra and probe.timestamps_valid and not probe.probe_warnings and probe.video.codec_tag == 'avc1':
        if not options['fast_start'] or probe.fast_start:
            return ProcessingDecision('passthrough', 'Already compatible and optimized.', False, False, index)
        return ProcessingDecision('faststart', 'Move MP4 metadata before media packets with stream copy.', False, False, index)
    return ProcessingDecision('remux', 'Copy selected compatible streams into a clean MP4 container.', False, False, index)


def analyze_media_for_processing(settings, input_path, processing=None, progress_callback=None, should_cancel=None):
    options = normalize_processing_options(processing)
    if progress_callback:
        progress_callback('inspecting', {'progress_pct': 0})
    tools = require_media_tools()
    source = probe_media(settings.ffprobe_binary or tools.ffprobe.path, Path(input_path))
    if should_cancel and should_cancel():
        raise MediaProcessingCancelled('Media inspection cancelled.')
    decision = decide(source, options)
    if options['ignore_dimensions']:
        decision = ProcessingDecision(
            **{**asdict(decision), 'ignore_source_dimensions': True},
        )
    LOG.info('Decision method=%s profile=%s selected_audio=%s source=%s reason=%s',
             decision.method, options['mode'], decision.selected_audio_stream_index,
             json.dumps(source.metadata()), decision.reason)
    return MediaProcessingAnalysis(source, options, decision)


def validate_output(source, output, options, decision):
    _healthy(output)
    tolerance = max(2.0, min(10.0, source.duration_seconds * .005))
    if abs(output.duration_seconds - source.duration_seconds) > tolerance:
        raise RuntimeError(f'Output duration differs from source: {output.duration_seconds:.3f}s vs {source.duration_seconds:.3f}s.')
    # Container duration alone can hide a prematurely ended video stream.
    if output.video.duration_seconds and abs(output.video.duration_seconds - source.duration_seconds) > tolerance:
        raise RuntimeError('Output video stream is truncated relative to source duration.')
    if source.audio and not output.audio:
        raise RuntimeError('Output lost the source audio.')
    if not output.timestamps_valid or output.probe_warnings:
        raise RuntimeError('Output has probe errors or invalid sampled timestamps.')
    if options['require_compatible']:
        def display_ratio(video):
            try:
                ratio = float(Fraction((video.display_aspect_ratio or '').replace(':', '/')))
            except (ValueError, ZeroDivisionError):
                ratio = video.width / video.height
            return 1 / ratio if abs(video.rotation) % 180 == 90 else ratio
        if (not getattr(decision, 'ignore_source_dimensions', False)
                and abs(display_ratio(output.video) / display_ratio(source.video) - 1) > .02):
            raise RuntimeError('Output display aspect ratio differs from the source.')
        if not output.is_mp4 or not _video_compatible(output) or output.video.codec_tag != 'avc1':
            raise RuntimeError('Output violates MP4/H.264 8-bit universal compatibility contract.')
        if not _audio_compatible(output.audio):
            raise RuntimeError('Output audio violates AAC LC mono/stereo compatibility contract.')
        if output.video_stream_count != 1 or len(output.audio_streams) > 1 or output.subtitle_streams:
            raise RuntimeError('Output contains unintended streams.')
        if options['fast_start'] and not output.fast_start:
            raise RuntimeError('MP4 moov metadata is not before mdat; Fast Start validation failed.')
        if options['target_height'] and output.video.height > options['target_height']:
            raise RuntimeError('Output exceeds requested resolution.')
        if options['max_frame_rate'] and output.video.frame_rate > options['max_frame_rate'] + .1:
            raise RuntimeError('Output exceeds requested maximum frame rate.')


def _dimensions(source, options, decision=None):
    v = source.video
    if getattr(decision, 'ignore_source_dimensions', False):
        # The actual decoded frame is safer than contradictory DAR/SAR tags.
        width, height = v.width, v.height
    else:
        try:
            sar = float(Fraction((v.sample_aspect_ratio or '1:1').replace(':', '/')))
        except (ValueError, ZeroDivisionError):
            sar = 1.0
        width, height = v.width * sar, v.height
    if abs(v.rotation) % 180 == 90:
        width, height = height, width
    cap = min(1080, options['target_height'] or 1080)
    max_width = min(1920, round(cap * 16 / 9))
    ratio = min(1.0, cap / height, max_width / width)
    return max(2, int(width * ratio) // 2 * 2), max(2, int(height * ratio) // 2 * 2)


def _ffmpeg_command(binary, source, output, options, decision, settings):
    cmd = [binary, '-nostdin', '-y', '-hide_banner', '-loglevel', 'warning', '-nostats',
           '-progress', 'pipe:1', '-protocol_whitelist', 'file,pipe',
           '-format_whitelist', 'mov,matroska,webm,avi,mpeg,mpegts,flv,asf,ogg', '-fflags', '+genpts',
           '-threads', str(settings.media_encode_threads), '-i', str(source.path),
           '-map', f'0:{source.video.index}']
    if decision.selected_audio_stream_index is not None:
        cmd += ['-map', f'0:{decision.selected_audio_stream_index}']
    cmd += ['-sn', '-dn', '-map_metadata', '-1', '-map_chapters', '-1']
    if decision.encodes_video:
        width, height = _dimensions(source, options, decision)
        filters = []
        if source.video.field_order not in (None, 'unknown', 'progressive'):
            filters += ['yadif=mode=send_frame:parity=auto:deint=interlaced']
        if source.video.color_transfer in ('smpte2084', 'arib-std-b67'):
            filters += ['zscale=t=linear:npl=100', 'format=gbrpf32le', 'tonemap=hable:desat=0',
                        'zscale=p=bt709:t=bt709:m=bt709:r=tv']
        filters += [f'scale={width}:{height}:flags=lanczos', 'setsar=1', 'format=yuv420p']
        cap = options['max_frame_rate'] or 60
        if (source.video.frame_rate or 0) > cap + .01:
            filters += [f'fps={cap}']
        cmd += ['-vf', ','.join(filters), '-filter_threads', str(settings.media_encode_threads),
                '-c:v', 'libx264', '-preset', options['encoder_preset'], '-crf', str(options['rf']),
                '-profile:v', 'high', '-level:v', '4.2', '-pix_fmt', 'yuv420p',
                '-threads', str(settings.media_encode_threads), '-maxrate', '20M', '-bufsize', '40M']
    else:
        cmd += ['-c:v', 'copy']
    cmd += ['-tag:v', 'avc1']
    if decision.selected_audio_stream_index is not None:
        cmd += (['-c:a', 'aac', '-profile:a', 'aac_low', '-b:a', '160k', '-ac', '2', '-ar', '48000']
                if decision.encodes_audio else ['-c:a', 'copy'])
    cmd += ['-avoid_negative_ts', 'make_zero']
    if options['fast_start']:
        cmd += ['-movflags', '+faststart']
    return cmd + ['-f', 'mp4', str(output)]


def _handbrake_command(binary, source, output, options, decision, settings):
    capabilities = inspect_handbrake(binary)
    width, height = _dimensions(source, options, decision)
    cmd = [binary, '--input', str(source.path), '--output', str(output), '--format', 'av_mp4',
           '--encoder', 'x264', '--encoder-preset', options['encoder_preset'],
           '--quality', str(options['rf']), '--encoder-profile', 'high', '--encoder-level', '4.2',
           '--encopts', f'threads={settings.media_encode_threads}:vbv-maxrate=20000:vbv-bufsize=40000',
           '--crop', '0:0:0:0', '--non-anamorphic', '--keep-display-aspect',
           '--maxWidth', str(width), '--maxHeight', str(height), '--subtitle', 'none',
           '--subtitle-burned=none']
    audio = select_audio(source, decision.selected_audio_stream_index)
    if audio:
        tracks = list(source.audio_streams or (source.audio,))
        cmd += ['--audio', str(tracks.index(audio) + 1), '--aencoder',
                capabilities['aac_encoder'] if decision.encodes_audio else 'copy:aac']
        if decision.encodes_audio:
            cmd += ['--ab', '160', '--mixdown', 'stereo', '--arate', '48']
    else:
        cmd += ['--audio', 'none']
    cap = options['max_frame_rate'] or 60
    cmd += ['--rate', str(cap), '--pfr']  # peak limit preserves 23.976/25 fps sources
    if options['fast_start']:
        cmd += ['--optimize']
    else:
        cmd += ['--no-optimize']
    return cmd


def _terminate(proc):
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
    except ProcessLookupError:
        pass


def _run(cmd, settings, duration, stage, progress, cancel, lock_fd, directory):
    # Nice is an executable with an argument array; no preexec_fn in a threaded worker.
    nice = shutil.which('nice')
    if nice:
        cmd = [nice, '-n', str(settings.media_process_nice)] + cmd
    kwargs = {'pass_fds': (lock_fd,)} if lock_fd is not None else {}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True, **kwargs)
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    started, reported, disk_checked = time.monotonic(), 0.0, 0.0
    tail, buffer, pct = '', '', 0.0
    try:
        while True:
            now = time.monotonic()
            if cancel and cancel():
                raise MediaProcessingCancelled('Media processing cancelled.')
            if now - started > settings.video_prep_timeout_seconds:
                raise RuntimeError('Media processing timed out.')
            if now - disk_checked > 1:
                disk_checked = now
                reserve = settings.telescope_disk_reserve_mb * 1024 * 1024
                if shutil.disk_usage(directory).free < reserve:
                    raise RuntimeError('Insufficient temporary disk space during media processing; reserve protected.')
            events = selector.select(.25)
            if events:
                raw = os.read(proc.stdout.fileno(), 65536)
                if raw:
                    text = raw.decode('utf-8', errors='replace')
                    tail = (tail + text)[-16000:]
                    buffer += text.replace('\r', '\n')
                    rows = buffer.split('\n')
                    buffer = rows.pop()[-16000:]
                    for row in rows:
                        if row.startswith('out_time_us=') or row.startswith('out_time_ms='):
                            try:
                                pct = float(row.split('=', 1)[1]) / 1000000 / duration * 100
                            except ValueError:
                                pass
                        match = re.search(r'Encoding:.*?([\d.]+)\s*%', row)
                        if match:
                            pct = float(match.group(1))
                elif proc.poll() is not None:
                    break
            elif proc.poll() is not None:
                break
            if progress and now - reported >= settings.telescope_progress_interval_seconds:
                pct = max(0.0, min(99.0, pct))
                elapsed = now - started
                progress(stage, {'progress_pct': pct, 'elapsed_seconds': elapsed,
                                 'eta_seconds': elapsed * (100 - pct) / pct if pct >= 2 else None})
                reported = now
        proc.wait()
        if proc.returncode:
            raise RuntimeError(f'Media encoder exited {proc.returncode}: {tail[-4000:]}')
        # FFmpeg can exit zero after silently dropping damaged packets. Fail closed.
        if any(term in tail.lower() for term in ('non-monotonous dts', 'non monotonically increasing dts',
                                                  'error while decoding', 'corrupt decoded frame')):
            raise RuntimeError(f'Media contains damaged packets or unrepaired timestamps: {tail[-4000:]}')
    except BaseException:
        _terminate(proc)
        raise
    finally:
        selector.close()
        proc.stdout.close()


def _fingerprint(source, options):
    stat = source.stat()
    return hashlib.sha256(json.dumps({'size': stat.st_size, 'mtime': stat.st_mtime_ns,
                                      'options': options, 'contract': 1}, sort_keys=True).encode()).hexdigest()


def _is_display_aspect_ratio_failure(error: Exception) -> bool:
    return 'output display aspect ratio differs from the source' in str(error).lower()


def _coded_dimension_fallback(decision: ProcessingDecision) -> ProcessingDecision:
    """Return a safe H.264 retry plan for inconsistent source display tags."""
    return ProcessingDecision(
        method='full_transcode' if decision.encodes_audio else 'video_transcode',
        reason=('Source display dimensions were inconsistent; retried from decoded '
                'pixel dimensions while preserving the compatibility checks.'),
        encodes_video=True,
        encodes_audio=decision.encodes_audio,
        selected_audio_stream_index=decision.selected_audio_stream_index,
        ignore_source_dimensions=True,
    )


def prepare_media_for_processing(settings, input_path, processing=None, analysis=None,
                                 progress_callback=None, should_cancel=None, process_lock_fd=None, allow_video_fallback=True):
    started = time.monotonic()
    input_path = Path(input_path)
    analysis = analysis or analyze_media_for_processing(settings, input_path, processing, progress_callback)
    source, options, decision = analysis.source_probe, analysis.processing, analysis.decision
    if should_cancel and should_cancel():
        raise MediaProcessingCancelled('Media processing cancelled.')
    tools = require_media_tools()
    probe_binary = settings.ffprobe_binary or tools.ffprobe.path
    output = input_path.with_name('output.mp4')
    partial = input_path.with_name('output.part.mp4')
    receipt = input_path.with_name('processing-receipt.json')
    fingerprint = _fingerprint(input_path, options)
    if decision.method == 'passthrough':
        if progress_callback:
            progress_callback('validating', {'progress_pct': 98})
        final_probe = probe_media(probe_binary, input_path)
        validate_output(source, final_probe, options, decision)
        return MediaProcessingResult(input_path, source, final_probe, decision, options, time.monotonic() - started)
    if output.exists() and receipt.exists():
        try:
            stored = json.loads(receipt.read_text())
            if stored['fingerprint'] == fingerprint:
                final_probe = probe_media(probe_binary, output)
                cached_decision = ProcessingDecision(**stored['decision'])
                validate_output(source, final_probe, options, cached_decision)
                return MediaProcessingResult(output, source, final_probe, cached_decision, options, stored['elapsed'], True)
        except (ValueError, KeyError, OSError, RuntimeError):
            LOG.warning('Discarding invalid local output checkpoint for %s', input_path)
    partial.unlink(missing_ok=True)
    stage = 'optimizing_video' if decision.encodes_video else ('converting_audio' if decision.encodes_audio else 'preparing_mp4')
    # Explicit compression uses HandBrake; compatibility repairs use FFmpeg.
    # HDR/interlaced/rotated sources need explicit filtering and use FFmpeg too.
    use_handbrake = (options['mode'] not in ('auto', 'fast_mp4', 'keep_original')
                     and source.video.color_transfer not in ('smpte2084', 'arib-std-b67')
                     and source.video.field_order in (None, 'unknown', 'progressive') and not source.video.rotation)
    if use_handbrake:
        binary = settings.handbrake_binary or (tools.handbrake.path if tools.handbrake else None)
        if not binary:
            raise RuntimeError('HandBrakeCLI is required for compression profiles; install the production image dependencies.')
        cmd = _handbrake_command(binary, source, partial, options, decision, settings)
    else:
        cmd = _ffmpeg_command(settings.ffmpeg_binary or tools.ffmpeg.path, source, partial, options, decision, settings)
    try:
        if progress_callback:
            progress_callback(stage, {'progress_pct': 0})
        try:
            _run(cmd, settings, source.duration_seconds, stage, progress_callback, should_cancel, process_lock_fd, input_path.parent)
            if progress_callback:
                progress_callback('validating', {'progress_pct': 98})
            final_probe = probe_media(probe_binary, partial)
            validate_output(source, final_probe, options, decision)
        except RuntimeError as exc:
            if _is_display_aspect_ratio_failure(exc) and not decision.ignore_source_dimensions:
                # Telegram files can carry contradictory DAR/SAR/container
                # tags. A failed display-ratio comparison is not evidence that
                # the source itself is unusable. Re-encode once with FFmpeg
                # using its decoded frame dimensions, while retaining every
                # other duration, stream, codec and fast-start guardrail.
                if not decision.encodes_video and not allow_video_fallback:
                    raise MediaVideoRepairRequired(str(exc)) from exc
                LOG.warning('Display-aspect validation failed; retrying from coded frame dimensions: %s', exc)
                partial.unlink(missing_ok=True)
                decision = _coded_dimension_fallback(decision)
                cmd = _ffmpeg_command(settings.ffmpeg_binary or tools.ffmpeg.path, source, partial, options, decision, settings)
                if progress_callback:
                    progress_callback('optimizing_video', {'progress_pct': 0})
                _run(cmd, settings, source.duration_seconds, 'optimizing_video', progress_callback, should_cancel, process_lock_fd, input_path.parent)
                if progress_callback:
                    progress_callback('validating', {'progress_pct': 98})
                final_probe = probe_media(probe_binary, partial)
                validate_output(source, final_probe, options, decision)
            elif (decision.encodes_video or isinstance(exc, MediaProcessingCancelled)
                    or 'disk space' in str(exc).lower() or 'timed out' in str(exc).lower()):
                raise
            else:
                # A structural stream-copy failure gets one controlled video repair.
                # User cancellation, capacity failures, and failed encodes never loop.
                if not allow_video_fallback:
                    raise MediaVideoRepairRequired(str(exc)) from exc
                LOG.warning('Stream-copy repair failed; trying compatibility encode: %s', exc)
                partial.unlink(missing_ok=True)
                decision = ProcessingDecision('full_transcode' if decision.encodes_audio else 'video_transcode',
                                              'Stream-copy validation failed; compatibility repair required.',
                                              True, decision.encodes_audio, decision.selected_audio_stream_index)
                cmd = _ffmpeg_command(settings.ffmpeg_binary or tools.ffmpeg.path, source, partial, options, decision, settings)
                if progress_callback:
                    progress_callback('optimizing_video', {'progress_pct': 0})
                _run(cmd, settings, source.duration_seconds, 'optimizing_video', progress_callback, should_cancel, process_lock_fd, input_path.parent)
                if progress_callback:
                    progress_callback('validating', {'progress_pct': 98})
                final_probe = probe_media(probe_binary, partial)
                validate_output(source, final_probe, options, decision)
        if should_cancel and should_cancel():
            raise MediaProcessingCancelled('Media processing cancelled.')
        # Durably commit file before recording reusable validated output.
        with partial.open('rb') as handle:
            os.fsync(handle.fileno())
        partial.replace(output)
        final_probe.path = output
        elapsed = time.monotonic() - started
        receipt_partial = receipt.with_suffix('.part.json')
        receipt_partial.write_text(json.dumps({'fingerprint': fingerprint, 'decision': asdict(decision), 'elapsed': elapsed}))
        with receipt_partial.open('rb') as handle:
            os.fsync(handle.fileno())
        receipt_partial.replace(receipt)
        return MediaProcessingResult(output, source, final_probe, decision, options, elapsed)
    finally:
        partial.unlink(missing_ok=True)
