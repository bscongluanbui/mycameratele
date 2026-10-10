"""Exact destination routing and additive migration; no live camera/Telegram."""
import copy
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from archive_app.core import Archive, Settings
from archive_app.telegram import Telegram, ApiRejected
from archive_app.channel_directory import ChannelDirectory


class RoutingTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).parent if os.name=='nt' else Path(tempfile.gettempdir())
        self.root=Path(tempfile.mkdtemp(prefix='.tmp-multi-route-',dir=parent))
        self.settings=Settings(self.root/'state',self.root/'cache',self.root/'input','UTC+07:00',
            keep_cache=False,enable_upload=True,token='700:fixture',owner_user_id=42,allowed_users=(42,),
            min_free_bytes=0,multi_channel_routing=True,telegram_destination='channel',storage_channel_id=-100999)
        self.archive=Archive(self.settings);self.calls=[];self.blocked=set();self.hook=None
        self.telegram=Telegram(self.settings);self.telegram.request=self.request
        self.archive.add_camera({'id':'A','name':'Cổng <chính>','channel_chat_id':-100111})
        self.archive.add_camera({'id':'B','name':'Sân','channel_chat_id':-100222})

    def tearDown(self):
        self.archive.close();shutil.rmtree(self.root)

    def request(self,method,fields,**kwargs):
        self.calls.append((method,copy.deepcopy(fields)))
        if self.hook:self.hook(method,fields)
        if method=='getMe':return {'id':700,'is_bot':True,'username':'fixture_bot'}
        if method=='getChat':
            if fields['chat_id'] in self.blocked:raise ApiRejected(403)
            return {'id':fields['chat_id'],'type':'channel','title':'Named '+str(fields['chat_id'])}
        if method=='getChatMember':return {'status':'administrator','user':{'id':700,'is_bot':True},'can_post_messages':True,'can_edit_messages':True}
        if method in ('sendVideo','sendDocument'):
            field='video' if method=='sendVideo' else 'document'
            return {'chat':{'id':fields['chat_id'],'type':'channel'},'message_id':91,
                    field:{'file_id':'fileid','file_unique_id':'unique'}}
        raise AssertionError(method)

    def record(self,camera='A',start='2026-10-10T08:00:00+07:00',end='2026-10-10T08:01:00+07:00'):
        source=self.settings.input_dir/(camera+'.ps');source.parent.mkdir(exist_ok=True)
        source.write_bytes(b'\x00\x00\x01\xba synthetic PS')
        return self.archive.ingest_entry({'camera':camera,'record_id':source.name+start,'path':str(source),'start_time':start,'end_time':end})

    def row(self,key):return dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?',(key,)).fetchone())

    def test_camera_a_b_upload_exact_channels_and_outbox_commits(self):
        a=self.record('A');b=self.record('B')
        self.assertEqual(self.telegram.upload_one(self.archive,'A'),'uploaded')
        self.assertEqual(self.telegram.upload_one(self.archive,'B'),'uploaded')
        self.assertEqual([f['chat_id'] for m,f in self.calls if m=='sendDocument'],[-100111,-100222])
        for key,channel in ((a['key'],-100111),(b['key'],-100222)):
            row=self.row(key);self.assertEqual((row['status'],row['storage_chat_id'],row['storage_message_id'],row['upload_target_chat_id']),('uploaded',channel,91,channel))
            self.assertFalse(Path(row['local_path']).exists())
        jobs=list(self.archive.conn.execute('SELECT * FROM channel_index_jobs'))
        self.assertEqual(len(jobs),2)

    def test_missing_mapping_has_no_claim_no_legacy_fallback(self):
        self.archive.update_camera('A',{'channel_chat_id':None});a=self.record()
        self.assertEqual(self.telegram.upload_one(self.archive,'A'),'storage_blocked')
        self.assertEqual(self.row(a['key'])['status'],'downloaded');self.assertIsNone(self.row(a['key'])['attempt_id'])
        self.assertFalse(any(m in ('sendVideo','sendDocument') for m,f in self.calls))

    def test_permission_failure_one_camera_does_not_starve_other(self):
        a=self.record('A');b=self.record('B');self.blocked.add(-100111)
        self.assertEqual(self.telegram.upload_one(self.archive),'uploaded')
        self.assertEqual(self.row(a['key'])['status'],'downloaded');self.assertEqual(self.row(b['key'])['storage_chat_id'],-100222)

    def test_disabled_mapping_blocks_camera_only(self):
        self.archive.update_camera('A',{'channel_enabled':False});self.record('A');b=self.record('B')
        self.assertEqual(self.telegram.upload_one(self.archive),'uploaded')
        self.assertEqual(self.row(b['key'])['status'],'uploaded')

    def test_duplicate_channel_add_update_and_invalid_ids_rejected(self):
        with self.assertRaises(ValueError):self.archive.add_camera({'id':'C','channel_chat_id':-100111})
        with self.assertRaises(ValueError):self.archive.update_camera('B',{'channel_chat_id':-100111})
        for value in (True,123,-222,1.5,{},'-1000'):
            with self.subTest(value=value),self.assertRaises(ValueError):self.archive.update_camera('A',{'channel_chat_id':value})

    def test_uploading_snapshot_blocks_concurrent_mapping_change(self):
        self.record();row=self.archive.claim_upload('A',channel_chat_id=-100111)
        self.assertEqual(row['upload_target_chat_id'],-100111)
        with self.assertRaises(ValueError):self.archive.update_camera('A',{'channel_chat_id':-100333})

    def test_mapping_change_between_preflight_and_claim_never_posts(self):
        original=self.telegram.verify_camera_channel
        def change(*args,**kwargs):
            result=original(*args,**kwargs);self.archive.update_camera('A',{'channel_chat_id':-100333});return result
        self.telegram.verify_camera_channel=change;row=self.record()
        self.assertEqual(self.telegram.upload_one(self.archive,'A'),'storage_blocked')
        self.assertEqual(self.row(row['key'])['status'],'downloaded')
        self.assertFalse(any(m=='sendDocument' for m,f in self.calls))

    def test_unknown_upload_is_never_blind_retried(self):
        def timeout(method,fields):
            if method=='sendDocument':raise TimeoutError('synthetic')
        self.hook=timeout;row=self.record()
        self.assertEqual(self.telegram.upload_one(self.archive,'A'),'upload_unknown')
        self.assertIsNone(self.telegram.upload_one(self.archive,'A'))
        self.assertEqual(self.row(row['key'])['upload_target_chat_id'],-100111)
        self.assertEqual(sum(m=='sendDocument' for m,f in self.calls),1)

    def test_outbox_failure_rolls_back_placement_and_quarantines(self):
        row=self.record()
        with patch.object(self.archive.channel_index,'enqueue_recording',side_effect=sqlite3.OperationalError('synthetic')):
            self.assertEqual(self.telegram.upload_one(self.archive,'A'),'upload_unknown')
        saved=self.row(row['key']);self.assertEqual(saved['status'],'upload_unknown');self.assertIsNone(saved['message_id'])
        self.assertTrue(Path(saved['local_path']).exists())

    def test_caption_is_recording_time_has_tags_no_path_or_token(self):
        row=self.record(start='2026-10-10T23:59:00+07:00',end='2026-10-11T00:01:00+07:00')
        text=self.telegram.caption(self.archive,row)
        for value in ('10/10/2026','11/10/2026 00:01:00','#A #Y2026 #M202610 #D20261010','02:00'):self.assertIn(value,text)
        self.assertNotIn(str(self.root),text);self.assertNotIn(self.settings.token,text)

    def test_restart_does_not_duplicate_committed_upload(self):
        row=self.record();self.telegram.upload_one(self.archive,'A');self.archive.close();self.archive=Archive(self.settings)
        self.assertIsNone(self.telegram.upload_one(self.archive,'A'));self.assertEqual(self.row(row['key'])['storage_chat_id'],-100111)

    def test_directory_seeds_named_channels_scoped_to_current_bot(self):
        directory=ChannelDirectory(self.archive);directory.refresh(self.telegram)
        rows={r['chat_id']:r for r in directory.list()}
        self.assertEqual(rows[-100111]['name'],'Named -100111');self.assertEqual(rows[-100222]['bound_camera_id'],'B')
        self.settings.token='701:other';self.assertFalse(any(r['ready'] for r in ChannelDirectory(self.archive).list()))

    def test_directory_rejects_forged_other_bot_membership_and_nonchannel(self):
        directory=ChannelDirectory(self.archive)
        self.assertFalse(directory.observe({'my_chat_member':{'chat':{'id':-100333,'type':'channel','title':'Other'},'new_chat_member':{'user':{'id':701,'is_bot':True},'status':'administrator'}}}))
        self.assertFalse(directory.remember({'id':-100333,'type':'supergroup','title':'Other'}))

    def test_directory_refresh_429_stops_and_sanitizes(self):
        directory=ChannelDirectory(self.archive)
        with patch.object(self.telegram,'verify_channel',side_effect=ApiRejected(429,12,'secret text')) as verify:
            rows=directory.refresh(self.telegram)
        self.assertEqual(verify.call_count,1);self.assertNotIn('secret text',json.dumps(rows))
        self.assertTrue(any(r['error']=='channel_rate_limited' for r in rows))

    def test_permission_checks_require_edit_for_channel_pin_not_group_pin_right(self):
        self.assertEqual(self.telegram.verify_camera_channel(self.archive,'A'),(-100111,700))

    def test_migration_backs_up_consistent_old_schema_without_rewriting_placement(self):
        self.archive.close()
        old=self.root/'legacy-state';old.mkdir();db=sqlite3.connect(old/'archive.db')
        db.executescript("""CREATE TABLE cameras(id TEXT PRIMARY KEY,name TEXT NOT NULL,model TEXT NOT NULL DEFAULT '',host TEXT NOT NULL DEFAULT '',device_port INTEGER NOT NULL DEFAULT 8000,rtsp_port INTEGER NOT NULL DEFAULT 554,http_port INTEGER NOT NULL DEFAULT 80,enabled INTEGER NOT NULL DEFAULT 1,probe_json TEXT,created_at REAL NOT NULL);
            INSERT INTO cameras(id,name,created_at) VALUES('legacy','Legacy',1);""")
        db.close();self.settings.state_dir=old;self.archive=Archive(self.settings)
        backups=list((old/'migration-backups').glob('multi-channel-*.db'));self.assertEqual(len(backups),1)
        with closing(sqlite3.connect(backups[0])) as backup:
            columns={r[1] for r in backup.execute('PRAGMA table_info(cameras)')}
            self.assertNotIn('channel_chat_id',columns);self.assertEqual(backup.execute('SELECT id FROM cameras').fetchone()[0],'legacy')
        camera=self.archive.cameras()[0];self.assertIsNone(camera['channel_chat_id'])
        self.archive.close();self.archive=Archive(self.settings)
        self.assertEqual(len(list((old/'migration-backups').glob('*.db'))),1)

    def test_start_time_order_and_soft_delete_refresh_index_without_media_send(self):
        later=self.record(start='2026-10-10T09:00:00+07:00',end='2026-10-10T09:01:00+07:00')
        earlier=self.record(start='2026-10-10T08:00:00+07:00',end='2026-10-10T08:01:00+07:00')
        self.telegram.upload_one(self.archive,'A');self.assertEqual(self.row(earlier['key'])['status'],'uploaded')
        self.assertEqual(self.row(later['key'])['status'],'downloaded')
        before=self.archive.conn.execute('SELECT generation FROM channel_index_jobs').fetchone()[0]
        self.archive.soft_delete(earlier['key'],42);self.archive.restore_recording(earlier['key'],42)
        self.assertEqual(self.archive.conn.execute('SELECT generation FROM channel_index_jobs').fetchone()[0],before+2)

    def test_verify_channel_mapping_changed_during_network_is_not_ready(self):
        def switch(method,fields):
            if method=='getChatMember':self.archive.update_camera('A',{'channel_chat_id':-100333})
        self.hook=switch
        with self.assertRaises(ValueError):self.telegram.verify_camera_channel(self.archive,'A')
        self.assertEqual(next(c for c in self.archive.cameras() if c['id']=='A')['channel_status'],'unconfigured')


class MultiSettingsTests(unittest.TestCase):
    def test_multichannel_can_start_without_legacy_destination(self):
        with patch.dict(os.environ,{'MULTI_CHANNEL_ROUTING':'true','ENABLE_UPLOAD':'true','TELEGRAM_BOT_TOKEN':'700:fixture','DISPLAY_TIMEZONE':'UTC+07:00'},clear=True):
            settings=Settings.from_env()
        self.assertTrue(settings.multi_channel_routing);self.assertEqual(settings.storage_channel_id,0)

    def test_invalid_flags_debounce_rejected(self):
        for key,value in (('MULTI_CHANNEL_ROUTING','yes'),('CHANNEL_INDEX_ENABLED','1'),('CHANNEL_INDEX_DEBOUNCE_SECONDS','301')):
            with self.subTest(key=key),patch.dict(os.environ,{key:value},clear=True),self.assertRaises(ValueError):Settings.from_env()


if __name__=='__main__':unittest.main()
