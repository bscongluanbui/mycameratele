"""Native green parent/camera buttons retain the existing menu behavior.

All archive data and HTTP responses are synthetic; no Telegram calls or media
uploads occur. Green is the Bot API ``success`` style, not decorated text.
"""
import copy
import io
import json
import unittest
from unittest.mock import patch

import test_time_menu as fixtures
from archive_app.telegram_menu import TimeMenus


class GreenMenuTests(unittest.TestCase):
    NOW = fixtures.TimeMenuTests.NOW
    setUp = fixtures.TimeMenuTests.setUp
    tearDown = fixtures.TimeMenuTests.tearDown
    camera = fixtures.TimeMenuTests.camera
    recording = fixtures.TimeMenuTests.recording
    callbacks = staticmethod(fixtures.TimeMenuTests.callbacks)
    camera_selection = fixtures.TimeMenuTests.camera_selection

    @staticmethod
    def flat(buttons):
        return [button for row in buttons for button in row]

    def assert_green_choices(self, buttons, prefix):
        choices = [button for button in self.flat(buttons)
                   if button.get('callback_data', '').startswith(prefix)]
        self.assertTrue(choices)
        for button in self.flat(buttons):
            if button in choices:
                self.assertEqual(button.get('style'), 'success')
            else:
                self.assertNotIn('style', button)
        return choices

    def test_all_home_buttons_are_green_full_width_with_original_callbacks(self):
        text, buttons = self.telegram.menu(self.archive, 'home', actor=43)
        self.assertEqual(text, '📹 Camera · Menu')
        self.assertTrue(all(len(row) == 1 for row in buttons))
        self.assertEqual(self.callbacks(buttons), [
            'today', 'yesterday', 'last6h', 'thisweek', 'lastweek', 'custom-time',
            'root', 'sync:all', 'ss:all', 'recent:0', 'trash:0', 'status',
        ])
        self.assertTrue(all(button.get('style') == 'success' for button in self.flat(buttons)))

    def test_shortcuts_return_fresh_green_objects_without_changing_commands(self):
        first = self.menus.shortcuts()
        first[0][0]['style'] = 'danger'
        first[0][0]['text'] = 'changed fixture'
        second = self.menus.shortcuts()
        self.assertEqual(second[0][0], {
            'text': '📅 Hôm nay', 'callback_data': 'today', 'style': 'success',
        })
        self.assertTrue(all(button['style'] == 'success' for button in self.flat(second)))
        self.assertTrue(all('style' not in command for command in self.menus.commands()))

    def test_root_camera_choices_are_green_on_both_pages_only(self):
        for index in range(12):
            self.camera(f'Camera{index:02}', f'Camera {index:02}')
        _, first = self.telegram.menu(self.archive, 'root', actor=43)
        _, second = self.telegram.menu(self.archive, 'r:1', actor=43)
        self.assertEqual(len(self.assert_green_choices(first, 'c:')), 10)
        self.assertEqual(len(self.assert_green_choices(second, 'c:')), 2)
        self.assertIn('r:1', self.callbacks(first))
        self.assertIn('r:0', self.callbacks(second))
        self.assertIn('home', self.callbacks(second))
        self.assertIn('sync:all', self.callbacks(first))

    def test_all_preset_camera_choices_are_green_and_controls_remain_default(self):
        self.camera()
        for day in ('2026-09-25', '2026-10-02', '2026-10-03'):
            self.recording(day + 'T01:00:00+07:00', day + 'T01:01:00+07:00')
        for action in ('today', 'yesterday', 'last6h', 'thisweek', 'lastweek'):
            with self.subTest(action=action):
                _, buttons = self.telegram.menu(self.archive, action, actor=43)
                choices = self.assert_green_choices(buttons, 'wc:')
                self.assertEqual(len(choices), 1)
                self.assertIn('Cửa trước', choices[0]['text'])
                self.assertIn('home', self.callbacks(buttons))
                self.assertTrue(any(data.startswith('bw:') for data in self.callbacks(buttons)))
                self.assertNotIn('today', self.callbacks(buttons))

    def test_preset_camera_pagination_keeps_green_and_frozen_callbacks(self):
        for index in range(11):
            camera = f'Camera{index:02}'
            self.camera(camera, f'Camera {index:02}')
            self.recording('2026-10-03T01:00:00+07:00',
                           '2026-10-03T01:01:00+07:00', camera=camera)
        _, first = self.telegram.menu(self.archive, 'today', actor=43)
        next_page = next(data for data in self.callbacks(first, 'w:') if data.endswith(':1'))
        self.assertEqual(len(self.assert_green_choices(first, 'wc:')), 10)
        self.clock_mock.return_value = self.NOW + 86400
        _, second = self.telegram.menu(self.archive, next_page, actor=43)
        choices = self.assert_green_choices(second, 'wc:')
        self.assertEqual(len(choices), 1)
        self.assertIn(f':{self.NOW}:', choices[0]['callback_data'])
        self.assertIn('Camera 10', choices[0]['text'])

    def test_custom_range_camera_choices_are_green_but_prompts_stay_default(self):
        self.camera()
        self.recording('2026-10-03T01:00:00+07:00', '2026-10-03T01:01:00+07:00')
        _, prompt = self.menus.begin(self.archive, 43)
        self.assertTrue(all('style' not in button for button in self.flat(prompt)))
        _, prompt = self.menus.accept(self.archive, 43, '03/10/26')
        self.assertTrue(all('style' not in button for button in self.flat(prompt)))
        text, cameras = self.menus.accept(self.archive, 43, '03/10/26')
        self.assertIn('03/10/26', text)
        self.assertEqual(len(self.assert_green_choices(cameras, 'wqc:')), 1)
        route = next(data for data in self.callbacks(cameras) if data.startswith('wqc:'))
        _, clips = self.telegram.menu(self.archive, route, actor=43)
        self.assertTrue(all('style' not in button for button in self.flat(clips)))
        back = next(data for data in self.callbacks(clips) if data.startswith('wq:'))
        _, restored = self.telegram.menu(self.archive, back, actor=43)
        self.assertEqual(restored, cameras)

    def test_empty_submenus_have_neutral_back_not_parent_menu_buttons(self):
        _, cameras = self.telegram.menu(self.archive, 'today', actor=43)
        self.assertEqual(cameras, [[{'text': '↩ Quay lại', 'callback_data': 'home'}]])
        _, root = self.telegram.menu(self.archive, 'root', actor=43)
        self.assertTrue(all('style' not in button for button in self.flat(root)))
        self.assertNotIn('today', self.callbacks(root))

    def test_video_and_calendar_sync_upload_sort_download_back_actions_stay_neutral(self):
        self.camera()
        key = self.recording('2026-10-03T01:00:00+07:00', '2026-10-03T01:01:00+07:00')
        token = self.telegram.camera_token('Front_Camera')
        routes = [f'c:{token}:asc', f'y:{token}:2026:asc', f'm:{token}:2026-10:asc',
                  f'd:{token}:2026-10-03:asc', f'wc:t:{self.NOW}:{token}:a:0:0',
                  'status', 'recent:0', 'ss:all']
        for route in routes:
            with self.subTest(route=route):
                _, buttons = self.telegram.menu(self.archive, route, actor=43)
                self.assertTrue(all('style' not in button for button in self.flat(buttons)))
        _, clips = self.telegram.menu(self.archive, routes[4], actor=43)
        self.assertIn('v:' + key[:32], self.callbacks(clips))
        self.assertIn('f:' + key[:32], self.callbacks(clips))
        self.assertIn('x:' + key[:32], self.callbacks(clips))

    def test_rename_and_unicode_camera_name_keep_green_and_immutable_callback(self):
        camera_id = 'X' * 64
        self.camera(camera_id, 'PN')
        self.recording('2026-10-03T01:00:00+07:00', '2026-10-03T01:01:00+07:00', camera=camera_id)
        _, root = self.telegram.menu(self.archive, 'root', actor=43)
        before = self.assert_green_choices(root, 'c:')[0]['callback_data']
        self.archive.update_camera(camera_id, {'name': 'Phòng ngủ tiếng Việt ✓'})
        _, renamed = self.telegram.menu(self.archive, 'root', actor=43)
        choice = self.assert_green_choices(renamed, 'c:')[0]
        self.assertEqual(choice['callback_data'], before)
        self.assertEqual(choice['text'], 'Phòng ngủ tiếng Việt ✓')
        _, timed = self.telegram.menu(self.archive, 'today', actor=43)
        self.assertIn('Phòng ngủ tiếng Việt ✓', self.assert_green_choices(timed, 'wc:')[0]['text'])
        for button in self.flat(renamed + timed):
            self.assertLessEqual(len(button['callback_data'].encode('utf-8')), 64)
            self.assertNotIn(camera_id, button['callback_data'])

    def test_legacy_day_camera_buttons_are_green_with_unchanged_callbacks(self):
        self.camera()
        key = self.recording('2026-10-03T01:00:00+07:00', '2026-10-03T01:01:00+07:00')
        _, cameras = self.telegram.menu(self.archive, 'd:2026-10-03', actor=43)
        selected = self.assert_green_choices(cameras, 'p:')[0]
        self.assertEqual(selected['text'], 'Cửa trước')
        self.assertRegex(selected['callback_data'], r'^p:2026-10-03:[a-f0-9]{12}:0$')
        _, clips = self.telegram.menu(self.archive, selected['callback_data'], actor=43)
        self.assertIn('v:' + key[:32], self.callbacks(clips))
        self.assertTrue(all('style' not in button for button in self.flat(clips)))

    def test_navigation_and_player_copy_keep_camera_style_and_neutral_back(self):
        self.camera()
        self.recording('2026-10-03T01:00:00+07:00', '2026-10-03T01:01:00+07:00')
        _, cameras = self.telegram.menu(self.archive, 'today', actor=43)
        original = copy.deepcopy(cameras)
        navigated = self.telegram.navigation_buttons(self.archive, 43, 'sync:all', cameras)
        self.assert_green_choices(navigated, 'wc:')
        back = next(button for button in self.flat(navigated) if button.get('callback_data') == 'nav-return')
        self.assertEqual(back, {'text': '↩ Quay lại', 'callback_data': 'nav-return'})
        self.settings.telegram_destination = 'channel'
        self.settings.storage_channel_id = -100123
        converted = self.telegram.player_buttons(self.archive, navigated, 43)
        self.assertEqual(converted, navigated)
        self.assertIsNot(converted, navigated)
        self.assertEqual(cameras, original)

    def test_native_video_conversion_preserves_green_camera_but_does_not_repost(self):
        self.camera()
        key = self.recording('2026-10-03T01:00:00+07:00', '2026-10-03T01:01:00+07:00')
        self.settings.telegram_destination = 'channel'
        self.settings.storage_channel_id = -100123
        self.archive.state('telegram_bot_id', '900')
        with self.archive.conn:
            self.archive.conn.execute('''UPDATE recordings SET bot_id=900,storage_kind='channel',
                storage_chat_id=-100123,storage_message_id=19 WHERE key=?''', (key,))
        _, cameras = self.telegram.menu(self.archive, 'today', actor=43)
        camera_button = self.assert_green_choices(cameras, 'wc:')[0]
        _, clips = self.telegram.menu(self.archive, self.camera_selection(cameras), actor=43)
        buttons = clips + [[dict(camera_button)]]
        saved = copy.deepcopy(buttons)
        with patch.object(self.telegram, 'request') as request:
            converted = self.telegram.player_buttons(self.archive, buttons, 43)
            request.assert_not_called()
        self.assertEqual(converted[0][0], {
            'text': '1. ▶ Xem', 'url': 'https://t.me/c/123/19?single&t=1',
        })
        self.assertEqual(converted[0][1:], buttons[0][1:])
        self.assertEqual(converted[-1][0], camera_button)
        self.assertEqual(buttons, saved)

    def test_json_send_and_edit_keep_native_success_style_and_original_payloads(self):
        self.settings.token = 'synthetic-token'
        self.camera()
        text, home = self.telegram.menu(self.archive, 'home', actor=43)
        with patch('archive_app.telegram.urllib.request.urlopen', return_value=io.BytesIO(
                json.dumps({'ok': True, 'result': {'message_id': 101}}).encode())) as opened:
            self.assertEqual(self.telegram.present_menu(self.archive, 43, 43, text, home), 101)
        request = opened.call_args.args[0]
        self.assertTrue(request.full_url.endswith('/sendMessage'))
        sent = json.loads(request.data)
        self.assertEqual(sent['reply_markup'], {'inline_keyboard': home})
        self.assertTrue(all(button['style'] == 'success' for button in self.flat(sent['reply_markup']['inline_keyboard'])))
        title, cameras = self.telegram.menu(self.archive, 'root', actor=43)
        with patch('archive_app.telegram.urllib.request.urlopen', return_value=io.BytesIO(
                json.dumps({'ok': True, 'result': {'message_id': 101}}).encode())) as opened:
            self.assertEqual(self.telegram.present_menu(self.archive, 43, 43, title, cameras, message_id=101), 101)
        request = opened.call_args.args[0]
        self.assertTrue(request.full_url.endswith('/editMessageText'))
        edited = json.loads(request.data)
        self.assertEqual(edited['reply_markup'], {'inline_keyboard': cameras})
        self.assertEqual(edited['chat_id'], 43)
        self.assertEqual(edited['message_id'], 101)
        self.assert_green_choices(edited['reply_markup']['inline_keyboard'], 'c:')
        self.assertNotIn('today', self.callbacks(edited['reply_markup']['inline_keyboard']))

    def test_green_button_keeps_utf8_callback_limit_and_default_helper_behavior(self):
        self.assertEqual(TimeMenus._button('Choice', 'fixture', green=True), {
            'text': 'Choice', 'callback_data': 'fixture', 'style': 'success',
        })
        self.assertEqual(TimeMenus._button('Back', 'home'), {'text': 'Back', 'callback_data': 'home'})
        for green in (False, True):
            self.assertEqual(len(TimeMenus._button('Boundary', 'é' * 32, green=green)['callback_data'].encode()), 64)
            for callback in ('', 'é' * 33):
                with self.subTest(green=green, callback=callback), self.assertRaises(ValueError):
                    TimeMenus._button('Invalid', callback, green=green)


if __name__ == '__main__':
    unittest.main()
