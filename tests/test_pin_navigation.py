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

    HEADINGS = ('📚 XEM THEO NĂM', '📆 XEM THEO THÁNG', '🗓 XEM THEO NGÀY', '📅 HÔM NAY')

    def at(self, stamp):
        self.now = datetime.fromisoformat(stamp).timestamp()

    def section(self, text, heading):
        start = text.index(heading)
        end = min((text.index(other, start + len(heading)) for other in self.HEADINGS
                   if other != heading and other in text[start + len(heading):]), default=len(text))
        for footer in ('🎬 Video mới nhất', '🤖 Bot quản lý'):
            if footer in text[start:end]:
                end = min(end, text.rfind('\n', start, text.index(footer, start)) + 1)
        return text[start:end]

    def ready(self, kind, period, *, camera='CAM01', channel=None, message=None):
        channel = self.channel if channel is None else channel
        if message is None:
            message = 2000 + self.archive.conn.execute('SELECT COUNT(*) FROM channel_index_messages').fetchone()[0]
        with self.archive.conn:
            self.archive.conn.execute('''INSERT INTO channel_index_messages
                (camera_id,channel_chat_id,index_type,period_key,tg_message_id,state,updated_at)
                VALUES(?,?,?,?,?,'ready',?)''', (camera, channel, kind, period, message, self.now))
        return fixtures.channel_message_url(channel, message)

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
        for kind, period in (('root', 'root'), ('year', '2026'), ('month', '2026-10'), ('day', '2026-10-10')):
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
        for kind, period in (('root', 'root'), ('year', '2026'), ('month', '2026-10'), ('day', '2026-10-10')):
            value = self.text(kind, period)
            self.assertIn('&lt;PN &amp; Nhà&gt;', value)
            self.assertNotIn('CAM01', value)
        self.assertEqual(before, [(r['id'], r['tg_message_id']) for r in self.archive.conn.execute('SELECT * FROM channel_index_messages')])

    def test_root_has_year_month_day_and_today_sections(self):
        self.at('2026-10-10T12:00:00+07:00')
        self.record()
        self.run_job()
        root = self.text('root', 'root')
        for heading in self.HEADINGS:
            self.assertIn(heading, root)
        for kind, period, heading, label in (
                ('year', '2026', self.HEADINGS[0], '2026'),
                ('month', '2026-10', self.HEADINGS[1], '10/2026'),
                ('day', '2026-10-10', self.HEADINGS[2], '10/10/2026')):
            section = self.section(root, heading)
            self.assertIn(self.node(kind, period)['tg_message_url'], section)
            self.assertIn(label, section)
        self.assertIn(self.node('day', '2026-10-10')['tg_message_url'], self.section(root, self.HEADINGS[3]))

    def test_month_and_day_shortcuts_use_full_unambiguous_date_labels(self):
        for stamp in ('2025-10-10T08:00:00+07:00', '2026-10-10T08:00:00+07:00'):
            self.record(stamp)
            self.run_job(day=stamp[:10])
        root = self.text('root', 'root')
        months = self.section(root, self.HEADINGS[1])
        days = self.section(root, self.HEADINGS[2])
        for year in (2025, 2026):
            self.assertIn(f'10/{year}', months)
            self.assertIn(f'10/10/{year}', days)
            self.assertIn(self.node('month', f'{year}-10')['tg_message_url'], months)
            self.assertIn(self.node('day', f'{year}-10-10')['tg_message_url'], days)

    def test_today_uses_configured_timezone_not_utc_date(self):
        self.at('2026-10-10T18:00:00+00:00')  # 11 October at 01:00 camera-local time.
        self.record('2026-10-11T00:30:00+07:00')
        self.run_job(day='2026-10-11')
        today = self.section(self.text('root', 'root'), self.HEADINGS[3])
        self.assertIn('11/10/2026', today)
        self.assertIn(self.node('day', '2026-10-11')['tg_message_url'], today)
        self.assertNotIn('10/10/2026', today)

    def test_today_empty_does_not_relabel_latest_old_day_as_today(self):
        self.at('2026-10-11T12:00:00+07:00')
        self.record()
        self.run_job()
        today = self.section(self.text('root', 'root'), self.HEADINGS[3])
        self.assertIn('11/10/2026', today)
        self.assertIn('Chưa có video', today)
        # Restrict the check to the Today line: root may separately link latest video.
        today_line = next(line for line in today.splitlines() if 'HÔM NAY' in line)
        self.assertNotIn(self.node('day', '2026-10-10')['tg_message_url'], today_line)
        self.assertIsNone(self.node('day', '2026-10-11'))

    def test_today_requires_ready_day_node_even_if_record_exists(self):
        self.at('2026-10-10T12:00:00+07:00')
        self.record()
        self.ready('year', '2026')
        self.ready('month', '2026-10')
        text = self.index._render('CAM01', self.channel, 'root', 'root')
        today_line = next(line for line in text.splitlines() if 'HÔM NAY' in line)
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
        self.assertIn('Chưa có video', self.section(root, self.HEADINGS[3]))
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
        self.assertNotIn('2025', root)
        for kind, period in (('year', '2025'), ('month', '2025-01'), ('day', '2025-01-01')):
            self.assertNotIn(fixtures.channel_message_url(self.channel, self.node(kind, period)['tg_message_id']), root)

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
        self.assertNotIn('2025', root)
        self.assertNotIn(other, root)

    def test_root_is_bounded_with_emoji_escaped_name_and_large_date_history(self):
        self.archive.update_camera('CAM01', {'name': '🟢&' * 35})
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
        for heading, bound in zip(self.HEADINGS[:3], (12, 12, 7)):
            self.assertEqual(len(re.findall(r'<a href=', self.section(root, heading))), bound)
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
        for heading, bound in zip(self.HEADINGS[:3], (12, 12, 7)):
            self.assertEqual(len(re.findall(r'<a href=', self.section(root, heading))), bound)
        self.assertIn('href=', self.section(root, self.HEADINGS[3]))

    def test_root_shortcuts_show_most_recent_months_and_days_first(self):
        for stamp in ('2026-09-30', '2026-10-01', '2026-10-02'):
            self.record(stamp + 'T08:00:00+07:00')
            self.run_job(day=stamp)
        root = self.text('root', 'root')
        months = self.section(root, self.HEADINGS[1])
        days = self.section(root, self.HEADINGS[2])
        self.assertLess(months.index('10/2026'), months.index('09/2026'))
        self.assertLess(days.index('02/10/2026'), days.index('01/10/2026'))
        self.assertLess(days.index('01/10/2026'), days.index('30/09/2026'))

    def test_only_root_is_pinned_and_navigation_never_sends_media(self):
        self.record()
        self.run_job()
        self.assertEqual(self.telegram.pins, [(self.channel, self.node('root', 'root')['tg_message_id'])])
        self.assertEqual({method for method, _ in self.telegram.calls}, {'sendMessage', 'editMessageText', 'pinChatMessage'})
        for fields in self.sends():
            self.assertTrue(fields['disable_notification'])
            self.assertEqual(fields['parse_mode'], 'HTML')

    def test_empty_startup_records_navigation_marker_but_creates_no_job_or_node(self):
        self.at('2026-10-10T12:00:00+07:00')
        self.assertEqual(self.index.refresh_navigation(), 0)
        self.assertEqual(self.markers()['pin_navigation_version'], 2)
        self.assertEqual(self.markers()['pin_today_ordinal'], datetime(2026, 10, 10).date().toordinal())
        self.assertEqual(self.archive.conn.execute('SELECT COUNT(*) FROM channel_index_jobs').fetchone()[0], 0)
        self.assertEqual(self.archive.conn.execute('SELECT COUNT(*) FROM channel_index_messages').fetchone()[0], 0)
        self.assertEqual(self.telegram.calls, [])

    def test_upgrade_refresh_enqueues_one_existing_day_once_without_api_calls(self):
        self.record()
        self.run_job()
        self.record('2026-10-11T08:00:00+07:00')
        self.run_job(day='2026-10-11')
        self.old_markers()
        before = len(self.telegram.calls)
        self.assertEqual(self.index.refresh_navigation(), 1)
        pending = list(self.archive.conn.execute("SELECT * FROM channel_index_jobs WHERE status='pending'"))
        self.assertEqual(len(pending), 1)
        self.assertIn(pending[0]['recorded_date'], ('2026-10-10', '2026-10-11'))
        self.assertEqual(len(self.telegram.calls), before)
        self.assertEqual(self.index.refresh_navigation(), 0)
        self.now += 61
        self.assertEqual(self.index.refresh_navigation(), 0)
        self.assertEqual(len(list(self.archive.conn.execute("SELECT * FROM channel_index_jobs WHERE status='pending'"))), 1)

    def test_refresh_uses_camera_local_midnight_and_clears_stale_today_without_upload(self):
        self.at('2026-10-10T23:57:00+07:00')
        self.record('2026-10-10T08:00:00+07:00')
        self.run_job()
        root_id = self.node('root', 'root')['tg_message_id']
        old_day = self.node('day', '2026-10-10')['tg_message_url']
        self.assertIn(old_day, self.section(self.text('root', 'root'), self.HEADINGS[3]))
        self.at('2026-10-11T00:01:00+07:00')
        self.assertEqual(self.index.process_one(), 'updated')
        root = self.text('root', 'root')
        today_line = next(line for line in root.splitlines() if 'HÔM NAY' in line)
        self.assertIn('11/10/2026', today_line)
        self.assertNotIn(old_day, today_line)
        self.assertIn('Chưa có video', self.section(root, self.HEADINGS[3]))
        self.assertIsNone(self.node('day', '2026-10-11'))
        self.assertEqual(self.node('root', 'root')['tg_message_id'], root_id)
        self.assertEqual(len(self.sends()), 4)
        self.assertTrue(all(method in ('sendMessage', 'editMessageText', 'pinChatMessage') for method, _ in self.telegram.calls))

    def test_process_one_refreshes_existing_pin_on_first_upgraded_boot(self):
        self.record()
        self.run_job()
        root_id = self.node('root', 'root')['tg_message_id']
        self.old_markers()
        self.assertEqual(self.index.process_one(), 'updated')
        self.assertEqual(self.markers()['pin_navigation_version'], 2)
        self.assertEqual(self.node('root', 'root')['tg_message_id'], root_id)
        self.assertEqual(len(self.sends()), 4)
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
            self.now += 1
            with self.archive.conn:
                self.archive.conn.execute("UPDATE channel_index_runtime SET value=1 WHERE name='pin_navigation_version'")
            self.assertEqual(self.index.refresh_navigation(), 0)
            self.assertEqual(enqueue.call_count, 1)
            self.now += 60
            self.assertEqual(self.index.refresh_navigation(), 1)
            self.assertEqual(enqueue.call_count, 2)
        self.assertEqual(self.markers()['pin_navigation_version'], 2)


if __name__ == '__main__':
    unittest.main()
