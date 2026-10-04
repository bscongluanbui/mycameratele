"""Private archive migration, cache retention and atomic catalog backups.

Fixtures are synthetic. No Telegram or camera network calls are performed.
"""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from archive_app.core import Archive, Settings, record_key


class PrivateSettingsTests(unittest.TestCase):
    def load(self, **values):
        environment = {'DISPLAY_TIMEZONE': 'UTC+07:00', **values}
        with patch.dict(os.environ, environment, clear=True):
            return Settings.from_env()

    def test_owner_is_upload_destination_and_default_authorized_user(self):
        settings = self.load(TELEGRAM_OWNER_USER_ID='42')
        self.assertEqual(settings.effective_owner, 42)
        self.assertEqual(settings.chat_id, '42')
        self.assertEqual(settings.allowed_users, (42,))
        self.assertEqual(settings.cache_retention_hours, 0)

    def test_positive_legacy_chat_alias_is_accepted(self):
        settings = self.load(TELEGRAM_CHAT_ID='42')
        self.assertEqual(settings.owner_user_id, 42)
        self.assertEqual(settings.effective_owner, 42)
        self.assertEqual(settings.allowed_users, (42,))

    def test_explicit_owner_overrides_obsolete_channel(self):
        settings = self.load(TELEGRAM_OWNER_USER_ID='42', TELEGRAM_CHAT_ID='-1009876543210')
        self.assertEqual(settings.effective_owner, 42)
        self.assertEqual(settings.chat_id, '42')

    def test_allowlist_supports_multiple_viewers_deduped_and_includes_owner(self):
        settings = self.load(TELEGRAM_OWNER_USER_ID='42', TELEGRAM_ALLOWED_USER_IDS='88, 99\n88 42')
        self.assertEqual(settings.allowed_users, (42, 88, 99))
        self.assertEqual(settings.effective_owner, 42)

    def test_invalid_allowlist_ids_are_rejected(self):
        for value in ('0', '-1', 'true', '42.0', '12e2', 'word', '42,0'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(TELEGRAM_OWNER_USER_ID='42', TELEGRAM_ALLOWED_USER_IDS=value)

    def test_invalid_owner_ids_are_rejected_even_with_legacy_positive_id(self):
        for value in ('0', '-100123', 'false', '42.0', '@my_channel'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(TELEGRAM_OWNER_USER_ID=value, TELEGRAM_CHAT_ID='42')

    def test_negative_legacy_channel_cannot_be_upload_owner(self):
        with self.assertRaises(ValueError):
            self.load(TELEGRAM_CHAT_ID='-100123')

    def test_ownerless_disabled_upload_setup_is_allowed(self):
        settings = self.load(ENABLE_UPLOAD='false', TELEGRAM_BOT_TOKEN='fixture-token')
        self.assertEqual(settings.effective_owner, 0)

    def test_enabled_upload_needs_owner_and_token(self):
        for values in ({'TELEGRAM_BOT_TOKEN': 'fixture-token'}, {'TELEGRAM_OWNER_USER_ID': '42'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.load(ENABLE_UPLOAD='true', **values)

    def test_enabled_upload_with_owner_succeeds(self):
        settings = self.load(ENABLE_UPLOAD='true', TELEGRAM_OWNER_USER_ID='42', TELEGRAM_BOT_TOKEN='fixture-token')
        self.assertTrue(settings.enable_upload)

    def test_username_is_normalized_and_validated(self):
        self.assertEqual(self.load(TELEGRAM_BOT_USERNAME='@ArchiveTestBot').bot_username, 'ArchiveTestBot')
        for value in ('ab', '1badbot', 'https://t.me/bot', 'bad/name', 'é_archive_bot'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(TELEGRAM_BOT_USERNAME=value)

    def test_invalid_retention_is_rejected(self):
        for value in ('-1', 'NaN', 'inf', 'text'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(CACHE_RETENTION_HOURS=value)

    def test_direct_settings_owner_property_never_returns_channel(self):
        settings = Settings(Path('state'), Path('cache'), Path('input'), 'UTC+07:00', chat_id='-100123')
        self.assertEqual(settings.effective_owner, 0)
        settings.chat_id = '42'
        self.assertEqual(settings.effective_owner, 42)
        settings.owner_user_id = 99
        self.assertEqual(settings.effective_owner, 99)
        settings.owner_user_id = True
        self.assertEqual(settings.effective_owner, 0)


class PrivateCoreTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent if os.name == 'nt' else Path(tempfile.gettempdir()).resolve()
        self.root = self.parent / ('.tmp-private-core-' + uuid.uuid4().hex)
        self.root.mkdir()
        (self.root / 'input').mkdir()
        self.source = self.root / 'input' / 'source.mp4'
        self.source.write_bytes(b'synthetic-camera-export')
        self.settings = Settings(self.root/'state', self.root/'cache', self.root/'input', 'UTC+07:00',
                                 keep_cache=False, min_free_bytes=0, owner_user_id=42,
                                 bot_username='ArchiveTestBot', cache_retention_hours=24)
        self.archive = Archive(self.settings)
        self.normalizer = patch('archive_app.core.normalize', side_effect=self.normalize)
        self.normalizer.start()

    def tearDown(self):
        self.normalizer.stop()
        self.archive.close()
        self.assertEqual(self.root.resolve().parent, self.parent)
        shutil.rmtree(self.root)

    @staticmethod
    def normalize(source, destination, settings):
        destination = Path(destination)
        destination.write_bytes(b'synthetic-normalized-mp4')
        return {'duration': 60, 'codec_video': 'h264', 'codec_audio': 'aac', 'bytes': destination.stat().st_size}

    def ingest(self):
        descriptor = {'camera': 'front', 'record_id': 'clip-001', 'path': str(self.source),
                      'start_time': '2026-10-03T10:00:00+07:00', 'end_time': '2026-10-03T10:01:00+07:00'}
        return self.archive.ingest_entry(descriptor)['key']

    def row(self, key):
        return dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?', (key,)).fetchone())

    def upload(self, key, **fields):
        self.archive.mark_uploaded(key, 42, 17, 'synthetic-file-id',
                                   file_unique_id='synthetic-unique-id', media_type='video', bot_id=123, **fields)

    def test_new_metadata_is_committed_and_survives_reopen(self):
        key = self.ingest()
        before = time.time()
        self.upload(key)
        self.archive.close()
        self.archive = Archive(self.settings)
        row = self.row(key)
        self.assertEqual(row['status'], 'uploaded')
        self.assertEqual(row['chat_id'], '42')
        self.assertEqual(row['file_unique_id'], 'synthetic-unique-id')
        self.assertEqual(row['media_type'], 'video')
        self.assertEqual(row['bot_id'], 123)
        self.assertGreaterEqual(row['uploaded_at'], before)
        self.assertIsNone(row['cleaned_at'])
        self.assertIsNone(self.archive.claim_upload())

    def test_legacy_four_argument_metadata_call_still_works(self):
        key = self.ingest()
        self.archive.mark_uploaded(key, '-1001234567890', 17, 'legacy-id')
        row = self.row(key)
        self.assertEqual(row['chat_id'], '-1001234567890')
        self.assertIsNone(row['file_unique_id'])
        self.assertIsNone(row['media_type'])
        self.assertIsNotNone(row['uploaded_at'])

    def test_invalid_optional_metadata_does_not_change_row(self):
        key = self.ingest()
        for fields in ({'file_unique_id': ''}, {'media_type': 'photo'}, {'bot_id': -1}, {'bot_id': True}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.archive.mark_uploaded(key, 42, 17, 'id', **fields)
            self.assertEqual(self.row(key)['status'], 'downloaded')

    def test_24_hour_retention_starts_after_committed_upload(self):
        key = self.ingest()
        self.upload(key)
        row = self.row(key)
        cached = Path(row['local_path'])
        with patch('archive_app.core.time.time', return_value=row['uploaded_at'] + 86399):
            self.assertFalse(self.archive.cleanup(key))
        self.assertTrue(cached.is_file())
        with patch('archive_app.core.time.time', return_value=row['uploaded_at'] + 86400):
            self.assertTrue(self.archive.cleanup(key))
        cleaned = self.row(key)
        self.assertFalse(cached.exists())
        self.assertTrue(self.source.is_file())
        self.assertEqual(cleaned['status'], 'uploaded')
        self.assertEqual(cleaned['file_id'], row['file_id'])
        self.assertEqual(cleaned['cleaned_at'], row['uploaded_at'] + 86400)
        self.assertEqual(len(self.archive.browse()['recordings']), 1)

    def test_cleanup_is_idempotent_without_restarting_retention(self):
        self.settings.cache_retention_hours = 0
        key = self.ingest()
        self.upload(key)
        self.assertTrue(self.archive.cleanup(key))
        cleaned_at = self.row(key)['cleaned_at']
        self.assertFalse(self.archive.cleanup(key))
        self.assertEqual(self.row(key)['cleaned_at'], cleaned_at)

    def test_status_reports_private_destination_without_sensitive_ids(self):
        status = self.archive.status()
        self.assertEqual(status['telegram_destination'], 'owner_private_chat')
        self.assertTrue(status['owner_configured'])
        self.assertFalse(status['owner_started'])
        self.assertEqual(status['allowed_users_count'], 1)
        self.assertEqual(status['cache_retention_hours'], 24)
        self.assertEqual(status['version'], '2.4')
        self.archive.state('telegram_owner_started:42', '1')
        self.assertTrue(self.archive.status()['owner_started'])
        for name in ('owner_user_id', 'chat_id', 'file_id', 'file_unique_id', 'token'):
            self.assertNotIn(name, status)

    def test_keep_cache_overrides_expired_retention(self):
        key = self.ingest()
        self.upload(key)
        self.settings.keep_cache = True
        with patch('archive_app.core.time.time', return_value=self.row(key)['uploaded_at'] + 86401):
            self.assertFalse(self.archive.cleanup(key))
        self.assertTrue(Path(self.row(key)['local_path']).exists())

    def test_uploaded_without_commit_metadata_is_not_cleaned(self):
        self.settings.cache_retention_hours = 0
        key = self.ingest()
        with self.archive.conn:
            self.archive.conn.execute("UPDATE recordings SET status='uploaded' WHERE key=?", (key,))
        self.assertFalse(self.archive.cleanup(key))
        self.assertTrue(Path(self.row(key)['local_path']).exists())

    def test_cleanup_rejects_symlink_without_deleting_target(self):
        self.settings.cache_retention_hours = 0
        key = self.ingest()
        self.upload(key)
        target = self.root/'outside.mp4'
        target.write_bytes(b'must-remain')
        cached = Path(self.row(key)['local_path'])
        cached.unlink()
        try:
            cached.symlink_to(target)
        except OSError:
            self.skipTest('Host has no symlink creation privilege')
        with self.assertRaises(ValueError):
            self.archive.cleanup(key)
        self.assertEqual(target.read_bytes(), b'must-remain')

    def test_browse_returns_private_bot_link_not_channel_reference(self):
        key = self.ingest()
        self.upload(key)
        public = self.archive.browse()['recordings'][0]
        self.assertEqual(public['telegram_url'], f'https://t.me/ArchiveTestBot?start=play_{key[:32]}')
        self.assertTrue(public['telegram_available'])
        for field in ('file_id', 'file_unique_id', 'bot_id', 'source_path', 'local_path', 'token'):
            self.assertNotIn(field, public)

    def test_unknown_username_has_replay_flag_but_no_link(self):
        key = self.ingest()
        self.upload(key)
        self.settings.bot_username = ''
        public = self.archive.browse()['recordings'][0]
        self.assertIsNone(public['telegram_url'])
        self.assertTrue(public['telegram_available'])
        self.archive.state('telegram_bot_username', 'DiscoveredBot')
        self.assertEqual(self.archive.telegram_url(self.row(key)), f'https://t.me/DiscoveredBot?start=play_{key[:32]}')

    def test_invalid_persisted_username_cannot_generate_external_link(self):
        key = self.ingest()
        self.upload(key)
        self.settings.bot_username = ''
        self.archive.state('telegram_bot_username', 'bad/name')
        self.assertIsNone(self.archive.telegram_url(self.row(key)))

    def test_downloaded_rows_have_no_replay_capability(self):
        self.ingest()
        row = self.archive.browse(status='all')['recordings'][0]
        self.assertFalse(row['telegram_available'])
        self.assertIsNone(row['telegram_url'])

    def test_daily_backup_has_durable_rows_and_skips_duplicate_day(self):
        key = self.ingest()
        self.upload(key)
        backup = self.archive.backup_daily()
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.parent, self.settings.state_dir/'backups')
        with closing(sqlite3.connect(backup)) as connection:
            self.assertEqual(connection.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            row = connection.execute('SELECT key,file_id,file_unique_id FROM recordings').fetchone()
            self.assertEqual(row, (key, 'synthetic-file-id', 'synthetic-unique-id'))
        self.assertIsNone(self.archive.backup_daily())
        self.assertEqual(list(backup.parent.glob('*.partial')), [])
        self.assertTrue(self.source.exists())
        self.assertTrue(Path(self.row(key)['local_path']).exists())

    def test_backup_does_not_include_uncommitted_writer_state(self):
        self.archive.state('committed', 'yes')
        self.archive.conn.execute("INSERT INTO state(name,value) VALUES('pending','no')")
        try:
            backup = self.archive.backup_daily()
            with closing(sqlite3.connect(backup)) as connection:
                self.assertEqual(connection.execute("SELECT value FROM state WHERE name='committed'").fetchone()[0], 'yes')
                self.assertIsNone(connection.execute("SELECT value FROM state WHERE name='pending'").fetchone())
        finally:
            self.archive.conn.rollback()

    def test_failed_atomic_publish_removes_partial_only(self):
        key = self.ingest()
        self.upload(key)
        with patch('archive_app.core.os.replace', side_effect=OSError('synthetic-publish-failure')):
            with self.assertRaises(OSError):
                self.archive.backup_daily()
        root = self.settings.state_dir/'backups'
        self.assertEqual(list(root.glob('*.db')), [])
        self.assertEqual(list(root.glob('*.partial')), [])
        self.assertTrue(Path(self.row(key)['local_path']).exists())
        self.assertEqual(self.row(key)['status'], 'uploaded')

    def test_backup_keeps_at_least_seven_and_only_prunes_managed_names(self):
        root = self.settings.state_dir/'backups'
        root.mkdir()
        today = datetime(2026, 10, 3, 10, tzinfo=timezone(timedelta(hours=7)))
        for age in range(1, 12):
            path = root/f'archive-{(today.date()-timedelta(days=age)).isoformat()}.db'
            with closing(sqlite3.connect(path)) as connection:
                connection.execute('CREATE TABLE fixture (value TEXT)')
                connection.commit()
        unmanaged = [root/'notes.db', root/'archive-2026-99-99.db', root/'archive-2026-09-01.db.copy']
        for path in unmanaged:
            path.write_bytes(b'unmanaged-preserved')
        with patch('archive_app.core.time.time', return_value=today.timestamp()):
            published = self.archive.backup_daily()
        self.assertEqual(published.name, 'archive-2026-10-03.db')
        for path in unmanaged:
            self.assertEqual(path.read_bytes(), b'unmanaged-preserved')
        valid = [p for p in root.glob('archive-*.db') if p.name != 'archive-2026-99-99.db']
        self.assertEqual(len(valid), 7)

    def test_backup_keeps_seven_snapshots_even_when_daily_schedule_had_gaps(self):
        root = self.settings.state_dir/'backups'
        root.mkdir()
        today = datetime(2026, 10, 3, 10, tzinfo=timezone(timedelta(hours=7)))
        for age in (1, 3, 8, 10, 15, 20):
            with closing(sqlite3.connect(root/f'archive-{(today.date()-timedelta(days=age)).isoformat()}.db')) as connection:
                connection.execute('CREATE TABLE fixture (value TEXT)')
                connection.commit()
        with patch('archive_app.core.time.time', return_value=today.timestamp()):
            self.archive.backup_daily()
        self.assertEqual(len(list(root.glob('archive-*.db'))), 7)

    def test_backup_rejects_short_retention(self):
        with self.assertRaises(ValueError):
            self.archive.backup_daily(retention_days=6)

    def test_backup_rejects_symlink_directory_without_touching_target(self):
        outside = self.root/'outside-backups'
        outside.mkdir()
        sentinel = outside/'do-not-touch'
        sentinel.write_bytes(b'preserved')
        try:
            (self.settings.state_dir/'backups').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Host has no symlink creation privilege')
        with self.assertRaises(ValueError):
            self.archive.backup_daily()
        self.assertEqual(sentinel.read_bytes(), b'preserved')
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ['do-not-touch'])

    def test_backup_does_not_follow_managed_filename_symlink(self):
        root = self.settings.state_dir/'backups'
        root.mkdir()
        outside = self.root/'outside-must-remain.db'
        outside.write_bytes(b'preserved')
        try:
            (root/'archive-2000-01-01.db').symlink_to(outside)
        except OSError:
            self.skipTest('Host has no symlink creation privilege')
        self.archive.backup_daily()
        self.assertEqual(outside.read_bytes(), b'preserved')
        self.assertTrue((root/'archive-2000-01-01.db').is_symlink())

    def test_backup_lock_is_released_and_competing_snapshot_is_skipped(self):
        root = self.settings.state_dir/'backups'
        root.mkdir()
        with self.archive._backup_lock(root) as acquired:
            self.assertTrue(acquired)
            self.assertIsNone(self.archive.backup_daily())
        self.assertTrue(self.archive.backup_daily().is_file())

    def test_old_schema_migration_preserves_keys_paths_metadata_and_status(self):
        self.archive.close()
        database = self.settings.state_dir/'archive.db'
        for suffix in ('', '-wal', '-shm'):
            path = Path(str(database)+suffix)
            if path.exists():
                path.unlink()
        key = record_key({'camera': 'front', 'record_id': 'legacy-clip'})
        local = self.settings.cache_dir/(key+'.mp4')
        local.write_bytes(b'legacy-cache-preserved')
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript('''CREATE TABLE recordings (
                key TEXT PRIMARY KEY, camera TEXT NOT NULL, record_id TEXT NOT NULL,
                start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
                source_path TEXT NOT NULL, local_path TEXT, status TEXT NOT NULL,
                duration REAL, codec_video TEXT, codec_audio TEXT, file_size INTEGER,
                sha256 TEXT, chat_id TEXT, message_id INTEGER, file_id TEXT,
                last_error TEXT, attempt_id TEXT, retry_at REAL DEFAULT 0,
                created_at REAL NOT NULL);''')
            connection.execute('''INSERT INTO recordings(key,camera,record_id,start_ms,end_ms,
                source_path,local_path,status,chat_id,message_id,file_id,created_at)
                VALUES(?,?,?,?,?,?,?,'uploaded',?,?,?,?)''',
                (key,'front','legacy-clip',1790996400000,1790996460000,str(self.source),str(local),'-1001234567890',17,'legacy-file-id',1))
            connection.commit()
        migration_time = time.time()
        self.archive = Archive(self.settings)
        row = self.row(key)
        self.assertEqual(row['status'], 'uploaded')
        self.assertEqual(row['local_path'], str(local))
        self.assertEqual(row['chat_id'], '-1001234567890')
        self.assertEqual(row['file_id'], 'legacy-file-id')
        self.assertEqual(row['message_id'], 17)
        self.assertIsNone(row['uploaded_at'])
        self.assertIsNone(row['file_unique_id'])
        self.assertIsNone(row['media_type'])
        self.assertIsNone(self.archive.claim_upload())
        self.assertFalse(self.archive.cleanup(key))
        self.assertEqual(local.read_bytes(), b'legacy-cache-preserved')
        self.assertGreaterEqual(float(self.archive.state('cleanup_legacy_hold_since')), migration_time)
        with patch('archive_app.core.time.time', return_value=migration_time + 86401):
            self.assertTrue(self.archive.cleanup(key))
        self.assertEqual(self.row(key)['status'], 'uploaded')
        self.assertEqual(self.row(key)['file_id'], 'legacy-file-id')


if __name__ == '__main__':
    unittest.main()
