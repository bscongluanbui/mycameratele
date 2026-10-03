"""Private-bot CLI integration with local synthetic state only."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import json, os, shutil, tempfile, unittest, uuid
from unittest.mock import patch

from archive_app.__main__ import main
from archive_app.core import Archive, Settings


class PrivateCliTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).parent if os.name == 'nt' else Path(tempfile.gettempdir())
        self.parent = parent.resolve()
        self.root = parent / ('.tmp-cli-' + uuid.uuid4().hex)
        self.root.mkdir()
        (self.root/'input').mkdir()
        self.env = {'STATE_DIR': str(self.root/'data'), 'CACHE_DIR': str(self.root/'cache'),
                    'INPUT_DIR': str(self.root/'input'), 'DISPLAY_TIMEZONE': 'UTC+07:00',
                    'TELEGRAM_OWNER_USER_ID': '42', 'TELEGRAM_ALLOWED_USER_IDS': '77,88',
                    'CACHE_MIN_FREE_GB': '0'}

    def tearDown(self):
        self.assertEqual(self.root.resolve().parent, self.parent)
        shutil.rmtree(self.root)

    def call(self, *args):
        output = StringIO()
        with patch.dict(os.environ, self.env, clear=True), patch('sys.argv', ['archive', *args]), redirect_stdout(output):
            result = main()
        return result, json.loads(output.getvalue())

    def test_doctor_reports_private_destination_without_user_ids(self):
        result, output = self.call('doctor')
        self.assertEqual(result, 0)
        self.assertEqual(output['telegram_destination'], 'owner_private_chat')
        self.assertTrue(output['owner_configured'])
        self.assertEqual(output['allowed_users_count'], 3)
        self.assertNotIn('allowed_users', output)

    def test_backup_cli_creates_one_daily_sqlite_copy(self):
        result, first = self.call('backup')
        self.assertEqual(result, 0); self.assertEqual(first['result'], 'created')
        self.assertEqual(self.call('backup')[1]['result'], 'already_exists_today')
        self.assertEqual(len(list((self.root/'data'/'backups').glob('archive-*.db'))), 1)

    def test_reconcile_requires_owner_and_preserves_confirmed_unique_id(self):
        with patch.dict(os.environ, self.env, clear=True):
            settings = Settings.from_env()
        archive = Archive(settings)
        source = settings.input_dir/'source.mp4'; source.write_bytes(b'synthetic source')
        def normalize(source, dest, settings):
            dest.write_bytes(b'synthetic mp4')
            return {'duration': 60, 'codec_video': 'h264', 'codec_audio': 'aac', 'bytes': 13}
        entry = {'camera': 'fixture_camera', 'record_id': 'fixture', 'path': str(source),
                 'start_time': '2026-10-03T10:00:00+07:00', 'end_time': '2026-10-03T10:01:00+07:00'}
        with patch('archive_app.core.normalize', side_effect=normalize):
            row = archive.ingest_entry(entry)
        archive.conn.execute("UPDATE recordings SET status='upload_unknown' WHERE key=?", (row['key'],)); archive.conn.commit()
        archive.close()
        base = ['reconcile', '--key', row['key'], '--message-id', '100', '--file-id', 'fixture-file']
        with self.assertRaises(ValueError): self.call(*base, '--chat-id', '77')
        result, _ = self.call(*base, '--chat-id', '42', '--file-unique-id', 'fixture-unique', '--media-type', 'video')
        self.assertEqual(result, 0)
        archive = Archive(settings)
        try:
            confirmed = dict(archive.conn.execute('SELECT * FROM recordings WHERE key=?', (row['key'],)).fetchone())
            self.assertEqual(confirmed['status'], 'uploaded'); self.assertEqual(confirmed['chat_id'], '42')
            self.assertEqual(confirmed['file_unique_id'], 'fixture-unique'); self.assertEqual(confirmed['media_type'], 'video')
        finally: archive.close()

    def test_retry_oversize_requeues_only_unposted_known_file_and_can_claim(self):
        with patch.dict(os.environ, self.env, clear=True): settings = Settings.from_env()
        archive = Archive(settings)
        source = settings.input_dir/'retry.mp4'; source.write_bytes(b'synthetic source')
        def normalize(source, dest, settings):
            dest.write_bytes(b'synthetic mp4'); return {'duration': 60, 'codec_video': 'h264', 'codec_audio': 'aac', 'bytes': 13}
        entry = {'camera': 'fixture', 'record_id': 'retry', 'path': str(source),
                 'start_time': '2026-10-03T10:00:00+07:00', 'end_time': '2026-10-03T10:01:00+07:00'}
        with patch('archive_app.core.normalize', side_effect=normalize): row = archive.ingest_entry(entry)
        archive.conn.execute("UPDATE recordings SET status='upload_unknown' WHERE key=?", (row['key'],)); archive.conn.commit(); archive.close()
        with self.assertRaises(ValueError): self.call('retry-oversize', '--key', row['key'])
        archive = Archive(settings)
        archive.conn.execute("UPDATE recordings SET status='needs_review',last_error='missing_or_oversize_file' WHERE key=?", (row['key'],)); archive.conn.commit(); archive.close()
        self.assertEqual(self.call('retry-oversize', '--key', row['key'])[0], 0)
        archive = Archive(settings)
        try: self.assertEqual(archive.claim_upload()['key'], row['key'])
        finally: archive.close()
