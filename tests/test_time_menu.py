"""Time shortcuts use synthetic SQLite metadata, never a live Telegram bot."""
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest
import uuid
from unittest.mock import patch

from archive_app.core import Archive, Settings
from archive_app.telegram import Telegram
from archive_app.telegram_menu import TimeMenus


def stamp(text):
    return int(datetime.fromisoformat(text).timestamp() * 1000)


class TimeMenuTests(unittest.TestCase):
    NOW = int(datetime.fromisoformat('2026-10-03T02:30:00+07:00').timestamp())

    def setUp(self):
        self.fixture_parent = Path(__file__).resolve().parent if os.name == 'nt' else Path(tempfile.gettempdir()).resolve()
        self.root = self.fixture_parent / ('.tmp-time-menu-' + uuid.uuid4().hex)
        self.assertEqual(self.root.resolve().parent, self.fixture_parent)
        self.root.mkdir()
        self.settings = Settings(self.root / 'state', self.root / 'cache', self.root / 'input',
                                 'UTC+07:00', owner_user_id=42, allowed_users=(42, 43))
        self.archive = Archive(self.settings)
        self.telegram = Telegram(self.settings)
        self.menus = TimeMenus(self.telegram)
        self.clock = patch('archive_app.telegram_menu.time.time', return_value=self.NOW)
        self.clock_mock = self.clock.start()
        self.counter = 0

    def tearDown(self):
        self.clock.stop()
        self.archive.close()
        self.assertEqual(self.root.resolve().parent, self.fixture_parent)
        shutil.rmtree(self.root)

    def camera(self, camera_id='Front_Camera', name='Cửa trước'):
        return self.archive.add_camera({'id': camera_id, 'name': name})

    def recording(self, start, end, camera='Front_Camera', status='uploaded'):
        self.counter += 1
        key = hashlib.sha256(f'{camera}:{self.counter}'.encode()).hexdigest()
        with self.archive.conn:
            self.archive.conn.execute('''INSERT INTO recordings
                (key,camera,record_id,start_ms,end_ms,source_path,status,created_at,file_id,media_type)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (key, camera, str(self.counter), stamp(start), stamp(end), 'synthetic-source',
                 status, self.NOW, 'synthetic-file-id', 'video'))
        return key

    @staticmethod
    def callbacks(buttons, prefix=None):
        return [button['callback_data'] for row in buttons for button in row
                if 'callback_data' in button and (prefix is None or button['callback_data'].startswith(prefix))]

    def camera_selection(self, buttons, camera_id='Front_Camera'):
        token = self.telegram.camera_token(camera_id)
        return next(data for data in self.callbacks(buttons, 'wc:') if f':{token}:' in data)

    def test_shortcut_buttons_and_persistent_commands(self):
        self.assertEqual(self.callbacks(self.menus.shortcuts()), ['today', 'yesterday', 'last6h', 'custom-time'])
        self.assertTrue(all(len(row) == 1 for row in self.menus.shortcuts()))
        commands = self.menus.commands()
        self.assertEqual([command['command'] for command in commands],
                         ['start', 'sync', 'today', 'yesterday', 'last6h', 'time', 'archive', 'recent', 'trash', 'status'])
        self.assertEqual([command['description'] for command in commands],
                         ['Start / Menu', 'Start sync', 'Hôm nay', 'Hôm qua', '6 giờ trước', 'Tùy chọn thời gian', 'Kho video',
                          'Video gần đây', 'Thùng rác', 'Trạng thái'])
        for command in commands:
            self.assertRegex(command['command'], r'^[a-z0-9_]{1,32}$')
            self.assertTrue(1 <= len(command['description']) <= 256)
        changed = self.menus.shortcuts()
        changed[0][0]['text'] = 'different'
        self.assertNotEqual(self.menus.shortcuts()[0][0]['text'], 'different')

    def test_today_first_lists_cameras_then_overlap_clips(self):
        self.camera()
        self.camera('Other', 'Nhà sau')
        crossing = self.recording('2026-10-02T23:59:00+07:00', '2026-10-03T00:01:00+07:00')
        exact_start = self.recording('2026-10-03T00:00:00+07:00', '2026-10-03T00:01:00+07:00')
        self.recording('2026-10-02T23:58:00+07:00', '2026-10-03T00:00:00+07:00')
        self.recording('2026-10-04T00:00:00+07:00', '2026-10-04T00:01:00+07:00')
        self.recording('2026-10-03T01:00:00+07:00', '2026-10-03T01:01:00+07:00', status='downloaded')
        text, cameras = self.menus.menu(self.archive, 'today')
        self.assertEqual(text, 'Hôm nay · Camera · trang 1')
        self.assertEqual(self.menus._window('t', self.NOW)[:2],
                         (stamp('2026-10-03T00:00:00+07:00'), stamp('2026-10-04T00:00:00+07:00')))
        self.assertEqual(self.callbacks(cameras, 'v:'), [])
        self.assertEqual(len(self.callbacks(cameras, 'wc:')), 1)
        self.assertIn('Cửa trước (2 video)', cameras[0][0]['text'])
        title, clips = self.menus.menu(self.archive, self.camera_selection(cameras))
        self.assertEqual(title.splitlines()[0], 'Cửa trước · Hôm nay · 2 video · trang 1')
        self.assertIn('1. 02/10 23:59:00 → 03/10 00:01:00', title)
        self.assertNotIn(self.settings.timezone, title)
        self.assertEqual(self.callbacks(clips, 'v:'), ['v:' + crossing[:32], 'v:' + exact_start[:32]])
        self.assertEqual(self.callbacks(clips, 'f:'), ['f:' + crossing[:32], 'f:' + exact_start[:32]])
        self.assertEqual(self.callbacks(clips, 'x:'), ['x:' + crossing[:32], 'x:' + exact_start[:32]])

    def test_yesterday_uses_display_timezone_local_day(self):
        self.camera()
        included = self.recording('2026-10-02T23:59:00+07:00', '2026-10-03T00:01:00+07:00')
        self.recording('2026-10-03T00:01:00+07:00', '2026-10-03T00:02:00+07:00')
        text, cameras = self.menus.menu(self.archive, 'yesterday')
        self.assertEqual(text, 'Hôm qua · Camera · trang 1')
        self.assertEqual(self.menus._window('y', self.NOW)[:2],
                         (stamp('2026-10-02T00:00:00+07:00'), stamp('2026-10-03T00:00:00+07:00')))
        _, clips = self.menus.menu(self.archive, self.camera_selection(cameras))
        self.assertEqual(self.callbacks(clips, 'v:'), ['v:' + included[:32]])

    def test_last_six_hours_cross_midnight_and_exclusive_bounds(self):
        self.camera()
        crossing_start = self.recording('2026-10-02T20:29:59+07:00', '2026-10-02T20:30:01+07:00')
        inside = self.recording('2026-10-02T23:59:00+07:00', '2026-10-03T00:01:00+07:00')
        self.recording('2026-10-02T20:20:00+07:00', '2026-10-02T20:30:00+07:00')
        self.recording('2026-10-03T02:30:00+07:00', '2026-10-03T02:31:00+07:00')
        text, cameras = self.menus.menu(self.archive, 'last6h')
        self.assertEqual(text, '6 giờ trước · Camera · trang 1')
        self.assertEqual(self.menus._window('h', self.NOW)[:2],
                         (stamp('2026-10-02T20:30:00+07:00'), stamp('2026-10-03T02:30:00+07:00')))
        _, clips = self.menus.menu(self.archive, self.camera_selection(cameras))
        self.assertEqual(self.callbacks(clips, 'v:'), ['v:' + crossing_start[:32], 'v:' + inside[:32]])

    def test_window_is_frozen_through_midnight_sort_and_back(self):
        self.camera()
        included = self.recording('2026-10-03T01:00:00+07:00', '2026-10-03T01:01:00+07:00')
        self.recording('2026-10-04T01:00:00+07:00', '2026-10-04T01:01:00+07:00')
        _, cameras = self.menus.menu(self.archive, 'today')
        selected = self.camera_selection(cameras)
        self.clock_mock.return_value = self.NOW + 2 * 86400
        text, clips = self.menus.menu(self.archive, selected)
        self.assertEqual(text.splitlines()[0], 'Cửa trước · Hôm nay · 1 video · trang 1')
        self.assertIn('03/10 01:00:00 → 03/10 01:01:00', text)
        self.assertEqual(self.callbacks(clips, 'v:'), ['v:' + included[:32]])
        sort = next(button['callback_data'] for row in clips for button in row if button['text'] == 'Mới → cũ')
        _, descending = self.menus.menu(self.archive, sort)
        self.assertEqual(self.callbacks(descending, 'v:'), ['v:' + included[:32]])
        back = self.callbacks(descending, 'w:')[0]
        self.assertIn(f':{self.NOW}:', back)
        _, back_cameras = self.menus.menu(self.archive, back)
        self.assertEqual(self.camera_selection(back_cameras).split(':')[2], str(self.NOW))

    def test_video_pagination_and_sort_preserve_window(self):
        self.camera()
        keys = [self.recording(f'2026-10-03T01:{minute:02}:00+07:00', f'2026-10-03T01:{minute:02}:30+07:00')
                for minute in range(12)]
        _, cameras = self.menus.menu(self.archive, 'today')
        _, ascending = self.menus.menu(self.archive, self.camera_selection(cameras))
        self.assertEqual(self.callbacks(ascending, 'v:'), ['v:' + key[:32] for key in keys[:10]])
        sort = next(button['callback_data'] for row in ascending for button in row if button['text'] == 'Mới → cũ')
        text, descending = self.menus.menu(self.archive, sort)
        self.assertEqual(text.splitlines()[0], 'Cửa trước · Hôm nay · 12 video · trang 1')
        self.assertIn('Cũ → mới', [button['text'] for row in descending for button in row])
        self.assertEqual(self.callbacks(descending, 'v:'), ['v:' + key[:32] for key in keys[::-1][:10]])
        next_page = next(button['callback_data'] for row in descending for button in row if button['text'] == 'Video →')
        _, second = self.menus.menu(self.archive, next_page)
        self.assertEqual(self.callbacks(second, 'v:'), ['v:' + key[:32] for key in keys[::-1][10:]])
        previous = next(button['callback_data'] for row in second for button in row if button['text'] == '← Video')
        self.assertEqual(previous, sort)

    def test_camera_pagination_and_clip_back_returns_same_camera_page(self):
        for index in range(12):
            self.camera(f'C{index:02}', f'Camera {index:02}')
            self.recording('2026-10-03T01:00:00+07:00', '2026-10-03T01:01:00+07:00', camera=f'C{index:02}')
        _, first = self.menus.menu(self.archive, 'today')
        self.assertEqual(len(self.callbacks(first, 'wc:')), 10)
        next_page = self.callbacks(first, 'w:')[0]
        _, second = self.menus.menu(self.archive, next_page)
        self.assertEqual(len(self.callbacks(second, 'wc:')), 2)
        selected = self.camera_selection(second, 'C11')
        _, clips = self.menus.menu(self.archive, selected)
        self.assertEqual(self.callbacks(clips, 'w:'), [next_page])

    def test_camera_rename_does_not_invalidate_callback(self):
        camera_id = 'C' * 64
        self.camera(camera_id, 'Tên cũ')
        self.recording('2026-10-03T01:00:00+07:00', '2026-10-03T01:01:00+07:00', camera=camera_id)
        _, cameras = self.menus.menu(self.archive, 'today')
        selected = self.camera_selection(cameras, camera_id)
        self.archive.update_camera(camera_id, {'name': 'Tên mới Tiếng Việt'})
        title, clips = self.menus.menu(self.archive, selected)
        self.assertIn('Tên mới Tiếng Việt', title)
        self.assertNotIn('Tên cũ', title)
        for callback in self.callbacks(cameras) + self.callbacks(clips):
            self.assertLessEqual(len(callback.encode('utf-8')), 64)
            self.assertNotIn('Tên', callback)
            self.assertNotIn(camera_id, callback)

    def test_empty_camera_window_and_bookmarked_camera_after_deletion(self):
        self.camera()
        text, empty = self.menus.menu(self.archive, 'today')
        self.assertIn('Chưa có video', text)
        self.assertEqual(self.callbacks(empty, 'wc:'), [])
        key = self.recording('2026-10-03T01:00:00+07:00', '2026-10-03T01:01:00+07:00')
        _, cameras = self.menus.menu(self.archive, 'today')
        selected = self.camera_selection(cameras)
        self.assertTrue(self.archive.soft_delete(key, 43))
        text, clips = self.menus.menu(self.archive, selected)
        self.assertIn('Chưa có video', text)
        self.assertEqual(self.callbacks(clips, 'v:'), [])
        self.assertEqual(self.callbacks(clips, 'f:'), [])
        self.assertEqual(self.callbacks(clips, 'x:'), [])

    def test_time_menu_text_is_labels_only_with_timestamps_and_short_empty_state(self):
        self.camera(name='PN')
        key = self.recording('2026-10-03T01:00:00+07:00', '2026-10-03T01:01:00+07:00')
        text, cameras = self.menus.menu(self.archive, 'today')
        self.assertEqual(text, 'Hôm nay · Camera · trang 1')
        title, clips = self.menus.menu(self.archive, self.camera_selection(cameras))
        self.assertEqual(title, 'PN · Hôm nay · 1 video · trang 1\n1. 03/10 01:00:00 → 03/10 01:01:00')
        for body in (text, title):
            self.assertNotIn(self.settings.timezone, body)
            self.assertNotIn('Chọn', body)
            self.assertNotIn('khoảng thời gian', body)
        self.assertIn('Mới → cũ', [button['text'] for row in clips for button in row])
        self.archive.soft_delete(key, 43)
        title, clips = self.menus.menu(self.archive, self.camera_selection(cameras))
        self.assertEqual(title, 'PN · Hôm nay · 0 video · trang 1\nChưa có video.')
        empty, _ = self.menus.menu(self.archive, 'today')
        self.assertEqual(empty, 'Hôm nay · Camera · trang 1\nChưa có video.')

    def test_invalid_tampered_callbacks_and_out_of_range_pages(self):
        self.camera()
        token = self.telegram.camera_token('Front_Camera')
        invalid = [None, '', 'last12h', 'w:t:0:a:0', f'w:t:{self.NOW + 301}:a:0',
                   f'w:t:{self.NOW}:asc:0', f'w:t:{self.NOW}:a:-1', f'w:z:{self.NOW}:a:0',
                   f'w:t:{self.NOW}:a:1', f'w:t:{self.NOW}:a:1000000',
                   f'wc:t:{self.NOW}:{token}:a:1:0', f'wc:t:{self.NOW}:bad_token:a:0:0',
                   f'wc:t:{self.NOW}:{token}:a:0:-1', f'wc:t:{self.NOW}:000000000000:a:0:0',
                   'w:t:99999999999:a:0', 'w:t:00000000001:a:0', 'x' * 65]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.menus.menu(self.archive, data)

    def test_historical_bookmark_has_no_expiry(self):
        self.camera()
        key = self.recording('2010-05-01T10:00:00+07:00', '2010-05-01T10:01:00+07:00')
        old_anchor = int(datetime.fromisoformat('2010-05-01T12:00:00+07:00').timestamp())
        text, cameras = self.menus.menu(self.archive, f'w:t:{old_anchor}:a:0')
        self.assertEqual(text, 'Hôm nay · Camera · trang 1')
        title, clips = self.menus.menu(self.archive, self.camera_selection(cameras))
        self.assertEqual(self.callbacks(clips, 'v:'), ['v:' + key[:32]])
        self.assertIn('01/05 10:00:00 → 01/05 10:01:00', title)

    def test_daily_shortcut_does_not_assume_twenty_four_hours_at_dst(self):
        # datetime.timezone is available without an OS zone database. This
        # local tzinfo changes from UTC-5 to UTC-4 at the March 8 midnight.
        from datetime import timedelta, tzinfo

        class ShiftedZone(tzinfo):
            def utcoffset(self, value):
                return timedelta(hours=-5 if value.date() < datetime(2026, 3, 8).date() else -4)

            def dst(self, value):
                return timedelta()

        # March 7 runs across the midnight offset change: 23 elapsed hours.
        anchor = int(datetime.fromisoformat('2026-03-07T12:00:00-05:00').timestamp())
        with patch('archive_app.telegram_menu.get_zone', return_value=ShiftedZone()):
            start, end, _ = self.menus._window('t', anchor)
        self.assertEqual(end - start, 23 * 3600000)


if __name__ == '__main__':
    unittest.main()
