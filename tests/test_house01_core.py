import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from archive_app.core import Archive, Settings
from archive_app.sync import SyncQueue


class House01SettingsTests(unittest.TestCase):
    def load(self, **values):
        with patch.dict(os.environ, values, clear=True):
            return Settings.from_env()

    def test_channel_setup_can_start_without_id_but_never_uses_owner_gate(self):
        s=self.load(TELEGRAM_DESTINATION='channel',TELEGRAM_OWNER_USER_ID='42',
                    TELEGRAM_BOT_TOKEN='synthetic-token',ENABLE_UPLOAD='true')
        self.assertEqual(s.telegram_destination,'channel')
        self.assertEqual(s.storage_channel_id,0)
        self.assertEqual(s.cache_retention_hours,1)

    def test_negative_channel_forces_channel_mode_without_replacing_owner(self):
        s=self.load(TELEGRAM_STORAGE_CHANNEL_ID='-100123456',TELEGRAM_OWNER_USER_ID='42')
        self.assertEqual(s.telegram_destination,'channel')
        self.assertEqual(s.storage_channel_id,-100123456)
        self.assertEqual(s.effective_owner,42)

    def test_invalid_tenant_and_channel_inputs_are_rejected(self):
        for tenant in ('../house02','House01','', 'house01/house02'):
            with self.subTest(tenant=tenant), self.assertRaises(ValueError):self.load(TENANT_ID=tenant)
        for channel in ('42','0','True','-100abc','@public_channel'):
            with self.subTest(channel=channel), self.assertRaises(ValueError):self.load(TELEGRAM_STORAGE_CHANNEL_ID=channel)


class House01CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='house01-tests-')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.settings=Settings(self.root/'state',self.root/'cache',self.root/'input','UTC+07:00',
                               keep_cache=False,min_free_bytes=0,cache_retention_hours=1,
                               enable_upload=True,token='synthetic-token',owner_user_id=42,
                               telegram_destination='channel',storage_channel_id=-100123456)
        self.archive=Archive(self.settings);self.addCleanup(self.archive.close)
        self.archive.add_camera({'id':'front','name':'Front'})

    def row(self):
        self.settings.input_dir.mkdir(exist_ok=True)
        p=self.settings.input_dir/'opaque-filename.mp4'
        p.write_bytes(b'\x00\x00\x00\x18ftypisom'+b'\x00'*20)
        with patch('archive_app.core.subprocess.run',side_effect=AssertionError('No heavy metadata tools')):
            return self.archive.ingest_entry({'camera':'front','record_id':'opaque-stable-name',
                'path':str(p),'start_time':'2026-10-04T08:00:00+07:00',
                'end_time':'2026-10-04T08:00:10+07:00'})

    def test_database_is_bound_to_one_tenant_without_rewriting_keys(self):
        row=self.row()
        other=Settings(self.settings.state_dir,self.settings.cache_dir,self.settings.input_dir,
                       'UTC+07:00',tenant_id='house02')
        with self.assertRaises(ValueError):Archive(other)
        self.assertEqual(self.archive.conn.execute('SELECT key FROM recordings WHERE key=?',(row['key'],)).fetchone()[0],row['key'])

    def test_channel_gate_does_not_need_owner_start_and_missing_id_blocks(self):
        q=SyncQueue(self.archive)
        self.assertIsNone(q._gate('front'))
        self.settings.storage_channel_id=0
        self.assertEqual(q._gate('front')[0],'channel_not_configured')
        self.settings.storage_channel_id=-100123456
        self.archive.update_camera('front',{'upload_enabled':False})
        self.assertEqual(q._gate('front')[0],'camera_upload_disabled')

    def test_cleanup_after_one_hour_keeps_uploaded_index_and_source(self):
        row=self.row();cached=Path(row['local_path']);source=Path(row['source_path'])
        self.archive.mark_uploaded(row['key'],-100123456,7,'fixture-id','fixture-unique','video',900,
                                   storage_kind='channel',storage_chat_id=-100123456,storage_message_id=7)
        uploaded=self.archive.find_recording(row['key'][:32])['uploaded_at']
        with patch('archive_app.core.time.time',return_value=uploaded+3599):
            self.assertFalse(self.archive.cleanup(row['key']));self.assertTrue(cached.exists())
        with patch('archive_app.core.time.time',return_value=uploaded+3601):
            self.assertTrue(self.archive.cleanup(row['key']));self.assertFalse(cached.exists())
        saved=self.archive.find_recording(row['key'][:32])
        self.assertEqual(saved['status'],'uploaded');self.assertEqual(saved['storage_message_id'],7)
        self.assertTrue(source.exists());self.assertEqual(self.archive.browse(status='uploaded')['total'],1)

    def test_unposted_and_uncertain_cache_is_never_time_purged(self):
        row=self.row();cached=Path(row['local_path'])
        for status in ('downloaded','upload_unknown','needs_review','failed'):
            with self.archive.conn:self.archive.conn.execute('UPDATE recordings SET status=? WHERE key=?',(status,row['key']))
            with patch('archive_app.core.time.time',return_value=9999999999):
                self.assertFalse(self.archive.cleanup(row['key']));self.assertTrue(cached.exists())

    def test_inconsistent_channel_metadata_is_not_committed(self):
        row=self.row()
        for chat,msg in ((-100999,7),(-100123456,8)):
            with self.subTest(chat=chat,msg=msg),self.assertRaises(ValueError):
                self.archive.mark_uploaded(row['key'],-100123456,7,'fixture-id',storage_kind='channel',
                                           storage_chat_id=chat,storage_message_id=msg)
        self.assertEqual(self.archive.conn.execute('SELECT status FROM recordings WHERE key=?',(row['key'],)).fetchone()[0],'downloaded')


if __name__=='__main__':unittest.main()
