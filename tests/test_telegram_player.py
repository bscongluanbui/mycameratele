"""Synthetic direct-player capabilities and exact, non-transcoding byte streams."""
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.parse
import uuid

from archive_app.core import Archive, Settings
from archive_app.telegram_player import (PlayerDenied, PlayerUnavailable,
                                        RangeRejected, TelegramPlayer, byte_range)


class Handler:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.wfile = io.BytesIO()
        self.response_headers = {}
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers[name] = value

    def end_headers(self):
        pass


class CloudResponse(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {'Content-Length': str(len(body))}

    def getcode(self):
        return self.status


class PlayerRangeTests(unittest.TestCase):
    def test_exact_open_and_suffix_ranges(self):
        for value, expected in [(None, (0, 9, False)), ('bytes=0-3', (0, 3, True)),
                                ('bytes=4-', (4, 9, True)), ('bytes=-3', (7, 9, True)),
                                ('bytes=-99', (0, 9, True)), ('bytes=8-99', (8, 9, True))]:
            with self.subTest(value=value):
                self.assertEqual(byte_range(value, 10), expected)

    def test_malformed_multiple_and_unsatisfiable_ranges(self):
        for value in ['bytes=', 'bytes=-', 'bytes=10-', 'bytes=9-1', 'bytes=-0',
                      'bytes=1-2,3-4', 'items=0-1', 'bytes=NaN-2', 'bytes=1--2',
                      'bytes=+'+('9'*150)+'-']:
            with self.subTest(value=value), self.assertRaises(RangeRejected):
                byte_range(value, 10)


class TelegramPlayerTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent if os.name == 'nt' else Path(tempfile.gettempdir()).resolve()
        self.root = self.parent/('.tmp-telegram-player-'+uuid.uuid4().hex)
        self.root.mkdir()
        (self.root/'input').mkdir()
        self.settings = Settings(self.root/'state', self.root/'cache', self.root/'input', 'UTC+07:00',
                                 token='synthetic-token', owner_user_id=42, allowed_users=(42, 43),
                                 api_mode='local', telegram_destination='channel', storage_channel_id=-100123)
        self.settings.player_public_url = 'http://127.0.0.1:8080'
        self.settings.bot_api_file_root = self.root/'bot-api'
        self.media_path = self.settings.bot_api_file_root/self.settings.token/'videos'/'file_1.mp4'
        self.media_path.parent.mkdir(parents=True)
        self.media_bytes = b'0123456789synthetic-mp4-stream'
        self.media_path.write_bytes(self.media_bytes)
        self.archive = Archive(self.settings)
        self.archive.state('telegram_bot_id', '7')
        self.key = 'a'*64
        self.archive.conn.execute('''INSERT INTO recordings
            (key,camera,record_id,start_ms,end_ms,source_path,status,created_at,
             file_id,file_unique_id,bot_id,media_type,media_container,storage_kind,
             storage_chat_id,storage_message_id,chat_id,message_id)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (self.key, 'front', 'sample', 1000, 2000, 'synthetic-unused', 'uploaded', 1,
             'synthetic-file-id', 'synthetic-unique-id', 7, 'video', 'mp4', 'channel',
             -100123, 19, '-100123', 19))
        self.archive.conn.commit()
        self.telegram = Mock()
        self.telegram.request.return_value = {'file_id': 'synthetic-file-id',
            'file_unique_id': 'synthetic-unique-id', 'file_path': str(self.media_path),
            'file_size': len(self.media_bytes)}
        self.player = TelegramPlayer(self.settings, telegram=self.telegram)
        self.url = self.player.issue(self.archive, self.key, 43)
        self.path = urllib.parse.urlsplit(self.url).path
        self.cap = self.path.split('/')[-1]

    def tearDown(self):
        self.archive.close()
        self.assertEqual(self.root.resolve().parent, self.parent)
        shutil.rmtree(self.root)

    def request(self, suffix='', *, headers=None, head=False):
        handler = Handler(headers)
        self.assertTrue(self.player.handle(handler, self.archive, self.path+suffix, head=head))
        return handler

    def update(self, field, value):
        self.assertIn(field, {'deleted_at', 'bot_id', 'file_id', 'storage_chat_id',
                              'storage_kind', 'storage_message_id', 'media_type', 'media_container'})
        self.archive.conn.execute(f'UPDATE recordings SET {field}=? WHERE key=?', (value, self.key))
        self.archive.conn.commit()

    def saved(self):
        name = self.player.PREFIX+__import__('hashlib').sha256(self.cap.encode()).hexdigest()
        return name, json.loads(self.archive.state(name))

    def test_player_is_one_click_html_not_extra_telegram_send(self):
        handler = self.request()
        body = handler.wfile.getvalue().decode()
        self.assertEqual(handler.status, 200)
        self.assertIn('<video controls autoplay muted playsinline', body)
        self.assertIn(self.path+'/video', body)
        self.telegram.request.assert_not_called()
        self.assertNotIn(self.settings.token, body)
        self.assertNotIn(str(self.media_path), body)
        self.assertNotIn(str(self.settings.storage_channel_id), body)
        self.assertNotIn('script', handler.response_headers['Content-Security-Policy'])
        self.assertIn('no-store', handler.response_headers['Cache-Control'])
        self.assertEqual(handler.response_headers['Referrer-Policy'], 'no-referrer')

    def test_exact_range_reads_original_bytes_and_no_archive_cache(self):
        handler = self.request('/video', headers={'Range': 'bytes=4-12'})
        self.assertEqual(handler.status, 206)
        self.assertEqual(handler.wfile.getvalue(), self.media_bytes[4:13])
        self.assertEqual(handler.response_headers['Content-Range'], f'bytes 4-12/{len(self.media_bytes)}')
        self.assertEqual(handler.response_headers['Content-Length'], '9')
        self.assertEqual(list(self.settings.cache_dir.iterdir()), [])
        self.telegram.request.assert_called_once_with('getFile', {'file_id': 'synthetic-file-id'})

    def test_full_download_is_original_bytes_attachment(self):
        handler = self.request('/download')
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.wfile.getvalue(), self.media_bytes)
        self.assertTrue(handler.response_headers['Content-Disposition'].startswith('attachment;'))

    def test_head_has_same_range_headers_without_body(self):
        handler = self.request('/video', headers={'Range': 'bytes=-5'}, head=True)
        self.assertEqual(handler.status, 206)
        self.assertEqual(handler.response_headers['Content-Length'], '5')
        self.assertEqual(handler.wfile.getvalue(), b'')

    def test_pause_seek_and_second_request_revalidate_capability(self):
        self.assertEqual(self.request('/video', headers={'Range': 'bytes=0-3'}).wfile.getvalue(), b'0123')
        self.assertEqual(self.request('/video', headers={'Range': 'bytes=8-10'}).wfile.getvalue(), self.media_bytes[8:11])
        self.settings.allowed_users = (42,)
        self.assertEqual(self.request('/video', headers={'Range': 'bytes=0-3'}).status, 403)
        self.assertEqual(self.telegram.request.call_count, 2)

    def test_deletion_revokes_html_and_stream_before_getfile(self):
        self.update('deleted_at', 123)
        for suffix in ('', '/video', '/download'):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.request(suffix).status, 403)
        self.telegram.request.assert_not_called()

    def test_bot_token_channel_and_placement_changes_revoke(self):
        cases = [('bot_id', 8), ('file_id', 'other-file'), ('storage_chat_id', -100999),
                 ('storage_message_id', 77), ('storage_kind', 'owner_private')]
        for field, value in cases:
            before = self.archive.find_recording(self.key)[field]
            self.update(field, value)
            self.assertEqual(self.request().status, 403)
            self.update(field, before)
        self.settings.storage_channel_id = -100999
        self.assertEqual(self.request().status, 403)
        self.settings.storage_channel_id = -100123
        self.settings.token = 'changed-secret'
        self.assertEqual(self.request().status, 403)

    def test_expired_invalid_nonfinite_and_wrong_actor_denied(self):
        name, original = self.saved()
        for field, value in [('expires', 0), ('expires', float('nan')),
                             ('expires', float('inf')), ('expires', True),
                             ('actor', 99), ('actor', '43'), ('actor', True)]:
            saved = dict(original, **{field: value})
            self.archive.state(name, json.dumps(saved))
            with self.subTest(field=field, value=value):
                self.assertEqual(self.request().status, 403)
        self.archive.state(name, json.dumps(original))
        altered = self.cap[:-1]+('0' if self.cap[-1] != '0' else '1')
        with self.assertRaises(PlayerDenied):
            self.player.resolve(self.archive, altered)

    def test_capability_state_stores_hash_not_url_and_is_short_lived(self):
        name, saved = self.saved()
        self.assertNotIn(self.cap, name)
        self.assertNotIn(self.cap, json.dumps(saved))
        self.assertEqual(saved['actor'], 43)
        with patch('archive_app.telegram_player.time.time', return_value=saved['expires']):
            self.assertEqual(self.request().status, 403)
        for ttl in (0, -1, 901, True, 1.5):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                self.player.issue(self.archive, self.key, 43, ttl=ttl)

    def test_foreign_actor_unknown_record_and_wrong_bot_not_issued(self):
        for actor in (99, '43', True, 0):
            with self.subTest(actor=actor), self.assertRaises(PlayerDenied):
                self.player.issue(self.archive, self.key, actor)
        with self.assertRaises(PlayerDenied):
            self.player.issue(self.archive, 'b'*64, 43)
        self.update('bot_id', 8)
        with self.assertRaises(PlayerDenied):
            self.player.issue(self.archive, self.key, 43)

    def test_untrusted_public_url_rejected(self):
        for value in ('', 'javascript:alert(1)', 'http://user:pass@example.com',
                      'http://example.com/?token=sample', 'http://example.com/#fragment',
                      'http://example.com/player', 'http://example.com:99999',
                      'http://example.com\n'):
            self.settings.player_public_url = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.player.issue(self.archive, self.key, 43)

    def test_malformed_unsatisfiable_and_multiple_range_416(self):
        for value in ('bytes=1-2,5-6', 'bytes=99999-', 'bytes=4-2', 'bytes=-0'):
            handler = self.request('/video', headers={'Range': value})
            with self.subTest(value=value):
                self.assertEqual(handler.status, 416)
                self.assertEqual(handler.response_headers['Content-Range'], f'bytes */{len(self.media_bytes)}')

    def test_api_identity_mismatch_or_failure_never_exposes_details(self):
        self.telegram.request.return_value['file_unique_id'] = 'other-file'
        self.assertEqual(self.request('/video').status, 502)
        self.telegram.request.side_effect = RuntimeError(self.settings.token+str(self.media_path))
        handler = self.request('/video')
        self.assertEqual(handler.status, 502)
        self.assertNotIn(self.settings.token.encode(), handler.wfile.getvalue())
        self.assertNotIn(str(self.media_path).encode(), handler.wfile.getvalue())

    def test_path_escape_other_bot_directory_and_nonregular_rejected(self):
        outside = self.root/'outside.mp4'
        outside.write_bytes(self.media_bytes)
        other = self.settings.bot_api_file_root/'other-bot'/'file.mp4'
        other.parent.mkdir()
        other.write_bytes(self.media_bytes)
        for path in (str(outside), str(other), str(self.media_path.parent), 'videos/file_1.mp4'):
            self.telegram.request.return_value['file_path'] = path
            with self.subTest(path=path):
                self.assertEqual(self.request('/video').status, 502)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX FIFO fixture')
    def test_local_fifo_is_rejected_without_blocking_open(self):
        fifo = self.media_path.parent/'synthetic-pipe.mp4'
        os.mkfifo(fifo)
        self.telegram.request.return_value['file_path'] = str(fifo)
        self.assertEqual(self.request('/video').status, 502)

    @unittest.skipUnless(hasattr(os, 'O_NOFOLLOW'), 'POSIX no-follow fixture')
    def test_final_symlink_swap_at_open_is_rejected(self):
        alternate = self.media_path.parent/'another.mp4'
        alternate.write_bytes(self.media_bytes)
        original_open = os.open
        def swap_before_open(path, flags):
            self.media_path.unlink()
            self.media_path.symlink_to(alternate.name)
            return original_open(path, flags)
        with patch('archive_app.telegram_player.os.open', side_effect=swap_before_open):
            self.assertEqual(self.request('/video').status, 502)

    def test_deleted_during_getfile_is_not_streamed(self):
        response = dict(self.telegram.request.return_value)
        def get_file(*args):
            self.update('deleted_at', 123)
            return response
        self.telegram.request.side_effect = get_file
        self.assertEqual(self.request('/video').status, 403)

    def test_mp4_document_plays_raw_document_only_downloads(self):
        self.update('media_type', 'document')
        self.url = self.player.issue(self.archive, self.key, 43)
        self.path = urllib.parse.urlsplit(self.url).path
        self.assertEqual(self.request('/video').wfile.getvalue(), self.media_bytes)
        self.update('media_container', 'bin')
        self.url = self.player.issue(self.archive, self.key, 43)
        self.path = urllib.parse.urlsplit(self.url).path
        self.assertNotIn(b'<video ', self.request().wfile.getvalue())
        self.assertEqual(self.request('/video').status, 502)
        self.assertEqual(self.request('/download').wfile.getvalue(), self.media_bytes)

    def test_camera_name_is_escaped_and_no_script_asset(self):
        self.archive.conn.execute('INSERT INTO cameras(id,name,created_at) VALUES(?,?,?)',
                                  ('front', '<script>alert("x")</script>', 1))
        self.archive.conn.commit()
        body = self.request().wfile.getvalue()
        self.assertNotIn(b'<script>', body)
        self.assertIn(b'&lt;script&gt;', body)

    def test_unknown_route_does_not_take_dashboard_and_malformed_cap_404(self):
        handler = Handler()
        self.assertFalse(self.player.handle(handler, self.archive, '/api/status'))
        self.assertTrue(self.player.handle(handler, self.archive, '/player/invalid'))
        self.assertEqual(handler.status, 404)

    def test_cloud_proxy_checks_exact_range_without_public_api_url(self):
        self.settings.api_mode = 'cloud'
        self.telegram.request.return_value['file_path'] = 'videos/file_1.mp4'
        opener = Mock(return_value=CloudResponse(self.media_bytes[3:8], 206,
            {'Content-Length': '5', 'Content-Range': f'bytes 3-7/{len(self.media_bytes)}'}))
        self.player.opener = opener
        handler = self.request('/video', headers={'Range': 'bytes=3-7'})
        self.assertEqual(handler.status, 206)
        self.assertEqual(handler.wfile.getvalue(), self.media_bytes[3:8])
        request = opener.call_args.args[0]
        self.assertEqual(request.headers['Range'], 'bytes=3-7')
        self.assertIn('/file/bot'+self.settings.token+'/', request.full_url)
        self.assertNotIn(self.settings.token.encode(), handler.wfile.getvalue())
        self.assertNotIn('Location', handler.response_headers)

    def test_cloud_refuses_ignored_range_wrong_size_and_path_injection(self):
        self.settings.api_mode = 'cloud'
        self.telegram.request.return_value['file_path'] = 'videos/file_1.mp4'
        self.player.opener = Mock(return_value=CloudResponse(self.media_bytes))
        self.assertEqual(self.request('/video', headers={'Range': 'bytes=3-7'}).status, 502)
        for path in ('../secret', '/absolute', 'https://other.example/file', 'videos/a?token=x'):
            self.telegram.request.return_value['file_path'] = path
            with self.subTest(path=path):
                self.assertEqual(self.request('/video').status, 502)


if __name__ == '__main__':
    unittest.main()
