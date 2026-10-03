"""Camera-first menus with immutable IDs, friendly names and reversible time sort.

Telegram and video payloads in this module are local synthetic fixtures.
"""
import os
import shutil
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from archive_app.core import Archive, Settings, record_key
from archive_app.telegram import Telegram


class CameraFirstMenuTests(unittest.TestCase):
    def setUp(self):
        self.fixture_parent = Path(__file__).resolve().parent if os.name == 'nt' else Path(tempfile.gettempdir()).resolve()
        self.root = self.fixture_parent / ('.tmp-camera-menu-' + uuid.uuid4().hex)
        self.assertEqual(self.root.resolve().parent, self.fixture_parent)
        self.root.mkdir()
        source_dir = self.root / 'input'
        source_dir.mkdir()
        self.source = source_dir / 'fixture.mp4'
        self.source.write_bytes(b'synthetic-source')
        self.settings = Settings(state_dir=self.root/'state', cache_dir=self.root/'cache', input_dir=source_dir,
                                 timezone='UTC+07:00', min_free_bytes=0, token='fixture',
                                 chat_id='42', owner_user_id=42, allowed_users=(42,), enable_upload=True)
        self.archive = Archive(self.settings)
        self.archive.state('telegram_owner_started:42','1')
        self.keys_by_message={}
        self.telegram = Telegram(self.settings)
        self.normalizer = patch('archive_app.core.normalize', side_effect=self.fake_normalize)
        self.normalizer.start()

    def tearDown(self):
        self.normalizer.stop()
        self.archive.close()
        self.assertEqual(self.root.resolve().parent, self.fixture_parent)
        shutil.rmtree(self.root)

    @staticmethod
    def fake_normalize(source, destination, settings):
        destination = Path(destination)
        destination.write_bytes(b'synthetic-normalized')
        return {'duration':60, 'codec_video':'h264', 'codec_audio':'aac', 'bytes':destination.stat().st_size}

    def camera(self, camera_id='Front_Camera', name='Cửa trước'):
        return self.archive.add_camera({'id':camera_id, 'name':name, 'model':'CS-H6c', 'host':'192.168.1.11'})

    def recording(self, camera='Front_Camera', start='2026-10-03T10:00:00+07:00', end=None,
                  record_id=None, message_id=17, uploaded=True):
        descriptor = {'camera':camera, 'record_id':record_id or str(uuid.uuid4()), 'path':str(self.source),
                      'start_time':start,
                      'end_time':end or (datetime.fromisoformat(start)+timedelta(minutes=1)).isoformat()}
        self.archive.ingest_entry(descriptor)
        key = record_key(descriptor)
        self.keys_by_message[message_id]=key
        if uploaded:
            self.archive.mark_uploaded(key, self.settings.chat_id, message_id, 'synthetic-file-id')
        return key

    @staticmethod
    def callbacks(buttons, kind=None):
        return [b['callback_data'] for row in buttons for b in row
                if 'callback_data' in b and (kind is None or b['callback_data'].startswith(kind+':'))]

    @staticmethod
    def plays(buttons):
        return [b['callback_data'] for row in buttons for b in row if b.get('callback_data','').startswith('v:')]

    def play(self,message_id):
        return 'v:'+self.keys_by_message[message_id][:32]

    def test_root_lists_friendly_camera_names_in_name_order(self):
        self.camera('Zulu', 'Zulu phòng ngủ')
        self.camera('Alpha', 'Alpha sân trước')
        title, buttons = self.telegram.menu(self.archive)
        self.assertIn('Camera', title)
        self.assertEqual([row[0]['text'] for row in buttons if row[0]['callback_data'].startswith('c:')], ['Alpha sân trước', 'Zulu phòng ngủ'])
        self.assertEqual(self.callbacks(buttons,'c'), [f'c:{self.telegram.camera_token("Alpha")}:asc',
                                                  f'c:{self.telegram.camera_token("Zulu")}:asc'])

    def test_camera_year_month_day_clip_hierarchy_and_back_buttons(self):
        self.camera()
        self.recording()
        _, root = self.telegram.menu(self.archive)
        camera_callback = self.callbacks(root, 'c')[0]
        title, years = self.telegram.menu(self.archive, camera_callback)
        self.assertIn('Cửa trước', title)
        self.assertIn('Năm', title)
        year_callback = self.callbacks(years, 'y')[0]
        self.assertIn('root', self.callbacks(years))
        title, months = self.telegram.menu(self.archive, year_callback)
        self.assertIn('Tháng', title)
        month_callback = self.callbacks(months, 'm')[0]
        self.assertIn(camera_callback, self.callbacks(months))
        title, days = self.telegram.menu(self.archive, month_callback)
        self.assertIn('Ngày', title)
        day_callback = self.callbacks(days, 'd')[0]
        self.assertIn(year_callback, self.callbacks(days))
        title, clips = self.telegram.menu(self.archive, day_callback)
        self.assertIn('Cửa trước', title)
        self.assertIn('2026-10-03', title)
        self.assertEqual(self.plays(clips), [self.play(17)])
        self.assertIn(month_callback, self.callbacks(clips))

    def test_clip_sort_ascending_descending_and_descending_second_page(self):
        self.camera()
        token = self.telegram.camera_token('Front_Camera')
        for minute in range(12):
            self.recording(start=f'2026-10-03T10:{minute:02}:00+07:00', message_id=100+minute)
        _, ascending = self.telegram.menu(self.archive, f'd:{token}:2026-10-03:asc')
        _, descending = self.telegram.menu(self.archive, f'd:{token}:2026-10-03:desc')
        self.assertEqual(self.plays(ascending)[0], self.play(100))
        self.assertEqual(self.plays(descending)[0], self.play(111))
        self.assertEqual(len(self.plays(ascending)), 10)
        next_page = next(c for c in self.callbacks(descending, 'p') if c.endswith(':desc:1'))
        _, second = self.telegram.menu(self.archive, next_page)
        self.assertEqual(self.plays(second), [self.play(101),self.play(100)])
        self.assertIn(f'p:{token}:2026-10-03:asc:0', self.callbacks(second))

    def test_year_month_day_sort_toggles(self):
        self.camera()
        token = self.telegram.camera_token('Front_Camera')
        for index, start in enumerate(['2025-02-03T10:00:00+07:00', '2026-01-03T10:00:00+07:00',
                                       '2026-10-03T10:00:00+07:00', '2026-10-07T10:00:00+07:00']):
            self.recording(start=start, message_id=20+index)
        _, years = self.telegram.menu(self.archive, f'c:{token}:desc')
        self.assertEqual(self.callbacks(years, 'y'), [f'y:{token}:2026:desc', f'y:{token}:2025:desc'])
        _, months = self.telegram.menu(self.archive, f'y:{token}:2026:desc')
        self.assertEqual(self.callbacks(months, 'm'), [f'm:{token}:2026-10:desc', f'm:{token}:2026-01:desc'])
        _, days = self.telegram.menu(self.archive, f'm:{token}:2026-10:desc')
        self.assertEqual(self.callbacks(days, 'd'), [f'd:{token}:2026-10-07:desc', f'd:{token}:2026-10-03:desc'])
        self.assertIn(f'm:{token}:2026-10:asc', self.callbacks(days))

    def test_camera_isolation_omits_other_cameras_calendar_and_clips(self):
        self.camera()
        self.camera('Rear_Camera', 'Cửa sau')
        self.recording(message_id=17)
        self.recording(camera='Rear_Camera', start='2025-02-02T10:00:00+07:00', message_id=18)
        self.recording(camera='Rear_Camera', message_id=19)
        token = self.telegram.camera_token('Front_Camera')
        _, years = self.telegram.menu(self.archive, f'c:{token}:asc')
        self.assertEqual(self.callbacks(years, 'y'), [f'y:{token}:2026:asc'])
        _, clips = self.telegram.menu(self.archive, f'd:{token}:2026-10-03:asc')
        self.assertEqual(self.plays(clips), [self.play(17)])

    def test_rename_changes_friendly_menu_without_changing_recording_identity(self):
        self.camera()
        key = self.recording()
        token = self.telegram.camera_token('Front_Camera')
        old_callback = f'd:{token}:2026-10-03:asc'
        before = dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?', (key,)).fetchone())
        self.archive.update_camera('Front_Camera', {'name':'Cửa sau đổi tên'})
        title, clips = self.telegram.menu(self.archive, old_callback)
        self.assertIn('Cửa sau đổi tên', title)
        self.assertEqual(self.plays(clips), [self.play(17)])
        _, root = self.telegram.menu(self.archive)
        self.assertEqual(root[0][0]['text'], 'Cửa sau đổi tên')
        after = dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?', (key,)).fetchone())
        self.assertEqual(after, before)
        self.assertEqual(after['camera'], 'Front_Camera')

    def test_rename_applies_to_new_upload_caption(self):
        self.camera()
        key = self.recording(uploaded=False)
        self.archive.update_camera('Front_Camera', {'name':'Tên mới ✓'})
        message = {'chat':{'id':42,'type':'private'}, 'message_id':90, 'document':{'file_id':'new-file-id','file_unique_id':'new-unique-id'}}
        with patch.object(self.telegram, 'request', return_value=message) as request:
            self.assertEqual(self.telegram.upload_one(self.archive), 'uploaded')
        caption = request.call_args.args[1]['caption']
        self.assertTrue(caption.startswith('Tên mới ✓ | '))
        self.assertIn(key, caption)

    def test_legacy_camera_and_clip_callbacks_also_show_renamed_display_name(self):
        self.camera()
        self.recording()
        _, old_camera_menu = self.telegram.menu(self.archive, 'd:2026-10-03')
        old_callback = old_camera_menu[0][0]['callback_data']
        self.archive.update_camera('Front_Camera', {'name':'Tên hiển thị mới'})
        _, refreshed = self.telegram.menu(self.archive, 'd:2026-10-03')
        self.assertEqual(refreshed[0][0]['text'], 'Tên hiển thị mới')
        self.assertEqual(refreshed[0][0]['callback_data'], old_callback)
        title, _ = self.telegram.menu(self.archive, old_callback)
        self.assertIn('Tên hiển thị mới', title)

    def test_empty_registered_camera_still_has_sort_and_back_controls(self):
        self.camera()
        _, root = self.telegram.menu(self.archive)
        title, years = self.telegram.menu(self.archive, root[0][0]['callback_data'])
        self.assertIn('Cửa trước', title)
        self.assertEqual(self.callbacks(years, 'y'), [])
        self.assertIn('root', self.callbacks(years))

    def test_cross_midnight_and_cross_year_clips_appear_in_both_local_days(self):
        self.camera()
        self.recording(start='2026-12-31T23:59:00+07:00', end='2027-01-01T00:01:00+07:00')
        token = self.telegram.camera_token('Front_Camera')
        _, years = self.telegram.menu(self.archive, f'c:{token}:asc')
        self.assertEqual(self.callbacks(years, 'y'), [f'y:{token}:2026:asc', f'y:{token}:2027:asc'])
        for day in ['2026-12-31', '2027-01-01']:
            _, clips = self.telegram.menu(self.archive, f'd:{token}:{day}:asc')
            self.assertEqual(len(self.plays(clips)), 1)

    def test_exact_midnight_end_does_not_create_next_day_or_year(self):
        self.camera()
        self.recording(start='2026-12-31T23:59:00+07:00', end='2027-01-01T00:00:00+07:00')
        token = self.telegram.camera_token('Front_Camera')
        _, years = self.telegram.menu(self.archive, f'c:{token}:asc')
        self.assertEqual(self.callbacks(years, 'y'), [f'y:{token}:2026:asc'])
        _, clips = self.telegram.menu(self.archive, f'd:{token}:2027-01-01:asc')
        self.assertEqual(self.plays(clips), [])

    def test_long_camera_id_and_unicode_name_keep_all_callback_payloads_under_64_bytes(self):
        camera_id = 'X'*64
        self.camera(camera_id, 'Camera sân trước tiếng Việt ✓')
        self.recording(camera=camera_id)
        token = self.telegram.camera_token(camera_id)
        callbacks = ['root', f'c:{token}:asc', f'y:{token}:2026:asc',
                     f'm:{token}:2026-10:asc', f'd:{token}:2026-10-03:asc', f'p:{token}:2026-10-03:desc:0']
        for callback in callbacks:
            _, buttons = self.telegram.menu(self.archive, callback)
            for generated in self.callbacks(buttons):
                self.assertLessEqual(len(generated.encode()), 64)
                self.assertNotIn(camera_id, generated)

    def test_stable_camera_callback_survives_insertion_of_earlier_name(self):
        self.camera('Zulu_Camera', 'Zulu')
        self.recording(camera='Zulu_Camera')
        _, before = self.telegram.menu(self.archive)
        old_callback = before[0][0]['callback_data']
        self.camera('Alpha_Camera', 'Alpha')
        title, years = self.telegram.menu(self.archive, old_callback)
        self.assertIn('Zulu', title)
        self.assertNotIn('Alpha', title)
        self.assertEqual(len(self.callbacks(years, 'y')), 1)

    def test_camera_root_pagination_keeps_all_registered_cameras_reachable(self):
        for index in range(12):
            self.camera(f'camera_{index:02}', f'Camera {index:02}')
        _, first = self.telegram.menu(self.archive)
        self.assertEqual(len(self.callbacks(first, 'c')), 10)
        self.assertIn('r:1', self.callbacks(first))
        _, second = self.telegram.menu(self.archive, 'r:1')
        self.assertEqual(len(self.callbacks(second, 'c')), 2)
        self.assertIn('r:0', self.callbacks(second))

    def test_new_menu_rejects_unknown_camera_sort_and_negative_or_out_of_range_pages(self):
        self.camera()
        self.recording()
        token = self.telegram.camera_token('Front_Camera')
        for callback in ['c:000000000000:asc', f'c:{token}:random',
                         f'p:{token}:2026-10-03:asc:-1', f'p:{token}:2026-10-03:asc:1',
                         'r:-1', 'r:1']:
            with self.subTest(callback=callback), self.assertRaises(ValueError):
                self.telegram.menu(self.archive, callback)

    def test_downloaded_or_ambiguous_uploads_do_not_enter_calendar(self):
        self.camera()
        key = self.recording(uploaded=False)
        with self.archive.conn:
            self.archive.conn.execute("UPDATE recordings SET status='upload_unknown' WHERE key=?", (key,))
        token = self.telegram.camera_token('Front_Camera')
        _, years = self.telegram.menu(self.archive, f'c:{token}:asc')
        self.assertEqual(self.callbacks(years, 'y'), [])
        _, clips = self.telegram.menu(self.archive, f'd:{token}:2026-10-03:asc')
        self.assertEqual(self.plays(clips), [])


if __name__ == '__main__':
    unittest.main()
