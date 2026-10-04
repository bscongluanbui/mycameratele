"""Native Telegram channel links; viewing never proxies or reposts media."""
import copy
import unittest
import urllib.parse
from unittest.mock import patch

import test_bot_controls as fixtures
from archive_app.telegram_player import PlayerDenied, TelegramPlayer


class NativeChannelViewTests(unittest.TestCase):
    tearDown = fixtures.BotControlTests.tearDown
    fake = fixtures.BotControlTests.fake
    message = fixtures.BotControlTests.message
    callback = fixtures.BotControlTests.callback
    row = fixtures.BotControlTests.row

    def setUp(self):
        fixtures.BotControlTests.setUp(self)
        self.settings.telegram_destination = 'channel'
        self.settings.storage_channel_id = -100123
        self.settings.player_public_url = 'https://camera.example.test'
        self.archive.state('telegram_bot_id', '900')
        # Generic delivery metadata can be historical/private. Native viewing
        # must use only the exact current channel placement, not these fields.
        self.archive.conn.execute('''UPDATE recordings SET bot_id=900,
            storage_kind='channel',storage_chat_id=-100123,storage_message_id=19,
            chat_id='42',message_id=1 WHERE key=?''', (self.key,))
        self.archive.conn.commit()

    def update(self, field, value):
        self.assertIn(field, {'bot_id', 'storage_kind', 'storage_chat_id',
                              'storage_message_id', 'media_type', 'deleted_at',
                              'status', 'chat_id', 'message_id'})
        self.archive.conn.execute(f'UPDATE recordings SET {field}=? WHERE key=?',
                                  (value, self.key))
        self.archive.conn.commit()

    def buttons(self):
        return [[{'text': '1. ▶ Xem', 'callback_data': 'v:'+self.prefix},
                 {'text': '1. ⬇ Tải', 'callback_data': 'f:'+self.prefix},
                 {'text': '1. 🗑 Xóa', 'callback_data': 'x:'+self.prefix}],
                [{'text': '⬇ Tải toàn bộ', 'callback_data': 'bd:front:2026-10-03:a'}],
                [{'text': '← Camera', 'callback_data': 'root'}]]

    def converted(self, actor=43):
        return self.telegram.player_buttons(self.archive, self.buttons(), actor)

    def assert_no_player_state(self):
        self.assertEqual(self.archive.conn.execute(
            "SELECT count(*) FROM state WHERE name LIKE 'telegram_player_%'").fetchone()[0], 0)

    def assert_no_media_api(self):
        self.assertFalse(any(method in ('getFile', 'sendVideo', 'sendDocument',
                                        'copyMessage', 'sendMediaGroup')
                             for method, _, _ in self.calls))

    def test_video_link_uses_exact_channel_placement_not_generic_delivery(self):
        result = self.converted()
        self.assertEqual(result[0][0], {'text': '1. ▶ Xem',
                                      'url': 'https://t.me/c/123/19?single&t=1'})
        self.assertNotIn('callback_data', result[0][0])
        self.assertNotIn(self.settings.token, result[0][0]['url'])
        self.assertNotIn('camera.example.test', result[0][0]['url'])
        self.assertEqual(self.calls, [])
        self.assert_no_player_state()

    def test_native_link_works_with_web_player_disabled(self):
        self.settings.player_public_url = ''
        self.assertEqual(self.converted()[0][0]['url'],
                         'https://t.me/c/123/19?single&t=1')
        self.assertEqual(self.calls, [])
        self.assert_no_player_state()

    def test_channel_mode_does_not_validate_or_use_external_player_url(self):
        self.settings.player_public_url = 'javascript:invalid-player-fixture'
        self.assertEqual(self.converted()[0][0]['url'],
                         'https://t.me/c/123/19?single&t=1')
        self.assert_no_player_state()

    def test_configured_channel_placement_is_native_even_with_legacy_destination(self):
        self.settings.telegram_destination = 'owner_private'
        self.assertEqual(self.converted()[0][0]['url'],
                         'https://t.me/c/123/19?single&t=1')
        self.assert_no_player_state()

    def test_document_link_opens_original_message_without_video_timestamp(self):
        self.update('media_type', 'document')
        self.assertEqual(self.converted()[0][0]['url'], 'https://t.me/c/123/19?single')
        self.assert_no_media_api()
        self.assert_no_player_state()

    def test_download_delete_bulk_and_navigation_callbacks_are_unchanged(self):
        before = self.buttons()
        result = self.telegram.player_buttons(self.archive, before, 43)
        self.assertEqual(result[0][1:], before[0][1:])
        self.assertEqual(result[1:], before[1:])
        self.assert_no_media_api()

    def test_conversion_does_not_mutate_original_or_shared_buttons(self):
        before = self.buttons()
        saved = copy.deepcopy(before)
        result = self.telegram.player_buttons(self.archive, before, 43)
        self.assertEqual(before, saved)
        self.assertIsNot(result, before)
        self.assertIsNot(result[0], before[0])
        result[0][1]['text'] = 'synthetic edited copy'
        self.assertEqual(before, saved)

    def test_existing_url_and_non_view_callback_are_not_reinterpreted(self):
        source = [[{'text': 'External help', 'url': 'https://example.test/help'},
                   {'text': 'v: label only', 'callback_data': 'home'}]]
        self.assertEqual(self.telegram.player_buttons(self.archive, source, 43), source)
        self.assert_no_player_state()

    def test_owner_and_each_allowlisted_viewer_can_receive_native_links(self):
        for actor in (42, 43, 44):
            with self.subTest(actor=actor):
                self.assertEqual(self.converted(actor)[0][0]['url'],
                                 'https://t.me/c/123/19?single&t=1')
        self.assert_no_player_state()

    def test_outsider_and_malformed_actor_ids_never_receive_native_links(self):
        for actor in (999, 0, -43, True, False, '43', None, 43.0):
            with self.subTest(actor=actor), self.assertRaises(ValueError):
                self.converted(actor)
        self.assertEqual(self.calls, [])
        self.assert_no_player_state()

    def test_deleted_record_cannot_receive_new_native_link(self):
        self.archive.soft_delete(self.key, 43)
        with self.assertRaises(ValueError):
            self.converted()
        self.assert_no_media_api()
        self.assert_no_player_state()

    def test_direct_helper_rejects_deleted_or_non_uploaded_rows(self):
        row = self.archive.find_recording(self.key)
        for changed in (dict(row, deleted_at=123), dict(row, deleted_at=0),
                        dict(row, status='downloaded'), dict(row, status=None)):
            with self.subTest(status=changed['status'], deleted=changed['deleted_at']), \
                    self.assertRaises(ValueError):
                self.telegram.native_video_link(self.archive, changed, 43)
        self.assert_no_media_api()
        self.assert_no_player_state()

    def test_restored_record_can_receive_native_link_again(self):
        self.archive.soft_delete(self.key, 43)
        self.archive.restore_recording(self.key, 44)
        self.assertEqual(self.converted()[0][0]['url'],
                         'https://t.me/c/123/19?single&t=1')

    def test_unknown_record_and_non_uploaded_status_are_rejected(self):
        with self.assertRaises(ValueError):
            self.telegram.player_buttons(self.archive,
                [[{'text': '▶ Xem', 'callback_data': 'v:'+'f'*32}]], 43)
        self.update('status', 'downloaded')
        with self.assertRaises(ValueError):
            self.converted()

    def test_other_bot_or_missing_bot_identity_is_rejected(self):
        for bot in (901, None, 0, -900):
            self.update('bot_id', bot)
            with self.subTest(bot=bot), self.assertRaises(ValueError):
                self.converted()
        self.update('bot_id', 900)
        self.archive.state('telegram_bot_id', '')
        with self.assertRaises(ValueError):
            self.converted()
        self.assertEqual(self.calls, [])

    def test_realistic_token_prefix_is_authoritative_not_stale_identity_state(self):
        self.settings.token = '900:synthetic-credential'
        self.archive.state('telegram_bot_id', '999')
        self.assertEqual(self.converted()[0][0]['url'],
                         'https://t.me/c/123/19?single&t=1')
        self.settings.token = '901:synthetic-credential'
        with self.assertRaises(ValueError):
            self.converted()
        self.assertEqual(self.calls, [])

    def test_changed_channel_and_owner_private_storage_are_rejected(self):
        self.settings.storage_channel_id = -100999
        with self.assertRaises(ValueError):
            self.converted()
        self.settings.storage_channel_id = -100123
        self.update('storage_kind', 'owner_private')
        with self.assertRaises(ValueError):
            self.converted()
        self.assert_no_player_state()

    def test_missing_exact_channel_placement_never_falls_back_to_generic_fields(self):
        self.update('chat_id', '-100123')
        self.update('message_id', 19)
        for field in ('storage_chat_id', 'storage_message_id'):
            prior = self.row()[field]
            self.update(field, None)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.converted()
            self.update(field, prior)

    def test_channel_id_format_and_type_are_strict(self):
        for value in (-123, -100, 100123, 0, True, False, '-100123',
                      None, -100123.0):
            self.settings.storage_channel_id = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.converted()
        self.assert_no_player_state()

    def test_storage_message_and_channel_boolean_string_float_ids_are_rejected(self):
        original = self.archive.find_recording(self.key)
        for field, values in [('storage_message_id', (True, False, '19', 19.0, 0, -19, None)),
                              ('storage_chat_id', (True, False, '-100123', -100123.0,
                                                   0, -123, -100999, None)),
                              ('bot_id', (True, False, '900', 900.0))]:
            for value in values:
                malformed = dict(original, **{field: value})
                with self.subTest(field=field, value=value), \
                        patch.object(self.archive, 'find_recording', return_value=malformed), \
                        self.assertRaises(ValueError):
                    self.converted()
        self.assertEqual(self.calls, [])

    def test_unknown_media_type_is_not_linked_as_a_video(self):
        for value in ('photo', '', None):
            self.update('media_type', value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.converted()

    def test_multiple_recording_rows_receive_distinct_original_message_links(self):
        first = self.archive.find_recording(self.key)
        second_key = 'b'*64 if self.key != 'b'*64 else 'a'*64
        second = dict(first, key=second_key, storage_message_id=20)
        originals = {self.prefix: first, second_key[:32]: second}
        source = [[{'text': '1. ▶ Xem', 'callback_data': 'v:'+self.prefix}],
                  [{'text': '2. ▶ Xem', 'callback_data': 'v:'+second_key[:32]}]]
        with patch.object(self.archive, 'find_recording',
                          side_effect=lambda key, **kwargs: originals.get(key) or
                          next((r for r in originals.values() if r['key'] == key), None)):
            result = self.telegram.player_buttons(self.archive, source, 43)
        self.assertEqual([line[0]['url'] for line in result],
                         ['https://t.me/c/123/19?single&t=1',
                          'https://t.me/c/123/20?single&t=1'])
        self.assert_no_player_state()

    def test_recent_poll_emits_native_buttons_and_no_media_or_capability(self):
        self.message('/recent')
        fields = next(fields for method, fields, _ in self.calls
                      if method == 'sendMessage' and
                      'inline_keyboard' in fields.get('reply_markup', {}))
        first = fields['reply_markup']['inline_keyboard'][0]
        self.assertEqual(first[0]['url'], 'https://t.me/c/123/19?single&t=1')
        self.assertEqual(first[1]['callback_data'], 'f:'+self.prefix)
        self.assertEqual(first[2]['callback_data'], 'x:'+self.prefix)
        self.assertEqual(self.archive.state('telegram_offset'), '2')
        self.assert_no_media_api()
        self.assert_no_player_state()

    def test_unauthorized_recent_poll_has_no_reply_or_link(self):
        self.message('/recent', actor=999)
        self.assertEqual([method for method, _, _ in self.calls], ['getUpdates'])
        self.assert_no_player_state()

    def test_day_and_time_selection_use_same_native_view_conversion(self):
        token = self.telegram.camera_token('front')
        for data in (f'd:{token}:2026-10-03:asc', 'recent:0'):
            with self.subTest(data=data):
                _, buttons = self.telegram.menu(self.archive, data, actor=43)
                converted = self.telegram.player_buttons(self.archive, buttons, 43)
                views = [b for line in converted for b in line if 'url' in b]
                self.assertEqual(views[0]['url'], 'https://t.me/c/123/19?single&t=1')
        self.assert_no_player_state()

    def test_nonchannel_blank_url_keeps_existing_callback_replay(self):
        self.settings.telegram_destination = 'owner_private'
        self.settings.storage_channel_id = 0
        self.settings.player_public_url = ''
        buttons = self.buttons()
        self.assertEqual(self.telegram.player_buttons(self.archive, buttons, 43), buttons)
        self.assert_no_player_state()

    def test_nonchannel_configured_player_keeps_existing_web_player(self):
        self.settings.telegram_destination = 'owner_private'
        self.settings.storage_channel_id = 0
        self.archive.mark_uploaded(self.key, 42, 1, 'fixture-file', 'fixture-unique',
                                   'video', bot_id=900)
        url = self.converted()[0][0]['url']
        self.assertTrue(url.startswith('https://camera.example.test/player/'))
        self.assertNotIn(self.settings.token, url)

    def test_disabling_web_player_revokes_previously_issued_capability(self):
        player = TelegramPlayer(self.settings, telegram=self.telegram)
        url = player.issue(self.archive, self.key, 43)
        cap = urllib.parse.urlsplit(url).path.split('/')[-1]
        self.assertEqual(player.resolve(self.archive, cap)['key'], self.key)
        self.settings.player_public_url = ''
        with self.assertRaises(PlayerDenied):
            player.resolve(self.archive, cap)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
