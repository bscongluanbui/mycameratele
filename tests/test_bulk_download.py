"""Queued original-media albums: mocked Telegram, no SD/local-media reads."""
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from archive_app.core import Archive, Settings
from archive_app.telegram import ApiRejected, Telegram
from archive_app.telegram_bulk import TelegramBulk


class BulkDownloadTests(unittest.TestCase):
    def setUp(self):
        parent = str(Path(__file__).resolve().parent) if os.name == 'nt' else None
        self.temporary = tempfile.TemporaryDirectory(prefix='.tmp-mycam-bulk-', dir=parent)
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.settings = Settings(root/'state', root/'cache', root/'input', 'UTC+07:00',
                                 owner_user_id=42, allowed_users=(42, 43), token='700:synthetic-token',
                                 telegram_destination='channel', storage_channel_id=-1001234567890,
                                 min_free_bytes=0)
        self.archive = Archive(self.settings)
        self.addCleanup(self.archive.close)
        self.telegram = Telegram(self.settings)
        self.bulk = TelegramBulk(self.telegram)
        self.calls = []
        self.next_message = 100
        self.telegram.request = self.request
        self.selection = {'start_ms': 1791046800000, 'end_ms': 1791133200000,
                          'camera': 'front', 'order': 'asc', 'label': 'Hôm nay'}
        self.clock = 1800000000.0
        self.time_patch = patch('archive_app.telegram_bulk.time.time', side_effect=lambda: self.clock)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)

    def recording(self, number=0, camera='front', field='video', bot=700, deleted=None):
        key = f'{number + 1:064x}'
        start = self.selection['start_ms'] + number * 60000
        with self.archive.conn:
            self.archive.conn.execute('''INSERT INTO recordings
                (key,camera,record_id,start_ms,end_ms,source_path,status,created_at,
                 file_id,file_unique_id,media_type,bot_id,chat_id,message_id,deleted_at,
                 storage_kind,storage_chat_id,storage_message_id)
                VALUES(?,?,?,?,?,?,'uploaded',?,?,?,?,?,?,?,?, 'channel',?,?)''',
                                      (key, camera, f'synthetic-{number}', start, start+50000,
                                       'DO-NOT-READ-LOCAL-MEDIA', self.clock,
                                       f'file-{number}', f'unique-{number}', field, bot,
                                       str(self.settings.storage_channel_id), number+1, deleted,
                                       self.settings.storage_channel_id, number+1))
        return key

    def response(self, actor, field, media, group=None):
        number = int(media.split('-')[-1])
        self.next_message += 1
        result = {'chat': {'id': actor, 'type': 'private'}, 'message_id': self.next_message,
                  field: {'file_id': media, 'file_unique_id': f'unique-{number}'}}
        if group:
            result['media_group_id'] = group
        return result

    def request(self, method, fields, **kwargs):
        self.calls.append((method, copy.deepcopy(fields), kwargs))
        self.assertFalse(kwargs)  # Never file_path/file_field, HTTP/media uploads.
        if method == 'sendMediaGroup':
            return [self.response(fields['chat_id'], item['type'], item['media'], 'album') for item in fields['media']]
        if method in ('sendVideo', 'sendDocument'):
            field = 'video' if method == 'sendVideo' else 'document'
            return self.response(fields['chat_id'], field, fields[field])
        return {'message_id': 900}

    def enqueue(self, actor=43, selection=None, update_id=1):
        return self.bulk.enqueue(self.archive, actor, selection or self.selection, update_id=update_id)

    def process(self, advance=32):
        result = self.bulk.process_one(self.archive)
        self.clock += advance
        return result

    def state(self, job, actor=43):
        return self.bulk.status(self.archive, actor, job['id'])

    def test_enqueues_all_pages_and_uses_groups_max_ten_not_local_files(self):
        for number in range(27):
            self.recording(number)
        with patch('builtins.open', side_effect=AssertionError('No local-media reads')):
            job = self.enqueue()
            self.assertEqual(job['total'], 27)
            self.assertEqual(self.calls, [])
            results = [self.process() for _ in range(3)]
        self.assertEqual([len(fields['media']) for method, fields, _ in self.calls], [10, 10, 7])
        self.assertTrue(all(method == 'sendMediaGroup' for method, _, _ in self.calls))
        self.assertEqual([item['media'] for _, fields, _ in self.calls for item in fields['media']], [f'file-{number}' for number in range(27)])
        self.assertTrue(all(item['supports_streaming'] for _, fields, _ in self.calls for item in fields['media']))
        self.assertEqual((results[-1]['state'], self.state(job)['sent']), ('done', 27))
        self.assertEqual(self.archive.conn.execute("SELECT COUNT(*) FROM recordings WHERE status='uploaded'").fetchone()[0], 27)

    def test_single_video_and_single_document_reuse_the_correct_file_type(self):
        self.recording(0, field='video');self.recording(1, field='document')
        job = self.enqueue()
        self.process();self.process()
        self.assertEqual([call[0] for call in self.calls], ['sendVideo', 'sendDocument'])
        self.assertTrue(self.calls[1][1]['disable_content_type_detection'])
        self.assertEqual(self.state(job)['sent'], 2)

    def test_document_album_never_changes_type_or_transcodes(self):
        for number in range(3):self.recording(number, field='document')
        self.enqueue();self.process()
        method, fields, _ = self.calls[0]
        self.assertEqual(method, 'sendMediaGroup')
        self.assertTrue(all(item['type'] == 'document' and item['disable_content_type_detection'] for item in fields['media']))
        self.assertFalse(any('supports_streaming' in item for item in fields['media']))

    def test_descending_and_all_camera_snapshot_is_frozen(self):
        for number in range(13):self.recording(number, camera='front' if number % 2 else 'back')
        job = self.enqueue(selection=dict(self.selection, camera=None, order='desc'))
        self.recording(13)
        self.process();self.process()
        files = [item['media'] for _, fields, _ in self.calls for item in fields['media']]
        self.assertEqual(files, [f'file-{number}' for number in reversed(range(13))])
        self.assertEqual(self.state(job)['total'], 13)

    def test_exact_request_is_idempotent_even_after_completion(self):
        self.recording();job = self.enqueue();self.process()
        self.assertEqual(self.enqueue()['id'], job['id'])
        self.assertEqual(self.enqueue()['state'], 'done')
        self.assertEqual(len(self.calls), 1)

    def test_same_update_id_after_poll_epoch_reset_starts_new_job(self):
        self.recording();old = self.enqueue();self.process()
        self.archive.state('telegram_poll_epoch', json.dumps({'epoch': 1, 'at': self.clock, 'new_cursor': 1}))
        new = self.enqueue()
        self.assertNotEqual(old['id'], new['id'])
        # Diagnostics changing inside one epoch do not alter request identity.
        self.archive.state('telegram_poll_epoch', json.dumps({'epoch': 1, 'at': self.clock+5, 'new_cursor': 2}))
        self.assertEqual(self.enqueue()['id'], new['id'])

    def test_same_update_id_after_api_backend_change_has_new_request_identity(self):
        self.recording();old = self.enqueue();self.process()
        self.settings.api_base = 'http://telegram-bot-api:8081'
        self.settings.api_mode = 'local'
        self.assertNotEqual(self.enqueue()['id'], old['id'])

    def test_repeat_active_clicks_use_one_job_and_are_durable(self):
        self.recording();job = self.enqueue()
        again = self.enqueue(selection=dict(self.selection, order='desc'), update_id=2)
        self.assertEqual(again['id'], job['id'])
        self.process()
        self.assertEqual(self.enqueue(update_id=2)['state'], 'done')
        fresh = self.enqueue(update_id=3)
        self.assertNotEqual(fresh['id'], job['id'])  # Explicit new request allowed.

    def test_rejects_non_allowlisted_bool_or_destination_override(self):
        self.recording()
        for actor in (44, -100, True, '43', 0):
            with self.assertRaises(ValueError):self.enqueue(actor=actor)
        with self.assertRaises(ValueError):self.enqueue(update_id=True)
        self.assertEqual(self.calls, [])

    def test_wrong_actor_cannot_read_or_cancel_another_job(self):
        self.recording();job = self.enqueue()
        for action in (self.bulk.status, self.bulk.cancel, self.bulk.menu):
            with self.assertRaises(ValueError):action(self.archive, 42, job['id'])
        self.assertEqual(self.state(job)['state'], 'queued')

    def test_access_removed_after_enqueue_cancels_without_media_call(self):
        self.recording();job = self.enqueue()
        self.settings.allowed_users = ()
        result = self.process()
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.archive.conn.execute('SELECT state FROM telegram_bulk_jobs WHERE id=?', (job['id'],)).fetchone()[0], 'cancelled')

    def test_cancel_stops_remaining_not_original_archive(self):
        for number in range(23):self.recording(number)
        job = self.enqueue();self.process()
        cancelled = self.bulk.cancel(self.archive, 43, job['id'])
        self.assertEqual((cancelled['state'], cancelled['sent']), ('cancelled', 10))
        self.assertIsNone(self.process())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.archive.conn.execute('SELECT COUNT(*) FROM recordings WHERE deleted_at IS NULL').fetchone()[0], 23)

    def test_cancel_during_post_keeps_confirmation_and_stops_future_batches(self):
        for number in range(12):self.recording(number)
        job = self.enqueue();original = self.request
        def during(method, fields, **kwargs):
            self.bulk.cancel(self.archive, 43, job['id'])
            return original(method, fields, **kwargs)
        self.telegram.request = during
        self.process()
        self.assertEqual((self.state(job)['state'], self.state(job)['sent']), ('cancelled', 10))
        self.assertIsNone(self.process())

    def test_deleted_changed_and_wrong_bot_rows_are_skipped(self):
        keys = [self.recording(number) for number in range(5)]
        job = self.enqueue()
        with self.archive.conn:
            self.archive.conn.execute('UPDATE recordings SET deleted_at=? WHERE key=?', (self.clock, keys[0]))
            self.archive.conn.execute("UPDATE recordings SET file_id='new-id' WHERE key=?", (keys[1],))
            self.archive.conn.execute('UPDATE recordings SET bot_id=701 WHERE key=?', (keys[2],))
            self.archive.conn.execute("UPDATE recordings SET media_type='audio' WHERE key=?", (keys[3],))
        self.process()
        self.assertEqual((self.state(job)['sent'], self.state(job)['skipped']), (1, 4))
        self.assertEqual(self.calls[0][1]['video'], 'file-4')

    def test_initial_wrong_bot_and_missing_id_never_send(self):
        self.recording(0, bot=701);key = self.recording(1)
        with self.archive.conn:self.archive.conn.execute('UPDATE recordings SET file_id=NULL WHERE key=?', (key,))
        job = self.enqueue();self.process()
        self.assertEqual((self.state(job)['state'], self.state(job)['skipped']), ('done', 2))
        self.assertEqual(self.calls, [])

    def test_old_channel_and_invalid_placement_are_skipped(self):
        keys = [self.recording(number) for number in range(4)]
        with self.archive.conn:
            self.archive.conn.execute('UPDATE recordings SET storage_chat_id=-1001111111111 WHERE key=?', (keys[0],))
            self.archive.conn.execute('UPDATE recordings SET storage_message_id=0 WHERE key=?', (keys[1],))
            self.archive.conn.execute("UPDATE recordings SET storage_kind='owner_private' WHERE key=?", (keys[2],))
        job = self.enqueue();self.process()
        self.assertEqual((self.state(job)['sent'], self.state(job)['skipped']), (1, 3))
        self.assertEqual(self.calls[0][1]['video'], 'file-3')

    def test_changed_placement_after_queue_never_replays_even_current_channel(self):
        keys = [self.recording(number) for number in range(3)]
        job = self.enqueue()
        with self.archive.conn:
            self.archive.conn.execute('UPDATE recordings SET storage_message_id=999 WHERE key=?', (keys[0],))
            self.archive.conn.execute('UPDATE recordings SET chat_id=? WHERE key=?', ('-1009999999999', keys[1]))
        self.process()
        self.assertEqual((self.state(job)['sent'], self.state(job)['skipped']), (1, 2))
        self.assertEqual(self.calls[0][1]['video'], 'file-2')

    def test_owner_private_placement_accepts_only_owner_archive(self):
        self.settings.telegram_destination = 'owner_private';self.settings.storage_channel_id = 0
        keys = [self.recording(number) for number in range(2)]
        with self.archive.conn:
            self.archive.conn.execute("UPDATE recordings SET storage_kind='owner_private',storage_chat_id=NULL,storage_message_id=NULL,chat_id='42' WHERE key=?", (keys[0],))
            self.archive.conn.execute("UPDATE recordings SET storage_kind='owner_private',storage_chat_id=NULL,storage_message_id=NULL,chat_id='43' WHERE key=?", (keys[1],))
        job = self.enqueue();self.process()
        self.assertEqual((self.state(job)['sent'], self.state(job)['skipped']), (1, 1))
        self.assertEqual(self.calls[0][1]['video'], 'file-0')

    def test_429_known_rejection_retries_only_after_persistent_deadline(self):
        for number in range(3):self.recording(number)
        job = self.enqueue();original = self.request
        self.telegram.request = Mock(side_effect=ApiRejected(429, 90))
        result = self.bulk.process_one(self.archive)
        self.assertEqual(result['state'], 'rate_limited')
        self.assertEqual(self.state(job)['sent'], 0)
        self.assertEqual(self.state(job)['retry_at'], self.clock + 90)
        self.telegram.request = original
        self.clock += 89
        self.assertIsNone(self.bulk.process_one(self.archive))
        self.clock += 2
        self.assertEqual(self.bulk.process_one(self.archive)['state'], 'done')
        self.assertEqual(len(self.calls), 1)

    def test_429_and_successful_progress_resume_after_reopening_database(self):
        for number in range(13):self.recording(number)
        job = self.enqueue();self.process()
        with closing(Archive(self.settings)) as reopened:
            restarted = TelegramBulk(self.telegram)
            self.assertEqual(restarted.recover(reopened), 0)
            self.assertEqual(restarted.process_one(reopened)['state'], 'done')
        self.assertEqual(self.state(job)['sent'], 13)
        self.assertEqual([len(fields['media']) for _, fields, _ in self.calls], [10, 3])

    def test_crash_after_claim_becomes_unknown_and_never_replays(self):
        for number in range(12):self.recording(number)
        job = self.enqueue()
        # Simulates loss of power after persisted claim but before confirmation.
        with self.archive.conn:
            self.archive.conn.execute("UPDATE telegram_bulk_jobs SET state='running' WHERE id=?", (job['id'],))
            self.archive.conn.execute("UPDATE telegram_bulk_items SET state='sending' WHERE job_id=? AND position<10", (job['id'],))
        self.assertEqual(self.bulk.recover(self.archive), 1)
        self.assertEqual((self.state(job)['state'], self.state(job)['unknown']), ('unknown', 10))
        self.assertIsNone(self.process())
        self.assertEqual(self.calls, [])

    def test_transport_timeout_and_5xx_are_unknown_not_automatic_retry(self):
        for error in (TimeoutError(), ApiRejected(500), ApiRejected(502)):
            with self.subTest(error=type(error).__name__):
                number = len(self.archive.conn.execute('SELECT key FROM recordings').fetchall())
                self.recording(number)
                job = self.enqueue(update_id=number+1)
                self.telegram.request = Mock(side_effect=error)
                self.assertEqual(self.process()['state'], 'unknown')
                self.assertIsNone(self.process())
                self.assertGreaterEqual(self.state(job)['unknown'], 1)
                self.assertEqual(self.telegram.request.call_count, 1)

    def test_caption_exception_before_post_requeues_known_unsent_batch(self):
        self.recording();job = self.enqueue()
        with patch.object(self.telegram, 'caption', side_effect=ValueError('Synthetic caption failure')):
            self.assertEqual(self.process()['state'], 'preflight_retry')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.archive.conn.execute("SELECT COUNT(*) FROM telegram_bulk_items WHERE state='sending'").fetchone()[0], 0)
        self.assertEqual(self.process()['state'], 'done')
        self.assertEqual(self.state(job)['sent'], 1)

    def test_explicit_400_rejection_halts_failed_without_retry(self):
        self.recording();job = self.enqueue()
        self.telegram.request = Mock(side_effect=ApiRejected(400))
        self.assertEqual(self.process()['state'], 'failed')
        self.assertIsNone(self.process())
        self.assertEqual(self.state(job)['unknown'], 0)
        self.assertEqual(self.telegram.request.call_count, 1)

    def test_response_wrong_chat_identity_or_length_halts_unknown(self):
        for invalid in ('chat', 'length', 'unique', 'group', 'duplicate', 'field'):
            with self.subTest(invalid=invalid):
                self.recording(len(self.archive.conn.execute('SELECT key FROM recordings').fetchall()))
                self.recording(len(self.archive.conn.execute('SELECT key FROM recordings').fetchall()))
                update_id = self.archive.conn.execute('SELECT COUNT(*) FROM recordings').fetchone()[0]
                job = self.enqueue(update_id=update_id)
                original = self.request
                def corrupted(method, fields, **kwargs):
                    response = original(method, fields, **kwargs)
                    if invalid == 'chat':response[0]['chat']['id'] = 42
                    elif invalid == 'length':response.pop()
                    elif invalid == 'unique':response[0]['video']['file_unique_id'] = 'other-file'
                    elif invalid == 'group':response[0]['media_group_id'] = 'different-album'
                    elif invalid == 'duplicate':response[1]['message_id'] = response[0]['message_id']
                    elif invalid == 'field':response[0]['document'] = response[0].pop('video')
                    return response
                self.telegram.request = corrupted
                self.assertEqual(self.process()['state'], 'unknown')
                self.assertIsNone(self.process())
                self.assertEqual(self.state(job)['sent'], 0)

    def test_invalid_selection_inputs_do_not_create_jobs_or_send(self):
        self.recording()
        for change in ({'start_ms': True}, {'end_ms': self.selection['start_ms']},
                       {'camera': "front' OR 1=1"}, {'order': 'injection'}, {'label': 'x'*201}):
            with self.assertRaises(ValueError):self.enqueue(selection=dict(self.selection, **change))
        self.assertEqual(self.calls, [])

    def test_empty_selection_has_no_job(self):
        self.assertEqual(self.enqueue()['state'], 'empty')
        self.assertIsNone(self.process())

    def test_bounded_queue_and_item_limit(self):
        for number in range(3):self.recording(number)
        with patch.object(self.bulk, 'MAX_ITEMS', 2):
            with self.assertRaises(ValueError):self.enqueue()
        self.assertEqual(self.archive.conn.execute('SELECT COUNT(*) FROM telegram_bulk_jobs').fetchone()[0], 0)
        self.enqueue()
        with patch.object(self.bulk, 'MAX_ACTIVE', 1):
            with self.assertRaises(ValueError):self.enqueue(actor=42, update_id=2)

    def test_bounded_repeat_request_mapping_and_history(self):
        self.recording();job = self.enqueue()
        with patch.object(self.bulk, 'MAX_REQUESTS_PER_JOB', 1):
            with self.assertRaises(ValueError):self.enqueue(update_id=2)
        self.assertEqual(self.enqueue()['id'], job['id'])
        self.process()
        with patch.object(self.bulk, 'MAX_HISTORY', 2):
            for update_id in range(2, 6):
                self.enqueue(update_id=update_id);self.process()
        self.assertLessEqual(self.archive.conn.execute('SELECT COUNT(*) FROM telegram_bulk_jobs').fetchone()[0], 2)

    def test_partial_schema_is_repaired_before_queueing(self):
        self.bulk.ensure(self.archive)
        with self.archive.conn:
            self.archive.conn.execute('DROP TABLE telegram_bulk_items')
            self.archive.conn.execute('DROP TABLE telegram_bulk_requests')
        self.bulk.ensure(self.archive)
        names = {row[0] for row in self.archive.conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'telegram_bulk_%' AND type='table'")}
        self.assertEqual(names, {'telegram_bulk_jobs', 'telegram_bulk_items', 'telegram_bulk_requests'})
        self.recording();self.assertEqual(self.enqueue()['total'], 1)

    def test_missing_placement_migration_does_not_replay_unsnapshotted_items(self):
        self.recording();job = self.enqueue()
        with self.archive.conn:self.archive.conn.execute('ALTER TABLE telegram_bulk_items DROP COLUMN placement')
        self.bulk.ensure(self.archive)
        self.process()
        self.assertEqual((self.state(job)['state'], self.state(job)['skipped']), ('done', 1))
        self.assertEqual(self.calls, [])

    def test_per_chat_pacing_survives_cancel_then_new_job(self):
        self.recording();job = self.enqueue()
        self.process(advance=0)
        self.enqueue(update_id=2)
        self.assertIsNone(self.process(advance=0))
        self.assertEqual(len(self.calls), 1)
        self.clock += 32
        self.assertEqual(self.process()['state'], 'done')

    def test_new_scope_cancels_old_jobs_and_hides_status(self):
        self.recording();job = self.enqueue()
        self.settings.storage_channel_id = -1009999999999
        self.bulk.recover(self.archive)
        with self.assertRaises(ValueError):self.state(job)
        self.assertIsNone(self.process())
        self.assertEqual(self.calls, [])

    def test_menu_has_concise_status_and_private_cancel_callbacks(self):
        self.recording();job = self.enqueue()
        text, rows = self.bulk.menu(self.archive, 43, job['id'])
        self.assertIn('0/1', text)
        callbacks = [button['callback_data'] for row in rows for button in row]
        self.assertIn('bulk-status:' + job['id'], callbacks)
        self.assertIn('bulk-cancel:' + job['id'], callbacks)
        self.assertTrue(all(len(value.encode()) <= 64 for value in callbacks))

    def test_terminal_notice_is_one_shot_and_never_retries_media(self):
        self.recording();job = self.enqueue();self.process()
        self.assertEqual(self.bulk.notify_one(self.archive), job['id'])
        self.assertIsNone(self.bulk.notify_one(self.archive))
        self.assertEqual([call[0] for call in self.calls], ['sendVideo', 'sendMessage'])

    def test_terminal_notice_transport_error_does_not_repeat_or_send_media(self):
        self.recording();self.enqueue();self.process()
        self.telegram.request = Mock(side_effect=TimeoutError())
        with self.assertRaises(TimeoutError):self.bulk.notify_one(self.archive)
        self.assertIsNone(self.bulk.notify_one(self.archive))
        self.assertEqual(self.telegram.request.call_count, 1)


if __name__ == '__main__':
    unittest.main()
