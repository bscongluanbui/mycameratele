"""Bot integration: direct player buttons, day ranges and queued all-page sends."""
import json
import unittest
from unittest.mock import patch

import test_bot_controls as fixtures
from archive_app.telegram_menu import TimeMenus


class PlayerBulkControlTests(unittest.TestCase):
    setUp=fixtures.BotControlTests.setUp
    tearDown=fixtures.BotControlTests.tearDown
    fake=fixtures.BotControlTests.fake
    message=fixtures.BotControlTests.message
    callback=fixtures.BotControlTests.callback

    def test_player_urls_replace_only_view_buttons_without_media_posts(self):
        self.settings.player_public_url='https://camera.example.test'
        self.archive.state('telegram_bot_id','900')
        self.archive.conn.execute('UPDATE recordings SET bot_id=900 WHERE key=?',(self.key,))
        self.archive.conn.commit()
        self.message('/recent')
        sent=next(c[1] for c in self.calls if c[0]=='sendMessage')
        buttons=sent['reply_markup']['inline_keyboard']
        self.assertIn('url',buttons[0][0])
        self.assertTrue(buttons[0][0]['url'].startswith('https://camera.example.test/player/'))
        self.assertNotIn('callback_data',buttons[0][0])
        self.assertEqual(buttons[0][1]['callback_data'],'f:'+self.prefix)
        self.assertEqual(buttons[0][2]['callback_data'],'x:'+self.prefix)
        self.assertFalse(any(c[0] in ('sendVideo','sendDocument','copyMessage','getFile') for c in self.calls))
        self.assertNotIn(self.settings.token,buttons[0][0]['url'])

    def test_day_range_enqueues_once_and_poll_does_not_post_album(self):
        menus=TimeMenus(self.telegram)
        menus.begin(self.archive,43);menus.accept(self.archive,43,'03/10/26')
        _,cameras=menus.accept(self.archive,43,'03/10/26')
        data=next(b['callback_data'] for row in cameras for b in row if b.get('callback_data','').startswith('bwq:'))
        self.archive.state('telegram_bot_id','900')
        self.archive.conn.execute('UPDATE recordings SET bot_id=900 WHERE key=?',(self.key,));self.archive.conn.commit()
        self.callback(data)
        jobs=self.archive.conn.execute('SELECT id,total FROM telegram_bulk_jobs').fetchall()
        self.assertEqual(len(jobs),1);self.assertEqual(jobs[0]['total'],1)
        self.assertFalse(any(c[0] in ('sendMediaGroup','sendVideo','sendDocument','copyMessage') for c in self.calls))
        self.callback(data)
        self.assertEqual(self.archive.conn.execute('SELECT count(*) FROM telegram_bulk_jobs').fetchone()[0],1)
        self.calls.clear();self.callback('bulk-status:'+jobs[0]['id'],update_id=2)
        self.assertTrue(any(c[0]=='sendMessage' for c in self.calls))
        self.callback('bulk-cancel:'+jobs[0]['id'],update_id=3)
        state=self.archive.conn.execute('SELECT state,cancel_requested FROM telegram_bulk_jobs').fetchone()
        self.assertTrue(state['state']=='cancelled' or state['cancel_requested']==1)

    def test_week_commands_exit_date_wizard_and_show_week_selection(self):
        for i,command in enumerate(('/thisweek','/lastweek','📅 Tuần này','📆 Tuần trước')):
            self.message('/time',update_id=i*2+1)
            self.calls.clear();self.message(command,update_id=i*2+2)
            sent=next(c[1] for c in self.calls if c[0]=='sendMessage')
            self.assertIn('Tuần',sent['text'])
            self.assertNotIn('Ngày giờ chưa đúng',sent['text'])
            self.assertNotIn('step',json.loads(self.archive.state('telegram_time_selection:43')))

    def test_calendar_day_list_has_download_all_and_resolves_same_day(self):
        token=self.telegram.camera_token('front')
        _,buttons=self.telegram.menu(self.archive,f'd:{token}:2026-10-03:asc',actor=43)
        data=next(b['callback_data'] for row in buttons for b in row if b.get('callback_data','').startswith('bd:'))
        selection=TimeMenus(self.telegram).download_window(self.archive,data,43)
        self.assertEqual(selection['camera'],'front')
        self.assertEqual(selection['order'],'asc')
        self.assertEqual(selection['end_ms']-selection['start_ms'],86400000)
        self.assertEqual(selection['label'],'03/10/26')

    def test_stale_empty_bulk_selection_returns_short_empty_menu(self):
        token=self.telegram.camera_token('front')
        self.callback(f'bd:{token}:2026-10-01:a')
        text=next(c[1]['text'] for c in self.calls if c[0]=='sendMessage')
        self.assertEqual(text,'Chưa có video.')

    def test_unallowed_user_never_creates_bulk_job_or_player_capability(self):
        self.settings.player_public_url='https://camera.example.test'
        self.callback('bw:t:1791001800:all:a',actor=999)
        self.assertFalse(any(c[0]=='sendMessage' for c in self.calls))
        self.assertEqual(self.archive.conn.execute("SELECT count(*) FROM state WHERE name LIKE 'telegram_player_cap:%'").fetchone()[0],0)


if __name__=='__main__':unittest.main()
