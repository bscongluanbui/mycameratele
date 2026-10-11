"""Fair bounded camera batches keep durable FIFO admission and pause semantics."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch
import uuid

from archive_app.core import Archive, Settings
from archive_app.sd_source import SDSourceError
from archive_app.sync import SyncQueue, _statistics
from archive_app.telegram import Telegram


class FairCameraQueueTests(unittest.TestCase):
    def setUp(self):
        self.parent = (Path(__file__).parent if os.name == 'nt' else Path(tempfile.gettempdir())).resolve()
        self.root = self.parent / ('.tmp-fair-sync-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.settings = Settings(self.root/'state', self.root/'cache', self.root/'input',
                                 'UTC+07:00', min_free_bytes=0, enable_upload=True,
                                 token='fixture', owner_user_id=42)
        self.a = Archive(self.settings)
        self.a.state('telegram_owner_started:42', '1')
        for camera in ('front', 'back', 'side'):
            self.a.add_camera({'id': camera})
        self.settings.input_dir.mkdir(exist_ok=True)
        self.source = self.settings.input_dir/'fixture.mp4'
        self.source.write_bytes(b'fixture byte-preserving SD source')
        self.q = SyncQueue(self.a)
        self.t = Telegram(self.settings)
        self.t.request = lambda method, fields, **kw: {
            'chat': {'id':42, 'type':'private'}, 'message_id':1,
            'document': {'file_id':'fixture-file', 'file_unique_id':'fixture-unique'}}

    def tearDown(self):
        self.a.close()
        self.assertEqual(self.root.resolve().parent, self.parent)
        shutil.rmtree(self.root)

    def entry(self, record='one', camera='front'):
        return {'camera':camera, 'record_id':record, 'path':str(self.source),
                'start_time':'2026-10-03T10:00:00+07:00', 'end_time':'2026-10-03T10:01:00+07:00'}

    def clean_statistics(self):
        return {**_statistics(), 'sd_downloaded':1, 'sd_imported':1,
                'imported':1, 'uploaded':1, 'sd_backlog':5}

    def finish_batch(self, stats=None, telegram=None):
        job = self.q._claim()
        self.assertIsNotNone(job)
        self.q._finish(job, stats or self.clean_statistics(), 'blocked', 'sd_batch_limit',
                       'fixture completed batch', requeue_backlog=True, telegram=telegram or self.t)
        return job

    def active(self):
        return [dict(row) for row in self.a.conn.execute(
            "SELECT * FROM sync_jobs WHERE state IN ('queued','running') ORDER BY created_at,id")]

    def test_backlog_requeues_after_other_cameras_even_with_coarse_clock(self):
        with patch('archive_app.sync.time.time', return_value=1000):
            self.q.enqueue('front', source='dashboard', actor='42')
            job = self.q._claim()
            self.q.enqueue('back')
            self.q._finish(job, self.clean_statistics(), 'blocked', 'sd_batch_limit',
                           'fixture batch', requeue_backlog=True, telegram=self.t)
        active = self.active()
        self.assertEqual([row['camera_id'] for row in active], ['back','front'])
        self.assertEqual(active[1]['source'], 'backlog')
        self.assertEqual(active[1]['actor'], '42')
        original = self.a.conn.execute('SELECT * FROM sync_jobs WHERE id=?',(job['id'],)).fetchone()
        self.assertEqual(original['state'], 'completed')
        self.assertTrue(json.loads(original['statistics_json'])['sd_requeued'])
        self.assertEqual(self.q._claim()['camera_id'], 'back')

    def test_sync_passes_configured_batch_and_immediately_continues_backlog(self):
        self.settings.sd_max_files_per_job = 5
        self.q.enqueue('front'); self.q.enqueue('back')
        def source(*, max_files, progress):
            self.assertEqual(max_files, 5)
            self.a.ingest_entry(self.entry())
            snapshot = {'backend':'hcnetsdk', 'downloaded':1, 'imported':1,
                        'backlog':7, 'searched':8, 'phase':'sd_complete'}
            progress(snapshot)
            return snapshot
        with patch.object(self.a, 'probe_camera', return_value={'tcp':{'8000':'open'}}), \
             patch('archive_app.sd_source.SDSource.sync', side_effect=source):
            self.assertTrue(self.q.run_once(self.t))
        self.assertEqual([row['camera_id'] for row in self.active()], ['back','front'])
        self.assertEqual(self.a.conn.execute("SELECT status FROM recordings").fetchone()[0], 'uploaded')

    def test_empty_duplicate_or_nonprogress_batch_never_busy_requeues(self):
        for changes in ({'sd_downloaded':0}, {'sd_imported':0}, {'sd_backlog':0}):
            with self.subTest(changes=changes):
                self.q.enqueue('front')
                self.finish_batch({**self.clean_statistics(), **changes})
                self.assertEqual(self.active(), [])

    def test_pending_ambiguous_review_and_delayed_records_block_requeue(self):
        key = self.a.ingest_entry(self.entry())['key']
        for status, retry_at in (('downloaded',0), ('upload_unknown',0), ('uploading',0),
                                 ('needs_review',0), ('failed',0), ('downloaded',time.time()+1000)):
            with self.subTest(status=status, retry_at=retry_at):
                self.a.conn.execute('UPDATE recordings SET status=?,retry_at=? WHERE key=?', (status,retry_at,key))
                self.a.conn.commit()
                self.q.enqueue('front'); self.finish_batch()
                self.assertEqual(self.active(), [])

    def test_sd_error_cache_spool_and_remux_failure_never_requeue(self):
        for changes in ({'sd_error_code':'sd_download_failed'}, {'spool_blocked':1},
                        {'remux_budget_blocked':1}, {'remux_failed':1}, {'failed':1}):
            with self.subTest(changes=changes):
                self.q.enqueue('front'); self.finish_batch({**self.clean_statistics(), **changes})
                self.assertEqual(self.active(), [])

    def test_gate_and_shared_telegram_retry_pause_block_continuation(self):
        for disabled in ('global', 'camera', 'token', 'owner'):
            with self.subTest(disabled=disabled):
                self.q.enqueue('front')
                old_token = self.settings.token
                if disabled == 'global': self.settings.enable_upload = False
                elif disabled == 'camera': self.a.update_camera('front', {'upload_enabled':False})
                elif disabled == 'token': self.settings.token = ''
                else: self.a.state('telegram_owner_started:42', '0')
                self.finish_batch()
                self.assertEqual(self.active(), [])
                self.settings.enable_upload = True; self.settings.token = old_token
                self.a.update_camera('front', {'upload_enabled':True})
                self.a.state('telegram_owner_started:42', '1')
        self.q.enqueue('front')
        with patch.object(self.t, 'upload_retry_until', return_value=time.time()+3600):
            self.finish_batch()
        self.assertEqual(self.active(), [])

    def test_capacity_bound_does_not_make_queue_exceed_limit(self):
        self.q.enqueue('front'); self.q.enqueue('back')
        with patch('archive_app.sync.MAX_ACTIVE_JOBS', 1):
            self.finish_batch()
        self.assertEqual([row['camera_id'] for row in self.active()], ['back'])

    def test_duplicate_finish_and_competing_enqueue_do_not_duplicate_continuation(self):
        self.q.enqueue('front')
        job = self.finish_batch()
        self.q._finish(job, self.clean_statistics(), 'blocked','sd_batch_limit','duplicate',
                       requeue_backlog=True, telegram=self.t)
        def enqueue(_):
            other = Archive(self.settings)
            try: return SyncQueue(other).enqueue('front')['jobs'][0]['id']
            finally: other.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(enqueue, range(8)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(self.active()), 1)

    def test_finish_and_manual_enqueue_race_keep_exactly_one_active_camera(self):
        self.q.enqueue('front'); job = self.q._claim()
        def operation(number):
            other = Archive(self.settings)
            try:
                queue = SyncQueue(other)
                if number == 0:
                    queue._finish(job, self.clean_statistics(), 'blocked', 'sd_batch_limit',
                                  'fixture batch', requeue_backlog=True, telegram=Telegram(self.settings))
                else:
                    queue.enqueue('front')
            finally: other.close()
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(operation, range(5)))
        self.assertEqual(len(self.active()), 1)
        self.assertEqual(self.active()[0]['camera_id'], 'front')

    def test_continuation_insert_failure_rolls_back_finished_state(self):
        self.q.enqueue('front'); job = self.q._claim()
        with patch('archive_app.sync.uuid.uuid4') as identifier:
            identifier.return_value.hex = job['id']
            with self.assertRaises(sqlite3.IntegrityError):
                self.q._finish(job, self.clean_statistics(), 'blocked','sd_batch_limit','fixture',
                               requeue_backlog=True, telegram=self.t)
        self.assertEqual(self.active()[0]['id'], job['id'])
        self.assertEqual(self.active()[0]['state'], 'running')

    def test_atomic_claim_across_independent_worker_connections(self):
        self.q.enqueue()
        def claim(_):
            other = Archive(self.settings)
            try:
                job = SyncQueue(other)._claim()
                return None if job is None else job['camera_id']
            finally: other.close()
        with ThreadPoolExecutor(max_workers=5) as pool:
            cameras = list(pool.map(claim, range(5)))
        self.assertEqual(sorted(camera for camera in cameras if camera), ['back','front','side'])
        self.assertEqual(cameras.count(None), 2)

    def test_shutdown_stops_claim_and_prevents_backlog_requeue(self):
        stop = Event(); q = SyncQueue(self.a, stop_event=stop)
        q.enqueue('front'); job = q._claim(); stop.set()
        q._finish(job, self.clean_statistics(), 'blocked','sd_batch_limit','stopping',
                  requeue_backlog=True, telegram=self.t)
        self.assertEqual(self.active(), [])
        q.enqueue('front')
        self.assertFalse(q.run_once(self.t)); self.assertIsNone(q._claim())
        self.assertEqual(self.active()[0]['state'], 'queued')

    def test_interrupted_source_is_terminal_but_keeps_queue_and_skips_continuation(self):
        stop = Event(); q = SyncQueue(self.a, stop_event=stop); q.enqueue('front')
        def source(*, max_files, progress, stop_requested):
            self.assertTrue(callable(stop_requested)); stop.set()
            return {'backend':'hcnetsdk','downloaded':1,'imported':1,'backlog':4,'interrupted':True}
        with patch.object(self.a,'probe_camera',return_value={'tcp':{'8000':'open'}}), \
             patch('archive_app.sd_source.SDSource.sync',side_effect=source):
            self.assertTrue(q.run_once(self.t))
        job = q.status()['latest']['front']
        self.assertEqual((job['state'], job['code']), ('blocked','sd_interrupted'))
        self.assertEqual(self.active(), [])

    def test_inflight_bytes_speed_and_native_status_are_persisted(self):
        self.q.enqueue('front'); observed = []
        snapshot = {'backend':'hcnetsdk','phase':'sd_download','current_file_bytes':3000,
                    'current_file_total_bytes':5000,'current_file_elapsed_seconds':2.5,
                    'current_file_bytes_per_second':1200.0,'download_speed_requested_kbps':8192,
                    'download_speed_status':'unsupported','download_speed_error_code':23}
        def source(*, max_files, progress):
            progress(snapshot)
            observed.append(self.q.status()['latest']['front']['statistics'])
            raise SDSourceError('sd_download_incomplete', 'fixture failure')
        with patch.object(self.a,'probe_camera',return_value={'tcp':{'8000':'open'}}), \
             patch('archive_app.sd_source.SDSource.sync',side_effect=source):
            self.q.run_once(self.t)
        for field,value in snapshot.items():
            if field.startswith(('current_file_', 'download_speed_')):
                self.assertEqual(observed[0]['sd_'+field], value)
        self.assertEqual(self.q.status()['latest']['front']['statistics']['sd_current_file_bytes'],3000)

    def remux_review(self, record, *, old=True, **fields):
        key = self.a.ingest_entry(self.entry(record))['key']
        stamp = time.time() - 3600 if old else time.time() + 1
        values = {'status':'needs_review','last_error':'failed_remux',
                  'created_at':stamp,'error_started_at':stamp, **fields}
        self.a.conn.execute('UPDATE recordings SET ' + ','.join(name+'=?' for name in values) + ' WHERE key=?',
                            (*values.values(),key))
        self.a.conn.commit()
        return key

    def run_fresh_batch(self):
        def source(*, max_files, progress):
            self.a.ingest_entry(self.entry('fresh-success'))
            snapshot = {'backend':'hcnetsdk','downloaded':1,'imported':1,
                        'backlog':8,'searched':10,'phase':'sd_complete'}
            progress(snapshot)
            return snapshot
        with patch.object(self.a,'probe_camera',return_value={'tcp':{'8000':'open'}}), \
             patch('archive_app.sd_source.SDSource.sync',side_effect=source):
            self.q.run_once(self.t)

    def test_old_unposted_remux_quarantine_does_not_block_healthy_sd_backlog(self):
        key = self.remux_review('old-failure')
        before = dict(self.a.conn.execute('SELECT * FROM recordings WHERE key=?',(key,)).fetchone())
        original = self.q.enqueue('front')['jobs'][0]['id']; self.q.enqueue('back')
        self.run_fresh_batch()
        self.assertEqual([row['camera_id'] for row in self.active()], ['back','front'])
        job = self.a.conn.execute('SELECT * FROM sync_jobs WHERE id=?',(original,)).fetchone()
        self.assertEqual(job['state'],'completed')
        self.assertEqual(json.loads(job['statistics_json'])['sd_quarantined_reviews'],1)
        self.assertEqual(dict(self.a.conn.execute('SELECT * FROM recordings WHERE key=?',(key,)).fetchone()),before)

    def test_new_remux_review_and_any_telegram_identity_still_block_backlog(self):
        for number, fields in enumerate(({'old':False}, {'file_id':'uncertain-file'},
                                        {'message_id':123}, {'storage_message_id':123},
                                        {'attempt_id':'uncertain-attempt'})):
            with self.subTest(fields=fields):
                self.a.conn.execute('DELETE FROM recordings'); self.a.conn.commit()
                self.remux_review('failure-'+str(number), **fields)
                self.q.enqueue('front'); self.run_fresh_batch()
                self.assertEqual(self.active(), [])
                self.assertEqual(self.q.status()['latest']['front']['code'],'needs_review')

    def test_atomic_recheck_rejects_new_review_after_quarantine_decision(self):
        self.remux_review('old-failure'); self.q.enqueue('front')
        original_finish = self.q._finish
        def finish(*args, **kwargs):
            if kwargs.get('requeue_backlog'):
                self.remux_review('new-failure', old=False)
            return original_finish(*args, **kwargs)
        with patch.object(self.q,'_finish',side_effect=finish):
            self.run_fresh_batch()
        self.assertEqual(self.active(), [])
        job = self.q.status()['latest']['front']
        self.assertEqual(job['state'],'blocked')
        self.assertFalse(job['statistics']['sd_requeued'])
        self.assertEqual(job['statistics']['sd_quarantined_reviews'],0)


if __name__ == '__main__':
    unittest.main()
