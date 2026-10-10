"""Local channel display labels: no real cameras, credentials or Telegram calls."""
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from archive_app.channel_directory import ChannelDirectory
from archive_app.core import Archive, Settings


class ChannelDisplayNameTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).parent if os.name=='nt' else Path(tempfile.gettempdir())
        self.root=Path(tempfile.mkdtemp(prefix='.tmp-channel-label-',dir=parent))
        self.settings=Settings(self.root/'state',self.root/'cache',self.root/'input','UTC+07:00',
            token='700:synthetic',min_free_bytes=0,multi_channel_routing=True,
            storage_channel_id=-100999)
        self.archive=Archive(self.settings)
        self.archive.add_camera({'id':'A','name':'Camera A','channel_chat_id':-100111})

    def tearDown(self):
        self.archive.close()
        shutil.rmtree(self.root)

    def camera(self,slug='A'):
        return next(item for item in self.archive.cameras() if item['id']==slug)

    def channels(self):
        return {item['chat_id']:item for item in ChannelDirectory(self.archive).list()}

    def test_default_empty_alias_preserves_numeric_manual_channel(self):
        self.assertEqual(self.camera()['channel_name'],'')
        channel=self.channels()[-100111]
        self.assertEqual(channel['name'],'-100111')
        self.assertEqual(channel['title'],'-100111')
        self.assertEqual(channel['channel_name'],'')

    def test_add_trims_unicode_alias_and_exposes_camera_and_directory(self):
        added=self.archive.add_camera({'id':'B','channel_chat_id':'-100222',
                                       'channel_name':'  Nhà ba mẹ 🎥  '})
        self.assertEqual(added['channel_name'],'Nhà ba mẹ 🎥')
        channel=self.channels()[-100222]
        self.assertEqual(channel['name'],'Nhà ba mẹ 🎥')
        self.assertEqual(channel['title'],'-100222')
        self.assertEqual(channel['bound_camera_id'],'B')

    def test_unicode_limit_counts_codepoints_not_utf8_bytes(self):
        alias='🎥'*128
        self.assertEqual(self.archive.update_camera('A',{'channel_name':alias})['channel_name'],alias)
        with self.assertRaises(ValueError):
            self.archive.update_camera('A',{'channel_name':alias+'🎥'})
        self.assertEqual(self.camera()['channel_name'],alias)

    def test_invalid_alias_types_controls_and_lengths_do_not_write(self):
        self.archive.update_camera('A',{'channel_name':'Original'})
        for alias in (None,True,4,[],{},'x'*129,'A\nB','A\tB','A\x00B','A\x7fB','A\x85B','A\ud800B'):
            with self.subTest(alias=repr(alias)),self.assertRaises(ValueError):
                self.archive.update_camera('A',{'channel_name':alias,'name':'Changed'})
            self.assertEqual(self.camera()['channel_name'],'Original')
            self.assertEqual(self.camera()['name'],'Camera A')

    def test_nonempty_alias_without_channel_is_rejected_before_password_write(self):
        with patch.object(self.archive,'_write_camera_password') as write:
            with self.assertRaises(ValueError):
                self.archive.add_camera({'id':'B','channel_name':'Unbound','sd_password':'synthetic'})
            write.assert_not_called()
        self.assertFalse(any(item['id']=='B' for item in self.archive.cameras()))
        self.archive.add_camera({'id':'B'})
        with patch.object(self.archive,'_write_camera_password') as write:
            with self.assertRaises(ValueError):
                self.archive.update_camera('B',{'channel_name':'Unbound','name':'Changed',
                                               'sd_password':'synthetic'})
            write.assert_not_called()
        self.assertEqual(self.camera('B')['name'],'B')

    def test_blank_alias_is_allowed_without_channel(self):
        camera=self.archive.add_camera({'id':'B','channel_name':'   '})
        self.assertEqual(camera['channel_name'],'')
        self.assertIsNone(camera['channel_chat_id'])

    def test_id_change_resets_alias_without_explicit_replacement(self):
        self.archive.update_camera('A',{'channel_name':'Previous channel'})
        camera=self.archive.update_camera('A',{'channel_chat_id':-100222})
        self.assertEqual(camera['channel_name'],'')
        self.assertEqual(camera['channel_chat_id'],-100222)
        self.assertEqual(self.channels()[-100222]['name'],'-100222')

    def test_same_id_patch_preserves_alias(self):
        self.archive.update_camera('A',{'channel_name':'PN'})
        self.assertEqual(self.archive.update_camera('A',{'channel_chat_id':'-100111'})['channel_name'],'PN')

    def test_new_id_can_receive_an_explicit_replacement_alias(self):
        self.archive.update_camera('A',{'channel_name':'Old'})
        camera=self.archive.update_camera('A',{'channel_chat_id':-100222,'channel_name':'New'})
        self.assertEqual((camera['channel_chat_id'],camera['channel_name']),(-100222,'New'))

    def test_clearing_id_clears_alias_and_explicit_nonempty_alias_is_atomic_error(self):
        self.archive.update_camera('A',{'channel_name':'PN'})
        with self.assertRaises(ValueError):
            self.archive.update_camera('A',{'channel_chat_id':None,'channel_name':'Must not detach'})
        self.assertEqual((self.camera()['channel_chat_id'],self.camera()['channel_name']),(-100111,'PN'))
        camera=self.archive.update_camera('A',{'channel_chat_id':None})
        self.assertEqual(camera['channel_name'],'')
        self.assertIsNone(camera['channel_chat_id'])

    def test_alias_survives_reopen_refresh_and_channel_events(self):
        self.archive.update_camera('A',{'channel_name':'Tên riêng'})
        self.archive.close()
        self.archive=Archive(self.settings)
        directory=ChannelDirectory(self.archive)
        directory.remember({'id':-100111,'type':'channel','title':'Telegram original'},status='ready')
        class SyntheticTelegram:
            def verify_channel(_,archive,chat_id,**kwargs):
                ChannelDirectory(archive).remember({'id':chat_id,'type':'channel',
                    'title':'Telegram refreshed'},status='ready')
        directory.refresh(SyntheticTelegram())
        directory.observe({'channel_post':{'chat':{'id':-100111,'type':'channel','title':'Telegram event'}}})
        channel=self.channels()[-100111]
        self.assertEqual(channel['name'],'Tên riêng')
        self.assertEqual(channel['title'],'Telegram event')
        self.assertEqual(self.camera()['channel_name'],'Tên riêng')

    def test_clearing_alias_restores_latest_canonical_title(self):
        directory=ChannelDirectory(self.archive)
        directory.remember({'id':-100111,'type':'channel','title':'Actual Telegram title'})
        self.archive.update_camera('A',{'channel_name':'Local name'})
        self.archive.update_camera('A',{'channel_name':'   '})
        self.assertEqual(self.channels()[-100111]['name'],'Actual Telegram title')

    def test_directory_orders_by_displayed_names_and_preserves_bot_isolation(self):
        self.archive.add_camera({'id':'B','channel_chat_id':-100222,'channel_name':'Alpha'})
        self.archive.update_camera('A',{'channel_name':'Zulu'})
        directory=ChannelDirectory(self.archive)
        directory.remember({'id':-100111,'type':'channel','title':'A canonical'},status='ready')
        directory.remember({'id':-100222,'type':'channel','title':'Z canonical'},status='ready')
        rows=[item for item in directory.list() if item['bound_camera_id']]
        self.assertEqual([item['chat_id'] for item in rows],[-100222,-100111])
        self.settings.token='701:other-synthetic'
        other=ChannelDirectory(self.archive).list()
        self.assertFalse(any(item['ready'] for item in other))
        self.assertEqual(next(item for item in other if item['chat_id']==-100111)['title'],'-100111')

    def test_alias_edit_during_upload_leaves_routing_readiness_jobs_and_recording_unchanged(self):
        self.archive.set_channel_status('A','ready')
        self.archive.channel_index.enqueue('A','2026-10-10')
        with self.archive.conn:
            self.archive.conn.execute('''INSERT INTO recordings
                (key,camera,record_id,start_ms,end_ms,source_path,status,attempt_id,upload_target_chat_id,created_at)
                VALUES('synthetic-key','A','synthetic-record',1,2,'synthetic.ps','uploading','attempt',-100111,1)''')
        before=self.camera()
        record=dict(self.archive.conn.execute("SELECT * FROM recordings WHERE key='synthetic-key'").fetchone())
        jobs=[dict(item) for item in self.archive.conn.execute('SELECT * FROM channel_index_jobs')]
        with patch.object(self.archive.channel_index,'enqueue_camera') as enqueue:
            after=self.archive.update_camera('A',{'channel_name':'Renamed while uploading'})
            enqueue.assert_not_called()
        self.assertEqual({key:value for key,value in after.items() if key!='channel_name'},
                         {key:value for key,value in before.items() if key!='channel_name'})
        self.assertEqual(self.archive.resolve_camera_channel('A'),-100111)
        self.assertEqual(dict(self.archive.conn.execute("SELECT * FROM recordings WHERE key='synthetic-key'").fetchone()),record)
        self.assertEqual([dict(item) for item in self.archive.conn.execute('SELECT * FROM channel_index_jobs')],jobs)
        with self.assertRaises(ValueError):
            self.archive.update_camera('A',{'channel_chat_id':-100222,'channel_name':'Different target'})
        self.assertEqual(self.camera()['channel_name'],'Renamed while uploading')

    def test_alias_is_not_used_as_a_routing_identifier_or_interpreted_as_markup(self):
        alias='<img src=x onerror=alert(1)>'
        self.archive.update_camera('A',{'channel_name':alias})
        self.assertEqual(self.channels()[-100111]['name'],alias)
        self.assertEqual(self.archive.resolve_camera_channel('A'),-100111)
        with self.assertRaises(ValueError):
            self.archive.add_camera({'id':'B','channel_chat_id':-100111,'channel_name':'Other'})

    def test_unknown_camera_alias_does_not_create_a_catalog_binding(self):
        before=self.channels()
        with self.assertRaises(KeyError):
            self.archive.update_camera('missing',{'channel_name':'Unknown'})
        self.assertEqual(self.channels(),before)

    def test_existing_camera_schema_migrates_additively_and_defaults_alias(self):
        legacy=self.root/'legacy-state'
        legacy.mkdir()
        with closing(sqlite3.connect(legacy/'archive.db')) as conn:
            conn.executescript('''CREATE TABLE cameras (
                id TEXT PRIMARY KEY,name TEXT NOT NULL,model TEXT NOT NULL DEFAULT '',
                host TEXT NOT NULL DEFAULT '',device_port INTEGER NOT NULL DEFAULT 8000,
                rtsp_port INTEGER NOT NULL DEFAULT 554,http_port INTEGER NOT NULL DEFAULT 80,
                enabled INTEGER NOT NULL DEFAULT 1,probe_json TEXT,created_at REAL NOT NULL,
                channel_chat_id INTEGER);
                INSERT INTO cameras(id,name,created_at,channel_chat_id) VALUES('legacy','Legacy',1,-100333);''')
        settings=Settings(legacy,self.root/'legacy-cache',self.root/'legacy-input','UTC+07:00',min_free_bytes=0)
        archive=Archive(settings)
        try:
            camera=archive.cameras()[0]
            self.assertEqual((camera['id'],camera['name'],camera['channel_chat_id'],camera['channel_name']),
                             ('legacy','Legacy',-100333,''))
            archive.update_camera('legacy',{'channel_name':'Local legacy label'})
        finally:archive.close()
        archive=Archive(settings)
        try:self.assertEqual(archive.cameras()[0]['channel_name'],'Local legacy label')
        finally:archive.close()


if __name__=='__main__':unittest.main()
