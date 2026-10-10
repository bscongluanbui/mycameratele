"""One existing bot browses immutable placements in legacy and camera channels."""
import copy
import unittest
from unittest.mock import patch

import test_bot_controls as controls
import test_bulk_download as bulk_fixtures


class MultiChannelBotTests(unittest.TestCase):
    tearDown = controls.BotControlTests.tearDown
    fake = controls.BotControlTests.fake
    message = controls.BotControlTests.message
    callback = controls.BotControlTests.callback
    row = controls.BotControlTests.row

    def setUp(self):
        controls.BotControlTests.setUp(self)
        self.settings.multi_channel_routing=True
        self.settings.telegram_destination='channel'
        self.settings.storage_channel_id=0
        self.settings.token='900:synthetic-token'
        with self.archive.conn:
            self.archive.conn.execute('''UPDATE recordings SET bot_id=900,
                storage_kind='channel',storage_chat_id=-100123,storage_message_id=19
                WHERE key=?''',(self.key,))

    def test_native_play_uses_original_chat_with_no_global_destination(self):
        self.assertEqual(self.telegram.native_video_link(self.archive,self.row(),43),
                         'https://t.me/c/123/19?single&t=1')
        self.assertEqual(self.calls,[])

    def test_legacy_record_link_survives_mapping_or_default_channel_change(self):
        self.settings.storage_channel_id=-100999
        self.assertEqual(self.telegram.native_video_link(self.archive,self.row(),43),
                         'https://t.me/c/123/19?single&t=1')

    def test_different_camera_channels_have_distinct_native_links(self):
        one=self.row()
        two=dict(one,key='b'*64,storage_chat_id=-100456,storage_message_id=87)
        rows={one['key'][:32]:one,two['key'][:32]:two}
        buttons=[[{'text':'Cam 1','callback_data':'v:'+one['key'][:32]},
                  {'text':'Cam 2','callback_data':'v:'+two['key'][:32]}]]
        with patch.object(self.archive,'find_recording',side_effect=lambda key:rows[key]):
            converted=self.telegram.player_buttons(self.archive,buttons,43)
        self.assertEqual([b['url'] for b in converted[0]],
                         ['https://t.me/c/123/19?single&t=1','https://t.me/c/456/87?single&t=1'])
        self.assertEqual(self.calls,[])

    def test_missing_actual_placement_has_no_global_or_generic_fallback(self):
        self.settings.storage_channel_id=-100123
        for field in ('storage_chat_id','storage_message_id'):
            with self.subTest(field=field),self.assertRaises(ValueError):
                self.telegram.native_video_link(self.archive,dict(self.row(),**{field:None}),43)

    def test_malformed_chat_or_message_is_not_turned_into_url(self):
        row=self.row()
        for chat in (-123,-100,0,True,'-100123',None,-100123.0):
            with self.subTest(chat=chat),self.assertRaises(ValueError):
                self.telegram.native_video_link(self.archive,dict(row,storage_chat_id=chat),43)
        for message in (0,-1,True,'19',None,19.0):
            with self.subTest(message=message),self.assertRaises(ValueError):
                self.telegram.native_video_link(self.archive,dict(row,storage_message_id=message),43)

    def test_other_bot_or_unallowed_viewer_cannot_get_native_links(self):
        for bot in (None,901,True,'900'):
            with self.subTest(bot=bot),self.assertRaises(ValueError):
                self.telegram.native_video_link(self.archive,dict(self.row(),bot_id=bot),43)
        with self.assertRaises(ValueError):
            self.telegram.native_video_link(self.archive,self.row(),999)

    def test_play_conversion_preserves_download_delete_bulk_and_back(self):
        source=[[{'text':'Xem','callback_data':'v:'+self.prefix},
                 {'text':'Tải','callback_data':'f:'+self.prefix},
                 {'text':'Xóa','callback_data':'x:'+self.prefix}],
                [{'text':'Tải tất cả','callback_data':'bd:front:2026-10-03:a'},
                 {'text':'Quay lại','callback_data':'home'}]]
        before=copy.deepcopy(source)
        result=self.telegram.player_buttons(self.archive,source,43)
        self.assertEqual(result[0][1:],before[0][1:])
        self.assertEqual(result[1:],before[1:])
        self.assertEqual(source,before)

    def test_owner_channel_setup_explains_per_camera_mapping(self):
        text,_=self.telegram.channel_setup(42)
        self.assertIn('Mỗi camera',text)
        self.assertIn('Community',text)
        self.assertNotIn('TELEGRAM_STORAGE_CHANNEL_ID',text)
        self.assertEqual(self.settings.storage_channel_id,0)
        self.assertEqual(self.calls,[])

    def test_owner_forward_reports_channel_without_changing_mapping(self):
        text,_=self.telegram.channel_setup(42,{'forward_origin':{'type':'channel',
                                                    'chat':{'type':'channel','id':-100777}}})
        self.assertIn('Channel ID: -100777',text)
        self.assertIn('Dashboard',text)
        self.assertEqual(self.settings.storage_channel_id,0)
        self.assertEqual(self.calls,[])

    def test_allowlisted_viewer_cannot_administer_channel_setup(self):
        with self.assertRaises(ValueError):self.telegram.channel_setup(43)

    def test_rebuild_default_and_explicit_dry_run_do_not_enqueue(self):
        for words in (['/rebuild_index','front','2026-10'],
                      ['/rebuild_index','front','2026-10','--dry-run']):
            with self.subTest(words=words),patch('archive_app.channel_index.ChannelIndex') as cls:
                cls.return_value.rebuild.return_value={'dry_run':True,'recordings':1,'dates':['2026-10-03'],'jobs':4}
                text,_=self.telegram.rebuild_index(self.archive,42,words)
                cls.assert_called_once_with(self.archive,self.telegram)
                cls.return_value.rebuild.assert_called_once_with('front','2026-10',dry_run=True)
                self.assertIn('Xem trước',text)
        self.assertEqual(self.calls,[])

    def test_rebuild_apply_only_schedules_when_owner_explicit(self):
        with patch('archive_app.channel_index.ChannelIndex') as cls:
            cls.return_value.rebuild.return_value={'dry_run':False,'recordings':1,'dates':['2026-10-03'],'jobs':4}
            text,_=self.telegram.rebuild_index(self.archive,42,['/rebuild_index','front','2026-10','--apply'])
            cls.return_value.rebuild.assert_called_once_with('front','2026-10',dry_run=False)
            self.assertIn('Đã xếp hàng',text)
        self.assertEqual(self.calls,[])

    def test_nonowner_rebuild_is_rejected_before_index_instantiation(self):
        with patch('archive_app.channel_index.ChannelIndex') as cls:
            for actor in (43,44,999,0,True,'42'):
                with self.subTest(actor=actor),self.assertRaises(ValueError):
                    self.telegram.rebuild_index(self.archive,actor,['/rebuild_index','front','2026-10','--apply'])
            cls.assert_not_called()

    def test_bad_rebuild_period_camera_flags_and_extra_input_are_rejected(self):
        arguments=(['front','2026-13'],['front','2026-02-30'],['front','0000'],
                   ['missing','2026-10'],['front','2026-10','--force'],
                   ['front','2026-10','--apply','extra'],['front'])
        with patch('archive_app.channel_index.ChannelIndex') as cls:
            for args in arguments:
                with self.subTest(args=args),self.assertRaises(ValueError):
                    self.telegram.rebuild_index(self.archive,42,['/rebuild_index',*args])
            cls.assert_not_called()

    def test_rebuild_command_poll_dispatches_owner_only_and_advances(self):
        with patch.object(self.telegram,'rebuild_index',return_value=('Xem trước',[])) as rebuild:
            self.message('/rebuild_index front 2026-10',actor=42)
            rebuild.assert_called_once_with(self.archive,42,['/rebuild_index','front','2026-10'])
        self.assertEqual(self.archive.state('telegram_offset'),'2')

    def test_camera_menu_only_exposes_verified_real_index_url(self):
        camera=self.archive.cameras()[0]
        token=self.telegram.camera_token(camera['id'])
        good='https://t.me/c/123/55'
        with patch.object(self.archive,'cameras',return_value=[dict(camera,channel_index_url=good)]):
            _,buttons=self.telegram.menu(self.archive,f'c:{token}:asc',actor=43)
        self.assertIn(good,[b.get('url') for line in buttons for b in line])
        for bad in (None,'','https://example.test/token','javascript:bad','https://t.me/c/123/0'):
            with self.subTest(url=bad),patch.object(self.archive,'cameras',return_value=[dict(camera,channel_index_url=bad)]):
                _,buttons=self.telegram.menu(self.archive,f'c:{token}:asc',actor=43)
                self.assertFalse(any('url' in b for line in buttons for b in line))

    def test_channel_membership_update_populates_directory_without_private_reply(self):
        from archive_app.channel_directory import ChannelDirectory
        self.updates=[{'update_id':1,'my_chat_member':{
            'chat':{'id':-100777,'type':'channel','title':'Camera Phòng khách'},
            'new_chat_member':{'status':'administrator','can_post_messages':True,
                               'can_edit_messages':True,'user':{'id':900,'is_bot':True}}}}]
        self.telegram.poll(self.archive)
        options=ChannelDirectory(self.archive).list()
        option=next(c for c in options if c['chat_id']==-100777)
        self.assertEqual(option['name'],'Camera Phòng khách')
        self.assertTrue(option['private'])
        self.assertFalse(option['ready'])  # Events alone do not verify readiness.
        self.assertEqual(self.archive.state('telegram_offset'),'2')
        self.assertEqual([c[0] for c in self.calls],['getUpdates'])
        self.assertIn('my_chat_member',self.calls[0][1]['allowed_updates'])
        self.assertIn('channel_post',self.calls[0][1]['allowed_updates'])

    def test_channel_post_titles_and_private_menu_are_processed_in_same_batch(self):
        from archive_app.channel_directory import ChannelDirectory
        self.updates=[{'update_id':1,'channel_post':{'message_id':50,
            'chat':{'id':-100777,'type':'channel','title':'Camera Sân'},'text':'fixture'}},
            {'update_id':2,'message':{'from':{'id':43},'chat':{'id':43,'type':'private'},'text':'/today'}}]
        self.telegram.poll(self.archive)
        self.assertEqual(next(c for c in ChannelDirectory(self.archive).list() if c['chat_id']==-100777)['name'],'Camera Sân')
        self.assertEqual(self.archive.state('telegram_offset'),'3')
        replies=[c for c in self.calls if c[0]=='sendMessage']
        self.assertTrue(replies)
        self.assertTrue(all(c[1]['chat_id']==43 for c in replies))

    def test_directory_only_batch_can_recover_lower_update_cursor_after_backend_change(self):
        from archive_app.channel_directory import ChannelDirectory
        self.archive.state('telegram_offset','900')
        self.archive.state('telegram_poll_backend','old-synthetic-backend')
        self.updates=[{'update_id':1,'channel_post':{'message_id':50,
            'chat':{'id':-100777,'type':'channel','title':'Camera Sân'},'text':'fixture'}}]
        self.telegram.poll(self.archive)
        self.assertEqual(self.archive.state('telegram_offset'),'2')
        self.assertTrue(any(c['chat_id']==-100777 for c in ChannelDirectory(self.archive).list()))

    def test_old_directory_update_is_not_applied_twice(self):
        from archive_app.channel_directory import ChannelDirectory
        self.archive.state('telegram_offset','2')
        self.archive.state('telegram_poll_backend',self.telegram._poll_backend(self.archive))
        self.updates=[{'update_id':1,'channel_post':{'message_id':50,
            'chat':{'id':-100777,'type':'channel','title':'Old camera title'},'text':'fixture'}}]
        self.telegram.poll(self.archive)
        self.assertFalse(any(c['chat_id']==-100777 for c in ChannelDirectory(self.archive).list()))
        self.assertEqual(self.archive.state('telegram_offset'),'2')


class MultiChannelBulkTests(unittest.TestCase):
    recording=bulk_fixtures.BulkDownloadTests.recording
    response=bulk_fixtures.BulkDownloadTests.response
    request=bulk_fixtures.BulkDownloadTests.request
    enqueue=bulk_fixtures.BulkDownloadTests.enqueue
    process=bulk_fixtures.BulkDownloadTests.process
    state=bulk_fixtures.BulkDownloadTests.state

    def setUp(self):
        bulk_fixtures.BulkDownloadTests.setUp(self)
        self.settings.multi_channel_routing=True

    def test_mixed_channel_album_uses_file_ids_without_vps_media(self):
        first=self.recording(0,camera='front')
        second=self.recording(1,camera='back')
        with self.archive.conn:
            self.archive.conn.execute('UPDATE recordings SET storage_chat_id=-100456 WHERE key=?',(second,))
        selection=dict(self.selection,camera=None)
        job=self.enqueue(selection=selection)
        self.assertEqual(job['total'],2)
        self.process()
        calls=[c for c in self.calls if c[0]=='sendMediaGroup']
        self.assertEqual(len(calls),1)
        self.assertEqual([v['media'] for v in calls[0][1]['media']],['file-0','file-1'])
        self.assertEqual(self.state(job)['state'],'done')

    def test_snapshot_validity_does_not_depend_on_latest_global_channel(self):
        self.recording(0)
        job=self.enqueue()
        item=dict(self.archive.conn.execute('SELECT * FROM telegram_bulk_items WHERE job_id=?',(job['id'],)).fetchone())
        self.settings.storage_channel_id=-100555
        self.assertIsNotNone(self.bulk._valid_row(self.archive,item))

    def test_snapshot_still_rejects_mutated_placement_other_bot_or_deleted(self):
        key=self.recording(0)
        job=self.enqueue()
        item=dict(self.archive.conn.execute('SELECT * FROM telegram_bulk_items WHERE job_id=?',(job['id'],)).fetchone())
        original=dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?',(key,)).fetchone())
        for field,value in (('storage_chat_id',-100456),('bot_id',901),('deleted_at',123)):
            with self.subTest(field=field),self.archive.conn:
                self.archive.conn.execute(f'UPDATE recordings SET {field}=? WHERE key=?',(value,key))
                self.assertIsNone(self.bulk._valid_row(self.archive,item))
                self.archive.conn.execute(f'UPDATE recordings SET {field}=? WHERE key=?',(original[field],key))


if __name__=='__main__':
    unittest.main()
