"""LAN collector tests use only synthetic original bytes and fake SDK sessions."""
from contextlib import contextmanager
from dataclasses import replace
import http.client
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from archive_app.sd_collector import (CollectorConfig, CollectorError, CollectorServer,
                                     CollectorState, MAX_BODY)
from archive_app.sd_source import MAX_NATIVE_FILE, SDSourceError
from archive_app.sd_collector_client import SDCollectorSource
from archive_app.core import parse_time


TOKEN = 'synthetic-collector-bearer-token-1234567890'


def recording(rid='ch01_0001', size=8):
    return {'record_id': rid, 'start_time': '2026-10-11T08:00:00+07:00',
            'end_time': '2026-10-11T08:01:00+07:00', 'size': size}


def create_body(host='192.168.2.7', camera_id='camera-7', password='secret', maximum=32 * 1024**2):
    return {'camera': {'id': camera_id, 'host': host, 'device_port': 8000,
            'sd_username': 'admin', 'sd_password': password, 'sd_channel': 1,
            'sd_timezone': 'Asia/Ho_Chi_Minh'}, 'max_bytes': maximum, 'download_speed_kbps': 0}


SEARCH = {'start': '2026-10-11T00:00:00+07:00', 'end': '2026-10-12T00:00:00+07:00'}


class FakeFactory:
    def __init__(self):
        self.providers, self.results = [], [recording()]
        self.data, self.fail = b'original', False
        self.enter_failure = False

    def __call__(self, address, port, username, password, channel, zone, maximum, **kwargs):
        factory = self
        class Provider:
            download_speed_status, download_speed_error_code = 'off', None
            download_speed_requested_kbps = kwargs['download_speed_kbps']
            def __init__(self):
                self.max_bytes, self.closed, self.calls = maximum, False, 0
            def __enter__(self):
                if factory.enter_failure:
                    raise SDSourceError('sd_auth_failed', 'SENSITIVE-NATIVE-OUTPUT')
                return self
            def __exit__(self, *_):
                self.closed = True
            def search(self, *_):
                return [dict(item) for item in factory.results]
            def download(self, entry, path):
                self.calls += 1
                path.write_bytes(factory.data)
                if factory.fail:
                    raise SDSourceError('sd_native_error', 'SENSITIVE-NATIVE-OUTPUT')
                return len(factory.data)
        provider = Provider()
        self.providers.append(provider)
        return provider


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.factory = FakeFactory()
        self.config = CollectorConfig(TOKEN, Path(self.tmp.name), min_free_bytes=0)
        self.state = CollectorState(self.config, self.factory)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def searched(self):
        sid = self.state.create(create_body())
        with self.state.use(sid) as session:
            self.state.search(session, SEARCH)
        return sid

    def download(self, sid, entry=None, maximum=32 * 1024**2):
        with self.state.use(sid) as session:
            with self.state.downloaded(session, {'recording': entry or recording(), 'max_bytes': maximum}) as (path, size):
                self.assertEqual(size, path.stat().st_size)
                self.assertTrue(self.state.reservations)
                return path.read_bytes()

    def test_config_env_file_and_limits(self):
        path = Path(self.tmp.name) / 'bearer'
        path.write_text(TOKEN + '\n')
        with patch.dict(os.environ, {'SD_COLLECTOR_TOKEN_FILE': str(path),
                'COLLECTOR_CACHE_DIR': self.tmp.name}, clear=True):
            config = CollectorConfig.from_env()
            self.assertEqual(config.token, TOKEN)
            self.assertEqual(config.cache_dir, Path(self.tmp.name))
            self.assertEqual(config.cache_max_bytes, 2 * 1024**3)
            self.assertEqual(config.min_free_bytes, 2 * 1024**3)
            self.assertEqual(config.idle_timeout, 900)
            self.assertEqual(config.max_sessions, 10)
            self.assertEqual(config.per_camera_sessions, 2)
        with self.assertRaises(ValueError):
            replace(self.config, token='weak')
        with self.assertRaises(ValueError):
            replace(self.config, max_sessions=11)
        with self.assertRaises(ValueError):
            replace(self.config, per_camera_sessions=3)

    def test_reject_camera_outside_cidr_dns_loopback_and_port(self):
        for host in ('example.com', '127.0.0.1', '192.168.3.7', '0.0.0.0', '224.0.0.1'):
            with self.subTest(host=host), self.assertRaises(CollectorError):
                self.state.create(create_body(host))
        body = create_body()
        body['camera']['device_port'] = 80
        with self.assertRaises(CollectorError):
            self.state.create(body)
        self.assertEqual(self.factory.providers, [])

    def test_reject_native_split_limits_and_unknown_fields(self):
        for value in (0, True, MAX_NATIVE_FILE + 1):
            with self.subTest(value=value), self.assertRaises(CollectorError):
                self.state.create(create_body(maximum=value))
        body = create_body()
        body['camera']['unexpected'] = 'x'
        with self.assertRaises(CollectorError):
            self.state.create(body)

    def test_session_reused_and_original_bytes_deleted_after_each_transfer(self):
        sid = self.searched()
        self.assertRegex(sid, r'^[a-f0-9]{32}$')
        self.assertEqual(self.download(sid), b'original')
        self.assertEqual(self.download(sid), b'original')
        self.assertEqual(len(self.factory.providers), 1)
        self.assertEqual(self.factory.providers[0].calls, 2)
        self.assertFalse(self.factory.providers[0].closed)
        self.assertEqual(self.state.reservations, {})
        self.assertEqual(list(self.state.root.iterdir()), [])
        self.state.close_session(sid)
        self.assertTrue(self.factory.providers[0].closed)

    def test_closed_search_session_feeds_two_parallel_download_sessions(self):
        searched = self.searched()
        self.state.close_session(searched)
        first, second = self.state.create(create_body()), self.state.create(create_body())
        self.assertEqual(self.download(first), b'original')
        self.assertEqual(self.download(second), b'original')
        self.assertEqual(len(self.factory.providers), 3)

    def test_shared_metadata_does_not_cross_credentials(self):
        searched = self.searched()
        self.state.close_session(searched)
        other = self.state.create(create_body(password='different-credential'))
        with self.assertRaises(CollectorError) as error:
            self.download(other)
        self.assertEqual(error.exception.code, 'sd_collector_recording_not_searched')

    def test_per_camera_limit_cannot_be_bypassed_with_alias_id(self):
        self.state.create(create_body(camera_id='first'))
        self.state.create(create_body(camera_id='second'))
        with self.assertRaises(CollectorError) as error:
            self.state.create(create_body(camera_id='third'))
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(len(self.factory.providers), 2)

    def test_global_session_limit(self):
        for number in range(10, 20):
            self.state.create(create_body(host=f'192.168.2.{number}'))
        with self.assertRaises(CollectorError):
            self.state.create(create_body(host='192.168.2.20'))
        self.assertEqual(len(self.factory.providers), 10)

    def test_busy_lock_prevents_overlapping_search_download_or_close(self):
        sid = self.searched()
        with self.state.use(sid):
            with self.assertRaises(CollectorError) as error:
                with self.state.use(sid):
                    self.fail('Concurrent session access admitted')
            self.assertEqual(error.exception.code, 'sd_collector_session_busy')
            with self.assertRaises(CollectorError):
                self.state.close_session(sid)

    def test_failed_session_creation_releases_slot_and_closes_helper(self):
        self.factory.enter_failure = True
        with self.assertRaises(SDSourceError):
            self.state.create(create_body())
        self.assertEqual(self.state.sessions, {})
        self.assertTrue(self.factory.providers[0].closed)

    def test_ttl_skips_busy_then_expires_sessions_and_metadata(self):
        now = [0.0]
        self.state.clock = lambda: now[0]
        sid = self.searched()
        with self.state.use(sid):
            now[0] = 901
            self.state.expire()
            self.assertIn(sid, self.state.sessions)
            self.assertFalse(self.factory.providers[0].closed)
        now[0] = 1802
        self.state.expire()
        self.assertEqual(self.state.sessions, {})
        self.assertEqual(self.state.metadata, {})
        self.assertTrue(self.factory.providers[0].closed)

    def test_tampered_recording_is_not_sent_to_camera(self):
        sid = self.searched()
        for entry in (recording(rid='another'), recording(size=9), dict(recording(), remote_filename='secret')):
            with self.subTest(entry=entry), self.assertRaises(CollectorError):
                self.download(sid, entry)
        self.assertEqual(self.factory.providers[0].calls, 0)

    def test_download_failure_and_incomplete_raw_cleanup(self):
        sid = self.searched()
        self.factory.fail = True
        with self.assertRaises(SDSourceError):
            self.download(sid)
        self.assertEqual(list(self.state.root.iterdir()), [])
        self.assertEqual(self.state.reservations, {})
        self.factory.fail, self.factory.data = False, b'truncated'
        with self.assertRaises(CollectorError):
            self.download(sid)
        self.assertEqual(list(self.state.root.iterdir()), [])
        self.assertEqual(self.state.reservations, {})

    def test_global_disk_reservation_held_through_response_consumption(self):
        self.state.config = replace(self.config, cache_max_bytes=8)
        sid = self.searched()
        other = self.state.create(create_body())
        with self.state.use(sid) as session:
            with self.state.downloaded(session, {'recording': recording(), 'max_bytes': 8}):
                with self.assertRaises(CollectorError) as error:
                    self.download(other, maximum=8)
                self.assertEqual(error.exception.code, 'sd_cache_budget')
        self.assertEqual(self.download(other, maximum=8), b'original')

    def test_free_disk_floor_and_request_limit_before_download(self):
        sid = self.searched()
        self.state.config = replace(self.config, min_free_bytes=10)
        with patch('archive_app.sd_collector.shutil.disk_usage', return_value=type('Usage', (), {'free': 17})()):
            with self.assertRaises(CollectorError) as error:
                self.download(sid)
            self.assertEqual(error.exception.status, 507)
        with self.assertRaises(CollectorError) as error:
            self.download(sid, maximum=7)
        self.assertEqual(error.exception.status, 413)
        self.assertEqual(self.factory.providers[0].calls, 0)

    def test_startup_and_ttl_remove_only_owned_orphan_files(self):
        own = self.state.root / ('a' * 32 + '.part')
        unknown = self.state.root / 'keep-user-file.ps'
        own.write_bytes(b'old')
        unknown.write_bytes(b'keep')
        self.state.close()
        self.state = CollectorState(self.config, self.factory)
        self.assertFalse(own.exists())
        self.assertEqual(unknown.read_bytes(), b'keep')
        own.write_bytes(b'old')
        os.utime(own, (time.time() - 1000, time.time() - 1000))
        self.state.expire()
        self.assertFalse(own.exists())
        self.assertTrue(unknown.exists())

    def test_shutdown_closes_all_helpers_and_blocks_new_sessions(self):
        self.state.create(create_body())
        self.state.create(create_body())
        self.state.close()
        self.assertTrue(all(item.closed for item in self.factory.providers))
        with self.assertRaises(CollectorError):
            self.state.create(create_body())


class CollectorHTTPTests(CollectorTests):
    def setUp(self):
        super().setUp()
        self.server = CollectorServer(('127.0.0.1', 0), self.state)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.state.close()
        self.server.server_close()
        self.server_thread.join(timeout=3)
        super().tearDown()

    @contextmanager
    def request(self, method, path, body=None, auth=True, headers=None):
        client = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        fields = {'Content-Type': 'application/json'}
        if auth:
            fields['Authorization'] = 'Bearer ' + TOKEN
        fields.update(headers or {})
        try:
            client.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=fields)
            yield client.getresponse()
        finally:
            client.close()

    def test_http_health_minimal_and_auth_before_json_body(self):
        with self.request('GET', '/health', auth=False) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {'ok': True})
        client = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        client.request('POST', '/v1/sessions', body=b'NOT-JSON', headers={'Content-Length': '8'})
        response = client.getresponse()
        self.assertEqual(response.status, 401)
        self.assertEqual(json.loads(response.read()), {'error': 'sd_collector_unauthorized'})
        client.close()
        self.assertEqual(self.factory.providers, [])

    def test_http_body_bound_and_native_error_sanitization(self):
        with self.request('POST', '/v1/sessions', {}, headers={'Content-Length': str(MAX_BODY + 1)}) as response:
            self.assertEqual(response.status, 413)
            response.read()
        self.factory.enter_failure = True
        with self.request('POST', '/v1/sessions', create_body()) as response:
            self.assertEqual(response.status, 502)
            raw = response.read()
            self.assertNotIn(b'SENSITIVE', raw)
            self.assertEqual(json.loads(raw), {'error': 'sd_auth_failed'})

    def test_http_session_search_raw_download_and_delete(self):
        with self.request('POST', '/v1/sessions', create_body()) as response:
            sid = json.loads(response.read())['session_id']
        with self.request('POST', f'/v1/sessions/{sid}/search', SEARCH) as response:
            self.assertEqual(json.loads(response.read()), {'recordings': [recording()]})
        with self.request('POST', f'/v1/sessions/{sid}/download', {'recording': recording(), 'max_bytes': 8}) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers['Content-Type'], 'application/octet-stream')
            self.assertEqual(response.headers['Content-Length'], '8')
            self.assertEqual(response.headers['X-SD-Speed-Status'], 'off')
            self.assertEqual(response.headers['X-SD-Speed-Kbps'], '0')
            self.assertEqual(response.read(), b'original')
        self._wait_empty()
        with self.request('DELETE', f'/v1/sessions/{sid}') as response:
            self.assertEqual(json.loads(response.read()), {'closed': True})
        self.assertTrue(self.factory.providers[0].closed)

    def _wait_empty(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not self.state.reservations and not list(self.state.root.glob('*.part')):
                return
            time.sleep(0.02)
        self.fail('Original-byte staging was not cleaned promptly')

    def test_http_disconnect_removes_raw_without_waiting_for_telegram(self):
        self.factory.data = b'x' * (8 * 1024**2)
        self.factory.results = [recording(size=len(self.factory.data))]
        sid = self.searched()
        payload = json.dumps({'recording': self.factory.results[0], 'max_bytes': len(self.factory.data)}).encode()
        connection = socket.create_connection(self.server.server_address, timeout=5)
        connection.sendall((f'POST /v1/sessions/{sid}/download HTTP/1.1\r\n'
            f'Host: localhost\r\nAuthorization: Bearer {TOKEN}\r\nContent-Type: application/json\r\n'
            f'Content-Length: {len(payload)}\r\n\r\n').encode() + payload)
        connection.recv(1024)
        connection.shutdown(socket.SHUT_RDWR)
        connection.close()
        self._wait_empty()
        self.assertEqual(self.factory.providers[0].calls, 1)

    def test_http_download_error_does_not_retain_raw(self):
        sid = self.searched()
        self.factory.fail = True
        with self.request('POST', f'/v1/sessions/{sid}/download', {'recording': recording(), 'max_bytes': 8}) as response:
            self.assertEqual(response.status, 502)
            self.assertEqual(json.loads(response.read()), {'error': 'sd_native_error'})
        self._wait_empty()

    def test_real_client_search_session_then_two_download_sessions_and_cleanup(self):
        cache = Path(self.tmp.name) / 'vps-cache'
        destination = cache / 'sd-stage' / 'camera' / 'clip.part'
        destination.parent.mkdir(parents=True)
        host, port = self.server.server_address
        def client():
            return SDCollectorSource(f'http://{host}:{port}', TOKEN,
                create_body()['camera'], 32 * 1024**2, cache_dir=cache)
        with client() as search:
            clips = search.search(parse_time(SEARCH['start']), parse_time(SEARCH['end']))
        with client() as first, client() as second:
            self.assertEqual(first.download(clips[0], destination), 8)
            self.assertEqual(destination.read_bytes(), b'original')
            destination.unlink()
            self.assertEqual(second.download(clips[0], destination), 8)
            self.assertEqual(destination.read_bytes(), b'original')
            destination.unlink()
        self._wait_empty()
        self.assertEqual(len(self.factory.providers), 3)
        self.assertTrue(all(item.closed for item in self.factory.providers))

    def test_default_container_bind_is_accepted(self):
        from archive_app.sd_collector import serve
        with patch('archive_app.sd_collector.CollectorConfig.from_env', return_value=self.config), \
                patch('archive_app.sd_collector.CollectorServer') as server, \
                patch('archive_app.sd_collector.signal.signal'):
            server.return_value.serve_forever.side_effect = KeyboardInterrupt
            serve('0.0.0.0', 8787)
            self.assertEqual(server.call_args.args[0], ('0.0.0.0', 8787))
            server.return_value.server_close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
