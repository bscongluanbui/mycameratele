"""Pinned navigation tests; synthetic database records and text-only API fixtures."""
import re
import unittest
from datetime import datetime
from unittest.mock import patch

import test_channel_index as fixtures


class PinNavigationTests(unittest.TestCase):
    # Reuse the fixture setup/helpers, not its TestCase inheritance/discovery.
    setUp = fixtures.ChannelIndexTests.setUp
    tearDown = fixtures.ChannelIndexTests.tearDown
    camera = fixtures.ChannelIndexTests.camera
    record = fixtures.ChannelIndexTests.record
    run_job = fixtures.ChannelIndexTests.run_job
    node = fixtures.ChannelIndexTests.node
    text = fixtures.ChannelIndexTests.text
    sends = fixtures.ChannelIndexTests.sends

    ROOT_LABELS = ('🎬 Video mới nhất', '📅 Hôm nay', '🗓 Tháng hiện tại',
                   '📁 Kho lưu trữ theo năm', '🤖 Bot quản lý')

    def at(self, stamp):
        self.now = datetime.fromisoformat(stamp).timestamp()

    def root_items(self, text):
        return [line for line in text.splitlines()[2:] if line]

    def root_line(self, text, prefix):
        return next(line for line in self.root_items(text)
                    if re.sub(r'<[^>]+>', '', line).startswith(prefix))

    def today_line(self, text):
        return self.root_line(text, '📅 ')

    def ready(self, kind, period, *, camera='CAM01', channel=None, message=None):
        channel = self.channel if channel is None else channel
        if message is None:
            message = 2000 + self.archive.conn.execute('SELECT COUNT(*) FROM channel_index_messages').fetchone()[0]
        url = fixtures.channel_message_url(channel, message)
        with self.archive.conn:
            self.archive.conn.execute('''INSERT INTO channel_index_messages
                (camera_id,channel_chat_id,index_type,period_key,tg_message_id,tg_message_url,state,updated_at)
                VALUES(?,?,?,?,?,?,'ready',?)''', (camera, channel, kind, period, message, url, self.now))
        return url

    def markers(self):
        return dict(self.archive.conn.execute('SELECT name,value FROM channel_index_runtime'))

    def old_markers(self, *, version=1, ordinal=None):
        with self.archive.conn:
            for name, value in (('pin_navigation_version', version), ('pin_today_ordinal', ordinal)):
                if value is None:
                    self.archive.conn.execute('DELETE FROM channel_index_runtime WHERE name=?', (name,))
                else:
                    self.archive.conn.execute('''INSERT INTO channel_index_runtime VALUES(?,?)
                        ON CONFLICT(name) DO UPDATE SET value=excluded.value''', (name, value))
        self.now += 61

    def test_all_navigation_titles_use_camera_display_name_without_code(self):
        self.record()
        self.run_job()
        for kind, period in (('root', 'root'), ('years', 'years'), ('year', '2026'), ('month', '2026-10'), ('day', '2026-10-10')):
            with self.subTest(kind=kind):
                value = self.text(kind, period)
                self.assertIn('Cổng chính', value)
                self.assertNotIn('CAM01', value)

    def test_rename_html_escape_and_all_message_ids_remain_stable(self):
        self.record()
        self.run_job()
        before = [(r['id'], r['tg_message_id']) for r in self.archive.conn.execute('SELECT * FROM channel_index_messages')]
        self.archive.update_camera('CAM01', {'name': '<PN & Nhà>'})
        self.now += 61
        self.assertEqual(self.index.process_one(), 'updated')
        for kind, period in (('root', 'root'), ('years', 'years'), ('year', '2026'), ('month', '2026-10'), ('day', '2026-10-10')):
            value = self.text(kind, period)
            self.assertIn('&lt;PN &amp; Nhà&gt;', value)
            self.assertNotIn('CAM01', value)
        self.assertEqual(before, [(r['id'], r['tg_message_id']) for r in self.archive.conn.execute('SELECT * FROM channel_index_messages')])

    def test_root_has_five_fixed_navigation_links_not_date_lists(self):
        self.at('2026-10-10T12:00:00+07:00')
        self.record()
        self.run_job()
        root = self.text('root', 'root')
        self.assertEqual([re.sub(r'<[^>]+>', '', line) for line in self.root_items(root)], list(self.ROOT_LABELS))
        self.assertEqual(len(re.findall(r'<a href=', root)), 5)
        for kind, period, label in (
                ('years', 'years', self.ROOT_LABELS[3]),
                ('month', '2026-10', self.ROOT_LABELS[2]),
                ('day', '2026-10-10', self.ROOT_LABELS[1])):
            self.assertIn(self.node(kind, period)['tg_message_url'], self.root_line(root, label))
        self.assertNotIn(self.node('year', '2026')['tg_message_url'], root)
        self.assertNotIn('10/10/2026', root)

    def test_root_navigation_labels_are_short_exact_and_today_is_not_duplicated(self):
        self.at('2026-10-10T12:00:00+07:00')
        self.record()
        self.run_job()
        root = self.text('root', 'root')
        visible = [re.sub(r'<[^>]+>', '', line) for line in root.splitlines()]
        for heading in self.ROOT_LABELS:
            self.assertEqual(visible.count(heading), 1)
        self.assertRegex(self.today_line(root), r'^<a href="[^"]+">📅 Hôm nay</a>$')
        for old in ('XEM THEO', 'HÔM NAY', 'Tháng cũ hơn', 'Ngày cũ hơn',
                    'tìm trong bot', 'Chưa có video'):
            self.assertNotIn(old, root)

    def test_historical_years_have_separate_pages_not_root_month_or_day_lists(self):
        self.at('2026-10-10T12:00:00+07:00')
        for stamp in ('2025-10-10T08:00:00+07:00', '2026-10-10T08:00:00+07:00'):
            self.record(stamp)
            self.run_job(day=stamp[:10])
        root = self.text('root', 'root')
        years = self.text('years', 'years')
        for year in (2025, 2026):
            self.assertIn(self.node('year', str(year))['tg_message_url'], years)
            self.assertIn(f'📁 {year}', years)
            self.assertIn(self.node('month', f'{year}-10')['tg_message_url'], self.text('year', str(year)))
            self.assertIn(self.node('day', f'{year}-10-10')['tg_message_url'], self.text('month', f'{year}-10'))
            self.assertNotIn(f'10/{year}', root)
            self.assertNotIn(f'10/10/{year}', root)

    def test_today_uses_configured_timezone_not_utc_date(self):
        self.at('2026-10-10T18:00:00+00:00')  # 11 October at 01:00 camera-local time.
        self.record('2026-10-11T00:30:00+07:00')
        self.run_job(day='2026-10-11')
        today = self.today_line(self.text('root', 'root'))
        url = re.search(r'href="([^"]+)"', today).group(1)
        self.assertEqual(url, self.node('day', '2026-10-11')['tg_message_url'])
        self.assertEqual(re.sub(r'<[^>]+>', '', today), '📅 Hôm nay')
        self.assertNotRegex(today, r'\d{2}/\d{2}/\d{4}')

    def test_today_empty_does_not_relabel_latest_old_day_as_today(self):
        self.at('2026-10-11T12:00:00+07:00')
        self.record()
        self.run_job()
        today_line = self.today_line(self.text('root', 'root'))
        self.assertEqual(re.sub(r'<[^>]+>', '', today_line), '📅 Ngày gần nhất có video · 10/10/2026')
        self.assertIn(self.node('day', '2026-10-10')['tg_message_url'], today_line)
        self.assertNotIn('Hôm nay', today_line)
        self.assertIsNone(self.node('day', '2026-10-11'))

    def test_today_requires_ready_day_node_even_if_record_exists(self):
        self.at('2026-10-10T12:00:00+07:00')
        self.record()
        self.ready('year', '2026')
        self.ready('month', '2026-10')
        text = self.index._render('CAM01', self.channel, 'root', 'root')
        today_line = self.today_line(text)
        self.assertEqual(today_line, '📅 Hôm nay')
        self.assertNotIn('href=', today_line)
        self.assertIsNone(self.node('day', '2026-10-10'))

    def test_deleted_today_record_removes_today_link_without_removing_graph(self):
        self.at('2026-10-10T12:00:00+07:00')
        key = self.record()
        self.run_job()
        day_id = self.node('day', '2026-10-10')['tg_message_id']
        self.archive.soft_delete(key, 23)
        self.now += 61
        self.assertEqual(self.index.process_one(), 'updated')
        root = self.text('root', 'root')
        self.assertNotIn(self.node('day', '2026-10-10')['tg_message_url'], root)
        self.assertEqual(self.today_line(root), '📅 Hôm nay · Chưa có video')
        self.assertEqual(self.node('day', '2026-10-10')['tg_message_id'], day_id)

    def test_shortcuts_filter_wrong_bot_channel_kind_status_and_deleted_records(self):
        self.record()
        self.run_job()
        for fields in ({'bot': 99}, {'channel': -1003333333333}, {'kind': 'owner_private'},
                       {'status': 'failed'}, {'deleted': self.now}, {'message': 0}):
            self.record('2025-01-01T08:00:00+07:00', **fields)
        for kind, period in (('year', '2025'), ('month', '2025-01'), ('day', '2025-01-01')):
            self.ready(kind, period)
        root = self.index._render('CAM01', self.channel, 'root', 'root')
        years = self.index._render('CAM01', self.channel, 'years', 'years')
        self.assertNotIn('2025', root)
        self.assertNotIn('2025', years)
        for kind, period in (('year', '2025'), ('month', '2025-01'), ('day', '2025-01-01')):
            self.assertNotIn(fixtures.channel_message_url(self.channel, self.node(kind, period)['tg_message_id']), root)
            self.assertNotIn(fixtures.channel_message_url(self.channel, self.node(kind, period)['tg_message_id']), years)

    def test_shortcuts_do_not_link_nonready_nodes_or_other_camera_graph(self):
        self.record()
        self.run_job()
        self.record('2025-01-01T08:00:00+07:00')
        for kind, period in (('year', '2025'), ('month', '2025-01'), ('day', '2025-01-01')):
            self.ready(kind, period)
        with self.archive.conn:
            self.archive.conn.execute("UPDATE channel_index_messages SET state='pending' WHERE period_key LIKE '2025%'")
        self.camera('CAM02', -1002222222222)
        self.record('2024-01-01T08:00:00+07:00', camera='CAM02', channel=-1002222222222)
        other = self.ready('day', '2024-01-01', camera='CAM02', channel=-1002222222222)
        root = self.index._render('CAM01', self.channel, 'root', 'root')
        years = self.index._render('CAM01', self.channel, 'years', 'years')
        self.assertNotIn('2025', root)
        self.assertNotIn('2025', years)
        self.assertNotIn(other, root)
        self.assertNotIn(other, years)

    def test_root_is_bounded_with_emoji_escaped_name_and_large_date_history(self):
        self.at('2026-10-31T12:00:00+07:00')
        self.archive.update_camera('CAM01', {'name': '🟢&' * 35})
        self.ready('years', 'years')
        for year in range(1960, 2027):
            self.record(f'{year}-01-01T08:00:00+07:00', message=year)
            self.ready('year', str(year))
        for year in (2025, 2026):
            for month in range(1, 13):
                stamp = f'{year}-{month:02d}-01'
                self.record(stamp + 'T08:00:00+07:00', message=year + month)
                self.ready('month', stamp[:7])
        for day in range(1, 32):
            stamp = f'2026-10-{day:02d}'
            self.record(stamp + 'T08:00:00+07:00', message=day)
            self.ready('day', stamp)
        root = self.index._render('CAM01', self.channel, 'root', 'root')
        self.assertLessEqual(len(root.encode('utf-16-le')) // 2, 4096)
        self.assertIn('&amp;', root)
        self.assertEqual(len(self.root_items(root)), 5)
        self.assertEqual(len(re.findall(r'<a href=', root)), 5)
        self.assertIn(self.node('years', 'years')['tg_message_url'], root)
        self.assertNotIn('📁 1960', root)
        self.assertNotIn('01/01/2025', root)

    def test_root_raw_html_budget_handles_max_name_channel_id_and_message_id(self):
        self.at('2026-12-31T12:00:00+07:00')
        channel = -1009999999999999999  # Valid signed-64-bit Telegram channel fixture.
        self.archive.update_camera('CAM01', {'name': '&' * 80})
        # Stress the index renderer's signed-64-bit database boundary; dashboard
        # configuration intentionally applies Telegram's smaller 52-bit limit.
        with self.archive.conn:
            self.archive.conn.execute('UPDATE cameras SET channel_chat_id=? WHERE id=?', (channel, 'CAM01'))
        self.ready('years', 'years', channel=channel)
        self.settings.bot_username = 'x' * 32
        message = 2147483647
        for year in range(2015, 2027):
            self.record(f'{year}-01-01T08:00:00+07:00', channel=channel, message=2147483647)
            self.ready('year', str(year), channel=channel, message=message)
            message -= 1
        for month in range(1, 13):
            stamp = f'2026-{month:02d}-01'
            self.record(stamp + 'T08:00:00+07:00', channel=channel, message=2147483647)
            self.ready('month', stamp[:7], channel=channel, message=message)
            message -= 1
        for day in range(25, 32):
            stamp = f'2026-12-{day:02d}'
            self.record(stamp + 'T08:00:00+07:00', channel=channel, message=2147483647)
            self.ready('day', stamp, channel=channel, message=message)
            message -= 1
        root = self.index._render('CAM01', channel, 'root', 'root')
        self.assertLessEqual(len(root.encode('utf-16-le')) // 2, 4096)
        self.assertIn('&amp;' * 80, root)
        self.assertIn('/c/9999999999999999/2147483647', root)
        self.assertEqual(len(self.root_items(root)), 5)
        self.assertEqual(len(re.findall(r'<a href=', root)), 5)
        self.assertIn('href=', self.today_line(root))

    def test_root_uses_only_current_month_and_most_recent_recorded_day(self):
        self.at('2026-10-03T12:00:00+07:00')
        for stamp in ('2026-09-30', '2026-10-01', '2026-10-02'):
            self.record(stamp + 'T08:00:00+07:00')
            self.run_job(day=stamp)
        root = self.text('root', 'root')
        self.assertIn(self.node('month', '2026-10')['tg_message_url'], self.root_line(root, '🗓 Tháng hiện tại'))
        self.assertNotIn(self.node('month', '2026-09')['tg_message_url'], root)
        self.assertIn(self.node('day', '2026-10-02')['tg_message_url'], self.today_line(root))
        self.assertIn('📅 Ngày gần nhất có video · 02/10/2026', self.today_line(root))
        self.assertNotIn(self.node('day', '2026-10-01')['tg_message_url'], root)
        self.assertNotIn(self.node('day', '2026-09-30')['tg_message_url'], root)

    def test_only_root_is_pinned_and_navigation_never_sends_media(self):
        self.record()
        self.run_job()
        self.assertEqual(self.telegram.pins, [(self.channel, self.node('root', 'root')['tg_message_id'])])
        self.assertEqual({method for method, _ in self.telegram.calls}, {'sendMessage', 'editMessageText', 'pinChatMessage'})
        for fields in self.sends():
            self.assertTrue(fields['disable_notification'])
            self.assertEqual(fields['parse_mode'], 'HTML')

    def test_day_has_twenty_four_hour_cells_not_old_four_hour_buckets(self):
        self.record('2026-10-10T01:20:00+07:00', message=55)
        self.record('2026-10-10T23:50:00+07:00', message=99)
        self.run_job()
        day = self.text('day', '2026-10-10')
        for hour in range(24):
            with self.subTest(hour=hour):
                if hour in (1,23):
                    self.assertRegex(day, f'>{hour:02d}:00</a>')
                else:
                    self.assertIn(f'{hour:02d} —', day)
        self.assertIn(fixtures.channel_message_url(self.channel,55),day)
        self.assertIn(fixtures.channel_message_url(self.channel,99),day)
        self.assertNotIn('00:00–03:59',day)
        self.assertEqual(len(re.findall(r'<a href=',day)),3)  # Two videos and parent month.

    def test_empty_startup_records_navigation_marker_but_creates_no_job_or_node(self):
        self.at('2026-10-10T12:00:00+07:00')
        self.assertEqual(self.index.refresh_navigation(), 0)
        self.assertEqual(self.markers()['pin_navigation_version'], 4)
        self.assertEqual(self.markers()['pin_today_ordinal'], datetime(2026, 10, 10).date().toordinal())
        self.assertEqual(self.archive.conn.execute('SELECT COUNT(*) FROM channel_index_jobs').fetchone()[0], 0)
        self.assertEqual(self.archive.conn.execute('SELECT COUNT(*) FROM channel_index_messages').fetchone()[0], 0)
        self.assertEqual(self.telegram.calls, [])

    def test_version_three_upgrade_refresh_enqueues_all_existing_days_once_without_api_calls(self):
        self.record()
        self.run_job()
        self.record('2026-10-11T08:00:00+07:00')
        self.run_job(day='2026-10-11')
        self.old_markers(version=3)
        before = len(self.telegram.calls)
        self.assertEqual(self.index.refresh_navigation(), 1)
        pending = list(self.archive.conn.execute("SELECT * FROM channel_index_jobs WHERE status='pending'"))
        self.assertEqual({row['recorded_date'] for row in pending}, {'2026-10-10', '2026-10-11'})
        self.assertEqual(len(self.telegram.calls), before)
        self.assertEqual(self.index.refresh_navigation(), 0)
        self.now += 61
        self.assertEqual(self.index.refresh_navigation(), 0)
        self.assertEqual(len(list(self.archive.conn.execute("SELECT * FROM channel_index_jobs WHERE status='pending'"))), 2)

    def test_upgrade_includes_historical_recorded_day_without_existing_index(self):
        self.record()
        self.run_job()
        self.record('2025-01-01T08:00:00+07:00')
        self.old_markers(version=3)
        self.assertEqual(self.index.refresh_navigation(),1)
        pending=list(self.archive.conn.execute("SELECT recorded_date FROM channel_index_jobs WHERE status='pending'"))
        self.assertEqual({row[0] for row in pending},{'2025-01-01','2026-10-10'})
        self.assertIsNone(self.node('day','2025-01-01'))  # Scheduling itself stays offline.

    def test_upgrade_keeps_uncertain_text_send_quarantined_without_duplicate_send(self):
        self.record()
        self.run_job()
        self.record('2026-10-11T08:00:00+07:00')
        self.run_job(day='2026-10-11')
        node=self.node('day','2026-10-10')
        with self.archive.conn:
            self.archive.conn.execute("UPDATE channel_index_messages SET state='needs_reconcile',last_error='send_outcome_unknown' WHERE id=?",(node['id'],))
            self.archive.conn.execute("UPDATE channel_index_jobs SET status='needs_reconcile',last_error='send_outcome_unknown' WHERE recorded_date='2026-10-10'")
        sends=len(self.sends())
        self.old_markers(version=3)
        self.assertEqual(self.index.refresh_navigation(),1)
        uncertain=self.node('day','2026-10-10')
        self.assertEqual((uncertain['state'],uncertain['tg_message_id']),('needs_reconcile',node['tg_message_id']))
        job=self.archive.conn.execute("SELECT status,last_error FROM channel_index_jobs WHERE recorded_date='2026-10-10'").fetchone()
        self.assertEqual(tuple(job),('needs_reconcile','send_outcome_unknown'))
        self.assertEqual(len(self.sends()),sends)
        self.assertEqual(self.index.process_one(),'updated')
        self.assertIsNone(self.index.process_one())
        self.assertEqual(len(self.sends()),sends)

    def test_refresh_uses_camera_local_midnight_and_relabels_latest_day_without_upload(self):
        self.at('2026-10-10T23:57:00+07:00')
        self.record('2026-10-10T08:00:00+07:00')
        self.run_job()
        root_id = self.node('root', 'root')['tg_message_id']
        old_day = self.node('day', '2026-10-10')['tg_message_url']
        self.assertIn(old_day, self.today_line(self.text('root', 'root')))
        self.assertIn('📅 Hôm nay', self.today_line(self.text('root', 'root')))
        self.at('2026-10-11T00:01:00+07:00')
        self.assertEqual(self.index.process_one(), 'updated')
        root = self.text('root', 'root')
        today_line = self.today_line(root)
        self.assertEqual(re.sub(r'<[^>]+>', '', today_line), '📅 Ngày gần nhất có video · 10/10/2026')
        self.assertIn(old_day, today_line)
        self.assertNotIn('Hôm nay', today_line)
        self.assertIsNone(self.node('day', '2026-10-11'))
        self.assertEqual(self.node('root', 'root')['tg_message_id'], root_id)
        self.assertEqual(len(self.sends()), 5)
        self.assertTrue(all(method in ('sendMessage', 'editMessageText', 'pinChatMessage') for method, _ in self.telegram.calls))

    def test_process_one_refreshes_existing_pin_on_first_upgraded_boot(self):
        self.record()
        self.run_job()
        root_id = self.node('root', 'root')['tg_message_id']
        self.old_markers(version=3)
        self.assertEqual(self.index.process_one(), 'updated')
        self.assertEqual(self.markers()['pin_navigation_version'], 4)
        self.assertEqual(self.node('root', 'root')['tg_message_id'], root_id)
        self.assertEqual(len(self.sends()), 5)
        self.assertIsNone(self.index.process_one())

    def test_refresh_schedules_paused_camera_but_preserves_upload_toggles(self):
        self.record()
        self.run_job()
        self.archive.update_camera('CAM01', {'enabled': False, 'upload_enabled': False})
        # Drain the ordinary rename/config refresh before testing rollover alone.
        self.now += 61
        self.index.process_one()
        self.old_markers()
        self.assertEqual(self.index.refresh_navigation(), 1)
        state = dict(self.archive.conn.execute('SELECT * FROM cameras WHERE id=?', ('CAM01',)).fetchone())
        self.assertFalse(state['enabled'])
        self.assertFalse(state['upload_enabled'])
        self.assertEqual(len(list(self.archive.conn.execute("SELECT * FROM channel_index_jobs WHERE status='pending'"))), 1)

    def test_disabled_feature_or_channel_does_not_schedule_pin_refresh(self):
        self.record()
        self.run_job()
        self.old_markers()
        self.settings.channel_index_enabled = False
        self.assertEqual(self.index.refresh_navigation(), 0)
        self.settings.channel_index_enabled = True
        self.settings.multi_channel_routing = False
        self.assertEqual(self.index.refresh_navigation(), 0)
        self.settings.multi_channel_routing = True
        self.archive.update_camera('CAM01', {'channel_enabled': False})
        self.assertEqual(self.index.refresh_navigation(), 0)
        self.assertEqual(len(list(self.archive.conn.execute("SELECT * FROM channel_index_jobs WHERE status='pending'"))), 0)

    def test_refresh_skips_root_without_ready_day_and_obsolete_binding(self):
        self.ready('root', 'root')
        self.old_markers()
        self.assertEqual(self.index.refresh_navigation(), 0)
        self.ready('day', '2026-10-10')
        with self.archive.conn:
            self.archive.conn.execute('UPDATE cameras SET channel_chat_id=? WHERE id=?', (-1009999999999, 'CAM01'))
        self.old_markers()
        self.assertEqual(self.index.refresh_navigation(), 0)
        self.assertEqual(self.archive.conn.execute('SELECT COUNT(*) FROM channel_index_jobs').fetchone()[0], 0)
        self.assertEqual(self.telegram.calls, [])

    def test_refresh_same_date_after_worker_recreation_does_not_requeue(self):
        self.record()
        self.run_job()
        self.old_markers()
        self.assertEqual(self.index.refresh_navigation(), 1)
        self.index.process_one()
        rebuilt = fixtures.ChannelIndex(self.archive, self.telegram)
        self.assertEqual(rebuilt.refresh_navigation(), 0)
        self.assertIsNone(rebuilt.process_one())

    def test_refresh_scan_is_throttled_to_once_per_minute(self):
        self.record()
        self.run_job()
        self.old_markers()
        with patch.object(self.index, '_enqueue', wraps=self.index._enqueue) as enqueue:
            self.assertEqual(self.index.refresh_navigation(), 1)
            first_scan_calls=enqueue.call_count
            self.assertGreaterEqual(first_scan_calls,1)
            self.now += 1
            with self.archive.conn:
                self.archive.conn.execute("UPDATE channel_index_runtime SET value=1 WHERE name='pin_navigation_version'")
            self.assertEqual(self.index.refresh_navigation(), 0)
            self.assertEqual(enqueue.call_count,first_scan_calls)
            self.now += 60
            self.assertEqual(self.index.refresh_navigation(), 1)
            self.assertGreater(enqueue.call_count,first_scan_calls)
        self.assertEqual(self.markers()['pin_navigation_version'], 4)


if __name__ == '__main__':
    unittest.main()
