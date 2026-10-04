"""Managed media cleanup and 72-hour error tombstones, without live media/API."""
from dataclasses import replace
from datetime import datetime, timezone
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import uuid

from archive_app.core import Archive, Settings, record_key
from archive_app import __main__ as cli
from archive_app.sd_source import SDSource
from archive_app.sync import SyncQueue, _statistics
from tests import test_sd_source as sd_fixtures


ERROR_AT = 1000.0
ERROR_TTL = 72 * 3600


class MediaRetentionTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent if os.name == 'nt' else Path(tempfile.gettempdir()).resolve()
        self.root = self.parent / ('.tmp-media-retention-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.settings = Settings(self.root/'state', self.root/'cache', self.root/'input', 'UTC+07:00',
                                 keep_cache=False, min_free_bytes=0, cache_retention_hours=0,
                                 error_retention_hours=72, media_mode='remux_copy')
        self.settings.input_dir.mkdir()
        self.archive = Archive(self.settings)
        self.archive.add_camera({'id': 'Front', 'name': 'Front'})
        self.source = self.settings.input_dir/'original.ps'
        self.source.write_bytes(b'synthetic-original-camera-source')

    def tearDown(self):
        self.archive.close()
        self.assertEqual(self.root.resolve().parent, self.parent)
        shutil.rmtree(self.root)

    def descriptor(self, record_id='clip-001'):
        return {'camera': 'Front', 'record_id': record_id, 'path': str(self.source),
                'start_time': '2026-10-03T10:00:00+07:00',
                'end_time': '2026-10-03T10:01:00+07:00'}

    def seed(self, status='needs_review', *, record_id='clip-001', extension='.mp4', error_at=ERROR_AT, source_kind=None):
        entry = self.descriptor(record_id)
        if source_kind is not None:
            entry['source'] = source_kind
        key = record_key(entry)
        path = self.settings.cache_dir/(key+extension)
        path.write_bytes(b'synthetic-managed-camera-media')
        with self.archive.conn:
            self.archive.conn.execute('''INSERT INTO recordings
                (key,camera,record_id,start_ms,end_ms,source_path,local_path,status,
                 file_size,created_at,processing_method,last_error)
                VALUES(?,'Front',?,1790996400000,1790996460000,?,?,?,?,1,'remux_copy','synthetic_error')''',
                (key, record_id, str(self.source), str(path), status, path.stat().st_size))
            if status in ('failed', 'needs_review', 'upload_unknown') and error_at is not None:
                self.archive.conn.execute('UPDATE recordings SET error_started_at=? WHERE key=?', (error_at,key))
        return key, path

    def row(self, key):
        return dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?', (key,)).fetchone())

    def expire(self, seconds=ERROR_TTL, **kwargs):
        return self.archive.expire_error_media(now=ERROR_AT+seconds, **kwargs)

    def aliases(self, key):
        paths = [self.settings.cache_dir/(key+suffix)
                 for suffix in ('.mp4', '.ps', '.ts', '.bin', '.part.mp4', '.part.ps')]
        stage = self.settings.cache_dir/'sd-stage'/'Front'
        stage.mkdir(parents=True, exist_ok=True)
        paths.extend(stage/(key+'.'+'a'*32+suffix) for suffix in ('.part','.source'))
        for path in paths:
            path.write_bytes(b'managed-alias-fixture')
        return paths

    def test_error_retention_environment_default_and_valid_override(self):
        with patch.dict(os.environ, {'STATE_DIR': str(self.root/'env-state'),
                                    'CACHE_DIR': str(self.root/'env-cache'),
                                    'INPUT_DIR': str(self.settings.input_dir)}, clear=True):
            self.assertEqual(Settings.from_env().error_retention_hours, 72)
            with patch.dict(os.environ, {'ERROR_RETENTION_HOURS': '96.5'}):
                self.assertEqual(Settings.from_env().error_retention_hours, 96.5)

    def test_error_retention_environment_rejects_invalid_values(self):
        for value in ('0','-1','nan','inf','invalid'):
            with self.subTest(value=value), patch.dict(os.environ, {'ERROR_RETENTION_HOURS': value}, clear=True):
                with self.assertRaises(ValueError):
                    Settings.from_env()

    def test_error_retained_until_exact_72_hour_boundary(self):
        key, path = self.seed(extension='.ps')
        self.assertEqual(self.expire(ERROR_TTL-1), 0)
        self.assertTrue(path.is_file())
        self.assertIsNone(self.row(key)['media_expired_at'])
        self.assertEqual(self.expire(), 1)
        self.assertFalse(path.exists())
        self.assertEqual(self.row(key)['media_expired_at'], ERROR_AT+ERROR_TTL)

    def test_all_error_statuses_expire_but_pending_and_active_media_remain(self):
        candidates = {status:self.seed(status,record_id=status)
                      for status in ('failed','needs_review','upload_unknown','downloaded','uploading','ingesting')}
        with self.archive.conn:
            self.archive.conn.execute('UPDATE recordings SET retry_at=? WHERE status=\'downloaded\'',
                                      (ERROR_AT+ERROR_TTL+1000,))
        self.assertEqual(self.expire(), 3)
        for status,(key,path) in candidates.items():
            with self.subTest(status=status):
                self.assertEqual(path.exists(), status in ('downloaded','uploading','ingesting'))
                self.assertEqual(self.row(key)['status'], status)

    def test_error_expiry_applies_even_when_keep_cache_is_true(self):
        self.settings.keep_cache = True
        key,path = self.seed(extension='.bin')
        self.assertEqual(self.expire(), 1)
        self.assertFalse(path.exists())
        self.assertIsNotNone(self.row(key)['media_expired_at'])

    def test_expiry_removes_all_managed_aliases_but_not_original_input(self):
        key,_ = self.seed()
        paths = self.aliases(key)
        unknown = self.settings.cache_dir/'operator-added.mp4'
        unknown.write_bytes(b'operator-file')
        self.assertEqual(self.expire(), 1)
        self.assertTrue(all(not p.exists() for p in paths))
        self.assertEqual(self.source.read_bytes(), b'synthetic-original-camera-source')
        self.assertEqual(unknown.read_bytes(), b'operator-file')

    def test_expiry_is_idempotent_and_preserves_failure_catalog(self):
        key,_ = self.seed('failed')
        before = self.row(key)
        self.assertEqual(self.expire(), 1)
        expired = self.row(key)
        self.assertEqual(self.expire(ERROR_TTL+600), 0)
        self.assertEqual(self.row(key)['media_expired_at'], expired['media_expired_at'])
        for field in ('key','camera','record_id','start_ms','end_ms','status','last_error','sha256'):
            self.assertEqual(expired[field], before[field])

    def test_failure_clock_latches_on_insert_and_is_not_reset_by_retry(self):
        key,_ = self.seed('failed')
        with self.archive.conn:
            self.archive.conn.execute("UPDATE recordings SET status='ingesting',last_error=NULL WHERE key=?", (key,))
            self.archive.conn.execute("UPDATE recordings SET status='needs_review',last_error='second_error' WHERE key=?", (key,))
        self.assertEqual(self.row(key)['error_started_at'], ERROR_AT)
        self.archive.close()
        self.archive = Archive(self.settings)
        self.assertEqual(self.row(key)['error_started_at'], ERROR_AT)

    def test_transition_from_downloaded_latches_error_clock(self):
        key,_ = self.seed('downloaded')
        self.assertIsNone(self.row(key)['error_started_at'])
        before = time.time()
        with self.archive.conn:
            self.archive.conn.execute("UPDATE recordings SET status='needs_review' WHERE key=?", (key,))
        self.assertGreaterEqual(self.row(key)['error_started_at'], before-1)
        self.assertLessEqual(self.row(key)['error_started_at'], time.time()+1)

    def test_inserted_error_latches_clock_before_first_cleanup(self):
        before = time.time()
        key,_ = self.seed('failed',error_at=None)
        self.assertGreaterEqual(self.row(key)['error_started_at'],before-1)
        self.assertLessEqual(self.row(key)['error_started_at'],time.time()+1)

    def test_expired_failed_manifest_entry_is_not_reingested(self):
        key,_ = self.seed('failed')
        self.expire()
        with patch('archive_app.core.normalize', side_effect=AssertionError('Expired media must not be reingested')) as normalizer:
            result = self.archive.ingest_entry(self.descriptor())
        normalizer.assert_not_called()
        self.assertEqual(result['key'], key)
        self.assertIsNotNone(result['media_expired_at'])

    def test_expired_manifest_row_is_returned_even_when_original_input_is_missing(self):
        key,_ = self.seed('failed')
        self.expire()
        self.source.unlink()
        with patch('archive_app.core.resolve_input',side_effect=AssertionError('Expired row must not resolve source')):
            result = self.archive.ingest_entry(self.descriptor())
        self.assertEqual(result['key'],key)
        self.assertIsNotNone(result['media_expired_at'])

    def test_expired_failed_sd_entry_is_not_downloaded_again(self):
        provider = sd_fixtures.FakeSource()
        item = provider.files[0]
        start = datetime.fromisoformat(item['start_time']).astimezone(timezone.utc).isoformat().replace('+00:00','Z')
        entry = {'camera':'Front','source':'camera-sd','record_id':'ch1:'+start}
        key,path = self.seed('failed', record_id=entry['record_id'],source_kind='camera-sd')
        self.assertEqual(key, record_key(entry))
        self.expire()
        config = {'id':'Front','host':'192.168.1.10','sd_channel':1,'sd_lookback_hours':168}
        with patch.object(SDSource, '_provider', return_value=provider):
            result = SDSource(self.archive,'Front',config).sync(now=sd_fixtures.NOW)
        self.assertEqual(result['already_known'], 1)
        self.assertEqual(result['downloaded'], 0)
        self.assertEqual(provider.downloads, [])
        self.assertFalse(path.exists())

    def test_expired_failed_raw_cache_does_not_enter_remux_migration(self):
        key,_ = self.seed('failed', extension='.ps')
        with self.archive.conn:
            self.archive.conn.execute("UPDATE recordings SET processing_method='passthrough' WHERE key=?", (key,))
        self.expire()
        with patch('archive_app.core.normalize', side_effect=AssertionError('Expired raw media must not remux')) as normalizer:
            result = self.archive.remux_pending_cache()
        normalizer.assert_not_called()
        self.assertEqual(result['converted'],0)
        self.assertEqual(result['failed'],0)
        self.assertEqual(self.row(key)['status'],'failed')

    def test_expired_record_does_not_enter_legacy_cache_invalidation(self):
        key,_ = self.seed('failed')
        with self.archive.conn:
            self.archive.conn.execute("UPDATE recordings SET processing_method='legacy' WHERE key=?",(key,))
        self.expire()
        self.assertEqual(self.archive.invalidate_legacy_cache(),0)
        self.assertEqual(self.row(key)['last_error'],'synthetic_error')

    def test_unknown_upload_expiry_can_be_reconciled_without_source_media(self):
        key,path = self.seed('upload_unknown')
        self.assertEqual(self.expire(),1)
        self.assertFalse(path.exists())
        self.archive.mark_uploaded(key,'-1001234567890',17,'confirmed-file-id',
                                   media_type='video',file_unique_id='confirmed-unique-id')
        row = self.row(key)
        self.assertEqual(row['status'],'uploaded')
        self.assertEqual(row['file_id'],'confirmed-file-id')
        self.assertEqual(row['message_id'],17)
        self.assertEqual(row['storage_message_id'],17)
        self.assertIsNotNone(row['media_expired_at'])

    def test_expired_oversize_retry_cli_rejects_even_manually_recreated_cache(self):
        key,path = self.seed('needs_review')
        with self.archive.conn:
            self.archive.conn.execute("UPDATE recordings SET last_error='missing_or_oversize_file' WHERE key=?",(key,))
        self.expire()
        path.write_bytes(b'manually-recreated-expired-cache')
        with patch('sys.argv',['archive-app','retry-oversize','--key',key]), patch.object(cli.Settings,'from_env',return_value=self.settings):
            with self.assertRaises(ValueError):
                cli.main()
        self.assertEqual(self.row(key)['status'],'needs_review')
        self.assertIsNotNone(self.row(key)['media_expired_at'])

    def test_upload_success_cleans_all_aliases_and_keeps_telegram_references(self):
        key,_ = self.seed('downloaded')
        paths = self.aliases(key)
        self.archive.mark_uploaded(key,'-1001234567890',17,'confirmed-file-id',media_type='video')
        self.assertTrue(self.archive.cleanup(key))
        self.assertTrue(all(not p.exists() for p in paths))
        self.assertTrue(self.source.exists())
        row = self.row(key)
        self.assertEqual(row['status'],'uploaded')
        self.assertEqual(row['file_id'],'confirmed-file-id')
        self.assertEqual(row['storage_message_id'],17)

    def test_cleanup_sweeps_leftovers_even_when_cleaned_at_already_exists(self):
        key,_ = self.seed('downloaded')
        self.archive.mark_uploaded(key,'-1001234567890',17,'confirmed-file-id')
        self.archive.cleanup(key)
        cleaned = self.row(key)['cleaned_at']
        leftover = self.settings.cache_dir/(key+'.ps')
        leftover.write_bytes(b'leftover-source')
        self.assertTrue(self.archive.cleanup(key))
        self.assertFalse(leftover.exists())
        self.assertEqual(self.row(key)['cleaned_at'], cleaned)
        self.assertFalse(self.archive.cleanup(key))

    def test_success_cleanup_never_deletes_other_recording_or_invalid_generated_names(self):
        key,_ = self.seed('downloaded')
        other_key,other = self.seed('needs_review', record_id='other-recording')
        unknown = self.settings.cache_dir/(key+'.operator.notes.mp4')
        unknown.write_bytes(b'operator-note')
        stage = self.settings.cache_dir/'sd-stage'/'Front'
        stage.mkdir(parents=True)
        invalid_stage = stage/(key+'.not-a-generated-uuid.source')
        invalid_stage.write_bytes(b'unknown-staging-file')
        self.archive.mark_uploaded(key,'-1001234567890',17,'confirmed-file-id')
        self.archive.cleanup(key)
        self.assertTrue(other.exists())
        self.assertIsNone(self.row(other_key)['media_expired_at'])
        self.assertTrue(unknown.exists())
        self.assertTrue(invalid_stage.exists())

    def test_external_local_path_is_not_statted_or_deleted_by_error_expiry(self):
        key,path = self.seed('failed')
        outside = self.root/'external-source.ts'
        outside.write_bytes(b'external-fixture')
        with self.archive.conn:
            self.archive.conn.execute('UPDATE recordings SET local_path=? WHERE key=?',(str(outside),key))
        original_stat = Path.stat
        def stat(candidate,*args,**kwargs):
            if candidate == outside:
                raise AssertionError('External source must not be statted')
            return original_stat(candidate,*args,**kwargs)
        with patch.object(Path,'stat',stat):
            try:
                self.archive.expire_error_media(now=ERROR_AT+ERROR_TTL)
            except ValueError:
                pass
        self.assertEqual(outside.read_bytes(),b'external-fixture')
        self.assertIsNone(self.row(key)['media_expired_at'])
        self.assertTrue(path.exists())

    def test_external_original_source_is_ignored_without_statting(self):
        key,path = self.seed('needs_review')
        outside = self.root/'read-only-original.ps'
        outside.write_bytes(b'external-original-source')
        with self.archive.conn:
            self.archive.conn.execute('UPDATE recordings SET source_path=? WHERE key=?',(str(outside),key))
        original_stat = Path.stat
        def stat(candidate,*args,**kwargs):
            if candidate == outside:
                raise AssertionError('External original source must not be statted')
            return original_stat(candidate,*args,**kwargs)
        with patch.object(Path,'stat',stat):
            self.assertEqual(self.expire(),1)
        self.assertFalse(path.exists())
        self.assertEqual(outside.read_bytes(),b'external-original-source')

    def test_linked_alias_is_rejected_before_any_managed_media_is_removed(self):
        key,path = self.seed('failed')
        target = self.root/'must-remain.ts'
        target.write_bytes(b'linked-target-fixture')
        linked = self.settings.cache_dir/(key+'.ts')
        try:
            linked.symlink_to(target)
        except OSError:
            self.skipTest('Host has no symlink creation privilege')
        try:
            self.expire()
        except ValueError:
            pass
        self.assertTrue(path.is_file())
        self.assertEqual(target.read_bytes(),b'linked-target-fixture')
        self.assertIsNone(self.row(key)['media_expired_at'])

    def test_expiry_limit_does_not_delete_more_than_requested(self):
        first,_ = self.seed('failed',record_id='first')
        second,_ = self.seed('needs_review',record_id='second')
        self.assertEqual(self.expire(limit=1),1)
        self.assertEqual(sum(self.row(key)['media_expired_at'] is not None for key in (first,second)),1)
        self.assertEqual(self.expire(limit=1),1)

    def test_delete_failure_keeps_durable_tombstones_and_retries_remaining_files(self):
        first,first_path = self.seed('failed',record_id='first-deleted',error_at=ERROR_AT-1)
        item = sd_fixtures.recording()
        start = datetime.fromisoformat(item['start_time']).astimezone(timezone.utc).isoformat().replace('+00:00','Z')
        second,second_path = self.seed('failed',record_id='ch1:'+start,source_kind='camera-sd')
        original_unlink = Path.unlink
        def interrupted_unlink(path,*args,**kwargs):
            if path == second_path:
                raise OSError('Synthetic media deletion failure')
            return original_unlink(path,*args,**kwargs)
        with patch.object(Path,'unlink',interrupted_unlink):
            try:
                self.expire()
            except OSError:
                pass
        first_row,second_row = self.row(first),self.row(second)
        self.assertFalse(first_path.exists())
        self.assertTrue(second_path.exists())
        self.assertEqual(first_row['media_expired_at'],ERROR_AT+ERROR_TTL)
        self.assertEqual(second_row['media_expired_at'],ERROR_AT+ERROR_TTL)
        self.assertEqual(first_row['cleanup_revision'],1)
        self.assertEqual(second_row['cleanup_revision'],0)
        provider = sd_fixtures.FakeSource()
        config = {'id':'Front','host':'192.168.1.10','sd_channel':1,'sd_lookback_hours':168}
        with patch.object(SDSource,'_provider',return_value=provider):
            result = SDSource(self.archive,'Front',config).sync(now=sd_fixtures.NOW)
        self.assertEqual(result['already_known'],1)
        self.assertEqual(provider.downloads,[])
        self.assertEqual(self.expire(ERROR_TTL+1),1)
        self.assertFalse(second_path.exists())
        self.assertEqual(self.row(second)['cleanup_revision'],1)
        self.assertEqual(self.row(second)['media_expired_at'],second_row['media_expired_at'])
        self.assertEqual(self.row(first)['media_expired_at'],first_row['media_expired_at'])

    def test_entire_batch_is_prevalidated_before_any_expiration_claim_or_unlink(self):
        first,first_path = self.seed('failed',record_id='first-valid',error_at=ERROR_AT-1)
        second,second_path = self.seed('failed',record_id='second-invalid')
        outside = self.root/'outside-batch-source.mp4'
        outside.write_bytes(b'outside-file')
        with self.archive.conn:
            self.archive.conn.execute('UPDATE recordings SET local_path=? WHERE key=?',(str(outside),second))
        try:
            self.expire()
        except ValueError:
            pass
        self.assertTrue(first_path.exists())
        self.assertTrue(second_path.exists())
        self.assertEqual(outside.read_bytes(),b'outside-file')
        self.assertIsNone(self.row(first)['media_expired_at'])
        self.assertIsNone(self.row(second)['media_expired_at'])

    def test_expired_errors_are_not_pending_and_do_not_block_empty_sync(self):
        self.seed('needs_review',record_id='first-expired')
        self.seed('failed',record_id='second-expired')
        self.assertEqual(self.expire(),2)
        queue = SyncQueue(self.archive)
        statistics = _statistics()
        self.assertEqual(queue._counts('Front',statistics),(2,0))
        self.assertEqual(statistics['expired'],2)
        for name in ('pending','ready','needs_review','upload_unknown','failed_records'):
            self.assertEqual(statistics[name],0)
        telegram = Mock()
        with patch.object(self.archive,'probe_camera',return_value={'tcp':{'8000':'open'}}), patch.object(SDSource,'sync',return_value={
                'backend':'hcnetsdk','searched':0,'downloaded':0,'imported':0,
                'already_known':0,'deferred':0,'backlog':0}):
            queue.enqueue('Front')
            self.assertTrue(queue.run_once(telegram))
        job = queue.status('Front')['latest']['Front']
        self.assertEqual(job['state'],'completed')
        self.assertEqual(job['statistics']['expired'],2)
        telegram.upload_one.assert_not_called()

    def test_reconciled_expired_upload_is_readable_and_not_counted_as_failed_expiry(self):
        key,_ = self.seed('upload_unknown')
        self.expire()
        self.archive.mark_uploaded(key,'-1001234567890',17,'confirmed-file-id',media_type='video')
        queue = SyncQueue(self.archive)
        statistics = _statistics()
        self.assertEqual(queue._counts('Front',statistics),(1,0))
        self.assertEqual(statistics['expired'],0)
        self.assertEqual(statistics['pending'],0)
        self.assertEqual(self.archive.find_recording(key)['file_id'],'confirmed-file-id')

    def test_expired_tombstone_cannot_be_claimed_even_after_manual_requeue(self):
        key,path = self.seed('failed')
        self.expire()
        path.write_bytes(b'manually-recreated-cache')
        with self.archive.conn:
            self.archive.conn.execute("UPDATE recordings SET status='downloaded',retry_at=0 WHERE key=?",(key,))
        self.assertIsNone(self.archive.claim_upload('Front'))
        self.assertEqual(self.row(key)['status'],'downloaded')

    def test_old_already_cleaned_row_is_selected_only_for_one_alias_migration(self):
        key,path = self.seed('downloaded')
        self.archive.mark_uploaded(key,'-1001234567890',17,'confirmed-file-id')
        path.unlink()
        with self.archive.conn:
            self.archive.conn.execute('UPDATE recordings SET cleaned_at=?,cleanup_revision=0 WHERE key=?',(ERROR_AT,key))
        query = "SELECT key FROM recordings WHERE status='uploaded' AND (cleaned_at IS NULL OR cleanup_revision<1)"
        self.assertEqual([row[0] for row in self.archive.conn.execute(query)],[key])
        self.assertFalse(self.archive.cleanup(key))
        self.assertEqual(self.row(key)['cleanup_revision'],1)
        self.assertEqual(self.row(key)['cleaned_at'],ERROR_AT)
        self.assertEqual(list(self.archive.conn.execute(query)),[])

    def test_legacy_error_upgrade_starts_one_conservative_72_hour_window(self):
        state = self.root/'legacy-state'
        state.mkdir()
        entry = self.descriptor('legacy-error')
        key = record_key(entry)
        path = self.settings.cache_dir/(key+'.bin')
        path.write_bytes(b'legacy-error-fixture')
        with closing(sqlite3.connect(state/'archive.db')) as conn, conn:
            conn.execute('''CREATE TABLE recordings (
                key TEXT PRIMARY KEY,camera TEXT NOT NULL,record_id TEXT NOT NULL,
                start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,source_path TEXT NOT NULL,
                local_path TEXT,status TEXT NOT NULL,duration REAL,codec_video TEXT,codec_audio TEXT,
                file_size INTEGER,sha256 TEXT,chat_id TEXT,message_id INTEGER,file_id TEXT,
                last_error TEXT,attempt_id TEXT,retry_at REAL DEFAULT 0,created_at REAL NOT NULL)''')
            conn.execute('''INSERT INTO recordings
                (key,camera,record_id,start_ms,end_ms,source_path,local_path,status,last_error,created_at)
                VALUES(?,'Front','legacy-error',1790996400000,1790996460000,?,?,'needs_review','legacy_error',1)''',
                (key,str(self.source),str(path)))
        before = time.time()
        migrated = Archive(replace(self.settings,state_dir=state))
        try:
            row = dict(migrated.conn.execute('SELECT * FROM recordings WHERE key=?',(key,)).fetchone())
            self.assertGreaterEqual(row['error_started_at'],before-1)
            self.assertLessEqual(row['error_started_at'],time.time()+1)
            self.assertEqual(migrated.expire_error_media(now=row['error_started_at']+ERROR_TTL-1),0)
            self.assertTrue(path.exists())
        finally:
            migrated.close()
        reopened = Archive(replace(self.settings,state_dir=state))
        try:
            self.assertEqual(reopened.conn.execute('SELECT error_started_at FROM recordings WHERE key=?',(key,)).fetchone()[0],row['error_started_at'])
            self.assertEqual(reopened.expire_error_media(now=row['error_started_at']+ERROR_TTL),1)
            self.assertFalse(path.exists())
        finally:
            reopened.close()


if __name__ == '__main__':
    unittest.main()
