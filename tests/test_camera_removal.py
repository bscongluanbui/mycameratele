"""Camera removal preserves archives and serializes against media workers."""
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from archive_app.core import Archive, CameraBusyError, Settings, normalize
from archive_app.dashboard import DashboardServer
from archive_app.sync import SyncQueue


class RemovalFixture(unittest.TestCase):
    def setUp(self):
        self.parent=(Path(__file__).parent if os.name=='nt' else Path(tempfile.gettempdir())).resolve()
        self.root=self.parent/('.tmp-camera-removal-'+uuid.uuid4().hex);self.root.mkdir()
        self.settings=Settings(self.root/'state',self.root/'cache',self.root/'input','UTC+07:00',min_free_bytes=0)
        self.settings.input_dir.mkdir()
        self.archive=Archive(self.settings)
        self.archive.add_camera({'id':'cash','name':'Quầy tiền','host':'192.168.2.9','sd_password':'synthetic-only',
                                 'channel_chat_id':-10012345,'channel_name':'Kho quầy'})

    def tearDown(self):
        self.archive.close()
        self.assertEqual(self.root.resolve().parent,self.parent);shutil.rmtree(self.root)

    def seed(self,status='uploaded',record_id='clip'):
        key=uuid.uuid4().hex*2
        with self.archive.conn:
            self.archive.conn.execute('''INSERT INTO recordings
                (key,camera,record_id,start_ms,end_ms,source_path,status,created_at,
                 chat_id,message_id,file_id,storage_chat_id,storage_message_id,storage_kind)
                VALUES(?, 'cash', ?, 1791676800000,1791676860000,'synthetic',?,?,
                    '-10012345',51,'saved-file',-10012345,51,'channel')''',(key,record_id,status,time.time()))
        return key


class CameraRemovalTests(RemovalFixture):
    def test_removal_keeps_exact_recordings_and_names_but_hides_active_camera(self):
        self.seed()
        before=[tuple(r) for r in self.archive.conn.execute('SELECT * FROM recordings')]
        queued=SyncQueue(self.archive).enqueue('cash')['jobs'][0]['id']
        self.assertEqual(self.archive.delete_camera('cash','Quầy tiền'),
                         {'deleted':True,'camera_id':'cash','recordings_preserved':True})
        self.assertEqual(self.archive.cameras(),[])
        self.assertEqual(self.archive.camera_name('cash'),'Quầy tiền')
        self.assertEqual(self.archive.browse()['recordings'][0]['camera_name'],'Quầy tiền')
        self.assertEqual([tuple(r) for r in self.archive.conn.execute('SELECT * FROM recordings')],before)
        self.assertFalse(self.archive._camera_password_path('cash').exists())
        camera=dict(self.archive.conn.execute('SELECT * FROM cameras').fetchone())
        self.assertIsNotNone(camera['deleted_at']);self.assertFalse(camera['enabled']);self.assertFalse(camera['upload_enabled'])
        self.assertIsNone(camera['channel_chat_id']);self.assertEqual(camera['channel_name'],'')
        job=SyncQueue(self.archive).status('cash')['jobs'][0]
        self.assertEqual((job['id'],job['state'],job['code']),(queued,'blocked','camera_deleted'))
        self.assertIsNotNone(job['finished_at'])
        self.assertEqual(self.archive.status()['counts']['cameras'],0)

    def test_missing_removed_bad_slug_and_confirmation_never_delete(self):
        for slug,name,kind in [('cash','wrong',ValueError),('cash',None,ValueError),('../cash','Quầy tiền',ValueError),
                                ('missing','Quầy tiền',KeyError)]:
            with self.subTest(slug=slug,name=name),self.assertRaises(kind):self.archive.delete_camera(slug,name)
        self.assertEqual(len(self.archive.cameras()),1)
        self.archive.delete_camera('cash','Quầy tiền')
        with self.assertRaises(KeyError):self.archive.delete_camera('cash','Quầy tiền')
        with self.assertRaises(KeyError):self.archive.update_camera('cash',{'enabled':True})
        with self.assertRaises(KeyError):self.archive.camera_sd_config('cash')
        with self.assertRaises(KeyError):self.archive.resolve_camera_channel('cash')

    def test_running_sync_refuses_removal_without_touching_secret(self):
        queue=SyncQueue(self.archive);queue.enqueue('cash');queue._claim()
        with self.assertRaises(CameraBusyError):self.archive.delete_camera('cash','Quầy tiền')
        self.assertEqual(self.archive.camera_sd_config('cash')['sd_password'],'synthetic-only')
        self.assertEqual(len(self.archive.cameras()),1)

    def test_inflight_upload_or_ingest_refuses_removal_and_preserves_rows(self):
        for status in ('uploading','ingesting'):
            key=self.seed(status,status)
            with self.subTest(status=status),self.assertRaises(CameraBusyError):self.archive.delete_camera('cash','Quầy tiền')
            self.assertEqual(self.archive.conn.execute('SELECT status FROM recordings WHERE key=?',(key,)).fetchone()[0],status)
            with self.archive.conn:self.archive.conn.execute('DELETE FROM recordings WHERE key=?',(key,))

    def test_running_channel_index_refuses_removal(self):
        with self.archive.conn:
            self.archive.conn.execute('CREATE TABLE channel_index_jobs(camera_id TEXT,status TEXT)')
            self.archive.conn.execute("INSERT INTO channel_index_jobs VALUES('cash','running')")
        with self.assertRaises(CameraBusyError):self.archive.delete_camera('cash','Quầy tiền')

    def test_failed_secret_cleanup_rolls_back_camera_and_queued_jobs(self):
        queue=SyncQueue(self.archive);queue.enqueue('cash')
        with patch.object(self.archive,'_write_camera_password',side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):self.archive.delete_camera('cash','Quầy tiền')
        self.assertEqual(len(self.archive.cameras()),1)
        self.assertEqual(queue.status('cash')['jobs'][0]['state'],'queued')
        self.assertEqual(self.archive.camera_sd_config('cash')['sd_password'],'synthetic-only')

    def test_deleted_ids_remain_reserved_channel_can_be_assigned_to_new_camera(self):
        self.seed();self.archive.delete_camera('cash','Quầy tiền')
        self.archive.close();self.archive=Archive(self.settings)
        self.assertEqual(self.archive.cameras(),[])
        with self.assertRaisesRegex(ValueError,'reserved'):
            self.archive.add_camera({'id':'cash','host':'192.168.2.9'})
        new=self.archive.add_camera({'id':'replacement','name':'Quầy mới','host':'192.168.2.9','channel_chat_id':-10012345})
        self.assertFalse(new['sd_password_configured']);self.assertEqual(new['channel_status'],'unconfigured')
        self.assertEqual(self.archive.browse()['recordings'][0]['camera_name'],'Quầy tiền')

    def test_delete_wins_prevents_new_sync_upload_and_ingest_claim(self):
        self.seed('downloaded')
        queue=SyncQueue(self.archive);queue.enqueue('cash')
        self.archive.delete_camera('cash','Quầy tiền')
        self.assertEqual(queue.enqueue()['jobs'],[])
        with self.assertRaises(KeyError):queue.enqueue('cash')
        self.assertIsNone(queue._claim());self.assertIsNone(self.archive.claim_upload())
        self.assertEqual(self.archive.pending_upload_cameras(),[])
        source=self.settings.input_dir/'sample.ps';source.write_bytes(b'\x00\x00\x01\xba'+b'fixture'*20)
        entry={'camera':'cash','record_id':'new','start_time':'2026-10-11T01:00:00+07:00',
               'end_time':'2026-10-11T01:01:00+07:00','path':'sample.ps'}
        with patch('archive_app.core.normalize') as media:
            self.assertEqual(self.archive.ingest_entry(entry)['status'],'camera_disabled');media.assert_not_called()

    def test_upload_claim_wins_deletion_waits(self):
        self.seed('downloaded')
        self.assertIsNotNone(self.archive.claim_upload('cash'))
        with self.assertRaises(CameraBusyError):self.archive.delete_camera('cash','Quầy tiền')

    def test_delete_between_ingest_precheck_and_claim_stops_normalize(self):
        source=self.settings.input_dir/'sample.ps';source.write_bytes(b'\x00\x00\x01\xba'+b'fixture'*20)
        entry={'camera':'cash','record_id':'new','start_time':'2026-10-11T01:00:00+07:00',
               'end_time':'2026-10-11T01:01:00+07:00','path':'sample.ps'}
        @contextmanager
        def remove_before_claim(*args):
            other=Archive(self.settings)
            try:other.delete_camera('cash','Quầy tiền')
            finally:other.close()
            yield True
        with patch('archive_app.pipeline_runtime.reserve_cache',remove_before_claim),patch('archive_app.core.normalize') as media:
            self.assertEqual(self.archive.ingest_entry(entry)['status'],'camera_disabled');media.assert_not_called()
        self.assertEqual(self.archive.conn.execute('SELECT count(*) FROM recordings').fetchone()[0],0)

    def test_ingest_claim_blocks_removal_from_second_connection(self):
        source=self.settings.input_dir/'sample.ps';source.write_bytes(b'\x00\x00\x01\xba'+b'fixture'*20)
        entry={'camera':'cash','record_id':'new','start_time':'2026-10-11T01:00:00+07:00',
               'end_time':'2026-10-11T01:01:00+07:00','path':'sample.ps'}
        def inspect_claim(*args,**kwargs):
            other=Archive(self.settings)
            try:
                with self.assertRaises(CameraBusyError):other.delete_camera('cash','Quầy tiền')
            finally:other.close()
            return normalize(*args,**kwargs)
        with patch('archive_app.core.normalize',side_effect=inspect_claim):
            self.assertEqual(self.archive.ingest_entry(entry)['status'],'downloaded')

    def test_delete_between_pending_remux_selection_and_claim_preserves_raw_cache(self):
        key=self.seed('downloaded')
        source=self.settings.cache_dir/(key+'.ps');source.write_bytes(b'\x00\x00\x01\xba'+b'fixture'*20)
        with self.archive.conn:
            self.archive.conn.execute("""UPDATE recordings SET processing_method='passthrough',local_path=?,
                file_id=NULL,message_id=NULL,chat_id=NULL,storage_chat_id=NULL,storage_message_id=NULL WHERE key=?""",(str(source),key))
        self.settings.media_mode='remux_copy'
        @contextmanager
        def remove_before_claim(*args):
            other=Archive(self.settings)
            try:other.delete_camera('cash','Quầy tiền')
            finally:other.close()
            yield True
        with patch('archive_app.pipeline_runtime.reserve_cache',remove_before_claim),patch('archive_app.core.normalize') as media:
            self.assertEqual(self.archive.remux_pending_cache('cash'),{'converted':0,'failed':0});media.assert_not_called()
        self.assertTrue(source.exists())
        self.assertEqual(self.archive.conn.execute('SELECT status FROM recordings WHERE key=?',(key,)).fetchone()[0],'downloaded')

    def test_additive_migration_preserves_legacy_camera(self):
        self.archive.close()
        path=self.root/'legacy';path.mkdir()
        connection=sqlite3.connect(path/'archive.db')
        connection.execute("CREATE TABLE cameras(id TEXT PRIMARY KEY,name TEXT NOT NULL,model TEXT DEFAULT '',host TEXT DEFAULT '',device_port INTEGER DEFAULT 8000,rtsp_port INTEGER DEFAULT 554,http_port INTEGER DEFAULT 80,enabled INTEGER DEFAULT 1,probe_json TEXT,created_at REAL)")
        connection.execute("INSERT INTO cameras(id,name,created_at) VALUES('legacy','Old camera',1)");connection.commit();connection.close()
        self.archive=Archive(Settings(path,self.root/'other-cache',self.settings.input_dir,'UTC+07:00',min_free_bytes=0))
        self.assertEqual(self.archive.cameras()[0]['name'],'Old camera')
        self.assertIsNone(self.archive.cameras()[0]['deleted_at'])
        self.assertEqual(self.archive.cameras()[0]['sd_download_threads'],1)


class CameraRemovalHTTPTests(RemovalFixture):
    def setUp(self):
        super().setUp()
        self.server=DashboardServer(('127.0.0.1',0),self.settings)
        self.server.daemon_threads=False
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01});self.thread.start()
        self.cookie='';self.csrf=''

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)
        super().tearDown()

    def request(self,method,path,data=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=30)
        fields={'Cookie':self.cookie,'X-CSRF-Token':self.csrf,**(headers or {})}
        if data is not None:fields['Content-Type']='application/json';data=json.dumps(data).encode()
        connection.request(method,path,data,fields);response=connection.getresponse();body=json.loads(response.read())
        cookie=response.getheader('Set-Cookie','');connection.close()
        return response.status,body,cookie

    def login(self,setup=True):
        if setup:self.server.auth.change('admin','fixture_admin','synthetic-password',1)
        status,body,cookie=self.request('POST','/api/login',{'username':'fixture_admin' if setup else 'admin',
                                    'password':'synthetic-password' if setup else 'admin'})
        self.assertEqual(status,200);self.cookie=cookie.split(';')[0];self.csrf=body['csrf_token']

    def test_delete_obeys_auth_initial_password_csrf_and_origin(self):
        data={'confirm_name':'Quầy tiền'}
        self.assertEqual(self.request('DELETE','/api/cameras/cash',data)[0],401)
        self.login(False)
        self.assertEqual(self.request('DELETE','/api/cameras/cash',data)[0],409)
        self.server.auth.change('admin','fixture_admin','synthetic-password',1)
        status,body,cookie=self.request('POST','/api/login',{'username':'fixture_admin','password':'synthetic-password'})
        self.cookie=cookie.split(';')[0];self.csrf=body['csrf_token']
        self.assertEqual(self.request('DELETE','/api/cameras/cash',data,{'X-CSRF-Token':'wrong'})[0],403)
        self.assertEqual(self.request('DELETE','/api/cameras/cash',data,{'Origin':'http://elsewhere.example'})[0],403)
        self.assertEqual(len(self.archive.cameras()),1)

    def test_delete_body_query_confirmation_and_missing_validation(self):
        self.login()
        for path,data in [('/api/cameras/cash',{}),('/api/cameras/cash',{'confirm_name':'wrong'}),
                          ('/api/cameras/cash',{'confirm_name':'Quầy tiền','purge':True}),
                          ('/api/cameras/cash?force=1',{'confirm_name':'Quầy tiền'}),
                          ('/api/cameras/bad%2Fslug',{'confirm_name':'Quầy tiền'})]:
            with self.subTest(path=path,data=data):self.assertEqual(self.request('DELETE',path,data)[0],400)
        self.assertEqual(self.request('DELETE','/api/cameras/missing',{'confirm_name':'Quầy tiền'})[0],404)
        self.assertEqual(len(self.archive.cameras()),1)

    def test_success_and_busy_responses(self):
        self.login();key=self.seed('uploading')
        status,body,_=self.request('DELETE','/api/cameras/cash',{'confirm_name':'Quầy tiền'})
        self.assertEqual((status,body['code']),(409,'camera_busy'))
        with self.archive.conn:self.archive.conn.execute("UPDATE recordings SET status='uploaded' WHERE key=?",(key,))
        status,body,_=self.request('DELETE','/api/cameras/cash',{'confirm_name':'Quầy tiền'})
        self.assertEqual(status,200);self.assertEqual(body,{'deleted':True,'camera_id':'cash','recordings_preserved':True})
        self.assertEqual(self.request('GET','/api/cameras')[1]['cameras'],[])
        self.assertEqual(self.request('DELETE','/api/cameras/cash',{'confirm_name':'Quầy tiền'})[0],404)
        status,body,_=self.request('POST','/api/cameras',{'id':'cash','name':'Quầy mới'})
        self.assertEqual((status,body['code']),(409,'camera_id_reserved'))


if __name__=='__main__':unittest.main()
