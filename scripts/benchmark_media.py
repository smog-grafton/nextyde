"""Run the real engine on isolated local fixtures; never uploads or contacts Telegram.

python scripts/benchmark_media.py --source /path/to/movie.mp4 --report docs/testing/media-benchmark.json [--large]
"""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.media_processing import prepare_media_for_processing
from app.media_tools import detect_media_tools, inspect_handbrake, inspect_hardware_encoders

parser = argparse.ArgumentParser()
parser.add_argument('--source', required=True)
parser.add_argument('--report', required=True)
parser.add_argument('--large', action='store_true')
args = parser.parse_args()
report_path = Path(args.report).resolve()
source = Path(args.source).resolve()
tools = detect_media_tools()
report = {'machine': platform.platform(), 'tools': asdict(tools), 'tests': [],
          'hardware_encoders_compiled': inspect_hardware_encoders(tools.ffmpeg.path),
          'handbrake_capabilities': inspect_handbrake(tools.handbrake.path) if tools.handbrake.available else None,
          'source': str(source), 'note': 'Local media operations only. No Telegram or object storage network calls. Sizes/durations are measured, not predicted.'}

def save():
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2))


def ffmpeg(args):
    subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y'] + args, check=True, capture_output=True, timeout=600)

with tempfile.TemporaryDirectory(prefix='teletyde-benchmark-') as directory:
    root = Path(directory)
    settings = SimpleNamespace(ffmpeg_binary=tools.ffmpeg.path, ffprobe_binary=tools.ffprobe.path,
                               handbrake_binary=tools.handbrake.path, media_encode_threads=2, media_process_nice=10,
                               telescope_disk_reserve_mb=2048, telescope_progress_interval_seconds=3,
                               video_prep_timeout_seconds=3600)
    def run(name, input_path, options=None):
        job = root / name; job.mkdir()
        target = job / 'source.media'
        shutil.copyfile(input_path, target)
        started = time.monotonic()
        print('Running ' + name, flush=True)
        try:
            result = prepare_media_for_processing(settings, target, options)
            row = dict(name=name, options=options or {'mode': 'auto'}, wall_seconds=round(time.monotonic()-started, 3), **result.metadata())
        except Exception as exc:
            row = {'name': name, 'error': str(exc), 'wall_seconds': time.monotonic()-started}
        report['tests'].append(row); save()
        print(json.dumps({k: row.get(k) for k in ('name','method','source_size','output_size','wall_seconds','error')}), flush=True)
        shutil.rmtree(job)
    compatible = root / 'compatible.mkv'
    ffmpeg(['-i', str(source), '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy', str(compatible)])
    run('actual_full_mkv_remux', compatible)
    run('actual_full_mkv_720p_compact', compatible, {'mode':'720p_compact'})
    run('actual_mp4_auto', source)
    clip = root / 'clip.mkv'
    ffmpeg(['-i', str(source), '-t','12', '-map','0:v:0','-map','0:a:0?', '-c','copy',str(clip)])
    for speed in ('veryfast','faster','fast'):
        run('actual_12s_720p_' + speed, clip, {'mode':'720p_compact','encoder_preset':speed})
    audio = root / 'audio.mkv'
    ffmpeg(['-i',str(clip),'-c:v','copy','-c:a','ac3',str(audio)])
    run('actual_h264_ac3',audio)
    hevc = root / 'hevc.mkv'
    ffmpeg(['-i',str(clip),'-t','4','-vf','scale=640:360','-c:v','libx265','-preset','ultrafast',
            '-x265-params','pools=1:frame-threads=1','-pix_fmt','yuv420p10le','-c:a','copy',str(hevc)])
    run('actual_excerpt_hevc_10bit',hevc)
    tracks = root / 'tracks.mkv'
    ffmpeg(['-i',str(clip),'-map','0:v:0','-map','0:a:0','-map','0:a:0','-c','copy',
            '-disposition:a:0','0','-disposition:a:1','0','-metadata:s:a:0','title=Original',
            '-metadata:s:a:1','title=VJ',str(tracks)])
    run('ambiguous_audio_rejected',tracks)
    run('explicit_vj_audio',tracks,{'audio_stream_index':2})
    portrait=root/'portrait.mkv'
    ffmpeg(['-f','lavfi','-i','testsrc2=size=360x640:rate=24000/1001','-t','4','-c:v','libx264',
            '-threads','2','-preset','ultrafast',str(portrait)])
    run('portrait_silent',portrait)
    if args.large:
        large=root/'large.mkv'
        print('Generating large raw-video MKV fixture (synthetic, not a movie compression forecast)',flush=True)
        ffmpeg(['-f','lavfi','-i','testsrc=size=1920x1080:rate=24','-t','8','-c:v','rawvideo',
                '-threads','2','-pix_fmt','bgr24','-allow_raw_vfw','1',str(large)])
        run('large_synthetic_raw_mkv_720p_compact',large,{'mode':'720p_compact'})
report['completed'] = True
save()
