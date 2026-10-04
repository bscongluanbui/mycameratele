"""Hierarchical menus replace one screen instead of retaining parent actions.

All recordings, credentials and Telegram responses here are synthetic. These
tests never contact Telegram and never read video bytes.
"""
import hashlib
import unittest
from unittest.mock import patch

import test_bot_controls as bot_fixtures
import test_time_menu as time_fixtures
from archive_app.telegram import ApiRejected


PARENT_ACTIONS = {
    'today', 'yesterday', 'last6h', 'thisweek', 'lastweek', 'custom-time',
    'recent:0', 'trash:0', 'status',
}


class HierarchicalMenuTests(unittest.TestCase):
    NOW = time_fixtures.TimeMenuTests.NOW
    setUp = time_fixtures.TimeMenuTests.setUp
    tearDown = time_fixtures.TimeMenuTests.tearDown
    camera = time_fixtures.TimeMenuTests.camera
    recording = time_fixtures.TimeMenuTests.recording
    callbacks = staticmethod(time_fixtures.TimeMenuTests.callbacks)

    def no_parent_actions(self, buttons):
        self.assertFalse(PARENT_ACTIONS.intersection(self.callbacks(buttons)))

    def test_home_keeps_top_level_time_and_archive_actions(self):
        text, buttons = self.telegram.menu(self.archive, 'home', actor=43)
        self.assertIn('Menu', text)
        self.assertTrue(PARENT_ACTIONS.issubset(self.callbacks(buttons)))
        self.assertIn('root', self.callbacks(buttons))

    def test_each_time_shortcut_contains_only_selected_cameras_and_home_back(self):
        self.camera()
        self.recording('2026-10-03T01:00:00+07:00', '2026-10-03T01:01:00+07:00')
        for action in ('today', 'yesterday', 'last6h', 'thisweek', 'lastweek'):
            with self.subTest(action=action):
                text, buttons = self.telegram.menu(self.archive, action, actor=43)
                self.no_parent_actions(buttons)
                self.assertIn('home', self.callbacks(buttons))
                self.assertNotIn('root', self.callbacks(buttons))
                back = next(b for row in buttons for b in row if b.get('callback_data') == 'home')
                self.assertIn('Quay lại', back['text'])
                self.assertIn('Camera', text)

    def test_empty_time_screen_still_has_home_back_without_parent_shortcuts(self):
        text, buttons = self.telegram.menu(self.archive, 'today', actor=43)
        self.assertIn('Chưa có video', text)
        self.no_parent_actions(buttons)
        self.assertEqual(self.callbacks(buttons), ['home'])

    def test_custom_range_camera_screen_hides_other_time_shortcuts(self):
        self.camera()
        self.recording('2026-10-03T01:00:00+07:00', '2026-10-03T01:01:00+07:00')
        self.menus.begin(self.archive, 43)
        self.menus.accept(self.archive, 43, '03/10/26')
        text, buttons = self.menus.accept(self.archive, 43, '03/10/26')
        self.assertIn('03/10/26', text)
        self.no_parent_actions(buttons)
        self.assertIn('home', self.callbacks(buttons))
        self.assertNotIn('root', self.callbacks(buttons))

    def test_camera_root_only_contains_camera_subtree_controls_and_home_back(self):
        camera = self.camera()
        _, buttons = self.telegram.menu(self.archive, 'root', actor=43)
        callbacks = self.callbacks(buttons)
        self.no_parent_actions(buttons)
        self.assertEqual(set(callbacks), {
            'c:' + self.telegram.camera_token(camera['id']) + ':asc',
            'sync:all', 'ss:all', 'home',
        })
        back = next(b for row in buttons for b in row if b.get('callback_data') == 'home')
        self.assertIn('Quay lại', back['text'])

    def test_calendar_child_back_returns_exact_immediate_parent_and_sort(self):
        self.camera()
        self.recording('2026-10-03T01:00:00+07:00', '2026-10-03T01:01:00+07:00')
        token = self.telegram.camera_token('Front_Camera')
        path = (
            (f'c:{token}:desc', 'root'),
            (f'y:{token}:2026:desc', f'c:{token}:desc'),
            (f'm:{token}:2026-10:desc', f'y:{token}:2026:desc'),
            (f'd:{token}:2026-10-03:desc', f'm:{token}:2026-10:desc'),
        )
        for selected, parent in path:
            with self.subTest(selected=selected):
                _, buttons = self.telegram.menu(self.archive, selected, actor=43)
                self.no_parent_actions(buttons)
                back = [b for row in buttons for b in row if 'Quay lại' in b['text']]
                self.assertEqual(len(back), 1)
                self.assertEqual(back[0]['callback_data'], parent)

    def test_time_video_back_preserves_anchor_sort_and_camera_page(self):
        for index in range(11):
            camera = f'Camera{index:02}'
            self.camera(camera, camera)
            self.recording('2026-10-03T01:00:00+07:00', '2026-10-03T01:01:00+07:00', camera=camera)
        token = self.telegram.camera_token('Camera10')
        data = f'wc:t:{self.NOW}:{token}:d:0:1'
        _, buttons = self.telegram.menu(self.archive, data, actor=43)
        self.no_parent_actions(buttons)
        back = next(b for row in buttons for b in row if b.get('callback_data', '').startswith('w:'))
        self.assertIn('Quay lại', back['text'])
        self.assertEqual(back['callback_data'], f'w:t:{self.NOW}:d:1')
        self.clock_mock.return_value = self.NOW + 2 * 86400
        _, parents = self.telegram.menu(self.archive, back['callback_data'], actor=43)
        expected = f'wc:t:{self.NOW}:{token}:d:0:1'
        self.assertIn(expected, self.callbacks(parents))

    def test_custom_video_back_preserves_range_token_sort_and_camera_page(self):
        for index in range(11):
            camera = f'Camera{index:02}'
            self.camera(camera, camera)
            self.recording('2026-10-03T01:00:00+07:00', '2026-10-03T01:01:00+07:00', camera=camera)
        self.menus.begin(self.archive, 43)
        self.menus.accept(self.archive, 43, '03/10/26')
        self.menus.accept(self.archive, 43, '03/10/26')
        selection = self.menus._session(self.archive, 43)['token']
        camera = self.telegram.camera_token('Camera10')
        _, buttons = self.telegram.menu(self.archive, f'wqc:{selection}:{camera}:d:0:1', actor=43)
        self.no_parent_actions(buttons)
        back = next(b for row in buttons for b in row if b.get('callback_data', '').startswith('wq:'))
        self.assertIn('Quay lại', back['text'])
        self.assertEqual(back['callback_data'], f'wq:{selection}:d:1')
        _, parent = self.telegram.menu(self.archive, back['callback_data'], actor=43)
        self.assertIn(f'wqc:{selection}:{camera}:d:0:1', self.callbacks(parent))


class HierarchicalPollTests(unittest.TestCase):
    setUp = bot_fixtures.BotControlTests.setUp
    tearDown = bot_fixtures.BotControlTests.tearDown
    message = bot_fixtures.BotControlTests.message

    def fake(self, method, fields, **kwargs):
        if method in ('editMessageText', 'editMessageReplyMarkup', 'deleteMessage'):
            self.calls.append((method, fields, kwargs))
            return {'message_id': fields.get('message_id', 100), 'chat': {'type': 'private', 'id': fields['chat_id']}}
        return bot_fixtures.BotControlTests.fake(self, method, fields, **kwargs)

    def callback(self, data, actor=43, update_id=1, chat_id=None, message_id=100):
        self.updates = [{
            'update_id': update_id,
            'callback_query': {
                'id': 'synthetic-callback', 'data': data, 'from': {'id': actor},
                'message': {'message_id': message_id,
                            'chat': {'type': 'private', 'id': actor if chat_id is None else chat_id}},
            },
        }]
        self.telegram.poll(self.archive)

    def selected_video_page(self, actor=43, message_id=81):
        """Create 12 indexed files so deletion still leaves a second page."""
        self.addCleanup(patch.stopall)
        patch('archive_app.telegram_menu.time.time', return_value=time_fixtures.TimeMenuTests.NOW).start()
        source = dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?', (self.key,)).fetchone())
        columns = tuple(source)
        sql = 'INSERT INTO recordings (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')'
        with self.archive.conn:
            for index in range(1, 12):
                row = dict(source)
                row.update(key=hashlib.sha256(f'synthetic-menu-page:{index}'.encode()).hexdigest(),
                           record_id=f'synthetic-page-{index}',
                           start_ms=source['start_ms'] + index * 60000,
                           end_ms=source['end_ms'] + index * 60000)
                self.archive.conn.execute(sql, tuple(row[name] for name in columns))
        token = self.telegram.camera_token('front')
        route = f'wc:t:{time_fixtures.TimeMenuTests.NOW}:{token}:d:1:0'
        self.callback(route, actor=actor, message_id=message_id)
        fields = next(c[1] for c in reversed(self.calls) if c[0] == 'editMessageText')
        self.assertIn('trang 2', fields['text'])
        return route, fields

    def latest_edit(self):
        return next(c[1] for c in reversed(self.calls) if c[0] == 'editMessageText')

    def test_callback_replaces_existing_screen_without_new_menu_message(self):
        self.callback('today')
        edits = [c[1] for c in self.calls if c[0] == 'editMessageText']
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]['message_id'], 100)
        self.assertEqual(edits[0]['chat_id'], 43)
        callbacks = time_fixtures.TimeMenuTests.callbacks(edits[0]['reply_markup']['inline_keyboard'])
        self.assertIn('home', callbacks)
        self.assertFalse(PARENT_ACTIONS.intersection(callbacks))
        self.assertFalse(any(c[0] == 'sendMessage' and 'inline_keyboard' in c[1].get('reply_markup', {}) for c in self.calls))

    def test_back_to_home_replaces_same_message_and_restores_parent_actions(self):
        self.callback('today')
        self.calls.clear()
        self.callback('home', update_id=2)
        edits = [c[1] for c in self.calls if c[0] == 'editMessageText']
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]['message_id'], 100)
        callbacks = time_fixtures.TimeMenuTests.callbacks(edits[0]['reply_markup']['inline_keyboard'])
        self.assertTrue(PARENT_ACTIONS.issubset(callbacks))
        self.assertFalse(any(c[0] == 'sendMessage' and 'inline_keyboard' in c[1].get('reply_markup', {}) for c in self.calls))

    def test_repeat_edit_not_modified_does_not_resend_or_pin_update_cursor(self):
        original = self.fake
        def request(method, fields, **kwargs):
            if method == 'editMessageText':
                self.calls.append((method, fields, kwargs))
                raise ApiRejected(400, description='Bad Request: message is not modified')
            return original(method, fields, **kwargs)
        self.request.side_effect = request
        self.callback('today')
        self.assertEqual(self.archive.state('telegram_offset'), '2')
        self.assertFalse(any(c[0] == 'sendMessage' and 'inline_keyboard' in c[1].get('reply_markup', {}) for c in self.calls))

    def test_allowlist_gate_rejects_foreign_actor_and_wrong_private_chat_edits(self):
        self.callback('today', actor=999)
        self.callback('today', actor=43, chat_id=44, update_id=2)
        self.assertFalse(any(c[0] in ('sendMessage', 'editMessageText', 'editMessageReplyMarkup') for c in self.calls))
        self.assertEqual(self.archive.state('telegram_offset'), '3')

    def test_all_viewers_edit_only_their_own_private_menu(self):
        self.callback('today', actor=43, message_id=100)
        self.callback('yesterday', actor=44, message_id=200, update_id=2)
        edits = [(c[1]['chat_id'], c[1]['message_id']) for c in self.calls if c[0] == 'editMessageText']
        self.assertEqual(edits, [(43, 100), (44, 200)])

    def test_new_command_retires_previous_inline_keyboard_after_success(self):
        self.callback('today', message_id=81)
        self.calls.clear()
        self.message('/start', update_id=2)
        menus = [c[1] for c in self.calls if c[0] == 'sendMessage'
                 and 'inline_keyboard' in c[1].get('reply_markup', {})]
        self.assertEqual(len(menus), 1)
        retired = [c[1] for c in self.calls if c[0] == 'editMessageReplyMarkup']
        self.assertEqual(len(retired), 1)
        self.assertEqual(retired[0]['message_id'], 81)
        self.assertEqual(retired[0]['reply_markup'], {'inline_keyboard': []})
        self.assertEqual(self.archive.state(self.telegram._menu_state_key(43)), '100')

    def test_keyboard_migration_removes_old_parent_reply_keyboard(self):
        self.callback('today')
        removals = [c[1] for c in self.calls if c[0] == 'sendMessage'
                    and c[1].get('reply_markup', {}).get('remove_keyboard') is True]
        self.assertEqual(len(removals), 1)
        self.assertEqual(removals[0]['chat_id'], 43)
        self.assertFalse(any(c[0] == 'sendMessage' and 'keyboard' in c[1].get('reply_markup', {}) for c in self.calls))
        self.calls.clear()
        self.callback('home', update_id=2)
        self.assertFalse(any(c[0] == 'sendMessage' and c[1].get('reply_markup', {}).get('remove_keyboard') for c in self.calls))

    def test_custom_date_text_updates_tracked_prompt_without_stacking_menus(self):
        self.callback('custom-time', message_id=82)
        self.calls.clear()
        self.message('03/10/26', update_id=2)
        edits = [c[1] for c in self.calls if c[0] == 'editMessageText']
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]['message_id'], 82)
        self.assertIn('Đến ngày nào', edits[0]['text'])
        self.assertFalse(any(c[0] == 'sendMessage' and 'inline_keyboard' in c[1].get('reply_markup', {}) for c in self.calls))
        self.calls.clear()
        self.message('03/10/26', update_id=3)
        edits = [c[1] for c in self.calls if c[0] == 'editMessageText']
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]['message_id'], 82)
        callbacks = time_fixtures.TimeMenuTests.callbacks(edits[0]['reply_markup']['inline_keyboard'])
        self.assertIn('home', callbacks)
        self.assertFalse(PARENT_ACTIONS.intersection(callbacks))

    def test_missing_message_known_rejection_sends_one_fallback_menu(self):
        original = self.fake
        def request(method, fields, **kwargs):
            if method == 'editMessageText':
                self.calls.append((method, fields, kwargs))
                raise ApiRejected(400, description='Bad Request: message to edit not found')
            return original(method, fields, **kwargs)
        self.request.side_effect = request
        self.callback('today', message_id=82)
        self.assertEqual(sum(c[0] == 'editMessageText' for c in self.calls), 1)
        self.assertEqual(sum(c[0] == 'sendMessage' and 'inline_keyboard' in c[1].get('reply_markup', {}) for c in self.calls), 1)
        self.assertEqual(self.archive.state(self.telegram._menu_state_key(43)), '100')

    def test_rate_limited_edit_does_not_send_duplicate_or_advance_cursor(self):
        original = self.fake
        def request(method, fields, **kwargs):
            if method == 'editMessageText':
                self.calls.append((method, fields, kwargs))
                raise ApiRejected(429, retry_after=20)
            return original(method, fields, **kwargs)
        self.request.side_effect = request
        with self.assertRaises(ApiRejected):
            self.callback('today', message_id=82)
        self.assertFalse(any(c[0] == 'sendMessage' and 'inline_keyboard' in c[1].get('reply_markup', {}) for c in self.calls))
        self.assertNotEqual(self.archive.state('telegram_offset'), '2')

    def test_unknown_edit_rejection_does_not_fallback_duplicate(self):
        original = self.fake
        def request(method, fields, **kwargs):
            if method == 'editMessageText':
                self.calls.append((method, fields, kwargs))
                raise ApiRejected(400, description='Bad Request: synthetic unsupported request')
            return original(method, fields, **kwargs)
        self.request.side_effect = request
        self.callback('today', message_id=82)
        self.assertFalse(any(c[0] == 'sendMessage' and 'inline_keyboard' in c[1].get('reply_markup', {}) for c in self.calls))
        self.assertEqual(self.archive.state('telegram_offset'), '2')

    def test_malformed_callback_message_id_falls_back_to_single_new_menu(self):
        for update_id, message_id in enumerate((None, True, False, 0, -1, '82', 82.0, 2147483648), 1):
            with self.subTest(message_id=message_id):
                self.calls.clear()
                self.callback('today', message_id=message_id, update_id=update_id)
                self.assertFalse(any(c[0] == 'editMessageText' for c in self.calls))
                self.assertEqual(sum(c[0] == 'sendMessage' and 'inline_keyboard' in c[1].get('reply_markup', {}) for c in self.calls), 1)

    def test_server_error_or_timeout_never_falls_back_to_new_menu(self):
        original = self.fake
        for error in (ApiRejected(500), TimeoutError('synthetic edit delivery timeout')):
            with self.subTest(error=type(error).__name__):
                def request(method, fields, **kwargs):
                    if method == 'editMessageText':
                        self.calls.append((method, fields, kwargs))
                        raise error
                    return original(method, fields, **kwargs)
                self.request.side_effect = request
                self.calls.clear()
                with self.assertRaises(type(error)):
                    self.callback('today', message_id=82)
                self.assertFalse(any(c[0] == 'sendMessage' and 'inline_keyboard' in c[1].get('reply_markup', {}) for c in self.calls))
                self.assertNotEqual(self.archive.state('telegram_offset'), '2')

    def test_failed_new_command_does_not_retire_previous_working_keyboard(self):
        self.callback('today', message_id=81)
        original = self.fake
        def request(method, fields, **kwargs):
            if method == 'sendMessage' and 'inline_keyboard' in fields.get('reply_markup', {}):
                self.calls.append((method, fields, kwargs))
                raise TimeoutError('synthetic new screen delivery timeout')
            return original(method, fields, **kwargs)
        self.request.side_effect = request
        self.calls.clear()
        with self.assertRaises(TimeoutError):
            self.message('/start', update_id=2)
        self.assertFalse(any(c[0] == 'editMessageReplyMarkup' for c in self.calls))
        self.assertEqual(self.archive.state(self.telegram._menu_state_key(43)), '81')
        self.assertEqual(self.archive.state('telegram_offset'), '2')

    def test_present_menu_rejects_non_allowlisted_or_mismatched_private_recipient(self):
        for actor, chat_id in ((999, 999), (43, 44), (True, True)):
            with self.subTest(actor=actor, chat_id=chat_id), self.assertRaises(ValueError):
                self.telegram.present_menu(self.archive, chat_id, actor, 'Synthetic screen', [], message_id=82)
        self.assertEqual(self.calls, [])

    def test_retiring_old_menu_failure_keeps_successful_new_menu_and_cursor(self):
        self.callback('today', message_id=81)
        original = self.fake
        def request(method, fields, **kwargs):
            if method == 'editMessageReplyMarkup':
                self.calls.append((method, fields, kwargs))
                raise TimeoutError('synthetic old keyboard retirement timeout')
            return original(method, fields, **kwargs)
        self.request.side_effect = request
        self.calls.clear()
        self.message('/start', update_id=2)
        self.assertEqual(sum(c[0] == 'sendMessage' and 'inline_keyboard' in c[1].get('reply_markup', {}) for c in self.calls), 1)
        self.assertEqual(self.archive.state(self.telegram._menu_state_key(43)), '100')
        self.assertEqual(self.archive.state('telegram_offset'), '3')

    def test_delete_cancel_returns_to_original_filtered_video_page_and_order(self):
        route, original = self.selected_video_page()
        prefix = next(b['callback_data'][2:] for row in original['reply_markup']['inline_keyboard']
                      for b in row if b.get('callback_data', '').startswith('x:'))
        self.calls.clear()
        self.callback('x:' + prefix, update_id=2, message_id=81)
        confirmation = self.latest_edit()
        callbacks = time_fixtures.TimeMenuTests.callbacks(confirmation['reply_markup']['inline_keyboard'])
        self.assertIn('nav-return', callbacks)
        self.assertEqual(self.telegram._menu_route(self.archive, 43), route)
        self.calls.clear()
        self.callback('cancel-delete', update_id=3, message_id=81)
        restored = self.latest_edit()
        self.assertEqual(restored['message_id'], 81)
        self.assertEqual(restored['text'], original['text'])
        self.assertEqual(restored['reply_markup'], original['reply_markup'])
        self.assertEqual(self.archive.state('telegram_delete_confirm:43'), '{}')

    def test_confirmed_delete_back_returns_to_filtered_page_not_camera_root(self):
        route, original = self.selected_video_page()
        selection = next(b['callback_data'] for row in original['reply_markup']['inline_keyboard']
                         for b in row if b.get('callback_data', '').startswith('x:'))
        self.callback(selection, update_id=2, message_id=81)
        confirmation = next(b['callback_data'] for row in self.latest_edit()['reply_markup']['inline_keyboard']
                            for b in row if b.get('callback_data', '').startswith('xc:'))
        self.callback(confirmation, update_id=3, message_id=81)
        self.assertIn('nav-return', time_fixtures.TimeMenuTests.callbacks(self.latest_edit()['reply_markup']['inline_keyboard']))
        self.assertEqual(self.telegram._menu_route(self.archive, 43), route)
        self.calls.clear()
        self.callback('nav-return', update_id=4, message_id=81)
        restored = self.latest_edit()
        self.assertIn('trang 2', restored['text'])
        self.assertIn('Hôm nay', restored['text'])
        self.assertEqual(restored['message_id'], 81)
        callbacks = time_fixtures.TimeMenuTests.callbacks(restored['reply_markup']['inline_keyboard'])
        self.assertNotIn(selection, callbacks)
        self.assertIn(f'w:t:{time_fixtures.TimeMenuTests.NOW}:d:0', callbacks)

    def test_bulk_confirmation_back_preserves_selected_video_window_and_page(self):
        route, original = self.selected_video_page()
        bulk = next(b['callback_data'] for row in original['reply_markup']['inline_keyboard']
                    for b in row if b.get('callback_data', '').startswith('bw:'))
        self.calls.clear()
        self.callback(bulk, update_id=2, message_id=81)
        panel = self.latest_edit()
        callbacks = time_fixtures.TimeMenuTests.callbacks(panel['reply_markup']['inline_keyboard'])
        self.assertIn('nav-return', callbacks)
        self.assertNotIn('home', callbacks)
        self.assertEqual(self.telegram._menu_route(self.archive, 43), route)
        self.calls.clear()
        self.callback('nav-return', update_id=3, message_id=81)
        restored = self.latest_edit()
        self.assertEqual(restored['text'], original['text'])
        self.assertEqual(restored['reply_markup'], original['reply_markup'])

    def test_bulk_status_and_cancel_do_not_overwrite_original_menu_route(self):
        route, original = self.selected_video_page()
        bulk = next(b['callback_data'] for row in original['reply_markup']['inline_keyboard']
                    for b in row if b.get('callback_data', '').startswith('bw:'))
        self.callback(bulk, update_id=2, message_id=81)
        status = next(b['callback_data'] for row in self.latest_edit()['reply_markup']['inline_keyboard']
                      for b in row if b.get('callback_data', '').startswith('bulk-status:'))
        self.callback(status, update_id=3, message_id=81)
        self.assertEqual(self.telegram._menu_route(self.archive, 43), route)
        cancel = next(b['callback_data'] for row in self.latest_edit()['reply_markup']['inline_keyboard']
                      for b in row if b.get('callback_data', '').startswith('bulk-cancel:'))
        self.callback(cancel, update_id=4, message_id=81)
        self.assertEqual(self.telegram._menu_route(self.archive, 43), route)
        self.callback('nav-return', update_id=5, message_id=81)
        self.assertEqual(self.latest_edit()['text'], original['text'])

    def test_unconfirmed_new_menu_response_never_retries_or_advances_cursor(self):
        original = self.fake
        for response in (None, False, {}, {'message_id': True}, {'message_id': 0}, {'message_id': '100'}):
            with self.subTest(response=response):
                def request(method, fields, **kwargs):
                    if method == 'sendMessage' and 'inline_keyboard' in fields.get('reply_markup', {}):
                        self.calls.append((method, fields, kwargs))
                        return response
                    return original(method, fields, **kwargs)
                self.request.side_effect = request
                self.calls.clear()
                with self.assertRaises(RuntimeError):
                    self.message('/today')
                self.assertEqual(sum(c[0] == 'sendMessage' and 'inline_keyboard' in c[1].get('reply_markup', {}) for c in self.calls), 1)
                self.assertIsNone(self.archive.state(self.telegram._menu_state_key(43)))
                self.assertNotEqual(self.archive.state('telegram_offset'), '2')

    def test_unconfirmed_edit_response_never_falls_back_or_advances_cursor(self):
        original = self.fake
        for response in (None, False, {}, {'message_id': 82}, {'message_id': '81'}, {'message_id': 81.0}):
            with self.subTest(response=response):
                def request(method, fields, **kwargs):
                    if method == 'editMessageText':
                        self.calls.append((method, fields, kwargs))
                        return response
                    return original(method, fields, **kwargs)
                self.request.side_effect = request
                self.calls.clear()
                with self.assertRaises(RuntimeError):
                    self.callback('today', message_id=81)
                self.assertEqual(sum(c[0] == 'editMessageText' for c in self.calls), 1)
                self.assertFalse(any(c[0] == 'sendMessage' for c in self.calls))
                self.assertIsNone(self.archive.state(self.telegram._menu_state_key(43)))
                self.assertNotEqual(self.archive.state('telegram_offset'), '2')

    def test_failed_menu_delivery_preserves_previous_navigation_route(self):
        self.archive.state(self.telegram._route_state_key(43), 'root')
        original = self.fake
        for method in ('editMessageText', 'sendMessage'):
            with self.subTest(method=method):
                def request(called, fields, **kwargs):
                    if called == method and 'inline_keyboard' in fields.get('reply_markup', {}):
                        self.calls.append((called, fields, kwargs))
                        return None
                    return original(called, fields, **kwargs)
                self.request.side_effect = request
                self.calls.clear()
                with self.assertRaises(RuntimeError):
                    if method == 'editMessageText':
                        self.callback('today', message_id=81)
                    else:
                        self.message('/today')
                self.assertEqual(self.telegram._menu_route(self.archive, 43), 'root')
                self.assertNotEqual(self.archive.state('telegram_offset'), '2')

    def test_boolean_message_id_in_edit_response_is_not_integer_confirmation(self):
        original = self.fake
        def request(method, fields, **kwargs):
            if method == 'editMessageText':
                self.calls.append((method, fields, kwargs))
                return {'message_id': True}
            return original(method, fields, **kwargs)
        self.request.side_effect = request
        with self.assertRaises(RuntimeError):
            self.callback('today', message_id=1)
        self.assertEqual(sum(c[0] == 'editMessageText' for c in self.calls), 1)
        self.assertFalse(any(c[0] == 'sendMessage' for c in self.calls))
        self.assertIsNone(self.archive.state(self.telegram._menu_state_key(43)))
        self.assertNotEqual(self.archive.state('telegram_offset'), '2')

    def test_sync_status_and_upload_actions_offer_only_one_back_to_original_parent(self):
        token = self.telegram.camera_token('front')
        parent = f'c:{token}:desc'
        self.callback(parent, message_id=81)
        for index, action in enumerate((f'sync:{token}', f'ss:{token}', f'up:{token}:0')):
            with self.subTest(action=action):
                self.calls.clear()
                self.callback(action, update_id=index * 2 + 2, message_id=81)
                panel = self.latest_edit()
                backs = [b for row in panel['reply_markup']['inline_keyboard'] for b in row
                         if 'Quay lại' in b['text'] or b['text'] == '↩ Camera']
                self.assertEqual(len(backs), 1)
                self.assertEqual(backs[0]['callback_data'], 'nav-return')
                self.assertEqual(self.telegram._menu_route(self.archive, 43), parent)
                self.calls.clear()
                self.callback('nav-return', update_id=index * 2 + 3, message_id=81)
                restored = self.latest_edit()
                self.assertIn('Chọn Năm', restored['text'])
                self.assertEqual(restored['message_id'], 81)
                self.assertEqual(self.telegram._menu_route(self.archive, 43), parent)
                callbacks = time_fixtures.TimeMenuTests.callbacks(restored['reply_markup']['inline_keyboard'])
                self.assertIn(f'y:{token}:2026:desc', callbacks)


if __name__ == '__main__':
    unittest.main()
