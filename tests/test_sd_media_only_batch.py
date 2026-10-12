"""Bounded all-bad SD batches advance only with verified retained originals."""
from datetime import timedelta
from pathlib import Path
import unittest
from unittest.mock import patch

from archive_app import core
from archive_app.sd_source import SDSource
from archive_app.sync import SyncQueue
from archive_app.telegram import Telegram
from tests import test_sd_media_quarantine as fixtures


class SDMediaOnlyBatchTests(unittest.TestCase):
    setUp = fixtures.SDMediaQuarantineTests.setUp
    tearDown = fixtures.SDMediaQuarantineTests.tearDown

    def prepare(self, bad_count=10, threads=1):
        self.settings.enable_upload = True
        self.settings.token = 'synthetic-token'
        self.settings.owner_user_id = 42
        self.settings.sd_max_files_per_job = 10
        self.archive.state('telegram_owner_started:42', '1')
        self.config['sd_download_threads'] = threads
        self.files = []
        for number in range(12):
            start = fixtures.NOW - timedelta(hours=1) + timedelta(minutes=number*2)
            self.files.append({'record_id': str(number), 'size': len(fixtures.BAD if number < bad_count else fixtures.MP4),
                               'start_time': start.isoformat(), 'end_time': (start+timedelta(minutes=1)).isoformat()})
        owner = self
        class Provider:
            backend = 'hcnetsdk'
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def search(self, *_): return list(owner.files)
            def download(self, recording, path):
                owner.downloads.append(recording['record_id'])
                path.write_bytes(fixtures.BAD if int(recording['record_id']) < bad_count else fixtures.MP4)
        self.provider = Provider
        self.queue = SyncQueue(self.archive)
        self.telegram = Telegram(self.settings)
        self.sent = []
        def request(method, fields, **kwargs):
            self.assertEqual(method, 'sendVideo')
            self.assertEqual(Path(kwargs['file_path']).read_bytes(), fixtures.MP4)
            self.sent.append(str(kwargs['file_path']))
            return {'message_id': len(self.sent), 'chat': {'id': 42, 'type': 'private'},
                    'video': {'file_id': 'good-' + str(len(self.sent)), 'file_unique_id': 'good-unique'}}
        self.telegram.request = request

    def run_queue(self):
        sync = SDSource.sync
        def fixed_time(source, **kwargs):
            return sync(source, now=fixtures.NOW, **kwargs)
        with (patch.object(self.archive, 'camera_sd_config', return_value=self.config),
              patch.object(self.archive, 'probe_camera', return_value={'tcp': {'8000': 'open'}}),
              patch.object(SDSource, '_provider', side_effect=self.provider),
              patch.object(SDSource, 'sync', autospec=True, side_effect=fixed_time),
              patch('archive_app.error_queue.retry_one', return_value=None)):
            self.assertTrue(self.queue.run_once(self.telegram))

    def exercise_all_bad_prefix(self, threads):
        self.prepare(threads=threads)
        first_id = self.queue.enqueue('front')['jobs'][0]['id']
        self.run_queue()
        first = next(job for job in self.queue.status()['jobs'] if job['id'] == first_id)
        self.assertEqual((first['state'], first['code']), ('completed', 'sd_batch_limit'))
        stats = first['statistics']
        self.assertEqual((stats['sd_downloaded'], stats['sd_imported'], stats['uploaded'], stats['sd_media_failed']), (10, 0, 0, 10))
        self.assertTrue(stats['sd_requeued'])
        self.assertEqual(stats['sd_quarantined_reviews'], 10)
        self.assertEqual(len(self.downloads), 10)
        self.assertEqual(self.sent, [])
        keys = list(stats['sd_media_failed_keys'])
        before = [dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?', (key,)).fetchone()) for key in keys]
        self.assertTrue(all(self.archive.retained_remux_failure(key) for key in keys))
        self.assertEqual(len([job for job in self.queue.status()['jobs'] if job['state'] == 'queued']), 1)
        self.run_queue()
        self.assertEqual(len(self.downloads), 12)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.archive.conn.execute("SELECT count(*) FROM recordings WHERE status='uploaded'").fetchone()[0], 2)
        after = [dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?', (key,)).fetchone()) for key in keys]
        self.assertEqual(before, after)
        self.assertTrue(all(Path(row['local_path']).read_bytes() == fixtures.BAD for row in after))
        self.assertFalse(any(job['state'] in ('queued', 'running') for job in self.queue.status()['jobs']))
        self.assertEqual(list((self.settings.cache_dir/'sd-stage'/'front').iterdir()), [])

    def test_all_bad_ten_file_prefix_continues_serial(self):
        self.exercise_all_bad_prefix(1)

    def test_all_bad_ten_file_prefix_continues_parallel(self):
        self.exercise_all_bad_prefix(2)

    def test_bad_first_file_with_healthy_neighbors_advances_and_never_posts_bad(self):
        self.prepare(bad_count=1)
        first_id = self.queue.enqueue('front')['jobs'][0]['id']
        self.run_queue()
        first = next(job for job in self.queue.status()['jobs'] if job['id'] == first_id)
        self.assertEqual((first['state'], first['statistics']['sd_media_failed'], first['statistics']['uploaded']), ('completed', 1, 9))
        self.assertTrue(first['statistics']['sd_requeued'])
        self.run_queue()
        self.assertEqual(len(self.sent), 11)
        self.assertEqual(self.archive.conn.execute("SELECT count(*) FROM recordings WHERE status='needs_review'").fetchone()[0], 1)

    def test_generic_media_error_still_blocks_without_continuation(self):
        self.prepare()
        normalize = core.normalize
        def unknown(source, dest, settings):
            if source.read_bytes() == fixtures.BAD:
                raise ValueError('synthetic unknown failure')
            return normalize(source, dest, settings)
        self.queue.enqueue('front')
        with patch('archive_app.core.normalize', side_effect=unknown):
            self.run_queue()
        latest = self.queue.status('front')['latest']['front']
        self.assertEqual(latest['state'], 'blocked')
        self.assertEqual(latest['statistics']['sd_error_code'], 'sd_media_validation_failed')
        self.assertFalse(latest['statistics']['sd_requeued'])
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.downloads), 1)
        self.assertFalse(any(job['state'] in ('queued', 'running') for job in self.queue.status()['jobs']))

    def test_media_only_exception_keeps_all_review_and_send_guardrails(self):
        self.prepare()
        first_id = self.queue.enqueue('front')['jobs'][0]['id']
        self.run_queue()
        first = next(job for job in self.queue.status()['jobs'] if job['id'] == first_id)
        stats = first['statistics']
        self.assertTrue(self.queue._all_bad_media_batch(stats))
        for field, value in (('ready', 1), ('upload_unknown', 1), ('failed_records', 1),
                             ('failed', 1), ('remux_failed', 1), ('remux_budget_blocked', 1),
                             ('spool_blocked', 1), ('sd_error_code', 'sd_media_validation_failed'),
                             ('sd_downloaded', 9), ('sd_media_failed', 9), ('sd_imported', 1),
                             ('uploaded', 1), ('pending', 11), ('sd_media_failed_keys', ['a'*64]*10)):
            with self.subTest(field=field):
                self.assertFalse(self.queue._all_bad_media_batch({**stats, field: value}))
        key = stats['sd_media_failed_keys'][0]
        for field, value in (('attempt_id', 'uncertain-attempt'), ('file_id', 'uncertain-send'), ('message_id', 42)):
            with self.subTest(field=field), self.archive.conn:
                self.archive.conn.execute('UPDATE recordings SET '+field+'=? WHERE key=?', (value, key))
                self.assertFalse(self.queue._all_bad_media_batch(stats))
                self.assertEqual(self.queue._quarantined_reviews(first, stats), 0)
                self.archive.conn.execute('UPDATE recordings SET '+field+'=NULL WHERE key=?', (key,))
        raw = self.archive.conn.execute('SELECT local_path FROM recordings WHERE key=?', (key,)).fetchone()[0]
        Path(raw).unlink()
        self.assertFalse(self.queue._all_bad_media_batch(stats))
        self.assertEqual(self.queue._quarantined_reviews(first, stats), 0)

    def test_quarantined_raw_expires_after_full_seventy_two_hours(self):
        self.prepare()
        self.queue.enqueue('front')
        self.run_queue()
        rows = self.archive.conn.execute("SELECT * FROM recordings WHERE status='needs_review'").fetchall()
        self.assertEqual(len(rows), 10)
        first_expiry = min(row['error_started_at'] for row in rows) + 72*3600
        expiry = max(row['error_started_at'] for row in rows) + 72*3600
        self.assertEqual(self.archive.expire_error_media(now=first_expiry-1), 0)
        self.assertTrue(all(Path(row['local_path']).is_file() for row in rows))
        self.assertEqual(self.archive.expire_error_media(now=expiry), 10)
        self.assertTrue(all(not Path(row['local_path']).exists() for row in rows))
        for row in self.archive.conn.execute('SELECT * FROM recordings'):
            self.assertEqual(row['status'], 'needs_review')
            self.assertIsNotNone(row['media_expired_at'])
            self.assertIsNone(row['file_id'])
            self.assertIsNone(row['message_id'])


if __name__ == '__main__':
    unittest.main()
