from __future__ import annotations
import asyncio
from dataclasses import replace
import fcntl
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

from app.db import StateStore
from app.telescope import TelescopeScheduler, TelescopeDiskCapacityError
from app.object_storage import ObjectStorageManager
from test_telescope import target
from test_web_and_video import make_settings


class FakeUploader:
    def __init__(self):
        self.fail = False
        self.calls = 0
        self.object = None

    async def _verified_result(self, *args, **kwargs):
        return self.object

    async def upload(self, chunks, **kwargs):
        self.calls += 1
        body = b''.join([chunk async for chunk in chunks])
        if self.fail:
            raise RuntimeError('Simulated upload failure')
        if len(body) != kwargs['total_bytes']:
            raise RuntimeError('Wrong upload size')
        if kwargs['mime_type'] != 'video/mp4':
            raise RuntimeError('Wrong media MIME')
        self.object = {'storage_target':'demo','bucket':'movies','object_key':kwargs['object_key'],
                       'public_url':'https://cdn.example/'+kwargs['object_key'], 'bytes':len(body), 'mime_type':'video/mp4'}
        await kwargs['verify_callback']()
        return self.object

    async def abort(self, *args):
        pass


class StagedPipelineTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_temp = tempfile.TemporaryDirectory()
        cls.media = Path(cls.media_temp.name) / 'fixture.mkv'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=24',
                        '-t','1','-c:v','libx264','-threads','2','-preset','ultrafast',str(cls.media)],check=True)
        cls.body = cls.media.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.media_temp.cleanup()

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(self.root)
        self.settings.telescope_enabled = True
        self.settings.telescope_disk_reserve_mb = 0
        self.settings.telescope_progress_interval_seconds = .01
        self.store = StateStore(str(self.root / 'state.db'))
        await self.store.init()
        self.downloads = 0
        owner = self
        class Stream:
            def __init__(self, offset):
                self.offset = offset
                self.sent = False
            def __aiter__(self): return self
            async def __anext__(self):
                if self.sent: raise StopAsyncIteration
                self.sent = True
                return owner.body[self.offset:]
            async def close(self): pass
        def download(*args, **kwargs):
            self.downloads += 1
            return Stream(kwargs.get('offset',0))
        self.message = SimpleNamespace(file=SimpleNamespace(size=len(self.body),mime_type='application/octet-stream'),media=object())
        worker = SimpleNamespace(settings=self.settings, client=SimpleNamespace(iter_download=download),
                                 resolve_message_reference=AsyncMock(return_value=(object(),self.message)),
                                 _is_supported_media=lambda _: True, _extract_file_name=lambda _: '../../movie.mkv')
        self.scheduler = TelescopeScheduler(worker,self.store,ObjectStorageManager({'demo':replace(target('demo'),capacity_bytes=0)}))
        self.uploader = FakeUploader()
        self.scheduler._uploader = lambda _:self.uploader
        self.location_patch = patch('app.telescope.telethon_utils.get_input_location',return_value=(1,object()))
        self.location_patch.start()
        self.job = await self.scheduler.submit('https://t.me/demo/1',storage_target='demo',processing={'mode':'auto'})
        self.job_id = self.job['job_id']

    async def asyncTearDown(self):
        self.location_patch.stop()
        await self.scheduler.stop()
        self.temp.cleanup()

    async def run_job(self):
        await self.store.next_queued_telescope_jobs(1)
        await self.scheduler._run_job(self.job_id)
        return await self.store.get_telescope_job(self.job_id)

    async def test_upload_retry_reuses_download_and_validated_output(self):
        self.uploader.fail = True
        with self.assertLogs('telebot.telescope',level='ERROR'):
            failed = await self.run_job()
        self.assertEqual(failed['status'],'failed')
        self.assertTrue(failed['checkpoint']['output_validated'])
        self.assertTrue((self.scheduler._job_dir(self.job_id)/'source.media').exists())
        self.uploader.fail = False
        await self.scheduler.retry(self.job_id)
        with patch('app.media_processing._run',side_effect=AssertionError('Must reuse encoded output')):
            ready = await self.run_job()
        self.assertEqual(ready['status'],'ready')
        self.assertEqual(self.downloads,1)
        self.assertEqual(self.uploader.calls,2)
        self.assertEqual(ready['mime_type'],'video/mp4')
        self.assertTrue(ready['processing_result']['reused_output'])

    async def test_changed_output_identity_never_reuses_old_multipart_parts(self):
        self.uploader.fail = True
        with self.assertLogs('telebot.telescope', level='ERROR'):
            failed = await self.run_job()
        checkpoint = dict(failed['checkpoint'], output_sha256='different-output')
        await self.store.update_telescope_job(self.job_id, checkpoint_json=checkpoint,
            multipart_upload_id='old-upload', multipart_parts_json=[{'PartNumber': 1, 'ETag': 'old'}], bytes_transferred=8)
        self.uploader.fail = False
        self.uploader.abort = AsyncMock()
        await self.scheduler.retry(self.job_id)
        ready = await self.run_job()
        self.assertEqual(ready['status'], 'ready')
        self.uploader.abort.assert_awaited_once()
        self.assertEqual(self.downloads, 1)

    async def test_restart_after_object_commit_avoids_reupload(self):
        ready = await self.run_job()
        self.assertEqual(ready['status'],'ready')
        await self.store.update_telescope_job(self.job_id,status='uploading',result_json=None)
        reopened = StateStore(str(self.root/'state.db'))
        await reopened.init()
        await reopened.recover_telescope_jobs()
        self.scheduler.store = reopened
        with patch('app.telescope.prepare_media_for_processing',side_effect=AssertionError('Object already validated')):
            ready = await self.run_job()
        self.assertEqual(ready['status'],'ready')
        self.assertEqual(self.uploader.calls,1)
        self.assertEqual(self.downloads,1)

    async def test_encode_failure_retry_uses_download_checkpoint(self):
        with patch('app.media_processing._run',side_effect=RuntimeError('encoder failure')):
            with self.assertLogs('telebot.telescope',level='ERROR'):
                failed = await self.run_job()
        self.assertEqual(failed['status'],'failed')
        self.assertFalse(failed['checkpoint'].get('output_validated',False))
        self.assertEqual(self.uploader.calls,0)
        await self.scheduler.retry(self.job_id)
        ready = await self.run_job()
        self.assertEqual(ready['status'],'ready')
        self.assertEqual(self.downloads,1)

    async def test_disk_reservations_include_concurrent_jobs(self):
        self.scheduler.work_root.mkdir(parents=True,exist_ok=True)
        with patch('app.telescope.shutil.disk_usage',return_value=SimpleNamespace(free=500)):
            self.scheduler._reserve_disk('first',100)
            with self.assertRaises(TelescopeDiskCapacityError):
                self.scheduler._reserve_disk('second',100)

    async def test_second_scheduler_cannot_recover_owned_jobs(self):
        self.scheduler._dispatch_loop = AsyncMock(side_effect=lambda: asyncio.sleep(0))
        # Use a real lock without dispatching this fixture job.
        lock_path = Path(self.store.db_path).with_suffix('.telescope.lock')
        with lock_path.open('a+b') as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError,'Another Telescope'):
                await self.scheduler.start()
        unchanged = await self.store.get_telescope_job(self.job_id)
        self.assertEqual(unchanged['status'],'queued')

    async def test_cancel_queued_job_is_durable(self):
        await self.scheduler.cancel(self.job_id)
        job = await self.store.get_telescope_job(self.job_id)
        self.assertEqual(job['status'],'cancelled')
        await self.store.recover_telescope_jobs()
        self.assertEqual(await self.store.next_queued_telescope_jobs(1),[])

    async def test_attempt_specific_callbacks_survive_manual_retry(self):
        self.settings.telescope_callback_url='https://portal.example/callback'
        self.settings.telescope_callback_secret='secret'
        await self.store.update_telescope_job(self.job_id,portal_source_id='42',status='failed',attempts=1)
        await self.scheduler._emit_event(await self.store.get_telescope_job(self.job_id),'failed')
        await self.store.update_telescope_job(self.job_id,attempts=2)
        await self.scheduler._emit_event(await self.store.get_telescope_job(self.job_id),'failed')
        events = await self.store.due_telescope_callbacks()
        self.assertEqual(len(events),2)
        self.assertEqual([e['payload']['attempt'] for e in events],[1,2])
