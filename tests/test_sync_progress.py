"""SD snapshots remain cumulative and visible even if a later file fails."""
import os,shutil,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from archive_app.core import Archive,Settings
from archive_app.sync import SyncQueue
from archive_app.sd_source import SDSourceError
from archive_app.telegram import Telegram

class SyncProgressTests(unittest.TestCase):
    def setUp(self):
        self.parent=(Path(__file__).parent if os.name=='nt' else Path(tempfile.gettempdir())).resolve()
        self.root=self.parent/('.tmp-sync-progress-'+uuid.uuid4().hex);self.root.mkdir()
        self.settings=Settings(self.root/'state',self.root/'cache',self.root/'input','UTC+07:00',min_free_bytes=0)
        self.a=Archive(self.settings);self.a.add_camera({'id':'fixture'})
    def tearDown(self):
        self.a.close();self.assertEqual(self.root.resolve().parent,self.parent);shutil.rmtree(self.root)
    def test_partial_sd_statistics_survive_later_failure_without_double_count(self):
        q=SyncQueue(self.a);q.enqueue('fixture');observed=[]
        def fake_sync(*,progress):
            snapshot={'phase':'sd_download','backend':'isapi','searched':2,'downloaded':1,'imported':1,'already_known':0,'deferred':0,'backlog':0}
            progress(snapshot);progress(snapshot)
            observed.append(q.status('fixture')['latest']['fixture'])
            raise SDSourceError('sd_download_incomplete','fixed failure')
        with patch.object(Archive,'probe_camera',return_value={'tcp':{}}),patch('archive_app.sd_source.SDSource.sync',side_effect=fake_sync):
            self.assertTrue(q.run_once(Telegram(self.settings)))
        self.assertEqual(observed[0]['phase'],'sd_download');self.assertEqual(observed[0]['state'],'running')
        job=q.status('fixture')['latest']['fixture'];self.assertEqual(job['code'],'sd_download_incomplete')
        self.assertEqual(job['statistics']['sd_downloaded'],1);self.assertEqual(job['statistics']['imported'],1)
    def test_camera_disabled_during_sd_is_explicit_even_without_records(self):
        q=SyncQueue(self.a);q.enqueue('fixture')
        def pause(*,progress):
            self.a.update_camera('fixture',{'enabled':False});raise SDSourceError('camera_disabled','fixed pause')
        with patch.object(Archive,'probe_camera',return_value={'tcp':{}}),patch('archive_app.sd_source.SDSource.sync',side_effect=pause):
            q.run_once(Telegram(self.settings))
        job=q.status('fixture')['latest']['fixture'];self.assertEqual((job['state'],job['code']),('blocked','camera_disabled'))

    def test_restart_recovers_interrupted_ingest_not_posted_uncertain_or_deleted(self):
        import time
        keys=['a'*64,'b'*64,'c'*64,'d'*64]
        for key in keys:
            self.a.conn.execute("INSERT INTO recordings(key,camera,record_id,start_ms,end_ms,source_path,status,created_at) VALUES(?, 'fixture', ?,1,2,'/cache/sd-stage/fixture/lost.source','ingesting',?)",(key,key,time.time()))
        self.a.conn.execute("UPDATE recordings SET status='upload_unknown',file_id='uncertain-id',message_id=1,chat_id='42' WHERE key=?",(keys[1],))
        self.a.conn.execute("UPDATE recordings SET status='uploaded',file_id='posted-id',message_id=2,chat_id='42' WHERE key=?",(keys[2],))
        self.a.conn.execute('UPDATE recordings SET deleted_at=1 WHERE key=?',(keys[3],));self.a.conn.commit()
        partial=self.settings.cache_dir/(keys[0]+'.part.mp4');partial.write_bytes(b'synthetic unconfirmed remux')
        final=self.settings.cache_dir/(keys[0]+'.mp4');final.write_bytes(b'synthetic preserved output')
        before=[dict(row) for row in self.a.conn.execute('SELECT * FROM recordings WHERE key!=? ORDER BY key',(keys[0],))]
        self.assertEqual(self.a.recover_ingests(),1);self.assertFalse(partial.exists());self.assertTrue(final.exists())
        row=self.a.conn.execute('SELECT * FROM recordings WHERE key=?',(keys[0],)).fetchone()
        self.assertEqual((row['status'],row['last_error']),('failed','worker_restarted_during_ingest'))
        after=[dict(row) for row in self.a.conn.execute('SELECT * FROM recordings WHERE key!=? ORDER BY key',(keys[0],))]
        self.assertEqual(before,after);self.assertEqual(self.a.recover_ingests(),0)
