"""Real loopback player HTTP, with synthetic Telegram metadata/media only."""
import http.client
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.parse
import uuid

from archive_app.core import Archive, Settings
from archive_app.dashboard import DashboardServer
from archive_app.telegram_player import TelegramPlayer


class PlayerSettingsTests(unittest.TestCase):
    def test_environment_player_defaults_and_explicit_public_url(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
            self.assertEqual(settings.player_public_url, '')
            self.assertEqual(settings.bot_api_file_root, Path('/var/lib/telegram-bot-api'))
        with patch.dict(os.environ, {'TELEGRAM_PLAYER_PUBLIC_URL': 'https://player.example/',
                                    'TELEGRAM_BOT_API_FILE_ROOT': '/readonly/bot-state'}, clear=True):
            settings = Settings.from_env()
            self.assertEqual(settings.player_public_url, 'https://player.example')
            self.assertEqual(settings.bot_api_file_root, Path('/readonly/bot-state'))
            self.assertEqual(TelegramPlayer(settings).public_url(), 'https://player.example')


class PlayerHTTPTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent if os.name == 'nt' else Path(tempfile.gettempdir()).resolve()
        self.root = self.parent/('.tmp-player-http-'+uuid.uuid4().hex)
        self.root.mkdir()
        (self.root/'input').mkdir()
        self.settings = Settings(self.root/'state', self.root/'cache', self.root/'input', 'UTC+07:00',
                                 token='synthetic-player-http-token', owner_user_id=42,
                                 allowed_users=(42, 43), api_mode='local',
                                 storage_channel_id=-100123, telegram_destination='channel',
                                 bot_api_file_root=self.root/'bot-api')
        self.media = self.settings.bot_api_file_root/self.settings.token/'videos'/'file_1.mp4'
        self.media.parent.mkdir(parents=True)
        self.original = b'loopback-byte-exact-synthetic-media-0123456789'
        self.media.write_bytes(self.original)
        self.key = 'c'*64
        with_archive = Archive(self.settings)
        try:
            with_archive.state('telegram_bot_id', '9')
            with_archive.conn.execute('''INSERT INTO recordings
                (key,camera,record_id,start_ms,end_ms,source_path,status,created_at,
                 file_id,file_unique_id,bot_id,media_type,media_container,storage_kind,
                 storage_chat_id,storage_message_id,chat_id,message_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (self.key, 'front', 'synthetic', 1000, 2000, 'unused', 'uploaded', 1,
                 'synthetic-http-file', 'synthetic-http-unique', 9, 'video', 'mp4',
                 'channel', -100123, 27, '-100123', 27))
            with_archive.conn.commit()
        finally:
            with_archive.close()
        with patch.dict(os.environ, {'DASHBOARD_COOKIE_SECURE': 'false'}):
            self.server = DashboardServer(('127.0.0.1', 0), self.settings)
        self.settings.player_public_url = 'http://127.0.0.1:'+str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.02}, daemon=True)
        self.thread.start()
        archive = Archive(self.settings)
        try:
            self.url = TelegramPlayer(self.settings).issue(archive, self.key, 43)
        finally:
            archive.close()
        self.path = urllib.parse.urlsplit(self.url).path
        self.api_patch = patch('archive_app.telegram.Telegram.request', return_value={
            'file_id': 'synthetic-http-file', 'file_unique_id': 'synthetic-http-unique',
            'file_path': str(self.media), 'file_size': len(self.original)})
        self.api = self.api_patch.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.api_patch.stop()
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.root.resolve().parent, self.parent)
        shutil.rmtree(self.root)

    def request(self, suffix='', *, path=None, method='GET', headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            connection.request(method, path if path is not None else self.path+suffix,
                               headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_direct_link_html_without_admin_login_and_no_extra_telegram_send(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertIn(b'<video controls autoplay muted playsinline', body)
        self.assertIn((self.path+'/video').encode(), body)
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.assertIn('no-store', headers['Cache-Control'])
        self.assertNotIn(self.settings.token.encode(), body)
        self.assertNotIn(str(self.media).encode(), body)
        self.assertNotIn('Set-Cookie', headers)
        self.api.assert_not_called()

    def test_range_seek_and_head_are_byte_exact_without_login(self):
        status, headers, body = self.request('/video', headers={'Range': 'bytes=3-15'})
        self.assertEqual(status, 206)
        self.assertEqual(body, self.original[3:16])
        self.assertEqual(headers['Content-Range'], f'bytes 3-15/{len(self.original)}')
        self.assertEqual(headers['Content-Length'], '13')
        status, headers, body = self.request('/video', method='HEAD', headers={'Range': 'bytes=-7'})
        self.assertEqual(status, 206)
        self.assertEqual(headers['Content-Length'], '7')
        self.assertEqual(headers['Content-Range'], f'bytes {len(self.original)-7}-{len(self.original)-1}/{len(self.original)}')
        self.assertEqual(body, b'')
        status, headers, body = self.request('/video', headers={'Range': 'bytes=20-'})
        self.assertEqual(status, 206)
        self.assertEqual(body, self.original[20:])
        self.assertEqual(list(self.settings.cache_dir.iterdir()), [])
        for call in self.api.call_args_list:
            self.assertEqual(call.args, ('getFile', {'file_id': 'synthetic-http-file'}))

    def test_download_is_original_bytes_and_csp_stays_private(self):
        status, headers, body = self.request('/download')
        self.assertEqual(status, 200)
        self.assertEqual(body, self.original)
        self.assertTrue(headers['Content-Disposition'].startswith('attachment;'))
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertIn("media-src 'self'", headers['Content-Security-Policy'])
        self.assertNotIn('Location', headers)

    def test_admin_api_remains_unauthorized_despite_valid_player_capability(self):
        for path in ('/api/status', '/api/cameras', '/api/account', '/api/archive'):
            with self.subTest(path=path):
                status, _, body = self.request(path=path)
                self.assertEqual(status, 401)
                self.assertIn(b'authentication_required', body)
        self.assertEqual(self.server.sessions, {})

    def test_tampered_capability_forbidden_and_unknown_shape_not_found(self):
        changed = self.path[:-1]+('0' if self.path[-1] != '0' else '1')
        self.assertEqual(self.request(path=changed)[0], 403)
        self.assertEqual(self.request(path='/player/not-a-capability')[0], 404)
        self.api.assert_not_called()

    def test_allowlist_revocation_and_deletion_apply_to_every_request(self):
        self.assertEqual(self.request('/video', headers={'Range': 'bytes=0-1'})[0], 206)
        self.settings.allowed_users = (42,)
        for suffix in ('', '/video', '/download'):
            self.assertEqual(self.request(suffix)[0], 403)
        self.settings.allowed_users = (42, 43)
        archive = Archive(self.settings)
        try:
            archive.soft_delete(self.key, 42)
        finally:
            archive.close()
        self.assertEqual(self.request('/video')[0], 403)
        self.assertEqual(self.api.call_count, 1)

    def test_concurrency_limit_returns_503_without_consuming_player(self):
        acquired = []
        try:
            for _ in range(8):
                acquired.append(self.server.player_slots.acquire(blocking=False))
            self.assertTrue(all(acquired))
            self.assertEqual(self.request('/video')[0], 503)
            self.api.assert_not_called()
        finally:
            for success in acquired:
                if success:
                    self.server.player_slots.release()
        self.assertEqual(self.request('/video')[0], 200)

    def test_malformed_and_unsatisfiable_range_416_with_no_media_bytes(self):
        for value in ('bytes=999999-', 'bytes=1-2,4-5', 'bytes=-0'):
            with self.subTest(value=value):
                status, headers, body = self.request('/video', headers={'Range': value})
                self.assertEqual(status, 416)
                self.assertEqual(headers['Content-Range'], f'bytes */{len(self.original)}')
                self.assertNotEqual(body, self.original)


if __name__ == '__main__':
    unittest.main()
