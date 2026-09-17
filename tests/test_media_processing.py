from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from app.media_processing import (analyze_media_for_processing, prepare_media_for_processing,
                                  validate_output, MediaProcessingCancelled)
from app.media_probe import probe_media
from app.processing_profiles import normalize_processing_options
from test_web_and_video import make_settings


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg integration dependencies unavailable')
class RealMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='teletyde-media-tests-')
        cls.root = Path(cls.temp.name)
        cls.base = cls.root / 'base.mp4'
        cls.ffmpeg(['-f', 'lavfi', '-i', 'testsrc2=size=1920x1080:rate=24000/1001',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '3',
                    '-c:v', 'libx264', '-threads', '2', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-movflags', '+faststart', str(cls.base)])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @staticmethod
    def ffmpeg(args):
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y'] + args,
                       check=True, capture_output=True, timeout=120)

    def fixture(self, name, args):
        directory = self.root / name
        directory.mkdir(exist_ok=True)
        output = directory / 'source.mkv'
        self.ffmpeg(['-i', str(self.base)] + args + [str(output)])
        return output

    def prepare(self, source, processing=None):
        settings = make_settings(source.parent)
        settings.handbrake_binary = os.getenv('HANDBRAKE_BINARY') or shutil.which('HandBrakeCLI')
        return prepare_media_for_processing(settings, source, processing)

    def test_large_compatible_mkv_decision_ignores_size(self):
        source = self.fixture('remux', ['-map', '0', '-c', 'copy'])
        result = self.prepare(source)
        self.assertEqual(result.method, 'remux')
        self.assertFalse(result.decision.encodes_video)
        self.assertTrue(result.output_probe.fast_start)
        self.assertAlmostEqual(result.output_probe.video.frame_rate, result.source_probe.video.frame_rate, delta=.02)
        self.assertEqual(self.prepare(source).reused, True)

    def test_compatible_mp4_passthrough_even_with_wrong_extension(self):
        source = self.root / 'disguised.media'
        shutil.copyfile(self.base, source)
        result = self.prepare(source)
        self.assertEqual(result.method, 'passthrough')
        self.assertEqual(result.delivery_path, source)

    def test_unoptimized_mp4_faststart(self):
        directory = self.root / 'faststart'; directory.mkdir(exist_ok=True)
        source = directory / 'source.mp4'
        self.ffmpeg(['-i', str(self.base), '-c', 'copy', str(source)])
        result = self.prepare(source)
        self.assertEqual(result.method, 'faststart')
        self.assertTrue(result.output_probe.fast_start)

    def test_incompatible_audio_preserves_video(self):
        source = self.fixture('audio', ['-c:v', 'copy', '-c:a', 'ac3'])
        result = self.prepare(source)
        self.assertEqual(result.method, 'audio_transcode')
        self.assertEqual(result.output_probe.audio.codec, 'aac')
        self.assertFalse(result.decision.encodes_video)

    def test_hevc_10bit_transcodes_video_only(self):
        source = self.fixture('hevc', ['-vf', 'scale=320:180', '-c:v', 'libx265',
                                     '-preset', 'ultrafast', '-x265-params', 'pools=1:frame-threads=1',
                                     '-pix_fmt', 'yuv420p10le', '-c:a', 'copy'])
        result = self.prepare(source)
        self.assertEqual(result.method, 'video_transcode')
        self.assertEqual(result.output_probe.video.pix_fmt, 'yuv420p')

    def test_multitrack_requires_explicit_or_unique_default(self):
        source = self.fixture('tracks', ['-map', '0:v:0', '-map', '0:a:0', '-map', '0:a:0',
                                        '-c', 'copy', '-disposition:a:0', '0', '-disposition:a:1', '0',
                                        '-metadata:s:a:0', 'title=Original', '-metadata:s:a:1', 'title=VJ'])
        with self.assertRaisesRegex(RuntimeError, 'Multiple audio tracks'):
            self.prepare(source)
        result = self.prepare(source, {'audio_stream_index': 2})
        self.assertEqual(result.decision.selected_audio_stream_index, 2)
        self.assertEqual(len(result.output_probe.audio_streams), 1)

    def test_portrait_keeps_aspect_ratio(self):
        source = self.fixture('portrait', ['-vf', 'scale=360:640', '-c:v', 'libx264',
                                         '-preset', 'ultrafast', '-threads', '2', '-c:a', 'copy'])
        result = self.prepare(source)
        self.assertEqual(result.method, 'remux')
        self.assertAlmostEqual(result.output_probe.video.width / result.output_probe.video.height, 360/640, places=2)

    @unittest.skipUnless(os.getenv('HANDBRAKE_BINARY') or shutil.which('HandBrakeCLI'), 'HandBrakeCLI required')
    def test_720p_compression_preserves_film_frame_rate(self):
        source = self.fixture('compact', ['-map', '0', '-c', 'copy'])
        result = self.prepare(source, {'mode': '720p_compact'})
        self.assertEqual(result.method, 'video_transcode')
        self.assertLessEqual(result.output_probe.video.height, 720)
        self.assertAlmostEqual(result.output_probe.video.frame_rate, 24000/1001, places=2)
        self.assertTrue(result.output_probe.fast_start)

    def test_mkv_subtitles_do_not_break_remux_or_burn_video(self):
        subtitle = self.root / 'captions.srt'
        subtitle.write_text('1\n00:00:00,000 --> 00:00:02,000\nCaption\n')
        source = self.fixture('subtitles', ['-i', str(subtitle), '-map', '0:v:0', '-map', '0:a:0',
                                          '-map', '1:0', '-c:v', 'copy', '-c:a', 'copy', '-c:s', 'ass'])
        result = self.prepare(source)
        self.assertEqual(result.method, 'remux')
        self.assertEqual(len(result.source_probe.subtitle_streams), 1)
        self.assertEqual(len(result.output_probe.subtitle_streams), 0)
        self.assertFalse(result.decision.encodes_video)

    def test_keep_original_preserves_mkv(self):
        source = self.fixture('original', ['-c', 'copy'])
        result = self.prepare(source, {'mode': 'keep_original'})
        self.assertEqual(result.delivery_path, source)
        self.assertFalse(result.processing['require_compatible'])

    def test_encoder_failure_leaves_no_final_output(self):
        source = self.fixture('failure', ['-c', 'copy'])
        with patch('app.media_processing._run', side_effect=RuntimeError('encoder failed')):
            with self.assertRaisesRegex(RuntimeError, 'encoder failed'):
                self.prepare(source)
        self.assertTrue(source.is_file())
        self.assertFalse((source.parent / 'output.mp4').exists())
        self.assertFalse((source.parent / 'output.part.mp4').exists())

    def test_disguised_playlist_is_rejected_before_following_references(self):
        playlist = self.root / 'playlist.mkv'
        playlist.write_text('#EXTM3U\n#EXT-X-TARGETDURATION:3\n#EXTINF:3,\n' + str(self.base) + '\n#EXT-X-ENDLIST\n')
        with self.assertRaisesRegex(RuntimeError, 'ffprobe failed'):
            probe_media('ffprobe', playlist)

    def test_truncated_output_is_rejected(self):
        probe = probe_media('ffprobe', self.base)
        with self.assertRaisesRegex(RuntimeError, 'duration differs'):
            validate_output(replace(probe, duration_seconds=7200), probe,
                            normalize_processing_options(), None)

    def test_cancelled_before_encode_leaves_source(self):
        source = self.fixture('cancel', ['-c', 'copy'])
        with self.assertRaises(MediaProcessingCancelled):
            prepare_media_for_processing(make_settings(source.parent), source, should_cancel=lambda: True)
        self.assertTrue(source.exists())


class ProfileTests(unittest.TestCase):
    def test_shared_laravel_contract_examples(self):
        fixture = Path(__file__).resolve().parents[1] / 'contracts/processing-v1.json'
        for row in json.loads(fixture.read_text())['examples']:
            self.assertEqual(normalize_processing_options(row['input']), row['expected'])

    def test_untrusted_options_are_rejected(self):
        for options in ({'command': 'touch /tmp/unsafe'}, {'rf': 99}, {'rf': float('nan')},
                        {'encoder_preset': '; echo unsafe'}, {'audio_stream_index': -1},
                        {'mode': []}, {'fast_start': 'false'}, {'rf': True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                normalize_processing_options(options)

    def test_profile_defaults_and_force_rules(self):
        self.assertEqual(normalize_processing_options({'mode': '720p_compact'})['rf'], 26)
        self.assertEqual(normalize_processing_options({'mode': 'auto', 'rf': 29})['rf'], 23)
        self.assertTrue(normalize_processing_options({'mode': 'auto', 'fast_start': False})['fast_start'])


class ChildProcessSafetyTests(unittest.TestCase):
    def test_cancellation_terminates_only_owned_child(self):
        import sys
        import time
        from app.media_processing import _run
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = make_settings(root)
            settings.telescope_disk_reserve_mb = 0
            pid_file = root / 'pid'
            sibling = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
            started = time.monotonic()
            try:
                with self.assertRaises(MediaProcessingCancelled):
                    _run([sys.executable, '-c', 'import os,time,pathlib; pathlib.Path(__import__("sys").argv[1]).write_text(str(os.getpid())); time.sleep(30)', str(pid_file)],
                         settings, 30, 'optimizing_video', None,
                         # Do not race the process startup: this test needs
                         # the PID to prove that only the owned child died.
                         lambda: pid_file.exists() and time.monotonic() - started > .5, None, root)
                self.assertIsNone(sibling.poll())
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pid_file.read_text()), 0)
            finally:
                sibling.terminate()
                sibling.wait(timeout=5)
