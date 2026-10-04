"""Synthetic SQLite deletion/window checks; no camera or live bot operations."""
from contextlib import closing
from datetime import datetime
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from archive_app.core import Archive, Settings, record_key


def ms(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


class ArchiveDeletionTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent if os.name == 'nt' else Path(tempfile.gettempdir()).resolve()
        self.root = self.parent / ('.tmp-deletion-' + uuid.uuid4().hex)
        self.root.mkdir()
        (self.root/'input').mkdir()
        self.settings = Settings(self.root/'state', self.root/'cache', self.root/'input', 'UTC+07:00',
                                 owner_user_id=42, allowed_users=(7, 9), bot_username='ArchiveTestBot',
                                 min_free_bytes=0)
        self.archive = Archive(self.settings)
        self.begin = ms('2026-10-03T00:00:00+07:00')
        self.end = ms('2026-10-04T00:00:00+07:00')

    def tearDown(self):
        self.archive.close()
        self.assertEqual(self.root.resolve().parent, self.parent)
        shutil.rmtree(self.root)

    def add(self, rid='one', camera='front', start=None, end=None, status='uploaded'):
        start = self.begin + 1000 if start is None else start
        end = self.begin + 2000 if end is None else end
        key = record_key({'camera': camera, 'record_id': rid})
        with self.archive.conn:
            self.archive.conn.execute('INSERT OR IGNORE INTO cameras(id,name,created_at) VALUES(?,?,?)', (camera, camera, time.time()))
            self.archive.conn.execute('''INSERT INTO recordings(key,camera,record_id,start_ms,end_ms,
                source_path,status,chat_id,message_id,file_id,file_unique_id,media_type,uploaded_at,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (key, camera, rid, start, end, str(self.root/'input'/'source.mp4'), status,
                 '42', 17, 'file-'+rid, 'unique-'+rid, 'video', 1000.0, time.time()))
        return key

    def row(self, key):
        return dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?', (key,)).fetchone())

    def audits(self, key):
        return [dict(row) for row in self.archive.conn.execute('SELECT * FROM recording_audit WHERE recording_key=? ORDER BY id', (key,))]

    def test_each_allowed_viewer_and_owner_can_delete_and_restore(self):
        for actor in (7, 9, 42):
            key = self.add('actor-'+str(actor))
            self.assertTrue(self.archive.soft_delete(key, actor))
            self.assertEqual(self.row(key)['deleted_by'], actor)
            self.assertTrue(self.archive.restore_recording(key, actor))
            self.assertIsNone(self.row(key)['deleted_by'])

    def test_unknown_ids_and_noninteger_ids_cannot_mutate(self):
        key = self.add()
        for actor in (8, 0, -1, True, '7', 7.0, None):
            for method in (self.archive.soft_delete, self.archive.restore_recording):
                with self.subTest(actor=actor, method=method.__name__), self.assertRaises(PermissionError):
                    method(key, actor)
        self.assertIsNone(self.row(key)['deleted_at'])
        self.assertEqual(self.audits(key), [])

    def test_without_owner_or_allowlist_nobody_can_mutate(self):
        self.settings.owner_user_id = 0
        self.settings.allowed_users = ()
        key = self.add()
        with self.assertRaises(PermissionError):
            self.archive.soft_delete(key, 42)

    def test_delete_and_restore_keep_all_original_metadata_and_files(self):
        key = self.add()
        cached = self.root/'cache'/(key+'.mp4')
        source = self.root/'input'/'source.mp4'
        cached.write_bytes(b'synthetic-cache')
        source.write_bytes(b'synthetic-source')
        with self.archive.conn:
            self.archive.conn.execute('UPDATE recordings SET local_path=? WHERE key=?', (str(cached), key))
        before = self.row(key)
        self.archive.soft_delete(key, 7)
        after = self.row(key)
        for field in before:
            if field not in ('deleted_at', 'deleted_by'):
                self.assertEqual(before[field], after[field], field)
        self.archive.restore_recording(key, 9)
        self.assertEqual(self.row(key), before)
        self.assertEqual(cached.read_bytes(), b'synthetic-cache')
        self.assertEqual(source.read_bytes(), b'synthetic-source')
        self.assertEqual(self.audits(key)[1]['actor'], 9)

    def test_repeated_clicks_are_idempotent_and_audited_once(self):
        key = self.add()
        self.assertTrue(self.archive.soft_delete(key, 7))
        first = self.row(key)
        self.assertFalse(self.archive.soft_delete(key, 9))
        self.assertEqual(self.row(key), first)
        self.assertEqual([row['action'] for row in self.audits(key)], ['delete'])
        self.assertTrue(self.archive.restore_recording(key, 9))
        self.assertFalse(self.archive.restore_recording(key, 7))
        self.assertEqual([row['action'] for row in self.audits(key)], ['delete', 'restore'])

    def test_unknown_or_unuploaded_rows_never_create_audit(self):
        key = self.add(status='downloaded')
        self.assertFalse(self.archive.soft_delete(key, 7))
        self.assertFalse(self.archive.soft_delete('0'*64, 7))
        self.assertFalse(self.archive.restore_recording(key, 7))
        self.assertEqual(self.audits(key), [])
        self.assertIsNone(self.row(key)['deleted_at'])

    def test_mutations_require_full_stable_keys(self):
        for key in ('a'*32, 'a'*63, 'A'*64, 'x'*64, None, "' OR 1=1--"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.archive.soft_delete(key, 7)

    def test_deleted_rows_hidden_in_all_public_and_bot_catalogs(self):
        key = self.add()
        stale = self.row(key)
        self.assertIsNotNone(self.archive.telegram_url(stale))
        self.archive.soft_delete(key, 7)
        self.assertEqual(self.archive.browse()['total'], 0)
        self.assertEqual(self.archive.browse(status='all')['total'], 0)
        self.assertEqual(self.archive.calendar('front')['years'], [])
        self.assertEqual(self.archive.list_day('2026-10-03'), [])
        self.assertEqual(self.archive.list_window(self.begin, self.end)['total'], 0)
        self.assertEqual(self.archive.window_cameras(self.begin, self.end)['total'], 0)
        self.assertIsNone(self.archive.find_recording(key[:32]))
        self.assertIsNone(self.archive.telegram_url(stale))
        self.assertEqual(self.archive.cameras()[0]['record_count'], 0)
        self.assertEqual(self.archive.cameras()[0]['uploaded_count'], 0)
        status = self.archive.status()
        self.assertEqual(status['counts']['recordings'], 0)
        self.assertEqual(status['counts']['deleted'], 1)
        self.assertEqual(status['queue']['uploaded'], 1)

    def test_restore_returns_same_clip_to_global_catalog(self):
        key = self.add()
        self.archive.soft_delete(key, 7)
        self.archive.restore_recording(key, 9)
        self.assertEqual(self.archive.browse()['recordings'][0]['key'], key)
        self.assertEqual(self.archive.calendar('front')['years'][0]['year'], 2026)
        self.assertEqual(self.archive.list_day('2026-10-03')[0]['file_id'], 'file-one')
        self.assertEqual(self.archive.list_window(self.begin, self.end)['total'], 1)
        self.assertEqual(self.archive.window_cameras(self.begin, self.end)['cameras'][0]['count'], 1)

    def test_ingest_deleted_uploaded_clip_does_not_resurrect_or_reupload(self):
        source = self.root/'input'/'source.mp4'
        source.write_bytes(b'synthetic-original')
        key = self.add()
        self.archive.soft_delete(key, 7)
        before = self.row(key)
        descriptor = {'camera': 'front', 'record_id': 'one', 'path': str(source),
                      'start_time': datetime.fromtimestamp(before['start_ms']/1000).astimezone().isoformat(),
                      'end_time': datetime.fromtimestamp(before['end_ms']/1000).astimezone().isoformat()}
        with patch('archive_app.core.normalize') as normalizer:
            result = self.archive.ingest_entry(descriptor)
        normalizer.assert_not_called()
        self.assertIsNotNone(result['deleted_at'])
        self.assertEqual(self.row(key), before)
        self.assertIsNone(self.archive.claim_upload())

    def test_tombstone_survives_database_reopen(self):
        key = self.add()
        self.archive.soft_delete(key, 7)
        self.archive.close()
        self.archive = Archive(self.settings)
        self.assertIsNone(self.archive.find_recording(key[:32]))
        self.assertEqual(self.archive.find_recording(key, include_deleted=True)['deleted_by'], 7)
        self.assertEqual(self.audits(key)[0]['action'], 'delete')

    def test_find_recording_rejects_malformed_prefix(self):
        self.add()
        for prefix in ('a', 'a'*31, 'a'*33, 'a'*65, 'A'*32, 'g'*32, None, True, '%'):
            with self.subTest(prefix=prefix):
                self.assertIsNone(self.archive.find_recording(prefix))

    def test_find_recording_rejects_ambiguous_prefix(self):
        first = self.add('first')
        second = self.add('second')
        prefix = 'a'*32
        with self.archive.conn:
            self.archive.conn.execute('UPDATE recordings SET key=? WHERE key=?', (prefix+'1'*32, first))
            self.archive.conn.execute('UPDATE recordings SET key=? WHERE key=?', (prefix+'2'*32, second))
        self.assertIsNone(self.archive.find_recording(prefix))
        self.assertEqual(self.archive.find_recording(prefix+'1'*32)['record_id'], 'first')
        self.archive.soft_delete(prefix+'1'*32, 7)
        self.assertEqual(self.archive.find_recording(prefix)['record_id'], 'second')
        self.assertIsNone(self.archive.find_recording(prefix, include_deleted=True))

    def test_trash_is_paginated_and_includes_internal_metadata(self):
        keys = [self.add(str(i)) for i in range(3)]
        for i, key in enumerate(keys):
            with patch('archive_app.core.time.time', return_value=100+i):
                self.archive.soft_delete(key, 7)
        page = self.archive.trash(offset=1, limit=1)
        self.assertEqual(page['total'], 3)
        self.assertEqual(page['offset'], 1)
        self.assertEqual(page['limit'], 1)
        self.assertEqual(page['recordings'][0]['key'], keys[1])
        self.assertEqual(page['recordings'][0]['camera_name'], 'front')
        self.assertIn('file_id', page['recordings'][0])
        self.assertEqual(self.archive.trash(order='asc', limit=1)['recordings'][0]['key'], keys[0])

    def test_window_uses_overlap_and_exclusive_upper_bound(self):
        inside = self.add('inside')
        cross_start = self.add('cross-start', start=self.begin-1000, end=self.begin+1000)
        cross_end = self.add('cross-end', start=self.end-1000, end=self.end+1000)
        self.add('ends-at-start', start=self.begin-2000, end=self.begin)
        self.add('starts-at-end', start=self.end, end=self.end+1000)
        self.add('pending', status='downloaded')
        result = self.archive.list_window(self.begin, self.end)
        self.assertEqual(result['total'], 3)
        self.assertEqual({r['key'] for r in result['recordings']}, {inside, cross_start, cross_end})

    def test_cross_midnight_clip_is_in_each_local_day_window(self):
        key = self.add('cross', start=self.end-60000, end=self.end+60000)
        self.assertEqual(self.archive.list_window(self.begin, self.end)['recordings'][0]['key'], key)
        self.assertEqual(self.archive.list_window(self.end, self.end+86400000)['recordings'][0]['key'], key)
        self.assertEqual(self.archive.list_day('2026-10-04')[0]['key'], key)

    def test_window_isolates_camera_and_paginated_stable_sort(self):
        keys = [self.add(str(i), start=self.begin+1000+i*1000, end=self.begin+2000+i*1000) for i in range(4)]
        self.add('other', camera='back')
        page = self.archive.list_window(self.begin, self.end, camera='front', offset=1, limit=2)
        self.assertEqual(page['total'], 4)
        self.assertEqual([r['key'] for r in page['recordings']], keys[1:3])
        self.assertEqual(self.archive.list_window(self.begin, self.end, camera='front', order='desc', limit=1)['recordings'][0]['key'], keys[3])
        self.assertEqual(self.archive.list_window(self.begin, self.end, camera='unknown')['total'], 0)

    def test_window_cameras_use_friendly_casefold_names_counts_and_pagination(self):
        self.add('front1', camera='front')
        self.add('front2', camera='front')
        self.add('back', camera='back')
        self.add('top', camera='top')
        self.archive.update_camera('front', {'name': 'B Front'})
        self.archive.update_camera('back', {'name': 'a Back'})
        self.archive.update_camera('top', {'name': 'c Top'})
        page = self.archive.window_cameras(self.begin, self.end, offset=1, limit=1)
        self.assertEqual(page['total'], 3)
        self.assertEqual(page['cameras'], [{'id': 'front', 'name': 'B Front', 'count': 2}])

    def test_window_rejects_invalid_bounds(self):
        for start, end in ((True, self.end), (self.begin, False), (float(self.begin), self.end),
                           (self.begin, self.begin), (self.end, self.begin), (-1, 1),
                           (self.begin, self.begin+32*86400000+1), (4133980800000, 4133980800001)):
            for method in (self.archive.list_window, self.archive.window_cameras):
                with self.subTest(start=start, end=end, method=method.__name__), self.assertRaises(ValueError):
                    method(start, end)

    def test_strict_pagination_applies_to_browse_window_and_trash(self):
        methods = (lambda **p: self.archive.browse(**p),
                   lambda **p: self.archive.list_window(self.begin, self.end, **p),
                   lambda **p: self.archive.window_cameras(self.begin, self.end, **p),
                   lambda **p: self.archive.trash(**p))
        for method in methods:
            for options in ({'offset': True}, {'offset': 1.0}, {'offset': -1}, {'offset': 1000001},
                            {'limit': False}, {'limit': 10.0}, {'limit': 0}, {'limit': 101}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    method(**options)

    def test_window_rejects_invalid_camera_and_order(self):
        for camera in (True, 5, '', 'bad/camera', "' OR 1=1--"):
            with self.subTest(camera=camera), self.assertRaises(ValueError):
                self.archive.list_window(self.begin, self.end, camera=camera)
        for order in ('ASC', 'random', True):
            with self.subTest(order=order), self.assertRaises(ValueError):
                self.archive.list_window(self.begin, self.end, order=order)

    def test_old_schema_migrates_without_altering_archived_identity(self):
        self.archive.close()
        database = self.settings.state_dir/'archive.db'
        for suffix in ('', '-wal', '-shm'):
            path = Path(str(database)+suffix)
            if path.exists():
                path.unlink()
        key = record_key({'camera': 'front', 'record_id': 'old'})
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript('''CREATE TABLE recordings (
                key TEXT PRIMARY KEY,camera TEXT NOT NULL,record_id TEXT NOT NULL,
                start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,source_path TEXT NOT NULL,
                local_path TEXT,status TEXT NOT NULL,duration REAL,codec_video TEXT,codec_audio TEXT,
                file_size INTEGER,sha256 TEXT,chat_id TEXT,message_id INTEGER,file_id TEXT,
                last_error TEXT,attempt_id TEXT,retry_at REAL DEFAULT 0,created_at REAL NOT NULL);''')
            connection.execute('''INSERT INTO recordings(key,camera,record_id,start_ms,end_ms,
                source_path,status,chat_id,message_id,file_id,created_at) VALUES(?,?,?,?,?,?,'uploaded',?,?,?,?)''',
                (key, 'front', 'old', self.begin+1000, self.begin+2000, 'old-source', '-100123', 17, 'old-file-id', 1))
            connection.commit()
        self.archive = Archive(self.settings)
        row = self.row(key)
        self.assertIsNone(row['deleted_at'])
        self.assertIsNone(row['deleted_by'])
        self.assertEqual(row['status'], 'uploaded')
        self.assertEqual(row['chat_id'], '-100123')
        self.assertEqual(row['file_id'], 'old-file-id')
        self.assertEqual(self.archive.find_recording(key[:32])['key'], key)
        self.assertTrue(self.archive.soft_delete(key, 7))
        self.assertTrue(self.archive.restore_recording(key, 42))
        self.assertEqual(self.row(key)['file_id'], 'old-file-id')

    def test_atomic_audit_failure_rolls_back_deletion(self):
        key = self.add()
        with self.archive.conn:
            self.archive.conn.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON recording_audit BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.archive.soft_delete(key, 7)
        self.assertIsNone(self.row(key)['deleted_at'])
        self.assertEqual(self.audits(key), [])

    def test_second_connection_repeated_delete_records_one_audit(self):
        key = self.add()
        other = Archive(self.settings)
        try:
            self.assertTrue(self.archive.soft_delete(key, 7))
            self.assertFalse(other.soft_delete(key, 9))
            self.assertEqual(len(self.audits(key)), 1)
            self.assertIsNone(other.find_recording(key[:32]))
        finally:
            other.close()


if __name__ == '__main__':
    unittest.main()
