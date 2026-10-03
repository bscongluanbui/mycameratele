"""SD protocol tests use loopback Digest HTTP and synthetic native SDK doubles.

No real camera credentials/recordings are used. Native child crash/timeout tests
launch real subprocesses so failures do not terminate the archive worker.
"""
import ctypes as C
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from unittest.mock import Mock
from urllib.error import HTTPError
import xml.etree.ElementTree as ET

from archive_app.core import Archive, Settings, record_key
from archive_app.sd_source import (DeviceInfo, DVRTime, FileCondition, FindData,
    HCNetSDKSource, ISAPISource, SDSource, SDSourceError, _NativeSession,
    _private_address, _sdk_library, _xml, get_zone, recover_staging, _native_worker)


NOW = datetime(2026, 10, 3, 5, 0, tzinfo=timezone.utc)


def recording(rid='ch01_00001', start='2026-10-03T10:00:00+07:00',
              end='2026-10-03T10:01:00+07:00', size=8):
    return {'record_id': rid, 'start_time': start, 'end_time': end, 'size': size}


def result_xml(items=(), status='OK', namespace=True, response=True):
    attributes = {'xmlns': 'http://www.hikvision.com/ver20/XMLSchema'} if namespace else {}
    root = ET.Element('CMSearchResult', attributes)
    ET.SubElement(root, 'responseStatus').text = str(response).lower()
    ET.SubElement(root, 'responseStatusStrg').text = status
    matches = ET.SubElement(root, 'matchList')
    for item in items:
        match = ET.SubElement(matches, 'searchMatchItem')
        ET.SubElement(match, 'trackID').text = '101'
        span = ET.SubElement(match, 'timeSpan')
        ET.SubElement(span, 'startTime').text = item['start_time']
        ET.SubElement(span, 'endTime').text = item['end_time']
        description = ET.SubElement(match, 'mediaSegmentDescriptor')
        ET.SubElement(description, 'playbackURI').text = item.get('uri',
            f"rtsp://127.0.0.1/Streaming/tracks/101?name={item['record_id']}&size={item['size']}")
    return ET.tostring(root)


class DigestFixture:
    def __init__(self):
        fixture = self
        self.requests, self.authorizations = [], []
        self.results = [result_xml([recording()])]
        self.media = b'fixture8'
        self.content_type = 'application/octet-stream'
        self.length = None
        self.status = 200
        self.close_early = False

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.respond()

            def do_GET(self):
                self.respond()

            def respond(self):
                data = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                auth = self.headers.get('Authorization', '')
                fixture.authorizations.append(auth)
                fields = {
                    match[0]: match[1] or match[2] for match in re.findall(r'(\w+)=(?:"([^"]*)"|([^, ]+))', auth)}
                uri = fields.get('uri', '')
                ha1 = hashlib.md5(b'admin:SDfixture:synthetic-camera-secret').hexdigest()
                ha2 = hashlib.md5(f'{self.command}:{uri}'.encode()).hexdigest()
                expected = hashlib.md5(f"{ha1}:nonce:{fields.get('nc')}:{fields.get('cnonce')}:auth:{ha2}".encode()).hexdigest()
                if not auth.startswith('Digest ') or fields.get('response') != expected or uri != self.path:
                    self.send_response(401)
                    self.send_header('WWW-Authenticate', 'Digest realm="SDfixture", nonce="nonce", algorithm=MD5, qop="auth"')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                fixture.requests.append((self.command, self.path, data))
                self.send_response(fixture.status)
                if self.path.endswith('/search'):
                    position = next((node.text for node in ET.fromstring(data).iter() if node.tag.rsplit('}', 1)[-1] == 'searchResultPostion'), '0')
                    page = min(int(position) // 40, len(fixture.results) - 1)
                    body = fixture.results[page]
                    self.send_header('Content-Type', 'application/xml')
                    self.send_header('Content-Length', str(len(body)))
                else:
                    body = fixture.media
                    self.send_header('Content-Type', fixture.content_type)
                    self.send_header('Content-Length', str(fixture.length if fixture.length is not None else len(body)))
                self.end_headers()
                self.wfile.write(body)
                if fixture.close_early:
                    self.close_connection = True

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def provider(self, **kwargs):
        return ISAPISource('127.0.0.1', self.server.server_address[1], 'admin',
                           'synthetic-camera-secret', 1, get_zone('UTC+07:00'), kwargs.get('max_bytes', 1024))


class ISAPIProtocolTests(unittest.TestCase):
    def setUp(self):
        self.fixture = DigestFixture()
        self.addCleanup(self.fixture.close)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'download.part'

    def test_real_digest_search_and_download(self):
        provider = self.fixture.provider()
        matches = provider.search(NOW - timedelta(days=1), NOW)
        self.assertEqual(matches[0]['record_id'], 'ch01_00001')
        self.assertEqual(provider.download(matches[0], self.path), 8)
        self.assertEqual(self.path.read_bytes(), b'fixture8')
        self.assertTrue(any(value.startswith('Digest ') for value in self.fixture.authorizations))
        self.assertFalse(any('synthetic-camera-secret' in value or value.startswith('Basic ') for value in self.fixture.authorizations))
        self.assertEqual([(method, path) for method, path, _ in self.fixture.requests],
                         [('POST', '/ISAPI/ContentMgmt/search'), ('GET', '/ISAPI/ContentMgmt/download')])
        xml = ET.fromstring(self.fixture.requests[0][2])
        self.assertEqual(next(node.text for node in xml.iter() if node.tag.endswith('trackID')), '101')
        self.assertEqual(next(node.text for node in xml.iter() if node.tag.endswith('startTime')), '2026-10-02T05:00:00Z')

    def test_no_matches_requires_genuine_search_result(self):
        self.fixture.results = [result_xml(status='NO MATCHES', response=False)]
        self.assertEqual(self.fixture.provider().search(NOW-timedelta(hours=1), NOW), [])
        self.fixture.results = [b'<ResponseStatus><statusCode>1</statusCode></ResponseStatus>']
        with self.assertRaises(SDSourceError) as caught:
            self.fixture.provider().search(NOW-timedelta(hours=1), NOW)
        self.assertEqual(caught.exception.code, 'sd_isapi_unsupported')

    def test_namespaceless_result_is_accepted(self):
        self.fixture.results = [result_xml([recording()], namespace=False)]
        self.assertEqual(len(self.fixture.provider().search(NOW-timedelta(days=1), NOW)), 1)

    def test_pagination_and_duplicate_page_rejected(self):
        first = [recording(f'file{i}', start=f'2026-10-03T10:00:{i:02d}+07:00') for i in range(40)]
        self.fixture.results = [result_xml(first, 'MORE'), result_xml([recording('next')])]
        self.assertEqual(len(self.fixture.provider().search(NOW-timedelta(days=1), NOW)), 41)
        self.fixture.results = [result_xml(first, 'MORE'), result_xml(first, 'MORE')]
        with self.assertRaises(SDSourceError) as caught:
            self.fixture.provider().search(NOW-timedelta(days=1), NOW)
        self.assertEqual(caught.exception.code, 'sd_protocol_error')

    def test_invalid_playback_uri_is_not_requested(self):
        for uri in ('rtsp://outside.example/video?name=x', 'rtsp://admin:secret@127.0.0.1/video?name=x', 'http://127.0.0.1/video?name=x'):
            self.fixture.results = [result_xml([{**recording(), 'uri': uri}])]
            with self.subTest(uri=uri), self.assertRaises(SDSourceError) as caught:
                self.fixture.provider().search(NOW-timedelta(days=1), NOW)
            self.assertEqual(caught.exception.code, 'sd_protocol_error')

    def test_error_status_never_contains_credentials(self):
        for status, code in ((403, 'sd_auth_failed'), (404, 'sd_isapi_unsupported'), (500, 'sd_http_error')):
            self.fixture.status = status
            with self.subTest(status=status), self.assertRaises(SDSourceError) as caught:
                self.fixture.provider().search(NOW-timedelta(hours=1), NOW)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn('synthetic-camera-secret', str(caught.exception))

    def test_status_document_download_rejected(self):
        self.fixture.content_type = 'application/xml'
        with self.assertRaises(SDSourceError) as caught:
            self.fixture.provider().download({**recording(), 'playback_uri': 'rtsp://127.0.0.1/video'}, self.path)
        self.assertEqual(caught.exception.code, 'sd_download_failed')
        self.assertFalse(self.path.exists())

    def test_download_size_bounds_and_metadata_mismatch(self):
        with self.assertRaises(SDSourceError) as caught:
            self.fixture.provider(max_bytes=7).download({**recording(), 'playback_uri': 'rtsp://127.0.0.1/video'}, self.path)
        self.assertEqual(caught.exception.code, 'sd_size_limit')
        with self.assertRaises(SDSourceError) as caught:
            self.fixture.provider().download({**recording(size=9), 'playback_uri': 'rtsp://127.0.0.1/video'}, self.path)
        self.assertEqual(caught.exception.code, 'sd_download_incomplete')

    def test_declared_length_larger_than_transfer_rejected(self):
        self.fixture.length, self.fixture.close_early = 9, True
        with self.assertRaises(SDSourceError) as caught:
            self.fixture.provider().download({**recording(), 'playback_uri': 'rtsp://127.0.0.1/video'}, self.path)
        self.assertEqual(caught.exception.code, 'sd_download_incomplete')

    def test_entity_and_oversized_xml_rejected(self):
        for raw in (b'<!DOCTYPE a [<!ENTITY x "secret">]><a>&x;</a>',
                    '<!DOCTYPE a [<!ENTITY x "secret">]><a>&x;</a>'.encode('utf-16'),
                    b' ' * 2_000_001, b'not XML'):
            with self.subTest(length=len(raw)), self.assertRaises(SDSourceError):
                _xml(raw)


class SDKFunction:
    def __init__(self, implementation):
        self.implementation = implementation

    def __call__(self, *arguments):
        return self.implementation(*arguments)


class FakeSDK:
    def __init__(self):
        self.calls, self.error, self.search_states, self.progress = [], 0, [1000, 1003], [0, 100]
        self.media, self.size, self.login = b'fixture8', 8, 7
        def constant(name, value):
            return SDKFunction(lambda *args: self.calls.append(name) or value)
        self.NET_DVR_Init = constant('init', 1)
        self.NET_DVR_Cleanup = constant('cleanup', 1)
        self.NET_DVR_GetLastError = SDKFunction(lambda: self.error)
        self.NET_DVR_Login_V30 = SDKFunction(lambda *args: self.calls.append('login') or self.login)
        self.NET_DVR_Logout = constant('logout', 1)
        self.NET_DVR_FindFile_V30 = SDKFunction(self.find)
        self.NET_DVR_FindNextFile_V30 = SDKFunction(self.next_file)
        self.NET_DVR_FindClose_V30 = constant('find_close', 1)
        self.NET_DVR_GetFileByName = SDKFunction(self.download)
        self.NET_DVR_PlayBackControl_V40 = constant('play_start', 1)
        self.NET_DVR_GetDownloadPos = SDKFunction(lambda handle: self.progress.pop(0))
        self.NET_DVR_StopGetFile = constant('download_stop', 1)

    def find(self, user, condition):
        self.calls.append('find')
        self.condition = FileCondition.from_buffer_copy(C.string_at(condition, C.sizeof(FileCondition)))
        return 11

    def next_file(self, handle, pointer):
        state = self.search_states.pop(0)
        if state == 1000:
            value = C.cast(pointer, C.POINTER(FindData)).contents
            value.filename = b'ch01_00001'
            value.start = DVRTime(2026, 10, 3, 10, 0, 0)
            value.end = DVRTime(2026, 10, 3, 10, 1, 0)
            value.size = self.size
        return state

    def download(self, user, remote, destination):
        self.calls.append('download')
        Path(os.fsdecode(destination)).write_bytes(self.media)
        return 19


class NativeSDKTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sdk = FakeSDK()

    def provider(self):
        return _NativeSession('192.168.31.166', 8000, 'admin', 'synthetic-camera-secret',
                              1, get_zone('UTC+07:00'), 1024, sdk=(self.sdk, self.root))

    def test_abi_is_platform_independent_32bit_fields(self):
        self.assertEqual([C.sizeof(item) for item in (DVRTime, FileCondition, FindData, DeviceInfo)], [24, 96, 188, 80])
        self.assertEqual(FileCondition.start.offset, 48)
        self.assertEqual(FindData.start.offset, 100)
        self.assertEqual(FindData.size.offset, 148)
        self.assertEqual(DeviceInfo.type.offset, 62)
        self.assertEqual(DeviceInfo.start_mirror.offset, 76)

    def test_search_download_stops_and_logs_out(self):
        with self.provider() as provider:
            files = provider.search(NOW-timedelta(hours=1), NOW)
            self.assertEqual(files[0]['start_time'], '2026-10-03T10:00:00+07:00')
            self.assertEqual(self.sdk.condition.channel, 1)
            self.assertEqual(self.sdk.condition.file_type, 255)
            self.assertEqual(self.sdk.condition.start.hour, 11)  # UTC04 -> camera UTC+7
            self.assertEqual(provider.download(files[0], self.root/'one.source'), 8)
        self.assertEqual(self.sdk.calls, ['init', 'login', 'find', 'find_close', 'download', 'play_start', 'download_stop', 'logout', 'cleanup'])
        self.assertEqual(self.sdk.NET_DVR_Login_V30.restype, C.c_int32)

    def test_login_rejection_cleans_sdk(self):
        self.sdk.login, self.sdk.error = -1, 1
        with self.assertRaises(SDSourceError) as caught:
            with self.provider():
                pass
        self.assertEqual(caught.exception.code, 'sd_auth_failed')
        self.assertEqual(self.sdk.calls, ['init', 'login', 'cleanup'])

    def test_search_exception_closes_handle(self):
        self.sdk.search_states = [1004]
        self.sdk.error = 23
        with self.provider() as provider, self.assertRaises(SDSourceError) as caught:
            provider.search(NOW-timedelta(hours=1), NOW)
        self.assertEqual(caught.exception.code, 'sd_native_unsupported')
        self.assertIn('find_close', self.sdk.calls)

    def test_download_network_failure_stops_handle(self):
        self.sdk.progress, self.sdk.error = [200], 10
        with self.provider() as provider, self.assertRaises(SDSourceError) as caught:
            provider.download(recording(), self.root/'one.source')
        self.assertEqual(caught.exception.code, 'sd_native_error')
        self.assertIn('download_stop', self.sdk.calls)

    def test_completed_native_file_with_wrong_size_rejected(self):
        with self.provider() as provider, self.assertRaises(SDSourceError) as caught:
            provider.download(recording(size=9), self.root/'one.source')
        self.assertEqual(caught.exception.code, 'sd_download_incomplete')

    def test_sdk_missing_and_architecture_mismatch(self):
        with patch.dict(os.environ, {'HCNETSDK_DIR': str(self.root)}):
            with self.assertRaises(SDSourceError) as caught:
                _sdk_library()
            self.assertEqual(caught.exception.code, 'sd_sdk_missing')
            (self.root/'libhcnetsdk.so').write_bytes(b'\x7fELF\x01\x01' + bytes(12) + bytes([40, 0]))
            with patch('archive_app.sd_source.platform.machine', return_value='x86_64'), self.assertRaises(SDSourceError) as caught:
                _sdk_library()
            self.assertEqual(caught.exception.code, 'sd_sdk_arch_mismatch')

    def child(self, body, timeout=2):
        script = self.root/'helper.py'
        script.write_text(body, encoding='utf-8')
        return HCNetSDKSource('192.168.31.166', 8000, 'admin', 'synthetic-camera-secret', 1,
                             get_zone('UTC+07:00'), 1024,
                             worker_command=[sys.executable, str(script)], session_timeout=timeout)

    def test_real_child_credentials_stdin_not_argv(self):
        body = '''import json,sys
request=json.loads(sys.stdin.readline())
assert request['password']=='synthetic-camera-secret'
assert all('synthetic-camera-secret' not in x for x in sys.argv)
print(json.dumps({'ok':True,'result':None}),flush=True)
for line in sys.stdin:
    item=json.loads(line)
    if item['command']=='close':break
    print(json.dumps({'ok':True,'result':[]}),flush=True)
'''
        provider = self.child(body)
        with provider:
            self.assertEqual(provider.search(NOW-timedelta(hours=1), NOW), [])
            process = provider.process
            self.assertEqual(provider.config['password'], '')
        self.assertIsNotNone(process.poll())

    def test_real_child_crash_isolated(self):
        provider = self.child('import os,sys\nsys.stdin.readline()\nos._exit(71)\n')
        with self.assertRaises(SDSourceError) as caught:
            with provider:
                pass
        self.assertEqual(caught.exception.code, 'sd_native_worker_failed')
        self.assertIsNone(provider.process)

    def test_real_child_hang_is_stopped(self):
        provider = self.child('import sys,time\nsys.stdin.readline()\ntime.sleep(30)\n', timeout=0.15)
        before = time.monotonic()
        with self.assertRaises(SDSourceError) as caught:
            with provider:
                pass
        self.assertEqual(caught.exception.code, 'sd_native_worker_timeout')
        self.assertLess(time.monotonic()-before, 5)
        self.assertIsNone(provider.process)

    def test_native_child_messages_are_not_echoed(self):
        provider = self.child("import sys,json\nsys.stdin.readline()\nprint(json.dumps({'ok':False,'code':'unknown','message':'synthetic-camera-secret'}),flush=True)\n")
        with self.assertRaises(SDSourceError) as caught:
            with provider:
                pass
        self.assertEqual(caught.exception.code, 'sd_native_error')
        self.assertNotIn('synthetic-camera-secret', str(caught.exception))

    def test_fixed_timezone_keeps_half_hour_offset(self):
        self.assertEqual(HCNetSDKSource._zone_name(get_zone('UTC+07:30')), 'UTC+07:30')
        self.assertEqual(HCNetSDKSource._zone_name(get_zone('UTC-03:30')), 'UTC-03:30')

    def test_linux_native_helper_accepts_legitimate_docker_pid_one_parent(self):
        stream = Mock()
        stream.buffer.readline.return_value = b''
        library = Mock()
        library.prctl.return_value = 0
        with patch('archive_app.sd_source.sys.platform', 'linux'), patch('archive_app.sd_source.C.CDLL', return_value=library), patch('archive_app.sd_source.os.getppid', return_value=1), patch.dict(os.environ, {'MYCAM_NATIVE_PARENT_PID': '1'}), patch('archive_app.sd_source.sys.stdin', stream):
            _native_worker()
        stream.buffer.readline.assert_called_once_with(32_001)

    def test_linux_native_helper_stops_if_exact_parent_changed(self):
        stream = Mock()
        library = Mock()
        library.prctl.return_value = 0
        with patch('archive_app.sd_source.sys.platform', 'linux'), patch('archive_app.sd_source.C.CDLL', return_value=library), patch('archive_app.sd_source.os.getppid', return_value=1), patch.dict(os.environ, {'MYCAM_NATIVE_PARENT_PID': '8765'}), patch('archive_app.sd_source.sys.stdin', stream):
            _native_worker()
        stream.buffer.readline.assert_not_called()


class FakeSource:
    def __init__(self, backend='hcnetsdk', files=None, fail=None):
        self.backend = backend
        self.files = files if files is not None else [recording()]
        self.fail = fail
        self.max_bytes = 1024
        self.downloads = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def search(self, start, end):
        self.window = start, end
        return self.files

    def download(self, item, path):
        self.downloads.append((item, path, self.max_bytes))
        path.write_bytes(b'fixture8')
        if self.fail:
            raise SDSourceError('sd_download_incomplete', 'Fixture transfer failed.')
        return 8


class SDOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        settings = Settings(self.root/'data', self.root/'cache', self.root/'input', 'UTC+07:00',
                            min_free_bytes=0, cache_max_bytes=1024*1024)
        settings.input_dir.mkdir()
        self.archive = Archive(settings)
        self.addCleanup(self.archive.conn.close)
        self.archive.add_camera({'id': 'PN', 'name': 'Phòng ngủ', 'host': '192.168.31.166'})
        self.config = {'id': 'PN', 'host': '192.168.31.166', 'sd_username': 'admin',
                       'sd_password': 'synthetic-camera-secret', 'sd_backend': 'auto', 'sd_channel': 1,
                       'sd_timezone': 'UTC+07:00', 'sd_lookback_hours': 168}

    def source(self):
        return SDSource(self.archive, 'PN', self.config)

    def normalize(self, source, destination, settings):
        destination.write_bytes(Path(source).read_bytes())
        return {'duration': 60, 'codec_video': 'h264', 'codec_audio': None, 'bytes': 8}

    def sync(self, provider, **kwargs):
        with patch.object(SDSource, '_provider', return_value=provider), patch('archive_app.core.normalize', side_effect=self.normalize):
            return self.source().sync(now=NOW, **kwargs)

    def test_finished_download_uses_ingest_and_no_input_write(self):
        provider = FakeSource()
        result = self.sync(provider)
        self.assertEqual(result['downloaded'], 1)
        self.assertEqual(result['imported'], 1)
        row = self.archive.conn.execute('SELECT * FROM recordings').fetchone()
        self.assertEqual(row['key'], record_key({'camera': 'PN', 'source': 'camera-sd', 'record_id': 'ch1:2026-10-03T03:00:00Z'}))
        self.assertEqual(row['record_id'], 'ch1:2026-10-03T03:00:00Z')
        self.assertEqual(row['status'], 'downloaded')
        self.assertEqual(list(self.archive.settings.input_dir.iterdir()), [])
        self.assertEqual(list((self.archive.settings.cache_dir/'sd-stage'/'PN').iterdir()), [])

    def test_switching_backend_does_not_duplicate_video(self):
        self.sync(FakeSource('hcnetsdk'))
        alternate = FakeSource('isapi', [{**recording(rid='different-native-name'), 'start_time': '2026-10-03T03:00:00Z', 'end_time': '2026-10-03T03:01:00Z'}])
        result = self.sync(alternate)
        self.assertEqual(result['already_known'], 1)
        self.assertEqual(alternate.downloads, [])
        self.assertEqual(self.archive.conn.execute('SELECT COUNT(*) FROM recordings').fetchone()[0], 1)

    def test_current_open_recording_is_deferred(self):
        provider = FakeSource(files=[recording(start='2026-10-03T11:58:00+07:00', end='2026-10-03T12:00:00+07:00')])
        self.assertEqual(self.sync(provider)['deferred'], 1)
        self.assertEqual(provider.downloads, [])

    def test_download_failure_cleans_partial_and_no_catalog(self):
        with self.assertRaises(SDSourceError):
            self.sync(FakeSource(fail=True))
        self.assertEqual(self.archive.conn.execute('SELECT COUNT(*) FROM recordings').fetchone()[0], 0)
        self.assertEqual(list((self.archive.settings.cache_dir/'sd-stage'/'PN').iterdir()), [])

    def test_media_failure_cleans_staging_and_marks_failed(self):
        with patch.object(SDSource, '_provider', return_value=FakeSource()), patch('archive_app.core.normalize', side_effect=ValueError('synthetic-camera-secret')), self.assertRaises(SDSourceError) as caught:
            self.source().sync(now=NOW)
        self.assertEqual(caught.exception.code, 'sd_media_validation_failed')
        self.assertNotIn('synthetic-camera-secret', str(caught.exception))
        self.assertEqual(self.archive.conn.execute('SELECT status FROM recordings').fetchone()[0], 'failed')
        self.assertEqual(list((self.archive.settings.cache_dir/'sd-stage'/'PN').iterdir()), [])

    def test_uncertain_and_deleted_records_never_redownloaded(self):
        self.sync(FakeSource())
        for status, deleted in (('upload_unknown', None), ('downloaded', 123), ('needs_review', None)):
            self.archive.conn.execute('UPDATE recordings SET status=?,deleted_at=?', (status, deleted))
            self.archive.conn.commit()
            provider = FakeSource()
            self.assertEqual(self.sync(provider)['already_known'], 1)
            self.assertEqual(provider.downloads, [])

    def test_changed_closed_end_requires_review(self):
        self.sync(FakeSource())
        with self.assertRaises(SDSourceError) as caught:
            self.sync(FakeSource(files=[recording(end='2026-10-03T10:02:00+07:00')]))
        self.assertEqual(caught.exception.code, 'sd_record_changed')
        self.assertEqual(self.archive.conn.execute('SELECT end_ms FROM recordings').fetchone()[0], 1790996460000)

    def test_unknown_size_download_gets_live_budget_not_one_byte(self):
        (self.archive.settings.cache_dir/'occupied').write_bytes(b'x'*1000)
        self.archive.settings.cache_max_bytes = 1300
        provider = FakeSource(files=[recording(size=0)])
        self.sync(provider)
        self.assertEqual(provider.downloads[0][2], 150)

    def test_batch_limit_reports_backlog_without_skipping_future_rescan(self):
        provider = FakeSource(files=[recording(rid=f'file{i}', start=f'2026-10-03T10:00:0{i}+07:00') for i in range(3)])
        first = self.sync(provider, max_files=1)
        self.assertEqual((first['imported'], first['backlog']), (1, 2))
        second = self.sync(provider, max_files=3)
        self.assertEqual((second['imported'], second['already_known']), (2, 1))

    def test_credentials_and_host_validation(self):
        self.config['sd_password'] = ''
        with self.assertRaises(SDSourceError) as caught:
            self.source().sync(now=NOW)
        self.assertEqual(caught.exception.code, 'sd_credentials_missing')
        with patch('archive_app.sd_source.socket.getaddrinfo', return_value=[(None,None,None,None,('127.0.0.1', 0))]), self.assertRaises(SDSourceError) as caught:
            _private_address('localhost')
        self.assertEqual(caught.exception.code, 'sd_host_invalid')

    def test_auto_native_missing_then_isapi_failure_reports_sdk_required(self):
        provider = FakeSource('isapi')
        provider.native_sdk_missing = True
        provider.search = lambda *_: (_ for _ in ()).throw(SDSourceError('sd_network_error', 'Fixture HTTP unavailable.'))
        with self.assertRaises(SDSourceError) as caught:
            self.sync(provider)
        self.assertEqual(caught.exception.code, 'sd_sdk_missing')

    def test_paused_camera_stops_before_download(self):
        provider = FakeSource()
        self.archive.update_camera('PN', {'enabled': False})
        with self.assertRaises(SDSourceError) as caught:
            self.sync(provider)
        self.assertEqual(caught.exception.code, 'camera_disabled')
        self.assertEqual(provider.downloads, [])

    def test_pause_during_download_does_not_count_import(self):
        provider = FakeSource()
        def download(item, path):
            path.write_bytes(b'fixture8')
            self.archive.update_camera('PN', {'enabled': False})
        provider.download = download
        with self.assertRaises(SDSourceError) as caught:
            self.sync(provider)
        self.assertEqual(caught.exception.code, 'camera_disabled')
        self.assertEqual(self.archive.conn.execute('SELECT COUNT(*) FROM recordings').fetchone()[0], 0)
        self.assertEqual(list((self.archive.settings.cache_dir/'sd-stage'/'PN').iterdir()), [])

    def test_stale_managed_staging_files_removed_not_unknown_files(self):
        root = self.archive.settings.cache_dir/'sd-stage'/'PN'
        root.mkdir(parents=True)
        managed = root/('a'*64+'.'+'b'*32+'.part')
        managed.write_bytes(b'partial')
        retained = root/'user-note.txt'
        retained.write_text('fixture')
        self.sync(FakeSource(files=[]))
        self.assertFalse(managed.exists())
        self.assertTrue(retained.exists())

    def test_progress_snapshots_contain_counts_not_credentials(self):
        snapshots = []
        self.sync(FakeSource(), progress=snapshots.append)
        self.assertEqual(snapshots[0]['phase'], 'sd_search')
        self.assertEqual(snapshots[-1]['phase'], 'sd_complete')
        self.assertEqual(snapshots[0]['imported'], 0)
        self.assertEqual(snapshots[-1]['imported'], 1)
        self.assertTrue(any(item['phase'] == 'sd_download' for item in snapshots))
        self.assertNotIn('synthetic-camera-secret', json.dumps(snapshots))

    def test_startup_recovery_removes_only_adapter_sources_all_cameras(self):
        root = self.archive.settings.cache_dir.resolve()/'sd-stage'
        for name in ('PN', 'disabled-camera'):
            directory = root/name
            directory.mkdir(parents=True)
            (directory/('a'*64+'.'+'b'*32+'.part')).write_bytes(b'partial')
            (directory/('c'*64+'.'+'d'*32+'.source')).write_bytes(b'completed-unimported')
            (directory/'user-note.txt').write_text('retained')
        normalized = self.archive.settings.cache_dir/('e'*64+'.mp4')
        normalized.write_bytes(b'archived')
        count = recover_staging(self.archive)
        self.assertEqual(count, 4)
        self.assertEqual(normalized.read_bytes(), b'archived')
        self.assertTrue((root/'PN'/'user-note.txt').exists())
        self.assertEqual(recover_staging(self.archive), 0)

    def test_startup_recovery_rejects_linked_root_or_camera(self):
        root = self.archive.settings.cache_dir.resolve()/'sd-stage'
        (root/'PN').mkdir(parents=True)
        original = Path.is_symlink
        for target in (root, root/'PN'):
            def linked(path, selected=target):
                return path == selected or original(path)
            with patch.object(Path, 'is_symlink', linked), self.assertRaises(SDSourceError) as caught:
                recover_staging(self.archive)
            self.assertEqual(caught.exception.code, 'sd_stage_invalid')

    def test_startup_recovery_ignores_linked_source_without_following_it(self):
        root = self.archive.settings.cache_dir.resolve()/'sd-stage'/'PN'
        root.mkdir(parents=True)
        managed = root/('a'*64+'.'+'b'*32+'.source')
        managed.write_bytes(b'retained-linked-fixture')
        original = Path.is_symlink
        with patch.object(Path, 'is_symlink', lambda path: path == managed or original(path)):
            self.assertEqual(recover_staging(self.archive), 0)
        self.assertTrue(managed.exists())


if __name__ == '__main__':
    unittest.main()
