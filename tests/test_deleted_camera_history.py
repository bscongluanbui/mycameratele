"""Removal preserves viewers' historical footage, never camera controls."""
import hashlib
import os
import shutil
import tempfile
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from archive_app.channel_directory import ChannelDirectory
from archive_app.channel_index import ChannelIndex
from archive_app.core import Archive, Settings
from archive_app.telegram import Telegram
from archive_app.telegram_menu import TimeMenus


class DeletedCameraHistoryTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parent if os.name == 'nt' else Path(tempfile.gettempdir())
        self.parent = parent.resolve()
        self.root = self.parent / ('.tmp-removed-history-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.settings = Settings(self.root/'state', self.root/'cache', self.root/'input', 'UTC+07:00',
                                 owner_user_id=42, allowed_users=(42,), token='123456:fixture',
                                 multi_channel_routing=True)
        self.archive = Archive(self.settings)
        self.channel = -1001111111111
        self.archive.add_camera({'id':'front', 'name':'Cửa trước', 'channel_chat_id':self.channel})
        self.telegram = Telegram(self.settings)
        self.menus = TimeMenus(self.telegram)
        self.now = int(datetime.fromisoformat('2026-10-11T12:00:00+07:00').timestamp())
        self.key = hashlib.sha256(b'retained-footage').hexdigest()
        start = int(datetime.fromisoformat('2026-10-11T08:00:00+07:00').timestamp()*1000)
        with self.archive.conn:
            self.archive.conn.execute('''INSERT INTO recordings
                (key,camera,record_id,start_ms,end_ms,source_path,status,created_at,file_id,media_type,
                 storage_kind,storage_chat_id,storage_message_id,bot_id)
                VALUES(?,'front','old-record',?,?,'fixture','uploaded',?,'retained-file','video',
                       'channel',?,77,123456)''', (self.key,start,start+60000,self.now,self.channel))
        self.token = self.telegram.camera_token('front')

    def tearDown(self):
        self.archive.close()
        self.assertEqual(self.root.resolve().parent,self.parent)
        shutil.rmtree(self.root)

    @staticmethod
    def callbacks(buttons):
        return [b['callback_data'] for row in buttons for b in row if 'callback_data' in b]

    def test_time_window_camera_clips_and_album_remain_available(self):
        self.archive.delete_camera('front','Cửa trước')
        self.assertEqual(self.archive.cameras(),[])
        with patch('archive_app.telegram_menu.time.time',return_value=self.now):
            title,buttons=self.menus.menu(self.archive,'today',actor=42)
            selection=next(b for b in self.callbacks(buttons) if b.startswith('wc:'))
            title,buttons=self.menus.menu(self.archive,selection,actor=42)
            self.assertIn('Cửa trước',title)
            self.assertIn('1 video',title)
            bulk=next(b for b in self.callbacks(buttons) if b.startswith('bw:'))
            self.assertEqual(self.menus.download_window(self.archive,bulk,42)['camera'],'front')
        day_bulk=f'bd:{self.token}:2026-10-11:a'
        self.assertEqual(self.menus.download_window(self.archive,day_bulk,42)['camera'],'front')

    def test_retained_calendar_does_not_grant_sync_or_upload_controls(self):
        self.archive.delete_camera('front','Cửa trước')
        title,buttons=self.telegram.menu(self.archive,f'c:{self.token}:asc',actor=42)
        self.assertIn('Cửa trước',title)
        self.assertFalse(any(b.startswith(('sync:','ss:','up:')) for b in self.callbacks(buttons)))
        with self.assertRaises(ValueError):self.telegram._camera(self.archive,self.token)
        self.assertEqual(self.telegram._archive_camera(self.archive,self.token)['name'],'Cửa trước')

    def test_pending_index_stops_and_directory_channel_is_available(self):
        directory=ChannelDirectory(self.archive)
        directory.remember({'id':self.channel,'type':'channel','title':'Kho cũ'})
        index=ChannelIndex(self.archive,self.telegram)
        index.enqueue('front','2026-10-11')
        self.archive.delete_camera('front','Cửa trước')
        with self.assertRaises(KeyError):index._camera('front')
        self.assertIsNone(next(c for c in directory.list() if c['chat_id']==self.channel)['bound_camera_id'])
        with patch.object(self.telegram,'request',side_effect=AssertionError('No network after removal')):
            with patch('archive_app.channel_index.time.time',return_value=self.now+100000000):
                index.process_one()
        saved=self.archive.conn.execute('SELECT storage_chat_id,storage_message_id,file_id FROM recordings WHERE key=?',(self.key,)).fetchone()
        self.assertEqual(tuple(saved),(self.channel,77,'retained-file'))


if __name__=='__main__':unittest.main()
