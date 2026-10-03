"""Private owner uploads and allowlisted viewer replay; all APIs are mocked."""
import copy
import json
import os
import shutil
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from archive_app.core import Archive, Settings, record_key
from archive_app.telegram import ApiRejected, Telegram


class PrivateTelegramTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent if os.name=='nt' else Path(tempfile.gettempdir()).resolve()
        self.root = self.parent/('.tmp-private-bot-'+uuid.uuid4().hex)
        self.root.mkdir()
        self.input=self.root/'input'
        self.input.mkdir()
        self.source=self.input/'source.mp4'
        self.source.write_bytes(b'synthetic-source')
        self.settings=Settings(self.root/'state',self.root/'cache',self.input,'UTC+07:00',
                               keep_cache=False,enable_upload=True,token='synthetic-bot-token',
                               chat_id='42',owner_user_id=42,allowed_users=(42,43,44),min_free_bytes=0)
        self.archive=Archive(self.settings)
        self.telegram=Telegram(self.settings)
        self.normalize=patch('archive_app.core.normalize',side_effect=self.fake_normalize)
        self.normalize.start()
        self.request=patch.object(self.telegram,'request').start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.archive.close()
        self.assertEqual(self.root.resolve().parent,self.parent)
        shutil.rmtree(self.root)

    @staticmethod
    def fake_normalize(source,destination,settings):
        destination=Path(destination)
        destination.write_bytes(b'synthetic-normalized')
        return {'duration':60,'codec_video':'h264','codec_audio':'aac','bytes':destination.stat().st_size}

    def ingest(self,minute=0):
        start=datetime.fromisoformat('2026-10-03T10:00:00+07:00')+timedelta(minutes=minute)
        entry={'camera':'front','record_id':uuid.uuid4().hex,'path':str(self.source),
               'start_time':start.isoformat(),'end_time':(start+timedelta(minutes=1)).isoformat()}
        self.archive.ingest_entry(entry)
        return record_key(entry)

    def row(self,key):
        return dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?',(key,)).fetchone())

    def confirmed(self,minute=0,media_type='video',chat_id=42):
        key=self.ingest(minute)
        self.archive.mark_uploaded(key,chat_id,17,'saved-file-'+key[:8],
                                   file_unique_id='saved-unique-'+key[:8],media_type=media_type)
        return key

    @staticmethod
    def media_response(chat_id=42,field='video'):
        return {'chat':{'id':chat_id,'type':'private'},'message_id':91,
                field:{'file_id':'returned-file-id','file_unique_id':'returned-unique-id'}}

    @staticmethod
    def message(actor,text,update_id=1,chat_id=None,chat_type='private'):
        return {'update_id':update_id,'message':{'from':{'id':actor},
                'chat':{'id':actor if chat_id is None else chat_id,'type':chat_type},'text':text}}

    @staticmethod
    def callback(actor,data,update_id=1,chat_id=None,chat_type='private'):
        return {'update_id':update_id,'callback_query':{'id':f'callback-{update_id}','from':{'id':actor},
                'data':data,'message':{'chat':{'id':actor if chat_id is None else chat_id,'type':chat_type}}}}

    def poll_updates(self,updates,get_me=None):
        calls=[]
        def fake(method,fields,**kwargs):
            calls.append((method,copy.deepcopy(fields),kwargs))
            if method=='getUpdates':return updates
            if method=='getMe':
                if isinstance(get_me,Exception):raise get_me
                return get_me or {'id':900,'is_bot':True,'username':'FixtureArchive_bot'}
            if method in ('sendVideo','sendDocument'):
                return self.media_response(fields['chat_id'],'video' if method=='sendVideo' else 'document')
            return {'message_id':92}
        self.request.side_effect=fake
        self.telegram.poll(self.archive)
        return calls

    def test_upload_waits_for_owner_start_without_claiming(self):
        key=self.ingest()
        self.assertIsNone(self.telegram.upload_one(self.archive))
        self.assertEqual(self.row(key)['status'],'downloaded')
        self.assertIsNone(self.row(key)['attempt_id'])
        self.request.assert_not_called()

    def test_viewer_start_never_enables_owner_upload_but_owner_start_does(self):
        key=self.ingest()
        self.poll_updates([self.message(43,'/start')])
        self.assertIsNone(self.archive.state('telegram_owner_started:42'))
        self.assertIsNone(self.archive.state('telegram_owner_started:43'))
        self.assertIsNone(self.telegram.upload_one(self.archive))
        self.poll_updates([self.message(42,'/start',2)])
        self.assertEqual(self.archive.state('telegram_owner_started:42'),'1')
        self.request.side_effect=None
        self.request.return_value=self.media_response(field='document')
        self.assertEqual(self.telegram.upload_one(self.archive),'uploaded')
        self.assertEqual(self.request.call_args.args[1]['chat_id'],42)
        self.assertEqual(self.row(key)['chat_id'],'42')

    def test_owner_is_authorized_even_when_direct_settings_allowlist_omits_it(self):
        self.settings.allowed_users=(43,)
        calls=self.poll_updates([self.message(42,'/start')])
        self.assertEqual(self.archive.state('telegram_owner_started:42'),'1')
        self.assertTrue(any(method=='sendMessage' and fields['chat_id']==42 for method,fields,_ in calls))

    def test_reject_nonlist_users_groups_channels_cross_chat_and_inline_callbacks(self):
        inline={'update_id':6,'callback_query':{'id':'inline','from':{'id':43},'data':'root','inline_message_id':'fixture'}}
        updates=[self.message(45,'/start',1),self.message(43,'/archive',2,chat_id=-10,chat_type='group'),
                 self.message(42,'/start',3,chat_id=-10010,chat_type='channel'),
                 self.message(43,'/start',4,chat_id=42),self.callback(43,'root',5,chat_id=44),inline]
        calls=self.poll_updates(updates)
        self.assertEqual([method for method,_,_ in calls],['getUpdates'])
        self.assertIsNone(self.archive.state('telegram_owner_started:42'))
        self.assertEqual(self.archive.state('telegram_offset'),'7')

    def test_all_allowlisted_viewers_can_browse_their_own_private_chat(self):
        calls=self.poll_updates([self.message(43,'/archive'),self.message(44,'/recent',2),self.message(42,'/status',3)])
        replies=[fields for method,fields,_ in calls if method=='sendMessage']
        self.assertEqual([r['chat_id'] for r in replies],[43,44,42])
        self.assertIn('Camera',replies[0]['text'])
        self.assertIn('Video gần đây',replies[1]['text'])
        self.assertIn('queue',replies[2]['text'])

    def test_viewer_callbacks_replay_file_id_to_each_viewer_without_local_bytes_or_index_mutation(self):
        key=self.confirmed()
        path=Path(self.row(key)['local_path'])
        path.unlink()
        before=self.row(key)
        calls=self.poll_updates([self.callback(43,'v:'+key[:32]),self.callback(44,'v:'+key[:32],2)])
        sends=[(fields,kwargs) for method,fields,kwargs in calls if method=='sendVideo']
        self.assertEqual([fields['chat_id'] for fields,_ in sends],[43,44])
        self.assertTrue(all(fields['video']==before['file_id'] and not kwargs for fields,kwargs in sends))
        self.assertEqual(self.row(key),before)
        self.assertFalse(path.exists())
        self.assertTrue(self.source.exists())

    def test_replay_rejects_direct_recipient_outside_allowlist(self):
        key=self.confirmed()
        with self.assertRaises(ValueError):self.telegram.replay(self.archive,key[:32],45)
        self.request.assert_not_called()

    def test_viewer_deep_start_replays_without_owner_handshake(self):
        key=self.confirmed()
        calls=self.poll_updates([self.message(43,'/start play_'+key[:32])])
        self.assertIsNone(self.archive.state('telegram_owner_started:42'))
        sends=[fields for method,fields,_ in calls if method=='sendVideo']
        self.assertEqual(sends[0]['chat_id'],43)
        self.assertEqual(sends[0]['video'],self.row(key)['file_id'])
        self.assertEqual(self.archive.state('telegram_bot_username'),'FixtureArchive_bot')

    def test_owner_deep_start_replays_and_records_owner_handshake_and_bot_identity(self):
        key=self.confirmed()
        self.poll_updates([self.message(42,'/start play_'+key[:32])])
        self.assertEqual(self.archive.state('telegram_owner_started:42'),'1')
        self.assertEqual(self.archive.state('telegram_bot_username'),'FixtureArchive_bot')
        self.assertEqual(self.archive.state('telegram_bot_id'),'900')

    def test_get_me_failure_does_not_undo_authorized_owner_start(self):
        calls=self.poll_updates([self.message(42,'/start')],TimeoutError('synthetic getMe timeout'))
        self.assertEqual(self.archive.state('telegram_owner_started:42'),'1')
        self.assertTrue(any(method=='sendMessage' for method,_,_ in calls))
        self.assertIsNone(self.archive.state('telegram_bot_username'))

    def test_document_replay_uses_saved_media_type_even_for_h264_row(self):
        key=self.confirmed(media_type='document')
        before=self.row(key)
        calls=self.poll_updates([self.callback(43,'v:'+key[:32])])
        sends=[fields for method,fields,_ in calls if method=='sendDocument']
        self.assertEqual(len(sends),1)
        self.assertEqual(sends[0]['document'],before['file_id'])
        self.assertEqual(self.row(key),before)

    def test_legacy_channel_record_replay_retains_original_metadata_and_uses_private_recipient(self):
        key=self.ingest()
        self.archive.mark_uploaded(key,-1001234567890,99,'legacy-same-bot-file')
        before=self.row(key)
        calls=self.poll_updates([self.callback(43,'v:'+key[:32])])
        send=next(fields for method,fields,_ in calls if method=='sendVideo')
        self.assertEqual(send['chat_id'],43)
        self.assertEqual(send['video'],'legacy-same-bot-file')
        self.assertEqual(self.row(key),before)
        _,buttons=self.telegram.menu(self.archive,'d:2026-10-03')
        _,clips=self.telegram.menu(self.archive,buttons[0][0]['callback_data'])
        self.assertFalse(any('url' in b for row in clips for b in row))

    def test_recent_menu_is_newest_first_and_paginated_by_ten(self):
        keys=[self.confirmed(minute=minute) for minute in range(12)]
        _,first=self.telegram.recent_menu(self.archive)
        replay=[b['callback_data'] for row in first for b in row if b.get('callback_data','').startswith('v:')]
        self.assertEqual(replay,['v:'+key[:32] for key in reversed(keys[2:])])
        self.assertTrue(any(b.get('callback_data')=='recent:1' for row in first for b in row))
        _,second=self.telegram.menu(self.archive,'recent:1')
        replay=[b['callback_data'] for row in second for b in row if b.get('callback_data','').startswith('v:')]
        self.assertEqual(replay,['v:'+keys[1][:32],'v:'+keys[0][:32]])
        self.assertTrue(all(len(b.get('callback_data','').encode())<=64 for row in first+second for b in row))

    def test_replay_rejects_unknown_nonuploaded_and_ambiguous_key_prefix(self):
        pending=self.ingest()
        for prefix in ['bad', 'a'*32,pending[:32]]:
            with self.subTest(prefix=prefix),self.assertRaises(ValueError):self.telegram.replay(self.archive,prefix,43)
        first=self.confirmed()
        second=self.confirmed()
        with self.archive.conn:
            self.archive.conn.execute('UPDATE recordings SET key=? WHERE key=?',(first[:32]+'a'*32,second))
        with self.assertRaises(ValueError):self.telegram.replay(self.archive,first[:32],43)
        self.request.assert_not_called()

    def test_invalid_upload_identity_remains_unknown_and_retains_cache(self):
        self.archive.state('telegram_owner_started:42','1')
        invalid=[]
        wrong_owner=self.media_response(43,field='document');invalid.append(wrong_owner)
        channel=self.media_response(field='document');channel['chat']['type']='channel';invalid.append(channel)
        missing_unique=self.media_response(field='document');del missing_unique['document']['file_unique_id'];invalid.append(missing_unique)
        invalid.append(self.media_response(field='video'))
        zero_message=self.media_response(field='document');zero_message['message_id']=0;invalid.append(zero_message)
        boolean_message=self.media_response(field='document');boolean_message['message_id']=True;invalid.append(boolean_message)
        missing_chat_type=self.media_response(field='document');del missing_chat_type['chat']['type'];invalid.append(missing_chat_type)
        for index,response in enumerate(invalid):
            with self.subTest(index=index):
                key=self.ingest(index)
                path=Path(self.row(key)['local_path'])
                self.request.return_value=response
                self.assertEqual(self.telegram.upload_one(self.archive),'upload_unknown')
                self.assertEqual(self.row(key)['status'],'upload_unknown')
                self.assertTrue(path.exists())
                self.assertIsNone(self.row(key)['message_id'])

    def test_confirmed_upload_persists_owner_media_unique_id_before_cache_cleanup(self):
        self.archive.state('telegram_owner_started:42','1')
        self.archive.state('telegram_bot_id','900')
        key=self.ingest()
        self.request.return_value=self.media_response(field='document')
        self.assertEqual(self.telegram.upload_one(self.archive),'uploaded')
        row=self.row(key)
        self.assertEqual((row['chat_id'],row['file_id'],row['file_unique_id'],row['media_type']),
                         ('42','returned-file-id','returned-unique-id','document'))
        self.assertGreater(row['uploaded_at'],0)
        self.assertEqual(row['bot_id'],900)
        self.assertFalse(Path(row['local_path']).exists())

    def test_known_different_bot_identity_blocks_replay_before_api_send(self):
        key=self.confirmed()
        with self.archive.conn:
            self.archive.conn.execute('UPDATE recordings SET bot_id=900 WHERE key=?',(key,))
        self.archive.state('telegram_bot_id','901')
        with self.assertRaises(ValueError):self.telegram.replay(self.archive,key[:32],43)
        self.request.assert_not_called()

    def test_unknown_viewer_deep_link_does_not_fetch_bot_identity_or_replay(self):
        key=self.confirmed()
        calls=self.poll_updates([self.message(45,'/start play_'+key[:32])])
        self.assertEqual([method for method,_,_ in calls],['getUpdates'])
        self.assertIsNone(self.archive.state('telegram_bot_username'))
        self.assertIsNone(self.archive.state('telegram_owner_started:42'))

    def test_removed_viewer_cannot_use_existing_replay_callback(self):
        key=self.confirmed()
        self.settings.allowed_users=(42,44)
        calls=self.poll_updates([self.callback(43,'v:'+key[:32])])
        self.assertEqual([method for method,_,_ in calls],['getUpdates'])

    def test_replay_rate_limit_preserves_update_cursor_and_original_metadata(self):
        key=self.confirmed()
        before=self.row(key)
        self.archive.state('telegram_offset',40)
        attempts=[]
        def fake(method,fields,**kwargs):
            if method=='getUpdates':return [self.callback(43,'v:'+key[:32],40)]
            if method=='answerCallbackQuery':return True
            if method=='sendVideo':
                attempts.append(fields)
                if len(attempts)==1:raise ApiRejected(429,10)
                return self.media_response(43)
            raise AssertionError(method)
        self.request.side_effect=fake
        with self.assertRaises(ApiRejected):self.telegram.poll(self.archive)
        self.assertEqual(self.archive.state('telegram_offset'),'40')
        self.assertEqual(self.row(key),before)
        self.assertEqual(json.loads(self.archive.state('telegram_replay_attempt')),{})
        retry_at=float(self.archive.state('telegram_replay_retry_at'))
        count=self.request.call_count
        self.telegram.poll(self.archive)
        self.assertEqual(self.request.call_count,count)
        with patch('archive_app.telegram.time.time',return_value=retry_at+1):
            self.telegram.poll(self.archive)
        self.assertEqual(len(attempts),2)
        self.assertEqual(self.archive.state('telegram_offset'),'41')
        self.assertEqual(json.loads(self.archive.state('telegram_replay_attempt'))['phase'],'done')
        self.assertEqual(self.row(key),before)

    def test_replay_timeout_consumes_event_once_and_new_manual_click_can_replay(self):
        key=self.confirmed()
        before=self.row(key)
        sends=[]
        updates=[self.callback(43,'v:'+key[:32],40)]
        def fake(method,fields,**kwargs):
            if method=='getUpdates':return updates
            if method=='answerCallbackQuery':return True
            if method=='sendVideo':
                sends.append(fields)
                if len(sends)==1:raise TimeoutError('synthetic ambiguous POST timeout')
                return self.media_response(43)
            raise AssertionError(method)
        self.request.side_effect=fake
        self.telegram.poll(self.archive)
        self.assertEqual(self.archive.state('telegram_offset'),'41')
        self.assertEqual(json.loads(self.archive.state('telegram_replay_attempt')),{'update_id':40,'phase':'unknown'})
        self.telegram.poll(self.archive)
        self.assertEqual(len(sends),1)
        updates[:]=[self.callback(43,'v:'+key[:32],41)]
        self.telegram.poll(self.archive)
        self.assertEqual(len(sends),2)
        self.assertEqual(self.archive.state('telegram_offset'),'42')
        self.assertEqual(self.row(key),before)

    def test_replay_server_error_for_deep_link_consumes_cursor_without_automatic_retry(self):
        key=self.confirmed()
        before=self.row(key)
        sends=[]
        def fake(method,fields,**kwargs):
            if method=='getUpdates':return [self.message(43,'/start play_'+key[:32],40)]
            if method=='getMe':return {'id':900,'username':'FixtureArchive_bot'}
            if method=='sendVideo':
                sends.append(fields)
                raise ApiRejected(503)
            raise AssertionError(method)
        self.request.side_effect=fake
        self.telegram.poll(self.archive)
        self.telegram.poll(self.archive)
        self.assertEqual(len(sends),1)
        self.assertEqual(self.archive.state('telegram_offset'),'41')
        self.assertEqual(json.loads(self.archive.state('telegram_replay_attempt'))['phase'],'unknown')
        self.assertEqual(self.row(key),before)

    def test_restart_consumes_pending_replay_without_media_post_and_keeps_state_bounded(self):
        key=self.confirmed()
        before=self.row(key)
        self.archive.state('telegram_offset',40)
        self.archive.state('telegram_replay_attempt',json.dumps({'update_id':40,'phase':'pending'}))
        self.archive.close()
        self.archive=Archive(self.settings)
        calls=self.poll_updates([self.callback(43,'v:'+key[:32],40)])
        self.assertEqual([method for method,_,_ in calls],['getUpdates'])
        self.assertEqual(self.archive.state('telegram_offset'),'41')
        self.assertEqual(json.loads(self.archive.state('telegram_replay_attempt')),{'update_id':40,'phase':'unknown'})
        self.assertEqual(self.row(key),before)
        for update_id in range(41,45):
            self.poll_updates([self.callback(43,'v:'+key[:32],update_id)])
        rows=self.archive.conn.execute("SELECT name,value FROM state WHERE name LIKE 'telegram_replay_%'").fetchall()
        self.assertEqual(len(rows),2)
        self.assertTrue(all(len(row['value'])<512 for row in rows))


if __name__=='__main__':
    unittest.main()
