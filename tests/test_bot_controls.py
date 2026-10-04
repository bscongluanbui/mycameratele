"""End-to-end bot controls with synthetic SQLite/media and a fake API."""
import hashlib,json, os, shutil, tempfile, unittest, uuid
from pathlib import Path
from unittest.mock import patch
from archive_app.core import Archive, Settings
from archive_app.telegram import Telegram


class BotControlTests(unittest.TestCase):
    def setUp(self):
        self.parent=Path(__file__).resolve().parent if os.name=='nt' else Path(tempfile.gettempdir()).resolve()
        self.root=self.parent/('.tmp-controls-'+uuid.uuid4().hex);self.root.mkdir()
        (self.root/'input').mkdir()
        self.settings=Settings(self.root/'data',self.root/'cache',self.root/'input','UTC+07:00',
                               owner_user_id=42,allowed_users=(43,44),token='synthetic-token',min_free_bytes=0)
        self.archive=Archive(self.settings);self.telegram=Telegram(self.settings)
        self.source=self.settings.input_dir/'source.mp4';self.source.write_bytes(b'synthetic source')
        def normalize(source,dest,settings):
            dest.write_bytes(b'synthetic mp4');return {'duration':60,'codec_video':'h264','codec_audio':'aac','bytes':13}
        entry={'camera':'front','record_id':'fixture','path':str(self.source),
               'start_time':'2026-10-03T10:00:00+07:00','end_time':'2026-10-03T10:01:00+07:00'}
        with patch('archive_app.core.normalize',side_effect=normalize):row=self.archive.ingest_entry(entry)
        self.key=row['key'];self.prefix=self.key[:32]
        self.archive.mark_uploaded(self.key,42,1,'fixture-file','fixture-unique','video')
        self.calls=[];self.updates=[]
        self.request=patch.object(self.telegram,'request',side_effect=self.fake).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.archive.close();self.assertEqual(self.root.resolve().parent,self.parent);shutil.rmtree(self.root)

    def fake(self,method,fields,**kwargs):
        self.calls.append((method,fields,kwargs))
        if method=='getUpdates':return self.updates
        if method=='getMe':return {'id':900,'username':'fixture_archive_bot'}
        if method in ('sendVideo','sendDocument'):
            field='video' if method=='sendVideo' else 'document'
            return {'message_id':99,'chat':{'type':'private','id':fields['chat_id']},
                    field:{'file_id':'fixture-file','file_unique_id':'fixture-unique'}}
        if method in ('setMyCommands','setChatMenuButton','answerCallbackQuery'):return True
        if method=='sendMessage':return {'message_id':100}
        raise AssertionError(method)

    def row(self):return dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?',(self.key,)).fetchone())
    def callback(self,data,actor=43,update_id=1,chat_id=None):
        self.updates=[{'update_id':update_id,'callback_query':{'id':'fixture-callback','data':data,'from':{'id':actor},
                       'message':{'chat':{'type':'private','id':actor if chat_id is None else chat_id}}}}]
        self.telegram.poll(self.archive)
    def message(self,text,actor=43,update_id=1):
        self.updates=[{'update_id':update_id,'message':{'from':{'id':actor},'chat':{'type':'private','id':actor},'text':text}}]
        self.telegram.poll(self.archive)
    def confirmation(self,actor=43):
        _,buttons=self.telegram.deletion_menu(self.archive,self.prefix,actor)
        return buttons[0][0]['callback_data']

    def test_allowed_viewer_download_reuses_video_id_after_cache_removed(self):
        Path(self.row()['local_path']).unlink();before=self.row()
        self.callback('f:'+self.prefix)
        method,fields,kwargs=next(c for c in self.calls if c[0]=='sendVideo')
        self.assertEqual(fields['chat_id'],43);self.assertEqual(fields['video'],'fixture-file')
        self.assertIn('Save to Downloads',fields['caption']);self.assertEqual(kwargs,{})
        self.assertEqual(self.row(),before)

    def test_download_keeps_document_type(self):
        self.archive.conn.execute("UPDATE recordings SET media_type='document' WHERE key=?",(self.key,));self.archive.conn.commit()
        self.callback('f:'+self.prefix,actor=44)
        send=next(c for c in self.calls if c[0]=='sendDocument')
        self.assertEqual(send[1]['document'],'fixture-file');self.assertEqual(send[1]['chat_id'],44)
        self.assertFalse(any(c[0]=='sendVideo' for c in self.calls))

    def test_first_delete_click_only_prompts_with_short_lived_confirmation(self):
        before=self.row();self.callback('x:'+self.prefix)
        self.assertEqual(self.row(),before)
        fields=next(c[1] for c in self.calls if c[0]=='sendMessage')
        self.assertIn('Tất cả người được phép',fields['text'])
        confirmation=fields['reply_markup']['inline_keyboard'][0][0]['callback_data']
        self.assertTrue(confirmation.startswith('xc:'+self.prefix));self.assertLessEqual(len(confirmation.encode()),64)

    def test_confirm_delete_hides_shared_catalog_and_any_allowed_user_can_restore(self):
        confirm=self.confirmation();self.callback(confirm)
        self.assertIsNotNone(self.row()['deleted_at']);self.assertEqual(self.row()['deleted_by'],43)
        self.assertEqual(self.archive.browse()['total'],0);self.assertIsNone(self.archive.find_recording(self.prefix))
        self.callback('u:'+self.prefix,actor=44,update_id=2)
        self.assertIsNone(self.row()['deleted_at']);self.assertEqual(self.archive.browse()['total'],1)
        self.assertTrue(self.source.is_file());self.assertEqual(self.row()['file_id'],'fixture-file')

    def test_other_viewer_cannot_reuse_confirmation_nonce(self):
        confirmation=self.confirmation(43);self.callback(confirmation,actor=44)
        self.assertIsNone(self.row()['deleted_at'])

    def test_expired_confirmation_cannot_delete(self):
        confirmation=self.confirmation();pending=json.loads(self.archive.state('telegram_delete_confirm:43'))
        with patch('archive_app.telegram.time.time',return_value=pending['expires']+1):self.callback(confirmation)
        self.assertIsNone(self.row()['deleted_at'])

    def test_cancel_confirmation_keeps_recording(self):
        self.confirmation();self.callback('cancel-delete')
        self.assertEqual(self.archive.state('telegram_delete_confirm:43'),'{}');self.assertIsNone(self.row()['deleted_at'])

    def test_outsiders_and_wrong_private_chat_cannot_delete_or_download(self):
        confirmation=self.confirmation()
        for index,data in enumerate(('f:'+self.prefix,'x:'+self.prefix,confirmation,'u:'+self.prefix)):
            self.callback(data,actor=999,update_id=index+1)
        self.callback(confirmation,actor=43,chat_id=44,update_id=10)
        self.assertIsNone(self.row()['deleted_at'])
        self.assertTrue(all(c[0]=='getUpdates' for c in self.calls))

    def test_deleted_recording_rejects_old_play_download_and_deep_link(self):
        self.archive.soft_delete(self.key,43)
        self.callback('v:'+self.prefix)
        self.callback('f:'+self.prefix,update_id=2)
        self.message('/start play_'+self.prefix,update_id=3)
        self.assertFalse(any(c[0] in ('sendVideo','sendDocument') for c in self.calls))

    def test_trash_button_offers_restore_not_download(self):
        self.archive.soft_delete(self.key,43);text,buttons=self.telegram.menu(self.archive,'trash:0')
        self.assertIn('1 video',text)
        callbacks=[b['callback_data'] for row in buttons for b in row]
        self.assertIn('u:'+self.prefix,callbacks);self.assertNotIn('f:'+self.prefix,callbacks)

    def test_start_shows_persistent_time_keyboard(self):
        self.message('/start')
        fields=next(c[1] for c in self.calls if c[0]=='sendMessage' and 'keyboard' in c[1].get('reply_markup',{}))
        keyboard=fields['reply_markup'];self.assertTrue(keyboard['is_persistent'])
        texts=[b['text'] for row in keyboard['keyboard'] for b in row]
        self.assertTrue({'🏠 Start','▶ Start sync','📅 Hôm nay','📆 Hôm qua','🕕 6 giờ trước','🗓 Tùy chọn thời gian'}.issubset(texts))
        inline=next(c[1]['reply_markup']['inline_keyboard'] for c in self.calls if c[0]=='sendMessage' and 'inline_keyboard' in c[1].get('reply_markup',{}))
        self.assertTrue(all(len(row)==1 for row in inline))
        self.assertIn('custom-time',[row[0]['callback_data'] for row in inline])

    def test_existing_viewer_gets_new_start_keyboard_from_normal_command_once(self):
        self.message('/status')
        keyboards=[c[1] for c in self.calls if c[0]=='sendMessage' and 'keyboard' in c[1].get('reply_markup',{})]
        self.assertEqual(len(keyboards),1)
        self.calls.clear();self.message('/today',update_id=2)
        self.assertFalse(any(c[0]=='sendMessage' and 'keyboard' in c[1].get('reply_markup',{}) for c in self.calls))
        self.calls.clear();self.message('🏠 Start',update_id=3)
        home=next(c[1] for c in self.calls if c[0]=='sendMessage' and 'inline_keyboard' in c[1].get('reply_markup',{}))
        self.assertTrue(all(len(row)==1 for row in home['reply_markup']['inline_keyboard']))
        self.assertTrue(any(c[0]=='sendMessage' and 'keyboard' in c[1].get('reply_markup',{}) for c in self.calls))

    def test_custom_time_text_flow_selects_camera_then_video_and_cancel_returns_home(self):
        self.message('/time')
        self.assertTrue(any('Từ lúc nào' in c[1].get('text','') for c in self.calls))
        self.message('03/10/2026 09:59',update_id=2)
        self.assertTrue(any('Đến lúc nào' in c[1].get('text','') for c in self.calls))
        self.calls.clear();self.message('03/10/2026 10:02',update_id=3)
        fields=next(c[1] for c in self.calls if c[0]=='sendMessage')
        selection=next(b['callback_data'] for row in fields['reply_markup']['inline_keyboard'] for b in row if b['callback_data'].startswith('wqc:'))
        self.calls.clear();self.callback(selection,update_id=4)
        fields=next(c[1] for c in self.calls if c[0]=='sendMessage')
        self.assertIn('v:'+self.prefix,[b['callback_data'] for row in fields['reply_markup']['inline_keyboard'] for b in row])
        self.message('/time',update_id=5)
        self.calls.clear();self.callback('cancel-time',update_id=6)
        self.assertEqual(self.archive.state('telegram_time_selection:43'),'{}')
        fields=next(c[1] for c in self.calls if c[0]=='sendMessage')
        self.assertIn('Menu',fields['text'])

    def test_custom_time_rejects_unlisted_or_wrong_private_chat_without_state_or_reply(self):
        self.message('/time',actor=999)
        self.callback('custom-time',actor=43,chat_id=44,update_id=2)
        self.assertTrue(all(c[0]=='getUpdates' for c in self.calls))
        self.assertIsNone(self.archive.state('telegram_time_selection:999'))
        self.assertIsNone(self.archive.state('telegram_time_selection:43'))

    def test_keyboard_post_failure_does_not_repeat_completed_action_or_pin_cursor(self):
        original=self.fake
        def request(method,fields,**kwargs):
            if method=='sendMessage' and 'keyboard' in fields.get('reply_markup',{}):
                raise TimeoutError('synthetic keyboard timeout')
            return original(method,fields,**kwargs)
        self.request.side_effect=request
        self.message('/today')
        self.assertEqual(self.archive.state('telegram_offset'),'2')
        self.assertEqual(sum(c[0]=='sendMessage' for c in self.calls),1)

    def test_pending_custom_input_plain_aliases_retain_normal_menu_actions(self):
        aliases=('Hôm nay','Hôm qua','6 giờ trước','6 giờ gần nhất','Start sync',
                 'Tùy chọn thời gian','Start','Hủy')
        for index,label in enumerate(aliases):
            with self.subTest(label=label):
                self.message('/time',update_id=index*10+1)
                self.calls.clear();self.message(label,update_id=index*10+2)
                replies=[c[1]['text'] for c in self.calls if c[0]=='sendMessage']
                self.assertFalse(any('Ngày giờ chưa đúng' in text for text in replies))
                if label=='Tùy chọn thời gian':
                    self.assertEqual(json.loads(self.archive.state('telegram_time_selection:43'))['step'],'start')
                    self.assertTrue(any('Từ lúc nào' in text for text in replies))
                else:
                    self.assertEqual(self.archive.state('telegram_time_selection:43'),'{}')
                if label=='Start sync':self.assertTrue(any('Đã tiếp nhận sync' in text for text in replies))

    def test_compact_command_labels_refresh_cached_v4_and_schema_changes(self):
        old='telegram_commands_v4:'+hashlib.sha256(self.settings.token.encode()).hexdigest()[:16]
        self.archive.state(old,'1')
        self.assertTrue(self.telegram.register_commands(self.archive))
        sent=[c for c in self.calls if c[0]=='setMyCommands']
        self.assertEqual(len(sent),1)
        self.assertEqual(next(c['description'] for c in sent[0][1]['commands'] if c['command']=='today'),'Hôm nay')
        self.assertTrue(self.telegram.register_commands(self.archive))
        self.assertEqual(len([c for c in self.calls if c[0]=='setMyCommands']),1)
        with patch('archive_app.telegram.TimeMenus.commands',return_value=[{'command':'today','description':'Hôm nay mới'}]):
            self.telegram._menu_retry_at=0
            self.assertTrue(self.telegram.register_commands(self.archive))
        self.assertEqual(len([c for c in self.calls if c[0]=='setMyCommands']),2)

    def test_command_and_text_shortcuts_choose_camera_before_videos(self):
        for index,label in enumerate(('/today','/yesterday','/last6h','📅 Hôm nay','📆 Hôm qua','🕕 6 giờ trước')):
            self.calls.clear()
            with patch('archive_app.telegram_menu.time.time',return_value=1791001800):self.message(label,update_id=index+1)
            fields=next(c[1] for c in self.calls if c[0]=='sendMessage')
            callbacks=[b['callback_data'] for row in fields['reply_markup']['inline_keyboard'] for b in row]
            self.assertFalse(any(c.startswith(('v:','f:','x:')) for c in callbacks))
            self.assertIn('Camera',fields['text']) if any(c.startswith('wc:') for c in callbacks) else self.assertIn('Chưa có video',fields['text'])

    def test_registers_native_command_menu_once_per_bot_token(self):
        self.assertTrue(self.telegram.register_commands(self.archive));self.assertTrue(self.telegram.register_commands(self.archive))
        commands=[c for c in self.calls if c[0]=='setMyCommands'];self.assertEqual(len(commands),1)
        self.assertTrue({'today','yesterday','last6h','trash'}.issubset({c['command'] for c in commands[0][1]['commands']}))
        self.assertEqual(next(c[1] for c in self.calls if c[0]=='setChatMenuButton')['menu_button'],{'type':'commands'})

    def test_failed_menu_registration_is_not_marked_ready_or_rapidly_retried(self):
        self.request.side_effect=TimeoutError('synthetic failure')
        with self.assertRaises(TimeoutError):self.telegram.register_commands(self.archive)
        count=self.request.call_count;self.assertFalse(self.telegram.register_commands(self.archive))
        self.assertEqual(self.request.call_count,count)
