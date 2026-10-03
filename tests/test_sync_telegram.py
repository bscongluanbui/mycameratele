"""Synthetic private-bot Start/upload controls; no live camera or Telegram calls."""
import hashlib
from dataclasses import replace
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import uuid

from archive_app.core import Archive, Settings
from archive_app.sync import SyncQueue
from archive_app.telegram import Telegram
from archive_app.telegram_menu import TimeMenus


class TelegramSyncTests(unittest.TestCase):
    def setUp(self):
        self.parent=Path(__file__).resolve().parent if os.name=='nt' else Path(tempfile.gettempdir()).resolve()
        self.root=self.parent/('.tmp-sync-bot-'+uuid.uuid4().hex);self.root.mkdir()
        (self.root/'input').mkdir()
        self.settings=Settings(self.root/'state',self.root/'cache',self.root/'input','UTC+07:00',
                               token='synthetic-token',owner_user_id=42,allowed_users=(43,44),
                               enable_upload=True,min_free_bytes=0)
        self.archive=Archive(self.settings);self.telegram=Telegram(self.settings)
        self.archive.add_camera({'id':'Front_Camera','name':'Cửa trước','host':'192.168.1.11'})
        self.archive.add_camera({'id':'Rear_Camera','name':'Cửa sau','host':'192.168.1.12'})
        self.archive.add_camera({'id':'disabled','name':'Tạm dừng','host':'192.168.1.13','enabled':False})
        self.queue=SyncQueue(self.archive);self.calls=[];self.updates=[]

    def tearDown(self):
        self.archive.close();self.assertEqual(self.root.resolve().parent,self.parent);shutil.rmtree(self.root)

    def fake_api(self,method,fields,**kwargs):
        self.calls.append((method,fields,kwargs))
        if method=='getUpdates':return self.updates
        if method in ('answerCallbackQuery','setMyCommands','setChatMenuButton'):return True
        if method=='sendMessage':return {'message_id':100}
        raise AssertionError('Unexpected API/network operation: '+method)

    def run_update(self,data,actor=43,chat=None,chat_type='private',message=False,update=1):
        envelope={'from':{'id':actor},'chat':{'id':actor if chat is None else chat,'type':chat_type}}
        if message:
            envelope['text']=data;self.updates=[{'update_id':update,'message':envelope}]
        else:
            self.updates=[{'update_id':update,'callback_query':{'id':'fixture','from':{'id':actor},'data':data,'message':{'chat':envelope['chat']}}}]
        with patch.object(self.telegram,'request',side_effect=self.fake_api), patch.object(self.archive,'probe_camera',side_effect=AssertionError('poll must not probe')):
            self.telegram.poll(self.archive)

    def camera(self,slug):return next(c for c in self.archive.cameras() if c['id']==slug)

    def test_native_commands_and_keyboard_have_sync_and_v4_registration(self):
        self.assertIn('sync',{x['command'] for x in TimeMenus.commands()})
        self.assertIn('▶ Start sync',{x['text'] for row in self.telegram.reply_keyboard()['keyboard'] for x in row})
        old='telegram_commands_v3:'+hashlib.sha256(self.settings.token.encode()).hexdigest()[:16]
        self.archive.state(old,'1')
        with patch.object(self.telegram,'request',side_effect=self.fake_api):
            self.assertTrue(self.telegram.register_commands(self.archive))
            self.assertTrue(self.telegram.register_commands(self.archive))
        self.assertEqual(sum(x[0]=='setMyCommands' for x in self.calls),1)

    def test_root_and_camera_controls_use_immutable_compact_tokens(self):
        _,buttons=self.telegram.menu(self.archive)
        self.assertIn('sync:all',[x['callback_data'] for row in buttons for x in row])
        token=self.telegram.camera_token('Front_Camera')
        _,buttons=self.telegram.menu(self.archive,f'c:{token}:asc')
        payloads=[x['callback_data'] for row in buttons for x in row]
        self.assertIn('sync:'+token,payloads);self.assertIn('ss:'+token,payloads);self.assertIn('up:'+token+':0',payloads)
        self.assertTrue(all(len(x.encode())<=64 for x in payloads))

    def test_allowed_command_enqueues_enabled_cameras_without_worker_io(self):
        self.run_update('/sync',message=True)
        jobs=self.queue.status()['jobs'];self.assertEqual({j['camera_id'] for j in jobs},{'Front_Camera','Rear_Camera'})
        self.assertTrue(all(j['state']=='queued' and j['source']=='telegram' and j['actor']=='43' for j in jobs))
        self.assertTrue(any('Đã tiếp nhận' in x[1].get('text','') for x in self.calls))

    def test_reply_keyboard_enqueues_all(self):
        self.run_update('▶ Start sync',message=True,actor=44)
        self.assertEqual(len(self.queue.status()['jobs']),2)

    def test_camera_start_is_scoped_and_repeated_start_deduplicates(self):
        token=self.telegram.camera_token('Front_Camera')
        self.run_update('sync:'+token);self.run_update('sync:'+token,update=2)
        jobs=self.queue.status()['jobs'];self.assertEqual(len(jobs),1);self.assertEqual(jobs[0]['camera_id'],'Front_Camera')

    def test_disabled_camera_rejects_start(self):
        self.run_update('sync:'+self.telegram.camera_token('disabled'))
        self.assertEqual(self.queue.status()['jobs'],[])

    def test_outsider_group_and_wrong_private_chat_never_mutate(self):
        token=self.telegram.camera_token('Front_Camera')
        cases=[(999,None,'private'),(43,999,'private'),(43,-100,'group'),(True,None,'private')]
        for index,(actor,chat,kind) in enumerate(cases,1):
            self.run_update('sync:'+token,actor=actor,chat=chat,chat_type=kind,update=index*2)
            self.run_update('up:'+token+':0',actor=actor,chat=chat,chat_type=kind,update=index*2+1)
        self.assertEqual(self.queue.status()['jobs'],[]);self.assertTrue(self.camera('Front_Camera')['upload_enabled'])
        self.assertTrue(all(call[0]=='getUpdates' for call in self.calls))

    def test_upload_off_is_idempotent_and_download_start_remains_available(self):
        token=self.telegram.camera_token('Front_Camera')
        self.run_update('up:'+token+':0');self.run_update('up:'+token+':0',update=2)
        self.assertFalse(self.camera('Front_Camera')['upload_enabled']);self.assertTrue(self.camera('Front_Camera')['enabled'])
        self.run_update('sync:'+token,update=3);self.assertEqual(len(self.queue.status()['jobs']),1)
        self.run_update('up:'+token+':1',update=4);self.assertTrue(self.camera('Front_Camera')['upload_enabled'])

    def test_invalid_tokens_and_toggle_payload_never_mutate(self):
        for index,data in enumerate(('sync:bad','up:bad:0','up:'+self.telegram.camera_token('Front_Camera')+':2'),1):
            self.run_update(data,update=index)
        self.assertEqual(self.queue.status()['jobs'],[]);self.assertTrue(self.camera('Front_Camera')['upload_enabled'])

    def test_status_is_read_only_and_reports_blocked_code(self):
        job=self.queue.enqueue(camera='Front_Camera')['jobs'][0]
        with self.archive.conn:
            self.archive.conn.execute("UPDATE sync_jobs SET state='blocked',phase='sd_download',code='sd_sdk_missing',message='SDK chưa sẵn sàng' WHERE id=?",(job['id'],))
        self.run_update('ss:'+self.telegram.camera_token('Front_Camera'))
        text=next(x[1]['text'] for x in self.calls if x[0]=='sendMessage')
        self.assertIn('Cần xử lý',text);self.assertIn('sd_sdk_missing',text);self.assertIn('SDK chưa sẵn sàng',text)
        self.assertEqual(len(self.queue.status()['jobs']),1)

    def test_upload_one_optional_scope_preserves_original_noarg_claim(self):
        self.archive.state('telegram_owner_started:42','1')
        with patch.object(self.archive,'claim_upload',return_value=None) as claim:
            self.assertIsNone(self.telegram.upload_one(self.archive));claim.assert_called_once_with()
        with patch.object(self.archive,'claim_upload',return_value=None) as claim:
            self.assertIsNone(self.telegram.upload_one(self.archive,'Front_Camera'));claim.assert_called_once_with('Front_Camera')

    def test_upload_one_owner_and_global_gates_still_prevent_claim(self):
        with patch.object(self.archive,'claim_upload') as claim:
            self.assertIsNone(self.telegram.upload_one(self.archive,'Front_Camera'));claim.assert_not_called()
        self.archive.state('telegram_owner_started:42','1')
        disabled=Telegram(replace(self.settings,enable_upload=False))
        with patch.object(self.archive,'claim_upload') as claim:
            self.assertIsNone(disabled.upload_one(self.archive,'Front_Camera'));claim.assert_not_called()


if __name__=='__main__':unittest.main()
