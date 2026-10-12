"""Old retained refusals do not stall healthy, same-channel SD history.

No camera, network, transcoding or real Telegram calls are used. A mocked
transport commits ordinary receipts, while all candidate media is real local
managed cache data. Every refused artifact and row remains unchanged.
"""
from contextlib import ExitStack
import importlib.util
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from archive_app.sync import SyncQueue, _statistics
from archive_app.telegram import ApiRejected
from tests import test_multi_channel_routing as fixtures


CHANNEL = -100111
BOT = 700
MP4 = b'\x00\x00\x00\x18ftypisom' + b'synthetic intact MP4 fixture bytes' * 4


class IsolatedReviewQueueTests(unittest.TestCase):
    setUp = fixtures.RoutingTests.setUp
    tearDown = fixtures.RoutingTests.tearDown
    request = fixtures.RoutingTests.request
    row = fixtures.RoutingTests.row

    def managed_record(self, record, *, camera='A', remuxed=True):
        source = self.settings.input_dir / (record + '.mp4')
        source.parent.mkdir(exist_ok=True)
        source.write_bytes(MP4)
        row = self.archive.ingest_entry({
            'camera': camera, 'record_id': record, 'path': str(source),
            'start_time': '2026-10-10T08:00:00+07:00',
            'end_time': '2026-10-10T08:01:00+07:00'})
        if remuxed:
            with self.archive.conn:
                self.archive.conn.execute("UPDATE recordings SET processing_method='remux_copy' WHERE key=?", (row['key'],))
        return row['key']

    def old_reviews(self, remux_count=7, refused_count=3):
        keys = []
        old = time.time() - 3600
        for number in range(remux_count + refused_count):
            raw = number < remux_count
            key = self.managed_record('old-' + str(number), remuxed=not raw)
            with self.archive.conn:
                self.archive.conn.execute('''UPDATE recordings SET status='needs_review',
                    last_error=?,created_at=?,error_started_at=?,attempt_id=?,upload_target_chat_id=?
                    WHERE key=?''', ('failed_remux' if raw else 'api_rejected', old, old,
                                    None if raw else ('%064x' % (number + 1)),
                                    None if raw else CHANNEL, key))
            keys.append(key)
        self.archive.state('telegram_bot_id', BOT)
        self.q = SyncQueue(self.archive)
        return keys

    def healthy_batch(self, *, count=10, retry=None):
        def source(*, max_files, progress):
            for number in range(count):
                self.managed_record('fresh-' + str(number))
                progress({'backend': 'hcnetsdk', 'searched': 904,
                          'downloaded': number + 1, 'imported': number + 1,
                          'backlog': 894, 'phase': 'sd_download'})
            return {'backend': 'hcnetsdk', 'searched': 904, 'downloaded': count,
                    'imported': count, 'backlog': 894, 'phase': 'sd_complete'}
        with ExitStack() as stack:
            stack.enter_context(patch.object(self.archive, 'probe_camera', return_value={'tcp': {'8000': 'open'}}))
            stack.enter_context(patch('archive_app.sd_source.SDSource.sync', side_effect=source))
            mocked_retry = Mock(side_effect=retry) if retry is not None else Mock(return_value=None)
            if importlib.util.find_spec('archive_app.error_queue') is None:
                # Baseline/rollback retains these regression fixtures but
                # removes the new runtime module. Original sync never invokes
                # the stub: it must reproduce the actual blocked job state.
                stack.enter_context(patch.dict(sys.modules, {
                    'archive_app.error_queue': SimpleNamespace(retry_one=mocked_retry)}))
            else:
                stack.enter_context(patch('archive_app.error_queue.retry_one', new=mocked_retry))
            self.assertTrue(self.q.run_once(self.telegram))

    def original_job(self, identifier):
        return next(job for job in self.q.status()['jobs'] if job['id'] == identifier)

    def fresh_receipt(self, record='receipt', *, camera='A', channel=CHANNEL, bot=BOT):
        key = self.managed_record(record, camera=camera)
        with self.archive.conn:
            self.archive.conn.execute('''UPDATE recordings SET attempt_id=?,upload_target_chat_id=?
                WHERE key=?''', ('f' * 64, channel, key))
        self.archive.mark_uploaded(key, channel, 91, 'confirmed-file',
                                   file_unique_id='confirmed-unique', media_type='video',
                                   bot_id=bot, storage_kind='channel',
                                   storage_chat_id=channel, storage_message_id=91)
        return key

    def review_statistics(self):
        stats = {**_statistics(), 'sd_backlog': 894, 'sd_downloaded': 10,
                 'sd_imported': 10, 'imported': 10, 'uploaded': 10}
        self.q._counts('A', stats)
        return stats

    def test_seven_retained_raw_and_three_old_refusals_continue_healthy_batch(self):
        keys = self.old_reviews()
        before = [self.row(key) for key in keys]
        media = [Path(row['local_path']).read_bytes() for row in before]
        identifier = self.q.enqueue('A')['jobs'][0]['id']
        self.q.enqueue('B')
        self.healthy_batch()
        job = self.original_job(identifier)
        self.assertEqual((job['state'], job['code']), ('completed', 'sd_batch_limit'))
        self.assertEqual(job['statistics']['uploaded'], 10)
        self.assertEqual(job['statistics']['sd_quarantined_reviews'], 10)
        self.assertTrue(job['statistics']['sd_requeued'])
        active = self.archive.conn.execute("SELECT camera_id,source FROM sync_jobs WHERE state='queued' ORDER BY created_at,id").fetchall()
        self.assertEqual([(row['camera_id'], row['source']) for row in active], [('B', 'dashboard'), ('A', 'backlog')])
        self.assertEqual([self.row(key) for key in keys], before)
        self.assertEqual([Path(row['local_path']).read_bytes() for row in before], media)
        self.assertEqual(sum(method == 'sendVideo' for method, fields in self.calls), 10)
        self.assertFalse(any(self.row(key)['file_id'] for key in keys))

    def test_current_api_refusal_is_not_bypassed_by_other_successful_uploads(self):
        keys = self.old_reviews()
        with self.archive.conn:
            self.archive.conn.execute('UPDATE recordings SET error_started_at=? WHERE key=?', (time.time() + 60, keys[-1]))
        identifier = self.q.enqueue('A')['jobs'][0]['id']
        self.healthy_batch()
        job = self.original_job(identifier)
        self.assertEqual((job['state'], job['code']), ('blocked', 'needs_review'))
        self.assertEqual(job['statistics']['uploaded'], 10)
        self.assertFalse(job['statistics']['sd_requeued'])

    def test_claimed_job_statistics_do_not_substitute_for_current_confirmed_receipt(self):
        self.old_reviews()
        self.q.enqueue('A')
        job = self.q._claim()
        stats = self.review_statistics()
        self.assertEqual(self.q._quarantined_reviews(job, stats), 0)
        self.q._finish(job, stats, 'blocked', 'sd_batch_limit', 'fixture', requeue_backlog=True, telegram=self.telegram)
        self.assertFalse(self.original_job(job['id'])['statistics']['sd_requeued'])

    def test_old_or_other_camera_channel_bot_receipt_never_proves_healthy_delivery(self):
        self.old_reviews()
        self.q.enqueue('A')
        job = self.q._claim()
        receipt = self.fresh_receipt()
        original = self.row(receipt)
        mutations = ({'uploaded_at': job['started_at'] - 1}, {'camera': 'B'},
                     {'chat_id': '-100222', 'storage_chat_id': -100222},
                     {'bot_id': BOT + 1}, {'file_id': ''}, {'file_unique_id': ''},
                     {'storage_message_id': 92})
        for changes in mutations:
            with self.subTest(changes=changes):
                with self.archive.conn:
                    self.archive.conn.execute('UPDATE recordings SET ' + ','.join(field + '=?' for field in changes) + ' WHERE key=?', (*changes.values(), receipt))
                self.assertEqual(self.q._quarantined_reviews(job, self.review_statistics()), 0)
                with self.archive.conn:
                    self.archive.conn.execute('UPDATE recordings SET ' + ','.join(field + '=?' for field in changes) + ' WHERE key=?', (*(original[field] for field in changes), receipt))
        self.assertEqual(self.q._quarantined_reviews(job, self.review_statistics()), 10)

    def test_changed_current_camera_channel_still_blocks_old_refusals(self):
        self.old_reviews()
        self.q.enqueue('A')
        job = self.q._claim()
        self.fresh_receipt()
        self.archive.update_camera('A', {'channel_chat_id': -100333})
        self.assertEqual(self.q._quarantined_reviews(job, self.review_statistics()), 0)

    def test_ready_ambiguous_and_generic_reviews_block_mixed_quarantine(self):
        self.old_reviews()
        self.q.enqueue('A')
        job = self.q._claim()
        self.fresh_receipt()
        other = self.managed_record('unresolved')
        for status in ('downloaded', 'uploading', 'upload_unknown', 'needs_review', 'failed'):
            with self.subTest(status=status):
                with self.archive.conn:
                    self.archive.conn.execute('UPDATE recordings SET status=?,last_error=? WHERE key=?', (status, 'unknown-generic-error', other))
                self.assertEqual(self.q._quarantined_reviews(job, self.review_statistics()), 0)

    def test_old_raw_candidate_also_needs_durable_unposted_media(self):
        keys = self.old_reviews()
        self.q.enqueue('A')
        job = self.q._claim()
        self.fresh_receipt()
        raw = keys[0]
        with self.archive.conn:
            self.archive.conn.execute('UPDATE recordings SET attempt_id=? WHERE key=?', ('a' * 64, raw))
        self.assertEqual(self.q._quarantined_reviews(job, self.review_statistics()), 0)
        with self.archive.conn:
            self.archive.conn.execute('UPDATE recordings SET attempt_id=NULL WHERE key=?', (raw,))
        Path(self.row(raw)['local_path']).unlink()
        self.assertEqual(self.q._quarantined_reviews(job, self.review_statistics()), 0)

    def test_new_error_between_quarantine_decision_and_finish_blocks_continuation(self):
        keys = self.old_reviews()
        self.q.enqueue('A')
        job = self.q._claim()
        self.fresh_receipt()
        stats = self.review_statistics()
        stats['sd_quarantined_reviews'] = self.q._quarantined_reviews(job, stats)
        self.assertEqual(stats['sd_quarantined_reviews'], 10)
        with self.archive.conn:
            self.archive.conn.execute('UPDATE recordings SET error_started_at=? WHERE key=?', (time.time(), keys[-1]))
        self.q._finish(job, stats, 'blocked', 'sd_batch_limit', 'fixture', requeue_backlog=True, telegram=self.telegram)
        finished = self.original_job(job['id'])
        self.assertEqual(finished['state'], 'blocked')
        self.assertEqual(finished['statistics']['sd_quarantined_reviews'], 0)
        self.assertFalse(finished['statistics']['sd_requeued'])

    def test_global_gate_and_telegram_retry_still_block_proven_isolated_reviews(self):
        self.old_reviews()
        for gate in ('upload_disabled', 'rate_limited'):
            with self.subTest(gate=gate):
                self.q.enqueue('A')
                job = self.q._claim()
                self.fresh_receipt('receipt-' + gate)
                stats = self.review_statistics()
                stats['sd_quarantined_reviews'] = self.q._quarantined_reviews(job, stats)
                self.assertEqual(stats['sd_quarantined_reviews'], 10)
                if gate == 'upload_disabled':
                    self.settings.enable_upload = False
                try:
                    with patch.object(self.telegram, 'upload_retry_until', return_value=time.time() + 3600 if gate == 'rate_limited' else 0):
                        self.q._finish(job, stats, 'blocked', 'sd_batch_limit', 'fixture', requeue_backlog=True, telegram=self.telegram)
                finally:
                    self.settings.enable_upload = True
                self.assertFalse(self.original_job(job['id'])['statistics']['sd_requeued'])

    def promote_old_api(self, key):
        def retry(archive, camera, *, after):
            self.assertIs(archive, self.archive)
            self.assertEqual(camera, 'A')
            self.assertTrue(archive.retained_api_rejection(key, after=after, camera=camera))
            with archive.conn:
                archive.conn.execute('''UPDATE recordings SET status='downloaded',
                    error_retry_count=error_retry_count+1,error_retry_at=? WHERE key=?''', (time.time() + 900, key))
            return {'key': key, 'ready': True, 'attempted': True, 'queued': False, 'kind': 'api'}
        return retry

    def test_one_old_retry_upload_is_counted_and_does_not_block_healthy_continuation(self):
        keys = self.old_reviews()
        identifier = self.q.enqueue('A')['jobs'][0]['id']
        self.healthy_batch(retry=self.promote_old_api(keys[-1]))
        job = self.original_job(identifier)
        self.assertEqual(job['state'], 'completed')
        self.assertTrue(job['statistics']['sd_requeued'])
        self.assertEqual(job['statistics']['error_retries'], 1)
        self.assertEqual(job['statistics']['uploaded'], 11)
        self.assertEqual(job['statistics']['sd_quarantined_reviews'], 9)
        self.assertEqual(self.row(keys[-1])['status'], 'uploaded')
        self.assertFalse(Path(self.row(keys[-1])['local_path']).exists())

    def reject_retry(self, key, code, description):
        base_request = self.telegram.request
        path = Path(self.row(key)['local_path'])
        def request(method, fields, **kwargs):
            if method in ('sendVideo', 'sendDocument') and kwargs.get('file_path') == path:
                raise ApiRejected(code, retry_after=60 if code == 429 else 0, description=description)
            return base_request(method, fields, **kwargs)
        self.telegram.request = request

    def test_exact_old_retry_definite_media_refusal_yields_without_resending_it(self):
        keys = self.old_reviews()
        key = keys[-1]
        self.reject_retry(key, 400, 'Bad Request: VIDEO_CONTENT_TYPE_INVALID')
        identifier = self.q.enqueue('A')['jobs'][0]['id']
        self.healthy_batch(retry=self.promote_old_api(key))
        job = self.original_job(identifier)
        self.assertEqual((job['state'], job['code']), ('completed', 'sd_batch_limit'))
        self.assertTrue(job['statistics']['sd_requeued'])
        self.assertEqual((job['statistics']['uploaded'], job['statistics']['error_retries']), (10, 1))
        self.assertEqual(self.row(key)['status'], 'needs_review')
        self.assertEqual(Path(self.row(key)['local_path']).read_bytes(), MP4)
        self.assertIsNone(self.row(key)['file_id'])

    def test_old_retry_429_still_pauses_continuation(self):
        keys = self.old_reviews()
        key = keys[-1]
        self.reject_retry(key, 429, 'Too Many Requests')
        identifier = self.q.enqueue('A')['jobs'][0]['id']
        self.healthy_batch(retry=self.promote_old_api(key))
        job = self.original_job(identifier)
        self.assertEqual(job['state'], 'blocked')
        self.assertFalse(job['statistics']['sd_requeued'])
        self.assertEqual(self.row(key)['status'], 'downloaded')
        self.assertEqual(self.row(key)['last_error'], 'rate_limited')

    def test_old_retry_timeout_preserves_unknown_and_blocks_continuation(self):
        keys = self.old_reviews()
        key = keys[-1]
        base_request = self.telegram.request
        path = Path(self.row(key)['local_path'])
        def request(method, fields, **kwargs):
            if method == 'sendVideo' and kwargs.get('file_path') == path:
                raise TimeoutError('synthetic unconfirmed request')
            return base_request(method, fields, **kwargs)
        self.telegram.request = request
        identifier = self.q.enqueue('A')['jobs'][0]['id']
        self.healthy_batch(retry=self.promote_old_api(key))
        job = self.original_job(identifier)
        self.assertEqual((job['state'], job['code']), ('blocked', 'upload_unknown'))
        self.assertFalse(job['statistics']['sd_requeued'])
        self.assertEqual(self.row(key)['status'], 'upload_unknown')
        self.assertTrue(Path(self.row(key)['local_path']).exists())

    def test_old_retry_permission_failure_still_blocks_continuation(self):
        keys = self.old_reviews()
        key = keys[-1]
        self.reject_retry(key, 403, 'Forbidden: bot is not a member of the channel chat')
        identifier = self.q.enqueue('A')['jobs'][0]['id']
        self.healthy_batch(retry=self.promote_old_api(key))
        job = self.original_job(identifier)
        self.assertEqual(job['state'], 'blocked')
        self.assertFalse(job['statistics']['sd_requeued'])
        self.assertEqual(self.row(key)['last_api_error_kind'], 'access')
        self.assertIsNone(self.row(key)['error_queue_at'])
        self.assertTrue(Path(self.row(key)['local_path']).exists())

    def test_old_retry_unclassified_bad_request_still_blocks_continuation(self):
        keys = self.old_reviews()
        key = keys[-1]
        self.reject_retry(key, 400, 'Bad Request: synthetic unexplained failure')
        identifier = self.q.enqueue('A')['jobs'][0]['id']
        self.healthy_batch(retry=self.promote_old_api(key))
        job = self.original_job(identifier)
        self.assertEqual(job['state'], 'blocked')
        self.assertFalse(job['statistics']['sd_requeued'])
        self.assertEqual(self.row(key)['last_api_error_kind'], 'unclassified')
        self.assertIsNone(self.row(key)['error_queue_at'])
        self.assertTrue(Path(self.row(key)['local_path']).exists())

    def test_permanent_three_attempt_queue_is_not_pending_but_unknown_is_never_hidden(self):
        keys = self.old_reviews()
        with self.archive.conn:
            self.archive.conn.execute("UPDATE recordings SET error_retry_count=3,error_queue_at=? WHERE camera='A'", (time.time(),))
        counts = self.review_statistics()
        self.assertEqual((counts['error_queue'], counts['needs_review'], counts['pending'], counts['ready']), (10, 0, 0, 0))
        self.q.enqueue('A')
        self.healthy_batch()
        finished = next(job for job in self.q.status()['jobs'] if job['state'] == 'completed')
        self.assertTrue(finished['statistics']['sd_requeued'])
        self.assertEqual(finished['statistics']['error_queue'], 10)
        self.assertEqual(finished['statistics']['error_retries'], 0)
        with self.archive.conn:
            self.archive.conn.execute("UPDATE recordings SET status='upload_unknown' WHERE key=?", (keys[-1],))
        suspicious = self.review_statistics()
        self.assertEqual((suspicious['upload_unknown'], suspicious['pending']), (1, 1))

    def test_no_retry_hook_is_invoked_after_current_file_upload_refusal(self):
        self.old_reviews()
        base_request = self.telegram.request
        def request(method, fields, **kwargs):
            if method == 'sendVideo':
                raise ApiRejected(400, description='Bad Request: VIDEO_CONTENT_TYPE_INVALID')
            return base_request(method, fields, **kwargs)
        self.telegram.request = request
        identifier = self.q.enqueue('A')['jobs'][0]['id']
        def forbidden(*args, **kwargs):
            self.fail('A current-file refusal must not admit an old retry')
        self.healthy_batch(retry=forbidden)
        job = self.original_job(identifier)
        self.assertEqual(job['state'], 'blocked')
        self.assertEqual(job['statistics']['error_retries'], 0)
        self.assertFalse(job['statistics']['sd_requeued'])

    def test_queue_marker_cannot_hide_ambiguous_send_from_job_state(self):
        keys = self.old_reviews()
        with self.archive.conn:
            self.archive.conn.execute("UPDATE recordings SET error_retry_count=3,error_queue_at=? WHERE camera='A'", (time.time(),))
            self.archive.conn.execute("UPDATE recordings SET status='upload_unknown' WHERE key=?", (keys[-1],))
        identifier = self.q.enqueue('A')['jobs'][0]['id']
        self.healthy_batch(count=1)
        job = self.original_job(identifier)
        self.assertEqual((job['state'], job['code']), ('blocked', 'upload_unknown'))
        self.assertFalse(job['statistics']['sd_requeued'])
        self.assertEqual((job['statistics']['pending'], job['statistics']['upload_unknown']), (1, 1))


if __name__ == '__main__':
    unittest.main()
