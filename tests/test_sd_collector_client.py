"""A real loopback HTTP fixture never uses camera, bot or SDK credentials."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from archive_app.core import Settings
from archive_app.sd_collector_client import SDCollectorSource, collector_endpoint
from archive_app.sd_source import SDSource, SDSourceError, MAX_NATIVE_FILE


TOKEN = 'fixture-collector-token-0123456789abcdef'
CAMERA = {'id': 'camera-fixture', 'host': '192.168.2.7', 'device_port': 8000,
          'sd_username': 'admin', 'sd_password': 'fixture-camera-secret',
          'sd_channel': 1, 'sd_timezone': 'UTC+07:00'}
RECORDING = {'record_id': 'ch01_fixture', 'start_time': '2026-10-11T10:00:00+07:00',
             'end_time': '2026-10-11T10:01:00+07:00', 'size': 8}
SESSION = '1234567890abcdef1234567890abcdef'


class CollectorFixture:
    def __init__(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.respond()

            def do_DELETE(self):
                self.respond()

            def respond(self):
                length = int(self.headers.get('Content-Length', '0'))
                payload = json.loads(self.rfile.read(length)) if length else None
                fixture.requests.append((self.command, self.path, payload, self.headers.get('Authorization')))
                if fixture.delay:
                    time.sleep(fixture.delay)
                if fixture.status != 200:
                    self.send_response(fixture.status)
                    if 300 <= fixture.status < 400:
                        self.send_header('Location', fixture.base+'/credential-target')
                    body = json.dumps(fixture.error).encode()
                    self.send_header('Content-Type', 'application/json')
                elif self.path.endswith('/download'):
                    self.send_response(200)
                    body = fixture.media
                    self.send_header('Content-Type', fixture.content_type)
                    for key, value in fixture.speed_headers.items():
                        self.send_header(key, value)
                else:
                    self.send_response(200)
                    value = ({'session_id': fixture.session} if self.path == '/v1/sessions'
                             else {'recordings': fixture.recordings} if self.path.endswith('/search')
                             else {'closed': True})
                    body = fixture.raw_json if fixture.raw_json is not None else json.dumps(value).encode()
                    self.send_header('Content-Type', 'application/json')
                if self.path.endswith('/download') and fixture.status == 200:
                    if fixture.length is not None:
                        self.send_header('Content-Length', fixture.length)
                    if fixture.duplicate_length:
                        self.send_header('Content-Length', fixture.duplicate_length)
                    if fixture.transfer_encoding:
                        self.send_header('Transfer-Encoding', fixture.transfer_encoding)
                    if fixture.content_encoding:
                        self.send_header('Content-Encoding', fixture.content_encoding)
                else:
                    self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                    self.wfile.flush()
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.base = 'http://127.0.0.1:'+str(self.server.server_port)
        self.reset()
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()

    def reset(self):
        self.requests, self.status, self.delay = [], 200, 0
        self.error = {'error': 'unrecognized-secret-'+TOKEN}
        self.session, self.recordings = SESSION, [dict(RECORDING)]
        self.media, self.length, self.content_type = b'fixture8', '8', 'application/octet-stream'
        self.duplicate_length = self.transfer_encoding = self.content_encoding = None
        self.raw_json = None
        self.speed_headers = {'X-SD-Speed-Status': 'off', 'X-SD-Speed-Error': '', 'X-SD-Speed-Kbps': '0'}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class CollectorClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = CollectorFixture()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.close()

    def setUp(self):
        self.fixture.reset()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'sd-stage'/'camera-fixture').mkdir(parents=True)
        self.output = self.root/'sd-stage'/'camera-fixture'/'one.part'

    def provider(self, **kwargs):
        return SDCollectorSource(self.fixture.base, TOKEN, CAMERA, 1024,
                                 cache_dir=self.root, **kwargs)

    def assert_code(self, code, operation):
        with self.assertRaises(SDSourceError) as raised:
            operation()
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertNotIn(CAMERA['sd_password'], str(raised.exception))

    def test_session_reused_across_search_and_two_downloads_and_closed(self):
        progress = []
        with self.provider() as provider:
            provider.download_progress = progress.append
            results = provider.search(datetime.now(timezone.utc), datetime.now(timezone.utc))
            self.assertEqual(results, [RECORDING])
            self.assertEqual(provider.download(results[0], self.output), 8)
            self.assertEqual(provider.download(results[0], self.output.with_name('two.part')), 8)
            self.assertEqual(self.output.read_bytes(), b'fixture8')
        self.assertEqual([request[:2] for request in self.fixture.requests], [
            ('POST', '/v1/sessions'), ('POST', '/v1/sessions/'+SESSION+'/search'),
            ('POST', '/v1/sessions/'+SESSION+'/download'),
            ('POST', '/v1/sessions/'+SESSION+'/download'), ('DELETE', '/v1/sessions/'+SESSION)])
        self.assertTrue(all(request[3] == 'Bearer '+TOKEN for request in self.fixture.requests))
        self.assertTrue(progress)
        self.assertEqual(progress[-1]['current_file_bytes'], 8)
        self.assertEqual(provider.camera['sd_password'], '')
        self.assertIsNone(provider.session_id)

    def test_credentials_only_in_session_json_not_url(self):
        with self.provider():
            pass
        first = self.fixture.requests[0]
        self.assertEqual(first[2]['camera'], CAMERA)
        self.assertNotIn(CAMERA['sd_password'], first[1])
        self.assertNotIn(TOKEN, first[1])

    def test_environment_proxy_is_not_used(self):
        with patch.dict(os.environ, {'HTTP_PROXY': 'http://127.0.0.1:1', 'HTTPS_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': ''}):
            with self.provider() as provider:
                self.assertEqual(provider.download(RECORDING, self.output), 8)

    def test_redirect_is_rejected_without_following_or_leaking_token(self):
        self.fixture.status = 307
        self.assert_code('sd_collector_redirect_rejected', lambda: self.provider().__enter__())
        self.assertEqual(len(self.fixture.requests), 1)

    def test_auth_error_is_fixed_and_sanitized(self):
        self.fixture.status = 401
        self.assert_code('sd_collector_auth_failed', lambda: self.provider().__enter__())

    def test_unknown_remote_error_never_exposes_arbitrary_text(self):
        self.fixture.status = 502
        self.assert_code('sd_collector_error', lambda: self.provider().__enter__())

    def test_approved_native_error_preserved(self):
        self.fixture.status, self.fixture.error = 502, {'error': 'sd_auth_failed'}
        self.assert_code('sd_auth_failed', lambda: self.provider().__enter__())

    def test_invalid_session_id_cannot_change_request_path(self):
        self.fixture.session = '../../credential-target'
        self.assert_code('sd_collector_protocol_error', lambda: self.provider().__enter__())

    def test_malformed_json_is_rejected(self):
        self.fixture.raw_json = b'{bad'
        self.assert_code('sd_collector_protocol_error', lambda: self.provider().__enter__())

    def test_search_metadata_duplicate_invalid_and_oversize_rejected(self):
        for rows in ([RECORDING, RECORDING], [{'record_id': 'bad'}], [dict(RECORDING, size=True)],
                     [dict(RECORDING, size=MAX_NATIVE_FILE+1)], [dict(RECORDING, end_time=RECORDING['start_time'])]):
            with self.subTest(rows=rows), self.provider() as provider:
                self.fixture.recordings = rows
                self.assert_code('sd_collector_protocol_error', lambda: provider.search(datetime.now(timezone.utc), datetime.now(timezone.utc)))

    def test_missing_invalid_duplicate_and_oversize_length_rejected_before_write(self):
        for length, duplicate, expected in ((None, None, 'sd_collector_protocol_error'),
                ('-8', None, 'sd_collector_protocol_error'), ('8', '8', 'sd_collector_protocol_error'),
                ('1025', None, 'sd_size_limit'), ('0', None, 'sd_size_limit')):
            with self.subTest(length=length, duplicate=duplicate), self.provider() as provider:
                self.fixture.length, self.fixture.duplicate_length = length, duplicate
                self.assert_code(expected, lambda: provider.download(RECORDING, self.output))
                self.assertFalse(self.output.exists())

    def test_known_recording_length_mismatch_rejected(self):
        with self.provider() as provider:
            self.assert_code('sd_download_incomplete', lambda: provider.download(dict(RECORDING, size=9), self.output))
        self.assertFalse(self.output.exists())

    def test_truncated_body_removes_own_partial(self):
        self.fixture.media = b'bad'
        with self.provider() as provider:
            self.assert_code('sd_download_incomplete', lambda: provider.download(RECORDING, self.output))
        self.assertFalse(self.output.exists())

    def test_progress_callback_failure_removes_partial_and_closes_session(self):
        def fail(_):
            raise RuntimeError('fixture callback failure')
        with self.assertRaises(RuntimeError), self.provider() as provider:
            provider.download_progress = fail
            provider.download(RECORDING, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.fixture.requests[-1][0], 'DELETE')

    def test_existing_file_is_never_removed(self):
        self.output.write_bytes(b'preserved')
        with self.provider() as provider:
            self.assert_code('sd_stage_invalid', lambda: provider.download(RECORDING, self.output))
        self.assertEqual(self.output.read_bytes(), b'preserved')

    def test_outside_staging_rejected(self):
        with self.provider() as provider:
            self.assert_code('sd_stage_invalid', lambda: provider.download(RECORDING, self.root/'outside.part'))
        self.assertFalse((self.root/'outside.part').exists())

    def test_transfer_encodings_and_nonbinary_content_rejected(self):
        for name, value in (('transfer_encoding', 'chunked'), ('content_encoding', 'gzip'), ('content_type', 'application/json')):
            with self.subTest(field=name), self.provider() as provider:
                setattr(self.fixture, name, value)
                self.assert_code('sd_collector_protocol_error', lambda: provider.download(RECORDING, self.output))
                setattr(self.fixture, name, None if name != 'content_type' else 'application/octet-stream')
        self.assertFalse(self.output.exists())

    def test_applied_and_unsupported_speed_headers_reported(self):
        for status, error in (('applied', ''), ('unsupported', '23'), ('rejected', '10')):
            with self.subTest(status=status), self.provider(download_speed_kbps=8192) as provider:
                self.fixture.speed_headers = {'X-SD-Speed-Status': status, 'X-SD-Speed-Error': error, 'X-SD-Speed-Kbps': '8192'}
                self.assertEqual(provider.download(RECORDING, self.output), 8)
                self.assertEqual(provider.download_speed_status, status)
                self.assertEqual(provider.download_speed_error_code, int(error) if error else None)
                self.output.unlink()

    def test_invalid_speed_headers_rejected_before_write(self):
        for headers in ({'X-SD-Speed-Status': 'off'}, {'X-SD-Speed-Status': 'applied', 'X-SD-Speed-Error': '23', 'X-SD-Speed-Kbps': '0'},
                        {'X-SD-Speed-Status': 'unsupported', 'X-SD-Speed-Error': '10', 'X-SD-Speed-Kbps': '0'}):
            with self.subTest(headers=headers), self.provider() as provider:
                self.fixture.speed_headers = headers
                self.assert_code('sd_collector_protocol_error', lambda: provider.download(RECORDING, self.output))
        self.assertFalse(self.output.exists())

    def test_close_error_does_not_hide_verified_download(self):
        with self.provider() as provider:
            self.assertEqual(provider.download(RECORDING, self.output), 8)
            self.fixture.status = 502
        self.assertEqual(self.output.read_bytes(), b'fixture8')

    def test_network_timeout_is_bounded_and_sanitized(self):
        self.fixture.delay = .15
        started = time.monotonic()
        self.assert_code('sd_collector_network_error', lambda: self.provider(timeout=.03).__enter__())
        self.assertLess(time.monotonic()-started, .5)

    def test_download_without_session_rejected(self):
        self.assert_code('sd_collector_session_missing', lambda: self.provider().download(RECORDING, self.output))


class CollectorRoutingTests(unittest.TestCase):
    def test_private_tailnet_and_loopback_literals_allowed_public_dns_rejected(self):
        for url in ('http://192.168.2.1:8787', 'https://100.92.56.98:8787/', 'http://[fd7a:115c:a1e0::1]:8787', 'http://127.0.0.1:8787'):
            self.assertEqual(len(collector_endpoint(url)), 3)
        for url in ('http://8.8.8.8:8787', 'https://example.com:8787', 'http://127.0.0.1',
                    'http://token@192.168.1.1:8787', 'http://192.168.1.1:8787/path',
                    'http://192.168.1.1:8787?token=secret', 'file:///tmp/collector', ''):
            with self.subTest(url=url), self.assertRaises(SDSourceError):
                collector_endpoint(url)

    def test_collector_selected_before_local_sdk_and_no_silent_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = Settings(root, root, root, 'UTC+07:00')
            settings.sd_collector_url, settings.sd_collector_token = 'http://100.92.56.98:8787', TOKEN
            config = dict(CAMERA, sd_backend='auto')
            with patch('archive_app.sd_source.HCNetSDKSource', side_effect=AssertionError('must not use VPS SDK')):
                source = SDSource(SimpleNamespace(settings=settings), CAMERA['id'], credentials=config)._provider()
            self.assertIsInstance(source, SDCollectorSource)
            self.assertEqual(source.camera['host'], CAMERA['host'])

    def test_explicit_isapi_keeps_existing_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = Settings(root, root, root, 'UTC+07:00')
            settings.sd_collector_url, settings.sd_collector_token = 'http://100.92.56.98:8787', TOKEN
            source = SDSource(SimpleNamespace(settings=settings), CAMERA['id'], credentials=dict(CAMERA, sd_backend='isapi'))._provider()
            self.assertEqual(source.backend, 'isapi')

