"""Per-camera navigation tests; only synthetic records and Telegram fixtures."""
import copy
import os
import shutil
import tempfile
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from archive_app.channel_index import ChannelIndex, channel_message_url
from archive_app.core import Archive, Settings
from archive_app.telegram import ApiRejected


class FixtureTelegram:
    def verify_camera_channel(self, archive, camera, *, require_index=True):
        return archive.resolve_camera_channel(camera),123456
    def __init__(self):
        self.calls, self.messages, self.pins = [], {}, []
        self.next_id, self.hook = 1000, None
        self.failure = None

    def request(self, method, fields):
        self.calls.append((method, copy.deepcopy(fields)))
        if self.hook:
            callback, self.hook = self.hook, None
            callback(method, fields)
        if self.failure:
            wanted, exc = self.failure
            if method == wanted:
                self.failure = None
                raise exc
        if method == 'sendMessage':
            self.next_id += 1
            message = {'message_id': self.next_id, 'chat': {'id': fields['chat_id']}, 'text': fields['text']}
            self.messages[(fields['chat_id'], self.next_id)] = message
            return copy.deepcopy(message)
        if method == 'editMessageText':
            key = (fields['chat_id'], fields['message_id'])
            if key not in self.messages:
                raise ApiRejected(400, description='Bad Request: message to edit not found')
            if self.messages[key]['text'] == fields['text']:
                raise ApiRejected(400, description='Bad Request: message is not modified')
            self.messages[key]['text'] = fields['text']
            return copy.deepcopy(self.messages[key])
        if method == 'pinChatMessage':
            self.pins.append((fields['chat_id'], fields['message_id']))
            return True
        raise AssertionError('Unexpected API method ' + method)


class ChannelIndexTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parent if os.name == 'nt' else Path(tempfile.gettempdir())
        self.root = parent / ('.tmp-channel-index-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.settings = Settings(self.root / 'state', self.root / 'cache', self.root / 'input', 'UTC+07:00',
            token='123456:synthetic', owner_user_id=23, allowed_users=(23,), bot_username='synthetic_bot',
            multi_channel_routing=True, channel_index_enabled=True, channel_index_debounce_seconds=60)
        self.now = 1780000000.0
        self.clock = patch('archive_app.channel_index.time.time', side_effect=lambda: self.now)
        self.clock.start()
        self.archive = Archive(self.settings)
        self.telegram = FixtureTelegram()
        self.index = ChannelIndex(self.archive, self.telegram)
        self.channel = -1001111111111
        self.camera('CAM01', self.channel)

    def tearDown(self):
        self.archive.close()
        self.clock.stop()
        shutil.rmtree(self.root)

    def camera(self, slug, channel, name='Cổng chính'):
        self.archive.add_camera({'id': slug, 'name': name, 'channel_chat_id': channel})

    def record(self, stamp='2026-10-10T08:00:00+07:00', *, camera='CAM01', channel=None,
               message=77, bot=123456, deleted=None, status='uploaded', kind='channel', end=None):
        start = int(datetime.fromisoformat(stamp).timestamp() * 1000)
        finish = int(datetime.fromisoformat(end).timestamp() * 1000) if end else start + 60000
        key = uuid.uuid4().hex + uuid.uuid4().hex
        with self.archive.conn:
            self.archive.conn.execute('''INSERT INTO recordings
                (key,camera,record_id,start_ms,end_ms,source_path,status,created_at,
                 storage_kind,storage_chat_id,storage_message_id,bot_id,deleted_at,uploaded_at)
                VALUES(?,?,?,?,?,'synthetic',?,?,?,?,?,?,?,?)''',
                (key, camera, key, start, finish, status, self.now, kind,
                 self.channel if channel is None else channel, message, bot, deleted, self.now))
        return key

    def run_job(self, camera='CAM01', day='2026-10-10'):
        self.index.enqueue(camera, day)
        self.now += 61
        return self.index.process_one()

    def node(self, kind, period, camera='CAM01', channel=None):
        return self.index._node(camera, channel or self.channel, kind, period)

    def text(self, kind, period, camera='CAM01', channel=None):
        node = self.node(kind, period, camera, channel)
        return self.telegram.messages[(node['channel_chat_id'], node['tg_message_id'])]['text']

    def sends(self):
        return [fields for method, fields in self.telegram.calls if method == 'sendMessage']

    def test_url_uses_actual_private_channel_reference(self):
        self.assertEqual(channel_message_url(self.channel, 77), 'https://t.me/c/1111111111/77')
        for channel, mid in ((123, 1), (-2001234, 1), (-1000, 1), (self.channel, 0), (self.channel, True)):
            with self.subTest(channel=channel, mid=mid), self.assertRaises(ValueError):
                channel_message_url(channel, mid)

    def test_first_record_creates_root_year_month_day_with_real_links(self):
        self.record()
        self.assertEqual(self.run_job(), 'updated')
        self.assertEqual(len(self.sends()), 4)
        root, year, month, day = [self.node(kind, period) for kind, period in
            (('root', 'root'), ('year', '2026'), ('month', '2026-10'), ('day', '2026-10-10'))]
        self.assertEqual(self.telegram.pins, [(self.channel, root['tg_message_id'])])
        self.assertIn(year['tg_message_url'], self.text('root', 'root'))
        self.assertIn(root['tg_message_url'], self.text('year', '2026'))
        self.assertIn(month['tg_message_url'], self.text('year', '2026'))
        self.assertIn(day['tg_message_url'], self.text('month', '2026-10'))
        self.assertIn(month['tg_message_url'], self.text('day', '2026-10-10'))
        self.assertIn('https://t.me/c/1111111111/77', self.text('day', '2026-10-10'))
        self.assertIn('https://t.me/synthetic_bot', self.text('root', 'root'))
        self.assertEqual(self.index.message_url('CAM01'), root['tg_message_url'])

    def test_all_posts_are_silent_text_only_and_only_root_is_pinned(self):
        self.record()
        self.run_job()
        for fields in self.sends():
            self.assertTrue(fields['disable_notification'])
            self.assertEqual(fields['parse_mode'], 'HTML')
            self.assertTrue(fields['link_preview_options']['is_disabled'])
        self.assertEqual({method for method, _ in self.telegram.calls}, {'sendMessage', 'editMessageText', 'pinChatMessage'})
        self.assertTrue(next(fields for method, fields in self.telegram.calls if method == 'pinChatMessage')['disable_notification'])

    def test_six_four_hour_buckets_use_first_recorded_not_first_uploaded(self):
        for hour in range(24):
            self.record(f'2026-10-10T{hour:02d}:00:00+07:00', message=200 + hour)
        self.run_job()
        text = self.text('day', '2026-10-10')
        for bucket in range(6):
            self.assertIn(f'{bucket * 4:02d}:00–{bucket * 4 + 3:02d}:59 · 4 video', text)
            self.assertIn(f'/c/1111111111/{200 + bucket * 4}', text)
        self.assertIn('Tổng: 24 video', text)
        self.assertIn('/c/1111111111/223', text)

    def test_unchanged_jobs_keep_stable_message_ids_and_do_not_edit(self):
        self.record()
        self.run_job()
        previous = [(row['id'], row['tg_message_id']) for row in self.archive.conn.execute('SELECT * FROM channel_index_messages')]
        calls = len(self.telegram.calls)
        self.assertEqual(self.run_job(), 'updated')
        self.assertEqual(len(self.telegram.calls), calls)
        self.assertEqual(previous, [(row['id'], row['tg_message_id']) for row in self.archive.conn.execute('SELECT * FROM channel_index_messages')])

    def test_new_clip_edits_existing_nodes_without_media_resend(self):
        self.record()
        self.run_job()
        day = self.node('day', '2026-10-10')['tg_message_id']
        self.record('2026-10-10T09:00:00+07:00', message=78)
        self.run_job()
        self.assertEqual(len(self.sends()), 4)
        self.assertEqual(day, self.node('day', '2026-10-10')['tg_message_id'])
        self.assertIn('Tổng: 2 video', self.text('day', '2026-10-10'))

    def test_late_record_creates_its_old_date_not_upload_date(self):
        self.record()
        self.run_job()
        latest = self.text('root', 'root')
        key = self.record('2025-01-01T00:00:00+07:00', message=90)
        self.assertTrue(self.index.enqueue_recording(key))
        self.now += 61
        self.assertEqual(self.index.process_one(), 'updated')
        self.assertIn('NĂM 2025', self.text('year', '2025'))
        self.assertIn('01/01/2025', self.text('day', '2025-01-01'))
        self.assertIn('10/10/2026', self.text('root', 'root'))
        self.assertIn('https://t.me/c/1111111111/77', latest)
        self.assertEqual(len(self.sends()), 7)

    def test_midnight_overlap_is_uploaded_once_and_indexed_by_start(self):
        key = self.record('2026-10-10T23:59:30+07:00', end='2026-10-11T00:00:30+07:00')
        self.index.enqueue_recording(key)
        self.now += 61
        self.index.process_one()
        self.assertIsNotNone(self.node('day', '2026-10-10'))
        self.assertIsNone(self.node('day', '2026-10-11'))
        self.assertEqual(self.archive.conn.execute('SELECT COUNT(*) FROM recordings').fetchone()[0], 1)

    def test_two_cameras_never_share_targets_or_index_nodes(self):
        other = -1002222222222
        self.camera('CAM02', other)
        self.record()
        self.record(camera='CAM02', channel=other)
        self.index.enqueue('CAM01', '2026-10-10')
        self.index.enqueue('CAM02', '2026-10-10')
        self.now += 61
        self.assertEqual(self.index.process_one(), 'updated')
        self.assertEqual(self.index.process_one(), 'updated')
        self.assertEqual(len(self.sends()), 8)
        self.assertIn('/c/2222222222/77', self.text('day', '2026-10-10', 'CAM02', other))
        self.assertNotIn('/c/1111111111/77', self.text('day', '2026-10-10', 'CAM02', other))

    def test_debounce_coalesces_and_does_not_starve_continuous_uploads(self):
        self.record()
        self.index.enqueue('CAM01', '2026-10-10')
        due = self.archive.conn.execute('SELECT due_at FROM channel_index_jobs').fetchone()[0]
        for _ in range(10):
            self.now += 5
            self.index.enqueue('CAM01', '2026-10-10')
        self.assertEqual(self.archive.conn.execute('SELECT COUNT(*) FROM channel_index_jobs').fetchone()[0], 1)
        self.assertEqual(self.archive.conn.execute('SELECT due_at FROM channel_index_jobs').fetchone()[0], due)
        self.assertIsNone(self.index.process_one())
        self.now += 11
        self.assertEqual(self.index.process_one(), 'updated')

    def test_enqueue_is_part_of_caller_transaction_and_rollback(self):
        self.record()
        self.archive.conn.execute('BEGIN IMMEDIATE')
        self.assertTrue(self.index.enqueue('CAM01', date(2026, 10, 10), commit=False))
        self.archive.conn.rollback()
        self.assertEqual(self.archive.conn.execute('SELECT COUNT(*) FROM channel_index_jobs').fetchone()[0], 0)
        self.assertEqual(self.telegram.calls, [])

    def test_rename_escapes_html_and_keeps_ids(self):
        self.record()
        self.run_job()
        ids = [row[0] for row in self.archive.conn.execute('SELECT tg_message_id FROM channel_index_messages ORDER BY id')]
        self.archive.update_camera('CAM01', {'name': '<Cổng & nhà>'})
        self.now += 61
        self.index.process_one()
        self.assertIn('&lt;Cổng &amp; nhà&gt;', self.text('root', 'root'))
        self.assertEqual(ids, [row[0] for row in self.archive.conn.execute('SELECT tg_message_id FROM channel_index_messages ORDER BY id')])

    def test_delete_and_restore_refresh_counts_without_touching_media(self):
        key = self.record()
        self.run_job()
        self.assertTrue(self.archive.soft_delete(key, 23))
        self.now += 61
        self.index.process_one()
        self.assertIn('Tổng: 0 video', self.text('day', '2026-10-10'))
        self.assertNotIn('/c/1111111111/77', self.text('day', '2026-10-10'))
        self.assertIn('Chưa có video', self.text('root', 'root'))
        self.assertTrue(self.archive.restore_recording(key, 23))
        self.now += 61
        self.index.process_one()
        self.assertIn('Tổng: 1 video', self.text('day', '2026-10-10'))
        self.assertEqual(len(self.sends()), 4)
        self.assertEqual(self.archive.conn.execute('SELECT status FROM recordings WHERE key=?', (key,)).fetchone()[0], 'uploaded')

    def test_empty_date_does_not_create_empty_years_or_nodes(self):
        self.assertEqual(self.run_job(day='2025-01-01'), 'updated')
        self.assertEqual(self.sends(), [])

    def test_current_channel_only_legacy_remains_untouched(self):
        self.record(channel=-1003333333333, message=99)
        self.record(message=77)
        self.run_job()
        self.assertIn('/c/1111111111/77', self.text('day', '2026-10-10'))
        self.assertNotIn('/c/3333333333/99', self.text('day', '2026-10-10'))
        self.assertEqual(self.index.rebuild('CAM01')['recordings'], 1)

    def test_wrong_bot_private_pending_and_deleted_rows_are_excluded(self):
        for fields in ({'bot': 99}, {'kind': 'owner_private'}, {'status': 'failed'}, {'deleted': self.now}, {'message': 0}):
            self.record(**fields)
        self.run_job()
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.index.rebuild('CAM01')['recordings'], 0)

    def test_channel_cutover_keeps_old_nodes_and_new_target_has_new_graph(self):
        key = self.record()
        self.run_job()
        old = self.node('root', 'root')['tg_message_id']
        new = -1004444444444
        self.archive.update_camera('CAM01', {'channel_chat_id': new})
        self.assertFalse(self.index.enqueue_recording(key))
        self.record(channel=new, message=100)
        self.run_job()
        self.assertEqual(len(self.sends()), 8)
        self.assertEqual(self.node('root', 'root')['tg_message_id'], old)
        self.assertIn('/c/4444444444/100', self.text('day', '2026-10-10', channel=new))
        self.assertNotIn('/c/1111111111/77', self.text('day', '2026-10-10', channel=new))

    def test_pending_old_mapping_becomes_obsolete_not_fallback(self):
        self.record()
        self.index.enqueue('CAM01', '2026-10-10')
        self.archive.update_camera('CAM01', {'channel_chat_id': -1004444444444})
        self.now += 61
        self.assertIsNone(self.index.process_one())
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.archive.conn.execute('SELECT status FROM channel_index_jobs').fetchone()[0], 'obsolete')

    def test_unconfigured_disabled_and_feature_off_send_nothing(self):
        self.record()
        self.archive.update_camera('CAM01', {'channel_enabled': False})
        self.assertFalse(self.index.enqueue('CAM01', '2026-10-10'))
        self.settings.multi_channel_routing = False
        self.assertFalse(self.index.enqueue('CAM01', '2026-10-10'))
        self.assertIsNone(self.index.process_one())
        self.settings.multi_channel_routing = True
        self.settings.channel_index_enabled = False
        self.assertFalse(self.index.enqueue('CAM01', '2026-10-10'))
        self.assertIsNone(self.index.process_one())
        self.assertEqual(self.telegram.calls, [])

    def test_cross_process_camera_lease_serializes_different_days(self):
        self.record()
        self.record('2026-10-11T08:00:00+07:00', message=78)
        self.index.enqueue('CAM01', '2026-10-10')
        self.index.enqueue('CAM01', '2026-10-11')
        self.now += 61
        other_archive = Archive(self.settings)
        other_api = FixtureTelegram()
        other = ChannelIndex(other_archive, other_api)
        seen = []
        self.telegram.hook = lambda method, fields: seen.append(other.process_one())
        try:
            self.assertEqual(self.index.process_one(), 'updated')
            self.assertEqual(seen, [None])
            self.assertEqual(other_api.calls, [])
            # Share simulated remote state for the second process' later edit.
            other_api.messages, other_api.next_id = self.telegram.messages, self.telegram.next_id
            self.assertEqual(other.process_one(), 'updated')
            self.assertEqual(len([x for x in other_api.calls if x[0] == 'sendMessage']), 1)
            self.assertEqual(self.archive.conn.execute('SELECT COUNT(*) FROM channel_index_messages').fetchone()[0], 5)
        finally:
            other_archive.close()

    def test_enqueue_while_job_running_preserves_a_followup_generation(self):
        self.record()
        self.index.enqueue('CAM01', '2026-10-10')
        self.now += 61
        self.telegram.hook = lambda method, fields: self.index.enqueue('CAM01', '2026-10-10')
        self.assertEqual(self.index.process_one(), 'retry')
        self.assertEqual(self.archive.conn.execute('SELECT status FROM channel_index_jobs').fetchone()[0], 'pending')
        self.assertEqual(self.index.process_one(), 'updated')
        self.assertEqual(len(self.sends()), 4)

    def test_429_keeps_node_pending_and_respects_global_retry_after(self):
        self.record()
        self.telegram.failure = ('sendMessage', ApiRejected(429, 120))
        self.assertEqual(self.run_job(), 'retry')
        self.assertEqual(self.node('root', 'root')['state'], 'pending')
        before = len(self.telegram.calls)
        self.now += 119
        self.assertEqual(self.index.process_one(), 'rate_limited')
        self.assertEqual(len(self.telegram.calls), before)
        self.now += 2
        self.assertEqual(self.index.process_one(), 'updated')
        self.assertEqual(self.archive.conn.execute('SELECT status FROM recordings').fetchone()[0], 'uploaded')

    def test_403_blocks_index_without_changing_uploaded_record(self):
        key = self.record()
        self.telegram.failure = ('sendMessage', ApiRejected(403))
        self.assertEqual(self.run_job(), 'blocked')
        self.now += 86400
        self.assertIsNone(self.index.process_one())
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.archive.conn.execute('SELECT status FROM recordings WHERE key=?', (key,)).fetchone()[0], 'uploaded')

    def test_pin_permission_error_keeps_known_root_without_duplicate(self):
        self.record()
        self.telegram.failure = ('pinChatMessage', ApiRejected(403))
        self.assertEqual(self.run_job(), 'blocked')
        root = self.node('root', 'root')['tg_message_id']
        self.index.rebuild('CAM01', dry_run=False)
        self.assertEqual(self.index.process_one(), 'updated')
        self.assertEqual(self.node('root', 'root')['tg_message_id'], root)
        self.assertEqual(len(self.sends()), 4)

    def test_timeout_send_needs_reconcile_and_rebuild_does_not_duplicate(self):
        self.record()
        self.telegram.failure = ('sendMessage', TimeoutError('synthetic timeout'))
        self.assertEqual(self.run_job(), 'needs_reconcile')
        self.assertEqual(self.node('root', 'root')['state'], 'needs_reconcile')
        self.now += 86400
        self.index.enqueue('CAM01', '2026-10-10')
        self.index.rebuild('CAM01', dry_run=False)
        self.assertIsNone(self.index.process_one())
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.index.rebuild('CAM01')['needs_reconcile'], 1)

    def test_server_500_and_invalid_confirmation_are_uncertain(self):
        for response in (ApiRejected(500), {'message_id': 88, 'chat': {'id': -1009999999999}}, True):
            with self.subTest(response=response):
                self.archive.conn.execute('DELETE FROM channel_index_jobs')
                self.archive.conn.execute('DELETE FROM channel_index_messages')
                self.archive.conn.commit()
                self.record()
                real = self.telegram.request
                if isinstance(response, Exception):
                    self.telegram.failure = ('sendMessage', response)
                    self.assertEqual(self.run_job(), 'needs_reconcile')
                else:
                    with patch.object(self.telegram, 'request', return_value=response):
                        self.assertEqual(self.run_job(), 'needs_reconcile')
                self.assertEqual(self.node('root', 'root')['state'], 'needs_reconcile')

    def test_operator_can_adopt_verified_unknown_message_then_finish(self):
        self.record()
        original = self.telegram.request
        def accepted_then_timeout(method, fields):
            result = original(method, fields)
            if method == 'sendMessage':
                raise TimeoutError('response lost')
            return result
        with patch.object(self.telegram, 'request', side_effect=accepted_then_timeout):
            self.assertEqual(self.run_job(), 'needs_reconcile')
        accepted = self.telegram.next_id
        self.assertEqual(self.index.reconcile('CAM01', self.channel, 'root', 'root', accepted), channel_message_url(self.channel, accepted))
        self.assertEqual(self.index.process_one(), 'updated')
        self.assertEqual(self.node('root', 'root')['tg_message_id'], accepted)
        self.assertEqual(len(self.sends()), 4)
        with self.assertRaises(ValueError):
            self.index.reconcile('CAM01', self.channel, 'root', 'root', accepted)

    def test_expired_running_send_is_quarantined_on_recovery(self):
        self.record()
        self.index.enqueue('CAM01', '2026-10-10')
        with self.archive.conn:
            self.archive.conn.execute("UPDATE channel_index_jobs SET status='running',lease_owner='dead',lease_until=?", (self.now - 1,))
            self.archive.conn.execute('INSERT INTO channel_index_leases VALUES(?,?,?,?)', ('CAM01', self.channel, 'dead', self.now - 1))
            self.archive.conn.execute('''INSERT INTO channel_index_messages
                (camera_id,channel_chat_id,index_type,period_key,state,updated_at)
                VALUES(?,?,'root','root','sending',?)''', ('CAM01', self.channel, self.now))
        self.assertEqual(self.index.recover(), 1)
        self.assertEqual(self.node('root', 'root')['state'], 'needs_reconcile')
        self.assertIsNone(self.index.process_one())
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.archive.conn.execute('SELECT COUNT(*) FROM channel_index_leases').fetchone()[0], 0)

    def test_expired_known_edit_job_retries_idempotently(self):
        self.record()
        self.run_job()
        with self.archive.conn:
            self.archive.conn.execute("UPDATE channel_index_jobs SET status='running',lease_owner='dead',lease_until=?", (self.now - 1,))
        self.assertEqual(self.index.recover(), 1)
        self.assertEqual(self.index.process_one(), 'updated')
        self.assertEqual(len(self.sends()), 4)

    def test_edit_timeout_retries_same_id_not_a_send(self):
        self.record()
        self.run_job()
        old = self.node('root', 'root')['tg_message_id']
        self.record('2026-10-10T09:00:00+07:00', message=78)
        self.telegram.failure = ('editMessageText', TimeoutError('synthetic'))
        self.assertEqual(self.run_job(), 'retry')
        self.now += 61
        self.assertEqual(self.index.process_one(), 'updated')
        self.assertEqual(self.node('root', 'root')['tg_message_id'], old)
        self.assertEqual(len(self.sends()), 4)

    def test_edit_not_modified_is_success(self):
        self.record()
        self.run_job()
        self.index.rebuild('CAM01', dry_run=False)
        self.assertEqual(self.index.process_one(), 'updated')
        self.assertEqual(len(self.sends()), 4)

    def test_deleted_day_is_recreated_once_and_parent_link_repaired(self):
        self.record()
        self.run_job()
        old = self.node('day', '2026-10-10')['tg_message_id']
        del self.telegram.messages[(self.channel, old)]
        self.index.rebuild('CAM01', '2026-10', dry_run=False)
        self.assertEqual(self.index.process_one(), 'updated')
        new = self.node('day', '2026-10-10')['tg_message_id']
        self.assertNotEqual(new, old)
        self.assertEqual(len(self.sends()), 5)
        self.assertIn(channel_message_url(self.channel, new), self.text('month', '2026-10'))
        self.assertNotIn(channel_message_url(self.channel, old), self.text('month', '2026-10'))

    def test_deleted_root_repaired_with_pin_and_other_year_backlinks_queued(self):
        self.record()
        self.run_job()
        self.record('2025-01-01T00:00:00+07:00', message=90)
        self.run_job(day='2025-01-01')
        old = self.node('root', 'root')['tg_message_id']
        del self.telegram.messages[(self.channel, old)]
        self.index.rebuild('CAM01', '2026', dry_run=False)
        self.assertIn(self.index.process_one(), ('updated', 'retry'))
        self.now += 61
        for _ in range(5):
            if self.index.process_one() is None:
                break
        new = self.node('root', 'root')['tg_message_id']
        self.assertNotEqual(new, old)
        self.assertEqual(len(self.sends()), 8)
        self.assertIn((self.channel, new), self.telegram.pins)
        for year in ('2025', '2026'):
            self.assertIn(channel_message_url(self.channel, new), self.text('year', year))
            self.assertNotIn(channel_message_url(self.channel, old), self.text('year', year))

    def test_cannot_edit_permission_is_not_treated_as_deleted(self):
        self.record()
        self.run_job()
        self.index.rebuild('CAM01', dry_run=False)
        self.telegram.failure = ('editMessageText', ApiRejected(400, description="Bad Request: message can't be edited"))
        self.assertEqual(self.index.process_one(), 'blocked')
        self.assertEqual(len(self.sends()), 4)

    def test_rebuild_dry_run_is_offline_and_apply_never_sends_directly(self):
        self.record()
        self.record('2026-11-01T00:00:00+07:00')
        plan = self.index.rebuild('CAM01', '2026-10')
        self.assertEqual(plan['dates'], ['2026-10-10'])
        self.assertEqual(plan['recordings'], 1)
        self.assertTrue(plan['dry_run'])
        self.assertEqual(self.archive.conn.execute('SELECT COUNT(*) FROM channel_index_jobs').fetchone()[0], 0)
        plan = self.index.rebuild('CAM01', '2026', dry_run=False)
        self.assertEqual(plan['jobs'], 2)
        self.assertEqual(self.telegram.calls, [])
        self.assertEqual(self.index.process_one(), 'updated')

    def test_invalid_period_dates_and_rebuild_values_are_rejected(self):
        for period in ('2026-13', '2026-02-30', '2026;DROP', '2026-1', 2026):
            with self.subTest(period=period), self.assertRaises((ValueError, TypeError)):
                self.index.rebuild('CAM01', period)
        for day in ('2026-13-10', '2026-02-30', 'x', datetime.now(), 1):
            with self.subTest(day=day), self.assertRaises(ValueError):
                self.index.enqueue('CAM01', day)
        with self.assertRaises(ValueError):
            self.index.rebuild('CAM01', dry_run=1)

    def test_render_bound_with_many_years_emoji_and_markup_in_name(self):
        self.archive.update_camera('CAM01', {'name': '🟢&' * 35})
        for year in range(1960, 2027):
            self.record(f'{year}-01-01T00:00:00+07:00', message=year)
            with self.archive.conn:
                self.archive.conn.execute('''INSERT INTO channel_index_messages
                    (camera_id,channel_chat_id,index_type,period_key,tg_message_id,state,updated_at)
                    VALUES(?,?,'year',?,?,'ready',?)''', ('CAM01', self.channel, str(year), year, self.now))
        text = self.index._render('CAM01', self.channel, 'root', 'root')
        self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 4096)
        self.assertIn('55 năm khác', text)
        self.assertIn('&amp;', text)

    def test_timezone_is_recorded_local_date_and_no_upload_time_used(self):
        key = self.record('2026-10-10T18:00:00+00:00')
        self.index.enqueue_recording(key)
        self.assertEqual(self.archive.conn.execute('SELECT recorded_date FROM channel_index_jobs').fetchone()[0], '2026-10-11')

    def test_missing_bot_identity_does_not_send(self):
        self.record()
        self.settings.token = ''
        self.index.enqueue('CAM01', '2026-10-10')
        self.now += 61
        self.assertIsNone(self.index.process_one())
        self.assertEqual(self.telegram.calls, [])

    def test_enqueue_recording_ignores_unknown_or_unmapped_record(self):
        self.assertFalse(self.index.enqueue_recording('f' * 64))
        self.assertFalse(self.index.enqueue_recording(self.record(channel=-1009999999999)))
        self.assertFalse(self.index.enqueue('unknown', '2026-10-10'))

    def test_job_states_do_not_modify_recordings_on_any_index_error(self):
        key = self.record()
        before = dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?', (key,)).fetchone())
        self.telegram.failure = ('sendMessage', TimeoutError())
        self.run_job()
        after = dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?', (key,)).fetchone())
        self.assertEqual(before, after)

    def test_preflight_permission_failure_never_creates_index_posts(self):
        self.record()
        with patch.object(self.telegram,'verify_camera_channel',side_effect=ApiRejected(403)):
            self.assertEqual(self.run_job(),'blocked')
        self.assertEqual(self.telegram.calls,[])

    def test_preflight_429_waits_before_rechecking(self):
        self.record()
        with patch.object(self.telegram,'verify_camera_channel',side_effect=ApiRejected(429,90)) as verify:
            self.assertEqual(self.run_job(),'retry')
            self.now+=89;self.assertIsNone(self.index.process_one());self.assertEqual(verify.call_count,1)
        self.now+=2;self.assertEqual(self.index.process_one(),'updated')


if __name__ == '__main__':
    unittest.main()
