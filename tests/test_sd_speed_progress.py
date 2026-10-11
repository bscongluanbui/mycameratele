"""Download bandwidth controls and live bytes never change recording contents."""
import ctypes as C
from datetime import timedelta
import io
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from archive_app.core import Archive, Settings
from archive_app.sd_source import (HCNetSDKSource, ISAPISource, SDSource,
                                   SDSourceError, _NativeSession, _native_worker, get_zone)
from tests.test_sd_source import FakeSDK, FakeSource, NOW, SDKFunction, recording


class NativeSpeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sdk = FakeSDK()
        self.controls = []
        self.speed_error = None
        self.start_ok = True
        def control(handle, command, pointer, length, output, out_length):
            value = C.cast(pointer, C.POINTER(C.c_uint32)).contents.value
            self.controls.append((command, length, value))
            if command == 24 and self.speed_error is not None:
                self.sdk.error = self.speed_error
                return 0
            return int(command != 1 or self.start_ok)
        self.sdk.NET_DVR_PlayBackControl_V40 = SDKFunction(control)

    def provider(self, speed=0):
        return _NativeSession('192.168.2.7', 8000, 'admin', 'fixture-secret', 1,
                              get_zone('UTC+07:00'), 1024, sdk=(self.sdk, self.root),
                              download_speed_kbps=speed)

    def download(self, provider, filename='one.part', **metadata):
        self.sdk.progress = [100]
        return provider.download(recording(**metadata), self.root / filename)

    def test_default_never_sends_speed_or_transcoding_commands(self):
        with self.provider() as provider:
            self.assertEqual(self.download(provider), 8)
            self.assertEqual(provider.download_speed_status, 'off')
        self.assertEqual(self.controls, [(1, 4, 0)])

    def test_speed_uses_playstart_then_command24_four_byte_dword_kbps(self):
        snapshots = []
        with self.provider(8192) as provider:
            provider.download_progress = snapshots.append
            self.assertEqual(self.download(provider), 8)
            self.assertEqual(provider.download_speed_status, 'applied')
        self.assertEqual(self.controls, [(1, 4, 0), (24, 4, 8192)])
        self.assertEqual((self.root/'one.part').read_bytes(), b'fixture8')
        self.assertEqual(snapshots, [{'download_speed_requested_kbps': 8192,
                                     'download_speed_status': 'applied',
                                     'download_speed_error_code': None}])

    def test_successful_setting_is_applied_to_each_new_download_handle(self):
        with self.provider(32768) as provider:
            self.download(provider)
            self.download(provider, 'two.part')
        self.assertEqual([command for command, _, _ in self.controls], [1, 24, 1, 24])

    def test_rejection_falls_back_once_for_session_without_redownload(self):
        for code, status in ((23, 'unsupported'), (10, 'rejected')):
            with self.subTest(code=code):
                self.controls.clear()
                self.sdk.calls.clear()
                self.speed_error = code
                with self.provider(4096) as provider:
                    self.download(provider, f'one-{code}.part')
                    self.download(provider, f'two-{code}.part')
                    self.assertEqual(provider.download_speed_status, status)
                    self.assertEqual(provider.download_speed_error_code, code)
                self.assertEqual([command for command, _, _ in self.controls], [1, 24, 1])
                self.assertEqual(self.sdk.calls.count('download'), 2)
                self.assertEqual(self.sdk.calls.count('download_stop'), 2)

    def test_speed_rejection_does_not_hide_transfer_failure(self):
        self.speed_error, self.sdk.progress = 23, [200]
        with self.provider(4096) as provider, self.assertRaises(SDSourceError) as caught:
            provider.download(recording(), self.root/'failure.part')
        self.assertEqual(caught.exception.code, 'sd_native_unsupported')
        self.assertIn('download_stop', self.sdk.calls)

    def test_failed_playstart_never_sets_speed_and_always_stops(self):
        self.start_ok, self.sdk.error = False, 10
        with self.provider(4096) as provider, self.assertRaises(SDSourceError):
            self.download(provider)
        self.assertEqual([command for command, _, _ in self.controls], [1])
        self.assertIn('download_stop', self.sdk.calls)

    def test_speed_mode_keeps_size_validation(self):
        with self.provider(4096) as provider, self.assertRaises(SDSourceError) as caught:
            self.download(provider, size=9)
        self.assertEqual(caught.exception.code, 'sd_download_incomplete')
        self.assertIn('download_stop', self.sdk.calls)

    def test_speed_validation_is_strict_and_bounded(self):
        for value in (True, '8192', -1, 1, 255, 32769, None):
            with self.subTest(value=value), self.assertRaises(SDSourceError) as caught:
                self.provider(value)
            self.assertEqual(caught.exception.code, 'sd_config_invalid')
        for value in (0, 256, 32768):
            self.assertEqual(self.provider(value).download_speed_requested_kbps, value)

    def test_worker_config_propagates_speed_and_fixed_status_event(self):
        stage = self.root/'sd-stage'/'CAM'
        stage.mkdir(parents=True)
        destination = stage/'one.part'
        self.sdk.progress = [100]
        requests = [
            {'command': 'init', 'address': '192.168.2.7', 'port': 8000,
             'username': 'admin', 'password': 'fixture-secret', 'channel': 1,
             'timezone': 'UTC+07:00', 'max_bytes': 1024, 'download_speed_kbps': 8192},
            {'command': 'download', 'recording': recording(), 'destination': str(destination), 'max_bytes': 1024},
            {'command': 'close'}]
        stream = Mock()
        stream.buffer = io.BytesIO(('\n'.join(json.dumps(item) for item in requests)+'\n').encode())
        output = io.StringIO()
        with patch('archive_app.sd_source.sys.stdin', stream), patch('archive_app.sd_source.sys.stdout', output), \
                patch('archive_app.sd_source.sys.platform', 'win32'), \
                patch('archive_app.sd_source._sdk_library', return_value=(self.sdk, self.root)), \
                patch.dict(os.environ, {'CACHE_DIR': str(self.root)}):
            _native_worker()
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(messages[1], {'event': 'download_status', 'download_speed_requested_kbps': 8192,
                                      'download_speed_status': 'applied', 'download_speed_error_code': None})
        self.assertEqual(messages[2], {'ok': True, 'result': 8})
        self.assertNotIn('fixture-secret', output.getvalue())
        self.assertEqual(self.controls, [(1, 4, 0), (24, 4, 8192)])


class NativeLiveProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def child(self, download_body, timeout=5):
        script = self.root/'helper.py'
        script.write_text('''import json,sys,time
request=json.loads(sys.stdin.readline())
assert request['download_speed_kbps']==8192
print(json.dumps({'ok':True,'result':None}),flush=True)
for line in sys.stdin:
    item=json.loads(line)
    if item['command']=='close': break
    if item['command']=='download':
''' + ''.join('        '+line+'\n' for line in download_body.splitlines()), encoding='utf-8')
        return HCNetSDKSource('192.168.2.7', 8000, 'admin', 'fixture-secret', 1,
                             get_zone('UTC+07:00'), 1024,
                             worker_command=[sys.executable, str(script)],
                             session_timeout=timeout, download_speed_kbps=8192)

    def test_partial_bytes_advance_before_final_response_without_import(self):
        provider = self.child("""print(json.dumps({'event':'download_status','download_speed_status':'applied','download_speed_error_code':None}),flush=True)
with open(item['destination'],'wb',buffering=0) as output:
    for chunk in (b'1234',b'5678',b'9012'):
        output.write(chunk)
        time.sleep(.12)
print(json.dumps({'ok':True,'result':12}),flush=True)""")
        snapshots = []
        provider.download_progress = snapshots.append
        with patch('archive_app.sd_source.DOWNLOAD_PROGRESS_INTERVAL', .04), provider:
            self.assertEqual(provider.download(recording(size=12), self.root/'file.part'), 12)
        sizes = [item['current_file_bytes'] for item in snapshots]
        self.assertIn(4, sizes)
        self.assertIn(8, sizes)
        self.assertEqual(sizes[-1], 12)
        self.assertTrue(all(item['current_file_total_bytes'] == 12 for item in snapshots))
        self.assertTrue(any(item['current_file_bytes_per_second'] > 0 for item in snapshots))
        self.assertEqual(snapshots[-1]['download_speed_status'], 'applied')

    def test_progress_never_renews_operation_deadline(self):
        # Exercise the real parent download/exchange loop without making its
        # deadline depend on Python/QEMU child startup or scheduler latency.
        provider = HCNetSDKSource('192.168.2.7', 8000, 'admin', 'fixture-secret', 1,
                                 get_zone('UTC+07:00'), 1024,
                                 worker_command=['fixture-native-helper'],
                                 session_timeout=.2, download_speed_kbps=8192)
        process = Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        provider.process = process
        destination = self.root/'hung.part'
        clock = [100.0]
        waits = []
        snapshots = []
        def progress(snapshot):
            snapshots.append(snapshot)
            clock[0] += .004  # Progress work also consumes the original budget.
        def wait_for_response(*, timeout):
            waits.append(timeout)
            if len(waits) > 20:
                self.fail('Progress renewed the original operation deadline')
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, .025)
            clock[0] += timeout
            with destination.open('ab') as output:
                output.write(b'x')  # The helper stays active without completing.
            raise queue.Empty
        provider.download_progress = progress
        provider.inbox = Mock()
        provider.inbox.get.side_effect = wait_for_response
        with patch('archive_app.sd_source.time.monotonic', side_effect=lambda: clock[0]), \
                patch('archive_app.sd_source.DOWNLOAD_PROGRESS_INTERVAL', .025), \
                self.assertRaises(SDSourceError) as caught:
            provider.download(recording(size=300), destination)
        self.assertEqual(caught.exception.code, 'sd_native_worker_timeout')
        self.assertGreaterEqual(clock[0]-100, .2-1e-9)
        self.assertLessEqual(clock[0]-100, .204+1e-9)
        self.assertGreater(len(snapshots), 2)
        self.assertGreater(snapshots[-1]['current_file_bytes'], snapshots[0]['current_file_bytes'])
        self.assertEqual(destination.stat().st_size, len(waits))
        self.assertLess(destination.stat().st_size, 300)
        self.assertIsNone(provider.process)
        self.assertEqual(provider.config['password'], '')
        process.stdin.close.assert_called_once()
        process.stdout.close.assert_called_once()

    def test_unrecognized_status_event_is_not_exposed(self):
        provider = self.child("""print(json.dumps({'event':'download_status','download_speed_status':'fixture-secret','download_speed_error_code':23}),flush=True)""")
        with self.assertRaises(SDSourceError) as caught:
            with provider:
                provider.download(recording(), self.root/'bad.part')
        self.assertEqual(caught.exception.code, 'sd_native_worker_failed')
        self.assertNotIn('fixture-secret', str(caught.exception))

    def test_duplicate_status_event_is_rejected_without_renewing_deadline(self):
        provider = self.child("""event={'event':'download_status','download_speed_status':'applied','download_speed_error_code':None}
print(json.dumps(event),flush=True)
print(json.dumps(event),flush=True)""")
        with self.assertRaises(SDSourceError) as caught:
            with provider:
                provider.download(recording(), self.root/'duplicate.part')
        self.assertEqual(caught.exception.code, 'sd_native_worker_failed')


class ISAPILiveProgressTests(unittest.TestCase):
    def test_progress_chunks_report_bytes_but_not_completed_media(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)/'video.part'
            provider = ISAPISource('192.168.2.7', 80, 'admin', 'fixture-secret', 1, get_zone('UTC'), 1024)
            response = Mock()
            response.headers = {'Content-Type': 'application/octet-stream', 'Content-Length': '8'}
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read1.side_effect = [b'1234', b'5678', b'']
            snapshots = []
            provider.download_progress = snapshots.append
            with patch.object(provider, '_request', return_value=response):
                self.assertEqual(provider.download(dict(recording(), playback_uri='rtsp://192.168.2.7/x'), destination), 8)
            self.assertEqual(snapshots[0]['current_file_bytes'], 4)
            self.assertEqual(snapshots[-1]['current_file_bytes'], 8)
            self.assertEqual(destination.read_bytes(), b'12345678')


class SDProgressOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        settings = Settings(self.root/'data', self.root/'cache', self.root/'input', 'UTC+07:00',
                            min_free_bytes=0, cache_max_bytes=1024*1024)
        settings.input_dir.mkdir()
        self.archive = Archive(settings)
        self.addCleanup(self.archive.conn.close)
        self.archive.add_camera({'id': 'PN', 'name': 'Phòng ngủ', 'host': '192.168.2.7'})
        self.config = {'id': 'PN', 'host': '192.168.2.7', 'sd_password': 'fixture-secret'}

    def normalize(self, source, destination, settings):
        destination.write_bytes(Path(source).read_bytes())
        return {'duration': 60, 'codec_video': 'h264', 'codec_audio': None, 'bytes': 8}

    def sync(self, provider, **kwargs):
        with patch.object(SDSource, '_provider', return_value=provider), \
                patch('archive_app.core.normalize', side_effect=self.normalize):
            return SDSource(self.archive, 'PN', self.config).sync(now=NOW, **kwargs)

    def test_partial_progress_does_not_import_or_count_completed_bytes(self):
        provider = FakeSource()
        snapshots = []
        def download(item, path):
            path.write_bytes(b'fixt')
            provider.download_progress({'current_file_bytes': 4, 'current_file_total_bytes': 8,
                                        'current_file_elapsed_seconds': 2, 'current_file_bytes_per_second': 2})
            self.assertEqual(self.archive.conn.execute('SELECT COUNT(*) FROM recordings').fetchone()[0], 0)
            path.write_bytes(b'fixture8')
        provider.download = download
        with patch('archive_app.sd_source.time.monotonic', side_effect=range(10000)):
            result = self.sync(provider, progress=snapshots.append)
        active = [item for item in snapshots if item['current_file_bytes'] == 4]
        self.assertTrue(active)
        self.assertEqual((active[0]['download_bytes'], active[0]['downloaded'], active[0]['imported']), (0, 0, 0))
        self.assertEqual((result['download_bytes'], result['downloaded'], result['imported']), (8, 1, 1))
        self.assertTrue(all(result[field] == 0 for field in ('current_file_bytes', 'current_file_total_bytes',
                                                          'current_file_elapsed_seconds', 'current_file_bytes_per_second')))

    def test_stop_request_finishes_current_file_and_stops_before_next(self):
        provider = FakeSource(files=[recording(rid=f'f{i}', start=f'2026-10-03T10:00:0{i}+07:00') for i in range(3)])
        stopped = False
        def progress(snapshot):
            nonlocal stopped
            if snapshot['imported'] == 1:
                stopped = True
        result = self.sync(provider, progress=progress, stop_requested=lambda: stopped)
        self.assertEqual((result['imported'], result['backlog'], result['interrupted']), (1, 2, True))
        self.assertEqual(len(provider.downloads), 1)

    def test_session_start_preserves_other_camera_active_partial(self):
        stage = self.archive.settings.cache_dir/'sd-stage'/'OTHER_CAM'
        stage.mkdir(parents=True)
        partial = stage/('a'*64+'.'+'b'*32+'.part')
        partial.write_bytes(b'active-worker')
        self.sync(FakeSource(files=[]))
        self.assertEqual(partial.read_bytes(), b'active-worker')

    def test_configured_speed_is_forwarded_to_native_source(self):
        self.archive.settings.sd_download_speed_kbps = 8192
        with patch('archive_app.sd_source._private_address', return_value='192.168.2.7'), \
                patch('archive_app.sd_source.HCNetSDKSource') as factory:
            SDSource(self.archive, 'PN', self.config)._provider()
        self.assertEqual(factory.call_args.kwargs['download_speed_kbps'], 8192)

    def test_concurrent_confirmed_upload_cleanup_does_not_fail_sd_ingest(self):
        original = self.archive.ingest_download
        def ingest_and_cleanup(entry, path):
            result = original(entry, path)
            # Represents cleanup after a parallel uploader has stored its
            # receipt. Source removal must be idempotent in the SD worker.
            path.unlink()
            return result
        with patch.object(self.archive, 'ingest_download', side_effect=ingest_and_cleanup):
            result = self.sync(FakeSource())
        self.assertEqual((result['downloaded'], result['imported']), (1, 1))


if __name__ == '__main__':
    unittest.main()
