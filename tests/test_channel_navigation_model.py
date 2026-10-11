"""Acceptance for the recording-time channel navigation model, without Telegram I/O.

Fixture helpers are borrowed, not inherited: unittest does not repeat the older
ChannelIndexTests suite when it collects this module.
"""
import html
import re
import unittest
from datetime import datetime
from html.parser import HTMLParser
from unittest.mock import patch

from archive_app.channel_index import ChannelIndex, channel_message_url
from tests import test_channel_index as _fixtures


class _Links(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.links, self.label, self.url = [], [], None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.url, self.label = dict(attrs).get('href'), []

    def handle_data(self, data):
        if self.url is not None:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.url is not None:
            self.links.append((''.join(self.label), self.url))
            self.url = None


class ChannelNavigationModelTests(unittest.TestCase):
    setUp = _fixtures.ChannelIndexTests.setUp
    tearDown = _fixtures.ChannelIndexTests.tearDown
    camera = _fixtures.ChannelIndexTests.camera
    record = _fixtures.ChannelIndexTests.record
    node = _fixtures.ChannelIndexTests.node
    text = _fixtures.ChannelIndexTests.text
    sends = _fixtures.ChannelIndexTests.sends

    def clock_day(self, stamp='2026-10-10T12:00:00+07:00'):
        self.now = datetime.fromisoformat(stamp).timestamp()

    def refresh(self, day='2026-10-10', *, camera='CAM01'):
        self.assertTrue(self.index.enqueue(camera, day))
        self.now += 61
        result = self.index.process_one()
        self.assertIn(result, ('updated', 'retry'))
        for _ in range(100):
            result = self.index.process_one()
            if result is None:
                break
            self.assertIn(result, ('updated', 'retry'))
        else:
            self.fail('Index work did not settle in 100 fixture jobs')

    def links(self, kind, period):
        return _Links(self.text(kind, period)).links

    def url(self, mid):
        return channel_message_url(self.channel, mid)

    def anchor_url(self, kind, period, label):
        return next(url for text, url in self.links(kind, period) if label in text)

    def clip_urls(self, kind, period, message_ids):
        wanted = {self.url(mid) for mid in message_ids}
        return [url for _, url in self.links(kind, period) if url in wanted]

    def legacy_schema(self):
        """Replace only the fixture constraint, retaining evidence and IDs."""
        sql = self.archive.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='channel_index_messages'").fetchone()[0]
        sql = re.sub(r"CHECK\s*\(\s*index_type\s+IN\s*\([^)]*\)\s*\)",
                     "CHECK(index_type IN ('root','year','month','day'))", sql, flags=re.I)
        with self.archive.conn:
            self.archive.conn.execute("DELETE FROM channel_index_messages WHERE index_type IN ('years','hour')")
            rows = self.archive.conn.execute('SELECT * FROM channel_index_messages ORDER BY id').fetchall()
            columns = [row[1] for row in self.archive.conn.execute('PRAGMA table_info(channel_index_messages)')]
            self.archive.conn.execute('ALTER TABLE channel_index_messages RENAME TO legacy_nodes')
            self.archive.conn.execute(sql)
            self.archive.conn.execute(f'INSERT INTO channel_index_messages({",".join(columns)}) SELECT {",".join(columns)} FROM legacy_nodes')
            self.archive.conn.execute('DROP TABLE legacy_nodes')
        return [dict(row) for row in rows]

    def test_initial_tree_has_one_short_root_pin_and_five_navigation_entries(self):
        self.clock_day()
        self.record()
        self.refresh()
        root = self.node('root', 'root')
        text = self.text('root', 'root')
        labels = [label for label, _ in self.links('root', 'root')]
        self.assertEqual(len(labels), 5)
        for label in ('Video mới nhất', 'Hôm nay', 'Tháng hiện tại',
                      'Kho lưu trữ theo năm', 'Bot quản lý'):
            self.assertTrue(any(label in value for value in labels), label)
        self.assertNotIn('CAM01', text)
        self.assertNotIn('Tổng:', text)
        self.assertNotIn('2026-10-10', text)
        self.assertEqual(self.telegram.pins, [(self.channel, root['tg_message_id'])])
        for kind, period in (('years', 'years'), ('year', '2026'),
                             ('month', '2026-10'), ('day', '2026-10-10')):
            self.assertEqual(self.node(kind, period)['state'], 'ready')
        self.assertIsNone(self.node('hour', '2026-10-10T08'))

    def test_root_does_not_grow_across_month_and_year_rollover(self):
        self.clock_day()
        self.record()
        self.refresh()
        root_id = self.node('root', 'root')['tg_message_id']
        for stamp in ('2026-11-01T08:00:00+07:00', '2027-01-01T08:00:00+07:00'):
            self.clock_day(stamp)
            self.record(stamp, message=90 if '2026-11' in stamp else 91)
            self.refresh(stamp[:10])
            self.assertEqual(self.node('root', 'root')['tg_message_id'], root_id)
            self.assertEqual(len(self.links('root', 'root')), 5)
        self.assertEqual(self.telegram.pins, [(self.channel, root_id)])
        self.assertEqual(self.anchor_url('root', 'root', 'Video mới nhất'), self.url(91))
        self.assertEqual(self.anchor_url('root', 'root', 'Tháng hiện tại'),
                         self.node('month', '2027-01')['tg_message_url'])

    def test_today_falls_back_to_latest_recorded_day_not_latest_upload(self):
        self.clock_day('2026-10-12T12:00:00+07:00')
        self.record('2026-10-10T08:00:00+07:00', message=90)
        self.refresh()
        self.record('2025-03-04T14:00:00+07:00', message=91)
        self.refresh('2025-03-04')
        self.assertEqual(self.anchor_url('root', 'root', 'Video mới nhất'), self.url(90))
        self.assertEqual(self.anchor_url('root', 'root', 'Ngày gần nhất'),
                         self.node('day', '2026-10-10')['tg_message_url'])
        self.assertNotIn('2025-03-04', self.text('root', 'root'))

    def test_empty_current_month_has_no_misleading_current_month_link(self):
        self.clock_day('2026-11-03T12:00:00+07:00')
        self.record()
        self.refresh()
        self.assertFalse(any('Tháng hiện tại' in label for label, _ in self.links('root', 'root')))
        self.assertIn('Tháng hiện tại', html.unescape(self.text('root', 'root')))
        self.assertIsNone(self.node('month', '2026-11'))

    def test_year_links_only_nonempty_months_and_returns_to_year_list(self):
        for stamp, message in (('2026-01-02T08:00:00+07:00', 77),
                               ('2026-10-10T08:00:00+07:00', 78)):
            self.record(stamp, message=message)
            self.refresh(stamp[:10])
        urls = [url for _, url in self.links('year', '2026')]
        for month in ('2026-01', '2026-10'):
            self.assertIn(self.node('month', month)['tg_message_url'], urls)
        self.assertIsNone(self.node('month', '2026-02'))
        self.assertIn(self.node('years', 'years')['tg_message_url'], urls)
        self.assertIn(self.node('root', 'root')['tg_message_url'], urls)
        self.assertNotIn(self.url(77), urls)

    def test_month_calendar_handles_leap_day_and_skips_empty_adjacent_months(self):
        for stamp, mid in (('2024-02-29T08:00:00+07:00', 77),
                           ('2024-07-01T08:00:00+07:00', 78)):
            self.record(stamp, message=mid)
            self.refresh(stamp[:10])
        feb = self.links('month', '2024-02')
        july = self.links('month', '2024-07')
        self.assertIn(self.node('day', '2024-02-29')['tg_message_url'], [u for _, u in feb])
        self.assertIn(self.node('month', '2024-07')['tg_message_url'], [u for _, u in feb])
        self.assertIn(self.node('month', '2024-02')['tg_message_url'], [u for _, u in july])
        self.assertIsNone(self.node('day', '2024-02-30'))
        self.assertIsNone(self.node('month', '2024-03'))
        self.assertNotIn(self.url(77), [u for _, u in feb])
        self.assertLessEqual(len(feb), 35)

    def test_day_exposes_all_24_hours_without_dumping_all_clips(self):
        for hour in range(24):
            for minute in (0, 5):
                self.record(f'2026-10-10T{hour:02d}:{minute:02d}:00+07:00',
                            message=100 + hour * 2 + minute // 5)
        self.refresh()
        urls = [u for _, u in self.links('day', '2026-10-10')]
        for hour in range(24):
            self.assertIn(self.url(100 + hour * 2), urls)
            self.assertNotIn(self.url(101 + hour * 2), urls)
            self.assertIsNone(self.node('hour', f'2026-10-10T{hour:02d}'))
        self.assertLessEqual(len(urls), 28)

    def test_sparse_day_links_only_recorded_hours_and_marks_missing_hours(self):
        self.record(message=100)
        self.record('2026-10-10T14:15:00+07:00', message=200)
        self.refresh()
        text = html.unescape(self.text('day', '2026-10-10'))
        for hour in range(24):
            self.assertRegex(text, rf'(?<!\d){hour:02d}(?::00)?(?!\d)')
        self.assertEqual(self.clip_urls('day', '2026-10-10', (100, 200)),
                         [self.url(100), self.url(200)])
        self.assertNotIn('lỗi camera', text.lower())

    def test_adjacent_days_skip_empty_dates_and_repair_old_day_after_backfill(self):
        self.record('2026-10-01T08:00:00+07:00', message=77)
        self.refresh('2026-10-01')
        self.record('2026-10-10T08:00:00+07:00', message=78)
        self.refresh()
        first_urls = [url for _, url in self.links('day', '2026-10-01')]
        last_urls = [url for _, url in self.links('day', '2026-10-10')]
        self.assertIn(self.node('day', '2026-10-10')['tg_message_url'], first_urls)
        self.assertIn(self.node('day', '2026-10-01')['tg_message_url'], last_urls)
        self.assertIsNone(self.node('day', '2026-10-02'))

    def test_late_upload_uses_hour_list_in_recording_order_not_message_order(self):
        for minute, mid in ((0, 100), (5, 102), (10, 101)):
            self.record(f'2026-10-10T14:{minute:02d}:00+07:00', message=mid)
        self.refresh()
        hour = self.node('hour', '2026-10-10T14')
        self.assertEqual(hour['state'], 'ready')
        self.assertIn(hour['tg_message_url'], [u for _, u in self.links('day', '2026-10-10')])
        self.assertEqual(self.clip_urls('hour', '2026-10-10T14', (100, 101, 102)),
                         [self.url(100), self.url(102), self.url(101)])
        self.assertIn(self.node('day', '2026-10-10')['tg_message_url'],
                      [u for _, u in self.links('hour', '2026-10-10T14')])
        self.assertEqual({method for method, _ in self.telegram.calls},
                         {'sendMessage', 'editMessageText', 'pinChatMessage'})

    def test_nonconsecutive_channel_messages_require_exact_hour_list(self):
        self.record('2026-10-10T14:00:00+07:00', message=100)
        self.record('2026-10-10T14:05:00+07:00', message=104)
        self.refresh()
        self.assertIsNotNone(self.node('hour', '2026-10-10T14'))
        self.assertEqual(self.clip_urls('hour', '2026-10-10T14', (100, 104)),
                         [self.url(100), self.url(104)])

    def test_backfilled_hour_paginates_all_clips_without_loss_or_duplicates(self):
        messages, keys = [], []
        for minute in range(50):
            mid = 200 + (49 - minute) * 2
            messages.append(mid)
            keys.append(self.record(f'2026-10-10T14:{minute:02d}:00+07:00', message=mid))
        self.refresh()
        periods = ['2026-10-10T14', '2026-10-10T14:2', '2026-10-10T14:3']
        all_urls = []
        for period in periods:
            text = self.text('hour', period)
            self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 4096)
            chunk = self.clip_urls('hour', period, messages)
            self.assertLessEqual(len(chunk), 20)
            all_urls.extend(chunk)
        self.assertEqual(all_urls, [self.url(mid) for mid in messages])
        self.assertEqual(len(set(all_urls)), 50)
        self.assertIn(self.node('hour', periods[1])['tg_message_url'],
                      [u for _, u in self.links('hour', periods[0])])
        self.assertIn(self.node('hour', periods[0])['tg_message_url'],
                      [u for _, u in self.links('hour', periods[1])])
        old_ids = [self.node('hour', period)['tg_message_id'] for period in periods]
        for key in keys[5:]:
            self.assertTrue(self.archive.soft_delete(key, 23))
        self.refresh()
        deleted_urls = {self.url(mid) for mid in messages[5:]}
        for period in periods:
            self.assertTrue(deleted_urls.isdisjoint({u for _, u in self.links('hour', period)}))
        self.assertEqual(old_ids, [self.node('hour', period)['tg_message_id'] for period in periods])
        self.assertNotIn(self.node('hour', periods[1])['tg_message_url'],
                         [u for _, u in self.links('hour', periods[0])])

    def test_duplicate_channel_reference_has_only_one_hour_entry(self):
        self.record('2026-10-10T14:00:00+07:00', message=100)
        self.record('2026-10-10T14:00:00+07:00', message=100)
        self.record('2026-10-10T14:05:00+07:00', message=104)
        self.refresh()
        self.assertEqual(self.clip_urls('hour', '2026-10-10T14', (100, 104)),
                         [self.url(100), self.url(104)])

    def test_overlapping_same_time_clips_have_distinguishable_entries(self):
        for mid in (100, 104):
            self.record('2026-10-10T14:00:00+07:00',
                        end='2026-10-10T14:05:00+07:00', message=mid)
        self.refresh()
        links = [(label, url) for label, url in self.links('hour', '2026-10-10T14')
                 if url in {self.url(100), self.url(104)}]
        self.assertEqual(len(links), 2)
        self.assertEqual(len({label for label, _ in links}), 2)
        for label, _ in links:
            self.assertIn('14:00:00', label)
            self.assertIn('14:05:00', label)

    def test_midnight_recording_belongs_only_to_start_day_and_start_hour(self):
        key = self.record('2026-10-10T23:58:00+07:00',
                          end='2026-10-11T00:03:00+07:00', message=100)
        self.assertTrue(self.index.enqueue_recording(key))
        self.refresh()
        self.assertIsNone(self.node('day', '2026-10-11'))
        self.assertIn(self.url(100), [u for _, u in self.links('day', '2026-10-10')])
        self.assertIsNone(self.node('hour', '2026-10-11T00'))
        self.assertEqual(self.index.rebuild('CAM01', '2026-10-11')['recordings'], 0)

    def test_year_list_paginates_all_history_while_root_remains_constant(self):
        self.clock_day()
        keys = {}
        for year in range(2000, 2021):
            stamp = f'{year}-01-01T00:00:00+07:00'
            keys[year] = self.record(stamp, message=year)
            self.refresh(stamp[:10])
        year_urls = {self.node('year', str(year))['tg_message_url'] for year in range(2000, 2021)}
        listed = []
        for period in ('years', 'years:2'):
            listed.extend(url for _, url in self.links('years', period) if url in year_urls)
            self.assertLessEqual(len(self.text('years', period).encode('utf-16-le')) // 2, 4096)
        self.assertEqual(set(listed), year_urls)
        self.assertEqual(len(listed), 21)
        self.assertEqual(len(self.links('root', 'root')), 4)  # Current month is empty.
        self.assertEqual(len(self.telegram.pins), 1)
        self.assertEqual(self.anchor_url('root', 'root', 'Kho lưu trữ theo năm'),
                         self.node('years', 'years')['tg_message_url'])
        old_page_id = self.node('years', 'years:2')['tg_message_id']
        self.assertTrue(self.archive.soft_delete(keys[2000], 23))
        self.refresh('2000-01-01')
        removed_year_url = self.node('year', '2000')['tg_message_url']
        self.assertNotIn(removed_year_url, [u for _, u in self.links('years', 'years:2')])
        self.assertNotIn(self.url(old_page_id), [u for _, u in self.links('years', 'years')])
        self.assertEqual(self.node('years', 'years:2')['tg_message_id'], old_page_id)

    def test_deleted_video_removes_hour_target_and_restore_reuses_known_nodes(self):
        first = self.record('2026-10-10T14:00:00+07:00', message=100)
        second = self.record('2026-10-10T14:05:00+07:00', message=104)
        self.refresh()
        root_id = self.node('root', 'root')['tg_message_id']
        hour_id = self.node('hour', '2026-10-10T14')['tg_message_id']
        self.assertTrue(self.archive.soft_delete(first, 23))
        self.assertTrue(self.archive.soft_delete(second, 23))
        self.refresh()
        day_urls = [url for _, url in self.links('day', '2026-10-10')]
        self.assertNotIn(self.url(100), day_urls)
        self.assertNotIn(self.url(104), day_urls)
        self.assertNotIn(self.url(hour_id), day_urls)
        self.assertNotIn(self.node('day', '2026-10-10')['tg_message_url'],
                         [url for _, url in self.links('month', '2026-10')])
        self.assertTrue(self.archive.restore_recording(first, 23))
        self.assertTrue(self.archive.restore_recording(second, 23))
        self.refresh()
        self.assertEqual(self.node('root', 'root')['tg_message_id'], root_id)
        self.assertEqual(self.node('hour', '2026-10-10T14')['tg_message_id'], hour_id)
        self.assertEqual(self.clip_urls('hour', '2026-10-10T14', (100, 104)),
                         [self.url(100), self.url(104)])

    def test_all_invalid_or_deleted_media_creates_no_navigation_nodes(self):
        for fields in ({'deleted': self.now}, {'status': 'failed'}, {'bot': 99},
                       {'kind': 'owner_private'}, {'message': 0},
                       {'channel': -1009999999999}):
            self.record(**fields)
        self.refresh()
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.archive.conn.execute('SELECT COUNT(*) FROM channel_index_messages').fetchone()[0], 0)

    def test_rename_escapes_html_with_stable_node_ids_and_bounded_text(self):
        self.record('2026-10-10T14:00:00+07:00', message=104)
        self.record('2026-10-10T14:05:00+07:00', message=100)
        self.refresh()
        ids = [tuple(row) for row in self.archive.conn.execute(
            'SELECT index_type,period_key,tg_message_id FROM channel_index_messages ORDER BY id')]
        self.archive.update_camera('CAM01', {'name': '🟢 <Cổng & nhà> ' * 5})
        self.refresh()
        for row in self.archive.conn.execute('SELECT * FROM channel_index_messages WHERE state="ready"'):
            text = self.text(row['index_type'], row['period_key'])
            self.assertIn('&lt;Cổng &amp; nhà&gt;', text)
            self.assertNotIn('<Cổng', text)
            self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 4096)
        self.assertEqual(ids, [tuple(row) for row in self.archive.conn.execute(
            'SELECT index_type,period_key,tg_message_id FROM channel_index_messages ORDER BY id')])
        self.assertEqual(len(self.telegram.pins), 1)

    def test_unknown_hour_send_is_not_retried_by_rebuild(self):
        self.record('2026-10-10T14:00:00+07:00', message=104)
        self.record('2026-10-10T14:05:00+07:00', message=100)
        real_request = self.telegram.request

        def lose_hour_ack(method, fields):
            sending_hour = self.archive.conn.execute(
                "SELECT 1 FROM channel_index_messages WHERE index_type='hour' AND state='sending'").fetchone()
            if method == 'sendMessage' and sending_hour:
                real_request(method, fields)
                raise TimeoutError('synthetic lost hour confirmation')
            return real_request(method, fields)

        with patch.object(self.telegram, 'request', side_effect=lose_hour_ack):
            self.index.enqueue('CAM01', '2026-10-10')
            self.now += 61
            self.assertEqual(self.index.process_one(), 'needs_reconcile')
        hour = self.node('hour', '2026-10-10T14')
        self.assertEqual(hour['state'], 'needs_reconcile')
        sends = len(self.sends())
        self.index.rebuild('CAM01', dry_run=False)
        self.now += 86400
        self.assertIsNone(self.index.process_one())
        self.assertEqual(len(self.sends()), sends)
        accepted = self.telegram.next_id
        self.index.reconcile('CAM01', self.channel, 'hour', '2026-10-10T14', accepted)
        self.assertEqual(self.index.process_one(), 'updated')
        self.assertEqual(self.node('hour', '2026-10-10T14')['tg_message_id'], accepted)
        self.assertEqual(len(self.sends()), sends)

    def test_legacy_four_kind_table_migration_preserves_all_durable_evidence(self):
        self.record()
        self.refresh()
        # Replace only the synthetic table constraint, modelling the pre-upgrade
        # DB exactly. Media and job tables are not changed or reconstructed.
        with self.archive.conn:
            self.archive.conn.execute("UPDATE channel_index_messages SET revision=7,attempt_id='known-attempt',last_error='test-marker' WHERE index_type='root'")
        before = self.legacy_schema()
        self.index = ChannelIndex(self.archive, self.telegram)
        after = [dict(row) for row in self.archive.conn.execute('SELECT * FROM channel_index_messages ORDER BY id')]
        self.assertEqual(before, after)
        self.refresh()
        self.assertIsNotNone(self.node('years', 'years'))
        old_root = next(row for row in before if row['index_type'] == 'root')
        self.assertEqual(self.node('root', 'root')['tg_message_id'], old_root['tg_message_id'])
        self.record('2026-10-10T14:00:00+07:00', message=104)
        self.record('2026-10-10T14:05:00+07:00', message=100)
        self.refresh()
        self.assertEqual(self.node('hour', '2026-10-10T14')['state'], 'ready')

    def test_legacy_migration_keeps_unknown_send_quarantined(self):
        self.record()
        self.refresh()
        with self.archive.conn:
            self.archive.conn.execute("""UPDATE channel_index_messages SET state='needs_reconcile',
                tg_message_id=NULL,tg_message_url=NULL,attempt_id='lost-confirmation',
                last_error='send_outcome_unknown' WHERE index_type='day'""")
            self.archive.conn.execute("UPDATE channel_index_jobs SET status='needs_reconcile'")
        before = self.legacy_schema()
        self.index = ChannelIndex(self.archive, self.telegram)
        after = [dict(row) for row in self.archive.conn.execute('SELECT * FROM channel_index_messages ORDER BY id')]
        self.assertEqual(before, after)
        sends = len(self.sends())
        self.assertEqual(self.index.rebuild('CAM01', dry_run=False)['needs_reconcile'], 1)
        self.now += 86400
        self.assertIsNone(self.index.process_one())
        self.assertEqual(len(self.sends()), sends)
        self.assertEqual(self.node('day', '2026-10-10')['attempt_id'], 'lost-confirmation')
        self.assertEqual(self.node('day', '2026-10-10')['state'], 'needs_reconcile')


if __name__ == '__main__':
    unittest.main()
