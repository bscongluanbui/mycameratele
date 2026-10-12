"""Persisted three-retry media quarantine; every Telegram call is mocked."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from archive_app import core
from archive_app.core import Archive, Settings
from archive_app.error_queue import MAX_RETRIES, RETRY_SECONDS, retry_one
from archive_app.telegram import ApiRejected, Telegram


CHANNEL = -1003953548424
BOT = 99
MP4 = b'\x00\x00\x00\x18ftypisom' + b'original encoded MP4 bytes' * 8
PS = b'\x00\x00\x01\xba' + b'original encoded camera PS bytes' * 8


class ErrorQueueTests(unittest.TestCase):
    def setUp(self):
        self.parent = (Path(__file__).resolve().parent if os.name == 'nt'
                       else Path(tempfile.gettempdir()).resolve())
        self.root = self.parent / ('.tmp-error-queue-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.after = time.time() - 10
        self.now = self.after + 20
        self.settings = Settings(self.root/'state', self.root/'cache', self.root/'input',
                                 'UTC+07:00', min_free_bytes=0, media_mode='remux_copy',
                                 enable_upload=True, token='fixture', owner_user_id=42,
                                 multi_channel_routing=True, channel_index_enabled=False)
        self.archive = Archive(self.settings)
        self.archive.add_camera({'id': 'front', 'name': 'Camera', 'channel_chat_id': CHANNEL})
        self.archive.add_camera({'id': 'other', 'channel_chat_id': CHANNEL-1})
        self.archive.state('telegram_bot_id', str(BOT))

    def tearDown(self):
        self.archive.close()
        self.assertEqual(self.root.resolve().parent, self.parent)
        shutil.rmtree(self.root)

    def row(self, key):
        return dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?', (key,)).fetchone())

    def reopen(self):
        self.archive.close()
        self.archive = Archive(self.settings)

    def seed(self, kind='api', *, number=1, camera='front', status='needs_review', count=0):
        key = hashlib.sha256((kind+str(number)+camera).encode()).hexdigest()
        body = MP4 if kind == 'api' else PS
        path = self.settings.cache_dir/(key + ('.mp4' if kind == 'api' else '.ps'))
        path.write_bytes(body)
        old = self.after - 1800 - number
        with self.archive.conn:
            self.archive.conn.execute('''INSERT INTO recordings
                (key,camera,record_id,start_ms,end_ms,source_path,local_path,status,
                 file_size,sha256,last_error,created_at,error_started_at,processing_method,
                 media_container,attempt_id,upload_target_chat_id,error_retry_count)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (key,camera,str(number),100000+number*1000,160000+number*1000,'camera-sd',
                 str(path),status,len(body),hashlib.sha256(body).hexdigest(),
                 'api_rejected' if kind == 'api' else 'failed_remux',old,old,
                 'remux_copy' if kind == 'api' else 'passthrough',
                 'mov,mp4' if kind == 'api' else 'mpeg',
                 'a'*64 if kind == 'api' else None,
                 CHANNEL if kind == 'api' else None,count))
        return key

    def receipt(self, *, camera='front', bot=BOT, channel=CHANNEL, uploaded_at=None):
        key = self.seed('api', number=500, camera=camera, status='uploaded')
        with self.archive.conn:
            self.archive.conn.execute('''UPDATE recordings SET chat_id=?,message_id=101,
                file_id='confirmed-file',file_unique_id='confirmed-unique',bot_id=?,
                uploaded_at=?,storage_kind='channel',storage_chat_id=?,storage_message_id=101
                WHERE key=?''', (str(channel),bot,self.after+1 if uploaded_at is None else uploaded_at,
                                 channel,key))
        return key

    def telegram(self):
        sender = Telegram(self.settings)
        return sender, patch.object(sender, 'verify_camera_channel', return_value=(CHANNEL,BOT))

    def retry(self, now=None):
        return retry_one(self.archive, 'front', after=self.after, now=self.now if now is None else now)

    def test_explicit_api_rejection_three_retries_then_never_sent_again_after_reopen(self):
        key = self.seed()
        self.receipt()
        original = self.row(key)
        sender, verified = self.telegram()
        with verified, patch('archive_app.telegram.check_upload_spool'), patch.object(
                sender, 'request', side_effect=ApiRejected(400, description=
                    'Bad Request: VIDEO_CONTENT_TYPE_INVALID')) as send:
            for attempt in range(1, MAX_RETRIES+1):
                result = self.retry(self.now + (attempt-1)*RETRY_SECONDS)
                self.assertEqual(result, {'key':key,'ready':True,'attempted':True,'queued':False,'kind':'api'})
                self.assertEqual(sender.upload_one(self.archive, 'front'), 'api_rejected')
                row = self.row(key)
                self.assertEqual(row['error_retry_count'], attempt)
                self.assertEqual(row['error_started_at'], original['error_started_at'])
                self.assertEqual(row['status'], 'needs_review')
                self.assertEqual(row['error_queue_at'] is not None, attempt == MAX_RETRIES)
                self.reopen()
            before = self.row(key)
            self.assertIsNone(self.retry(self.now+10*RETRY_SECONDS))
            self.assertIsNone(sender.upload_one(self.archive, 'front'))
            self.assertIsNone(self.archive.claim_upload('front', channel_chat_id=CHANNEL))
            self.assertEqual(self.archive.pending_upload_cameras('front'), [])
            self.assertEqual(self.row(key), before)
            self.assertEqual(send.call_count, MAX_RETRIES)
            self.assertEqual(Path(before['local_path']).read_bytes(), MP4)

    def test_new_access_or_unclassified_rejection_is_not_retried_or_queued(self):
        for code, description in ((403,'Forbidden: bot is not an administrator'),
                                  (401,'Unauthorized'),(400,'Bad Request: unknown request')):
            with self.subTest(code=code):
                with self.archive.conn:
                    self.archive.conn.execute('DELETE FROM recordings')
                key = self.seed(count=2)
                self.receipt()
                self.assertTrue(self.retry()['ready'])
                sender, verified = self.telegram()
                with verified, patch('archive_app.telegram.check_upload_spool'), patch.object(
                        sender,'request',side_effect=ApiRejected(code,description=description)):
                    self.assertEqual(sender.upload_one(self.archive,'front'), 'api_rejected')
                row = self.row(key)
                self.assertEqual(row['status'], 'needs_review')
                self.assertIsNone(row['error_queue_at'])
                before = dict(row)
                self.assertIsNone(self.retry(self.now+RETRY_SECONDS))
                self.assertEqual(self.row(key), before)

    def test_raw_failed_remux_three_retries_preserve_original_then_stop(self):
        key = self.seed('raw')
        original = self.row(key)
        with patch('archive_app.core.normalize', side_effect=core.MediaRemuxError(
                'sd_remux_codec_unsupported')) as remux:
            for attempt in range(1, MAX_RETRIES+1):
                result = self.retry(self.now+(attempt-1)*RETRY_SECONDS)
                self.assertEqual(result, {'key':key,'ready':False,'attempted':True,
                                         'queued':attempt == MAX_RETRIES,'kind':'raw'})
                row = self.row(key)
                self.assertEqual(row['error_retry_count'], attempt)
                self.assertEqual(row['error_started_at'], original['error_started_at'])
                self.assertEqual(row['sha256'], original['sha256'])
                self.assertEqual(Path(row['local_path']).read_bytes(), PS)
                self.reopen()
            before = self.row(key)
            self.assertIsNone(self.retry(self.now+10*RETRY_SECONDS))
            self.assertEqual(self.archive.remux_pending_cache('front'), {'converted':0,'failed':0})
            self.assertEqual(self.row(key), before)
            self.assertEqual(remux.call_count, MAX_RETRIES)

    def test_raw_success_uses_exact_key_and_promotes_only_that_file(self):
        key = self.seed('raw', number=1)
        untouched = self.seed('raw', number=2)
        original = self.row(key)
        def normalize(source, destination, settings):
            self.assertEqual(Path(source), Path(original['local_path']))
            self.assertEqual(settings.media_mode, 'remux_copy')
            Path(destination).write_bytes(MP4)
            return {'path':str(destination),'bytes':len(MP4),'duration':60,
                    'sha256':hashlib.sha256(MP4).hexdigest(),'processing_method':'remux_copy',
                    'container':'mov,mp4','file_extension':'.mp4'}
        # Oldest is selected deterministically; make key 1 older than key 2.
        with self.archive.conn:
            self.archive.conn.execute('UPDATE recordings SET created_at=created_at-100 WHERE key=?', (key,))
        before = self.row(untouched)
        with patch('archive_app.core.normalize', side_effect=normalize), patch.object(
                self.archive, 'remux_pending_cache', wraps=self.archive.remux_pending_cache) as operation:
            self.assertEqual(self.retry(), {'key':key,'ready':True,'attempted':True,'queued':False,'kind':'raw'})
            operation.assert_called_once_with('front', limit=1, key=key)
        self.assertEqual(self.row(untouched), before)
        self.assertEqual(self.row(key)['processing_method'], 'remux_copy')
        self.assertEqual(self.row(key)['error_retry_count'], 1)
        self.assertIsNone(self.row(key)['error_queue_at'])
        self.assertFalse(Path(original['local_path']).exists())

    def test_remux_budget_before_attempt_refunds_counter_and_deadline(self):
        key = self.seed('raw', count=2)
        before = self.row(key)
        with patch.object(self.archive, 'remux_pending_cache', return_value={
                'converted':0,'failed':0,'budget_blocked':1}):
            self.assertEqual(self.retry(), {'key':key,'ready':False,'attempted':False,'queued':False,'kind':'raw'})
        self.assertEqual(self.row(key), before)
        self.reopen()
        self.assertEqual(self.row(key), before)

    def test_raw_original_byte_mode_does_not_reserve_retry_or_change_error(self):
        for count in (0,2,3):
            with self.subTest(count=count):
                with self.archive.conn:
                    self.archive.conn.execute('DELETE FROM recordings')
                key = self.seed('raw', count=count)
                self.settings.media_mode = 'raw'
                before = self.row(key)
                with patch.object(self.archive,'remux_pending_cache') as remux:
                    self.assertIsNone(self.retry())
                    remux.assert_not_called()
                self.assertEqual(self.row(key), before)
                self.assertEqual(Path(before['local_path']).read_bytes(), PS)
                self.reopen()
                self.assertEqual(self.row(key), before)
        self.settings.media_mode = 'remux_copy'

    def test_delay_prevents_second_retry_and_does_not_change_first_failure_time(self):
        key = self.seed('raw')
        with patch('archive_app.core.normalize', side_effect=core.MediaRemuxError('sd_remux_codec_unsupported')):
            self.retry()
            before = self.row(key)
            self.assertIsNone(self.retry(self.now+RETRY_SECONDS-0.01))
            self.assertEqual(self.row(key), before)
            self.assertIsNotNone(self.retry(self.now+RETRY_SECONDS))
        self.assertEqual(self.row(key)['error_retry_count'], 2)

    def test_api_needs_current_camera_bot_channel_and_new_receipt(self):
        for change in ('none','other-camera','other-bot','other-channel','old-receipt'):
            with self.subTest(change=change):
                with self.archive.conn:
                    self.archive.conn.execute('DELETE FROM recordings')
                key = self.seed()
                if change != 'none':
                    self.receipt(camera='other' if change == 'other-camera' else 'front',
                                 bot=BOT+1 if change == 'other-bot' else BOT,
                                 channel=CHANNEL-1 if change == 'other-channel' else CHANNEL,
                                 uploaded_at=self.after-1 if change == 'old-receipt' else None)
                before = self.row(key)
                self.assertIsNone(self.retry())
                self.assertEqual(self.row(key), before)

    def test_disabled_camera_and_disabled_upload_never_retried(self):
        key = self.seed('raw')
        for field in ('enabled','upload_enabled'):
            with self.subTest(field=field):
                with self.archive.conn:
                    self.archive.conn.execute('UPDATE cameras SET '+field+'=0 WHERE id=?', ('front',))
                before = self.row(key)
                self.assertIsNone(self.retry())
                self.assertEqual(self.row(key), before)
                with self.archive.conn:
                    self.archive.conn.execute('UPDATE cameras SET '+field+'=1 WHERE id=?', ('front',))

    def test_uncertain_inflight_and_known_global_errors_not_retried(self):
        self.receipt()
        key = self.seed()
        for status,error in (('uploading','api_rejected'),('upload_unknown','api_rejected'),
                             ('needs_review','local_upload_path'),('needs_review','rate_limited'),
                             ('needs_review','storage_blocked'),('needs_review','ambiguous_TimeoutError')):
            with self.subTest(status=status,error=error):
                with self.archive.conn:
                    self.archive.conn.execute('UPDATE recordings SET status=?,last_error=? WHERE key=?', (status,error,key))
                before = self.row(key)
                self.assertIsNone(self.retry())
                self.assertEqual(self.row(key), before)

    def test_third_api_timeout_remains_unknown_and_is_never_queued_or_retried(self):
        key = self.seed(count=2)
        self.receipt()
        self.assertTrue(self.retry()['ready'])
        sender, verified = self.telegram()
        with verified, patch('archive_app.telegram.check_upload_spool'), patch.object(
                sender,'request',side_effect=TimeoutError('synthetic')) as send:
            self.assertEqual(sender.upload_one(self.archive,'front'), 'upload_unknown')
            self.assertEqual(self.row(key)['error_retry_count'], 3)
            self.assertEqual(self.row(key)['status'], 'upload_unknown')
            self.assertIsNone(self.row(key)['error_queue_at'])
            self.reopen()
            self.assertIsNone(self.retry(self.now+RETRY_SECONDS))
            self.assertIsNone(sender.upload_one(self.archive,'front'))
            send.assert_called_once()

    def test_third_rate_limit_keeps_reserved_normal_upload_not_terminal_error(self):
        key = self.seed(count=2)
        self.receipt()
        self.retry()
        sender, verified = self.telegram()
        with verified, patch('archive_app.telegram.check_upload_spool'), patch.object(
                sender,'request',side_effect=ApiRejected(429,retry_after=900)):
            self.assertEqual(sender.upload_one(self.archive,'front'), 'api_rejected')
        row = self.row(key)
        self.assertEqual(row['status'], 'downloaded')
        self.assertEqual(row['last_error'], 'rate_limited')
        self.assertEqual(row['error_retry_count'], 3)
        self.assertIsNone(row['error_queue_at'])
        self.assertIsNone(self.retry(self.now+RETRY_SECONDS))

    def test_existing_exhausted_raw_error_is_parked_without_fourth_attempt(self):
        key = self.seed('raw', count=3)
        with patch.object(self.archive,'remux_pending_cache') as remux:
            self.assertEqual(self.retry(), {'key':key,'ready':False,'attempted':False,'queued':True,'kind':'raw'})
            remux.assert_not_called()
        self.reopen()
        self.assertIsNone(self.retry(self.now+RETRY_SECONDS))
        self.assertEqual(self.row(key)['error_retry_count'], 3)

    def test_queued_file_is_not_claimed_even_if_status_is_accidentally_downloaded(self):
        key = self.seed(count=3)
        with self.archive.conn:
            self.archive.conn.execute("UPDATE recordings SET status='downloaded',error_queue_at=? WHERE key=?", (self.now,key))
        self.assertIsNone(self.archive.claim_upload('front',channel_chat_id=CHANNEL))
        self.assertEqual(self.archive.pending_upload_cameras('front'), [])
        self.assertIsNone(self.retry(self.now+RETRY_SECONDS))

    def test_crash_during_third_raw_retry_recovers_into_permanent_error_queue(self):
        key = self.seed('raw', count=3, status='ingesting')
        self.assertEqual(self.archive.recover_ingests(), 1)
        row = self.row(key)
        self.assertEqual(row['status'], 'needs_review')
        self.assertEqual(row['last_error'], 'failed_remux')
        self.assertIsNotNone(row['error_queue_at'])
        self.reopen()
        self.assertIsNone(self.retry(self.now+RETRY_SECONDS))

    def test_crash_after_third_raw_reservation_before_claim_is_permanent(self):
        key = self.seed('raw', count=3, status='failed')
        original = self.row(key)
        self.assertEqual(self.archive.recover_ingests(), 1)
        row = self.row(key)
        self.assertEqual(row['status'], 'needs_review')
        self.assertEqual(row['error_retry_count'], 3)
        self.assertEqual(row['error_started_at'], original['error_started_at'])
        self.assertIsNotNone(row['error_queue_at'])
        self.reopen()
        with patch('archive_app.core.normalize') as remux:
            self.assertIsNone(self.retry(self.now+RETRY_SECONDS))
            self.assertEqual(self.archive.remux_pending_cache('front'), {'converted':0,'failed':0})
            remux.assert_not_called()

    def test_crash_after_first_raw_reservation_preserves_due_and_counter(self):
        key = self.seed('raw', count=1, status='failed')
        deadline = self.now+RETRY_SECONDS
        with self.archive.conn:
            self.archive.conn.execute('UPDATE recordings SET error_retry_at=? WHERE key=?', (deadline,key))
        original = self.row(key)
        self.assertEqual(self.archive.recover_ingests(), 1)
        row = self.row(key)
        self.assertEqual(row['status'], 'needs_review')
        self.assertEqual(row['error_retry_count'], 1)
        self.assertEqual(row['error_retry_at'], deadline)
        self.assertEqual(row['error_started_at'], original['error_started_at'])
        self.assertIsNone(row['error_queue_at'])
        self.reopen()
        self.assertIsNone(self.retry(deadline-0.01))
        with patch('archive_app.core.normalize',side_effect=core.MediaRemuxError('sd_remux_codec_unsupported')):
            self.assertTrue(self.retry(deadline)['attempted'])
        self.assertEqual(self.row(key)['error_retry_count'], 2)

    def test_missing_changed_or_remote_identified_sources_are_not_retried(self):
        for changed in ('missing','size','file_id','message_id','chat_id','storage_chat_id','expired','deleted'):
            with self.subTest(changed=changed):
                with self.archive.conn:
                    self.archive.conn.execute('DELETE FROM recordings')
                key = self.seed('raw')
                row = self.row(key)
                if changed == 'missing':
                    Path(row['local_path']).unlink()
                elif changed == 'size':
                    Path(row['local_path']).write_bytes(PS+b'changed')
                else:
                    field = {'expired':'media_expired_at','deleted':'deleted_at'}.get(changed,changed)
                    with self.archive.conn:
                        self.archive.conn.execute('UPDATE recordings SET '+field+'=? WHERE key=?',
                                                  ('confirmed' if changed in ('file_id','chat_id') else 123,key))
                before = self.row(key)
                self.assertIsNone(self.retry())
                self.assertEqual(self.row(key), before)

    def test_current_job_errors_are_not_eligible_for_immediate_repeat(self):
        key = self.seed('raw')
        for field in ('created_at','error_started_at'):
            with self.subTest(field=field):
                with self.archive.conn:
                    self.archive.conn.execute('UPDATE recordings SET '+field+'=? WHERE key=?', (self.after,key))
                self.assertIsNone(self.retry())
                with self.archive.conn:
                    self.archive.conn.execute('UPDATE recordings SET '+field+'=? WHERE key=?', (self.after-1,key))

    def test_invalid_timestamps_or_camera_do_not_modify_queue(self):
        key = self.seed('raw')
        before = self.row(key)
        for value in (None, True, -1, float('nan'),float('inf'),'1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                retry_one(self.archive,'front',after=value,now=self.now)
        with self.assertRaises(ValueError):
            retry_one(self.archive,'../front',after=self.after,now=self.now)
        self.assertEqual(self.row(key), before)


if __name__ == '__main__':
    unittest.main()
