"""SQLite-real sync jobs, camera controls and synthetic SD/Telegram transport."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from archive_app.core import Archive,Settings,record_key
from archive_app.sd_source import SDSourceError
from archive_app.sync import SyncQueue
from archive_app.telegram import Telegram,ApiRejected


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.parent=(Path(__file__).parent if os.name=='nt' else Path(tempfile.gettempdir())).resolve()
        self.root=self.parent/('.tmp-sync-'+uuid.uuid4().hex);self.root.mkdir()
        (self.root/'input').mkdir()
        self.source=self.root/'input'/'synthetic.mp4';self.source.write_bytes(b'synthetic source')
        self.settings=Settings(self.root/'state',self.root/'cache',self.root/'input','UTC+07:00',
                               min_free_bytes=0,enable_upload=True,token='synthetic-token',owner_user_id=42,
                               allowed_users=(77,),keep_cache=True)
        self.archive=Archive(self.settings);self.archive.state('telegram_owner_started:42','1')
        self.queue=SyncQueue(self.archive)
        self.archive.add_camera({'id':'front','name':'Cua truoc','host':'192.168.1.11'})
        self.probe=patch.object(self.archive,'probe_camera',return_value={'tcp':{'8000':'open'}});self.probe.start()
        self.sd=patch('archive_app.sd_source.SDSource.sync',return_value={
            'backend':'isapi','searched':0,'downloaded':0,'imported':0,'already_known':0,'deferred':0,'backlog':0})
        self.sd_mock=self.sd.start()
        def normalize(source,destination,settings):
            destination.write_bytes(b'synthetic normalized');return {'duration':60,'codec_video':'h264','codec_audio':'aac','bytes':20}
        self.normalize=patch('archive_app.core.normalize',side_effect=normalize);self.normalize.start()
        self.telegram=Telegram(self.settings);self.calls=[]
        def request(method,fields,**kwargs):
            self.calls.append((method,fields,kwargs))
            return {'message_id':len(self.calls),'chat':{'id':42,'type':'private'},
                    'video':{'file_id':'synthetic-file-'+str(len(self.calls)),'file_unique_id':'synthetic-unique'}}
        self.telegram.request=request

    def tearDown(self):
        self.normalize.stop();self.sd.stop();self.probe.stop();self.archive.close()
        self.assertEqual(self.root.resolve().parent,self.parent);shutil.rmtree(self.root)

    def entry(self,camera='front',record_id='one'):
        return {'camera':camera,'record_id':record_id,'path':str(self.source),
                'start_time':'2026-10-03T10:00:00+07:00','end_time':'2026-10-03T10:01:00+07:00'}

    def manifest(self,entries=None,name='manifest.json'):
        path=self.settings.input_dir/name;path.write_text(json.dumps({'recordings':entries or [self.entry()]}),encoding='utf-8')
        return path

    def row(self,key):return dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?',(key,)).fetchone())

    def run_job(self,camera='front'):
        self.queue.enqueue(camera);self.assertTrue(self.queue.run_once(self.telegram))
        return self.queue.status(camera)['latest'][camera]

    def test_camera_upload_toggle_default_validation_and_persistence(self):
        self.assertTrue(self.archive.cameras()[0]['upload_enabled'])
        self.archive.update_camera('front',{'upload_enabled':False})
        with closing(Archive(self.settings)) as other:self.assertFalse(other.cameras()[0]['upload_enabled'])
        for value in ('false',0,1,None):
            with self.subTest(value=value),self.assertRaises(ValueError):self.archive.update_camera('front',{'upload_enabled':value})

    def test_migration_enables_old_camera_without_mutating_recording_identity(self):
        key=self.archive.ingest_entry(self.entry())['key'];before=self.row(key)
        self.archive.conn.execute('ALTER TABLE cameras DROP COLUMN upload_enabled');self.archive.conn.commit()
        other=Archive(self.settings)
        try:self.assertTrue(other.cameras()[0]['upload_enabled'])
        finally:other.close()
        self.assertEqual(self.row(key),before)

    def test_claim_upload_optional_camera_and_toggle_tombstone_filters(self):
        self.archive.add_camera({'id':'back'});a=self.archive.ingest_entry(self.entry())['key']
        b=self.archive.ingest_entry(self.entry('back'))['key']
        self.archive.update_camera('front',{'upload_enabled':False})
        self.assertIsNone(self.archive.claim_upload('front'))
        self.assertEqual(self.archive.claim_upload('back')['key'],b)
        self.archive.update_camera('front',{'upload_enabled':True})
        self.archive.conn.execute('UPDATE recordings SET deleted_at=? WHERE key=?',(time.time(),a));self.archive.conn.commit()
        self.assertIsNone(self.archive.claim_upload())

    def test_manifest_camera_filter_does_not_ingest_other_camera(self):
        path=self.manifest([self.entry(),self.entry('back'),{'camera':'back','invalid':'ignored'}])
        self.assertEqual(len(self.archive.ingest_manifest(path,camera='front')),1)
        self.assertEqual([r[0] for r in self.archive.conn.execute('SELECT camera FROM recordings')],['front'])
        self.assertEqual([c['id'] for c in self.archive.cameras()],['front'])

    def test_default_manifest_still_imports_all_cameras(self):
        self.assertEqual(len(self.archive.ingest_manifest(self.manifest([self.entry(),self.entry('back')]))),2)

    def test_enqueue_dedupes_queued_and_running_atomically(self):
        first=self.queue.enqueue('front')['jobs'][0]
        self.assertRegex(first['id'],r'^[a-f0-9]{32}$')
        self.assertEqual(self.queue.enqueue('front')['jobs'][0]['id'],first['id'])
        self.queue._claim()
        self.assertEqual(self.queue.enqueue('front')['jobs'][0]['id'],first['id'])
        self.assertEqual(self.queue.status()['latest']['front']['state'],'running')

    def test_concurrent_enqueue_uses_one_durable_job(self):
        def enqueue(_):
            other=Archive(self.settings)
            try:return SyncQueue(other).enqueue('front')['jobs'][0]['id']
            finally:other.close()
        with ThreadPoolExecutor(max_workers=4) as executor:ids=list(executor.map(enqueue,range(8)))
        self.assertEqual(len(set(ids)),1)
        self.assertEqual(len(self.queue.status()['jobs']),1)

    def test_enqueue_all_includes_upload_off_but_ignores_disabled_camera(self):
        self.archive.add_camera({'id':'back','upload_enabled':False})
        self.archive.add_camera({'id':'disabled','enabled':False})
        self.assertEqual({j['camera_id'] for j in self.queue.enqueue()['jobs']},{'front','back'})
        with self.assertRaises(ValueError):self.queue.enqueue('disabled')
        with self.assertRaises(KeyError):self.queue.enqueue('missing')

    def test_queue_source_actor_validation_history_limit_and_heartbeat(self):
        for args in ({'camera':'../x'},{'source':'token:secret'},{'actor':True},{'actor':'user password'}, {'actor':0}):
            with self.subTest(args=args),self.assertRaises(ValueError):self.queue.enqueue(**args)
        self.assertFalse(self.queue.status()['worker_alive'])
        heartbeat=self.settings.state_dir/'heartbeat';heartbeat.write_text('synthetic')
        self.assertTrue(self.queue.enqueue('front',actor=42)['worker_alive'])
        os.utime(heartbeat,(time.time()-121,time.time()-121));self.assertFalse(self.queue.status()['worker_alive'])
        for limit in (0,101,True):
            with self.assertRaises(ValueError):self.queue.status(limit=limit)

    def test_active_queue_and_terminal_history_are_bounded(self):
        self.archive.add_camera({'id':'back'})
        with patch('archive_app.sync.MAX_ACTIVE_JOBS',1):
            with self.assertRaises(ValueError):self.queue.enqueue()
        self.assertEqual(self.queue.status()['jobs'],[])
        with patch('archive_app.sync.MAX_HISTORY',2):
            for _ in range(4):self.run_job()
        self.assertEqual(len(self.queue.status()['jobs']),2)

    def test_restart_recovers_jobs_but_not_ambiguous_recordings(self):
        key=self.archive.ingest_entry(self.entry())['key'];self.archive.claim_upload('front')
        job=self.queue.enqueue('front')['jobs'][0];self.queue._claim()
        other=Archive(self.settings)
        try:
            self.assertEqual(other.recover_uploads(),1)
            q=SyncQueue(other);self.assertEqual(q.recover(),1);self.assertEqual(q.recover(),0)
            self.assertEqual(q.status()['jobs'][0]['id'],job['id'])
        finally:other.close()
        self.assertEqual(self.row(key)['status'],'upload_unknown')
        self.assertEqual(self.run_job()['code'],'upload_unknown');self.assertEqual(self.calls,[])

    def test_idle_worker_returns_false(self):self.assertFalse(self.queue.run_once(self.telegram))

    def test_sd_confirmed_no_recordings_is_completed_empty_result(self):
        job=self.run_job();self.assertEqual((job['state'],job['code']),('completed','no_new_recordings'))
        self.assertEqual(job['statistics']['sd_backend'],'isapi');self.assertEqual(self.calls,[])

    def test_missing_credentials_is_explicitly_blocked_not_synced(self):
        self.sd_mock.side_effect=SDSourceError('sd_credentials_missing','synthetic secret should never echo')
        job=self.run_job();self.assertEqual((job['state'],job['code']),('blocked','sd_credentials_missing'))
        self.assertNotIn('synthetic secret',json.dumps(job))

    def test_sd_adapter_import_upload_pipeline_uses_managed_staging(self):
        def sd_sync(*, progress=None):
            root=self.settings.cache_dir/'sd-stage'/'front';root.mkdir(parents=True)
            source=root/'sample.source';source.write_bytes(b'synthetic SDK download')
            self.archive.ingest_download(dict(self.entry(),source='sd-hcnetsdk'),source)
            source.unlink()
            return {'backend':'hcnetsdk','searched':1,'downloaded':1,'imported':1,'already_known':0,'deferred':0,'backlog':0}
        self.sd_mock.side_effect=sd_sync
        job=self.run_job();self.assertEqual((job['state'],job['code']),('completed','completed'))
        self.assertEqual(job['statistics']['sd_downloaded'],1);self.assertEqual(job['statistics']['uploaded'],1)
        self.assertEqual(len(self.calls),1)

    def test_sd_failure_still_imports_uploads_exports_but_not_completed_sd(self):
        self.sd_mock.side_effect=SDSourceError('sd_sdk_missing','SDK missing')
        self.manifest();job=self.run_job()
        self.assertEqual(job['code'],'sd_sdk_missing');self.assertEqual(job['state'],'blocked')
        self.assertEqual(job['statistics']['uploaded'],1)

    def test_job_import_upload_counts_and_camera_isolation(self):
        self.archive.add_camera({'id':'back'});self.manifest([self.entry(),self.entry('back')])
        job=self.run_job();self.assertEqual(job['statistics']['imported'],1);self.assertEqual(job['statistics']['uploaded'],1)
        self.assertEqual([r[0] for r in self.archive.conn.execute('SELECT camera FROM recordings')],['front'])
        second=self.run_job();self.assertEqual(second['code'],'no_new_recordings');self.assertEqual(second['statistics']['already_known'],1)
        self.assertEqual(len(self.calls),1)

    def test_probe_error_sanitized_and_does_not_block_existing_export_source(self):
        self.archive.probe_camera=unittest.mock.Mock(side_effect=OSError('token or password'))
        self.manifest();job=self.run_job();self.assertEqual(job['code'],'completed')
        self.assertEqual(job['statistics']['probe_error_type'],'OSError');self.assertNotIn('token or password',json.dumps(job))

    def test_bad_manifest_has_explicit_sanitized_failed_state(self):
        (self.settings.input_dir/'bad.json').write_text('contains password not JSON')
        job=self.run_job();self.assertEqual((job['state'],job['code']),('failed','scan_failed'))
        self.assertEqual(job['statistics']['failed'],1);self.assertNotIn('contains password',json.dumps(job))

    def test_global_upload_disabled_still_scans_retains_cache(self):
        self.settings.enable_upload=False;self.manifest();job=self.run_job()
        self.assertEqual(job['code'],'upload_disabled');self.assertEqual(job['statistics']['imported'],1)
        self.assertTrue(Path(self.row(record_key(self.entry()))['local_path']).is_file());self.assertEqual(self.calls,[])

    def test_camera_upload_disabled_still_scans_retains_cache(self):
        self.archive.update_camera('front',{'upload_enabled':False});self.manifest();job=self.run_job()
        self.assertEqual(job['code'],'camera_upload_disabled');self.assertEqual(job['statistics']['ready'],1)
        self.assertEqual(self.calls,[])

    def test_owner_gate_and_token_gate_respected(self):
        self.manifest();self.archive.state('telegram_owner_started:42','0')
        self.assertEqual(self.run_job()['code'],'owner_not_started')
        self.archive.state('telegram_owner_started:42','1');self.settings.token=''
        self.assertEqual(self.run_job()['code'],'telegram_not_configured');self.assertEqual(self.calls,[])

    def test_toggle_reread_between_uploads_stops_remaining_camera_only(self):
        self.manifest([self.entry(record_id='one'),self.entry(record_id='two')])
        real=self.telegram.request
        def request(*args,**kwargs):
            response=real(*args,**kwargs);self.archive.update_camera('front',{'upload_enabled':False});return response
        self.telegram.request=request;job=self.run_job()
        self.assertEqual(job['code'],'camera_upload_disabled');self.assertEqual(job['statistics']['uploaded'],1)
        self.assertEqual(job['statistics']['ready'],1)

    def test_disabled_after_enqueue_is_blocked_without_ingest_or_sd(self):
        self.manifest();self.queue.enqueue('front');self.archive.update_camera('front',{'enabled':False})
        self.assertTrue(self.queue.run_once(self.telegram));self.assertEqual(self.queue.status()['jobs'][0]['code'],'camera_disabled')
        self.sd_mock.assert_not_called();self.assertEqual(self.calls,[])

    def test_known_rate_limit_defers_without_duplicate_post(self):
        self.manifest();self.telegram.request=unittest.mock.Mock(side_effect=ApiRejected(429,120))
        self.assertEqual(self.run_job()['code'],'rate_limited');self.assertEqual(self.telegram.request.call_count,1)
        self.assertEqual(self.run_job()['code'],'rate_limited');self.assertEqual(self.telegram.request.call_count,1)

    def test_ambiguous_upload_not_retried_on_second_job(self):
        self.manifest();self.telegram.request=unittest.mock.Mock(side_effect=TimeoutError('password'))
        self.assertEqual(self.run_job()['code'],'upload_unknown');self.assertEqual(self.telegram.request.call_count,1)
        self.assertEqual(self.run_job()['code'],'upload_unknown');self.assertEqual(self.telegram.request.call_count,1)

    def test_needs_review_not_claimed_or_uploaded(self):
        key=self.archive.ingest_entry(self.entry())['key']
        self.archive.conn.execute("UPDATE recordings SET status='needs_review' WHERE key=?",(key,));self.archive.conn.commit()
        self.assertEqual(self.run_job()['code'],'needs_review');self.assertEqual(self.calls,[])

    def test_tombstone_retains_file_ids_and_hash_after_job_rescan(self):
        self.manifest();self.run_job();key=record_key(self.entry());self.archive.soft_delete(key,42);before=self.row(key)
        job=self.run_job();self.assertEqual(job['code'],'no_new_recordings');self.assertEqual(job['statistics']['deleted'],1)
        self.assertEqual(self.row(key),before);self.assertEqual(len(self.calls),1)

    def test_upload_batch_limit_is_bounded(self):
        self.manifest([self.entry(record_id=str(index)) for index in range(3)])
        with patch('archive_app.sync.MAX_UPLOADS_PER_JOB',2):job=self.run_job()
        self.assertEqual(job['code'],'upload_batch_limit');self.assertEqual(job['statistics']['uploaded'],2)
        self.assertEqual(job['statistics']['ready'],1)

    def test_sd_backlog_is_not_successfully_finished(self):
        self.sd_mock.return_value={**self.sd_mock.return_value,'backlog':3}
        key=self.archive.ingest_entry(self.entry())['key'];self.archive.mark_uploaded(key,'42',1,'fixture-file')
        self.assertEqual(self.run_job()['code'],'sd_batch_limit')

    def test_public_sync_status_never_exposes_paths_or_tokens(self):
        self.manifest();self.run_job();serialized=json.dumps(self.queue.status())
        for secret in ('synthetic-token',str(self.root),'file_id','source_path','local_path'):
            self.assertNotIn(secret,serialized)

    def test_sd_camera_secret_private_persistent_clear_and_blank_preserves(self):
        self.archive.update_camera('front',{'sd_password':'synthetic-device-password'})
        camera=self.archive.cameras()[0];self.assertTrue(camera['sd_password_configured']);self.assertNotIn('sd_password',camera)
        path=self.settings.state_dir/'camera-secrets'/'front.password'
        if os.name!='nt':self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
        with closing(Archive(self.settings)) as other:self.assertEqual(other.camera_sd_config('front')['sd_password'],'synthetic-device-password')
        self.archive.update_camera('front',{'sd_password':''});self.assertEqual(path.read_text(),'synthetic-device-password')
        self.archive.update_camera('front',{'sd_password_clear':True});self.assertFalse(path.exists())
        self.assertFalse(self.archive.cameras()[0]['sd_password_configured'])

    def test_sd_config_validation_password_utf8_and_safe_secret_rollback(self):
        cases=[{'sd_backend':'shell'},{'sd_channel':True},{'sd_channel':0},{'sd_lookback_hours':721},
               {'sd_timezone':'Not/AZone'},{'sd_password':'x'*65},{'sd_password':'é'*33},
               {'sd_username':'é'*33},{'sd_password':'nul\0byte'},{'sd_password_clear':1},
               {'sd_password':'test','sd_password_clear':True}]
        for changes in cases:
            with self.subTest(changes=changes),self.assertRaises((ValueError,UnicodeError)):self.archive.update_camera('front',changes)
        with patch.object(self.archive,'_write_camera_password',side_effect=OSError('write failed')):
            with self.assertRaises(OSError):self.archive.add_camera({'id':'failed_add','sd_password':'synthetic'})
        self.assertNotIn('failed_add',[camera['id'] for camera in self.archive.cameras()])

    def test_managed_download_ingestion_confined_without_weakening_manifest(self):
        root=self.settings.cache_dir/'sd-stage'/'front';root.mkdir(parents=True)
        source=root/'record.source';source.write_bytes(b'fixture')
        entry=self.entry();entry['path']=str(source)
        with self.assertRaises(ValueError):self.archive.ingest_entry(entry)
        row=self.archive.ingest_download(entry,source);self.assertEqual(row['status'],'downloaded')
        with self.assertRaises(ValueError):self.archive.ingest_download(self.entry('back'),source)
        with self.assertRaises(ValueError):self.archive.ingest_download(self.entry(),self.source)

    def test_secret_link_cannot_read_or_write_outside_managed_secret_directory(self):
        destination=self.settings.state_dir/'camera-secrets';target=self.root/'external-secrets';target.mkdir()
        try:destination.symlink_to(target,target_is_directory=True)
        except OSError:self.skipTest('Symlink creation is unavailable on this host')
        with self.assertRaises(ValueError):self.archive.camera_sd_config('front')
        with self.assertRaises(ValueError):self.archive.update_camera('front',{'sd_password':'synthetic-secret'})
        self.assertEqual(list(target.iterdir()),[])

    def test_managed_download_rejects_linked_camera_stage_source(self):
        root=self.settings.cache_dir/'sd-stage'/'front';root.mkdir(parents=True)
        link=root/'linked.source'
        try:link.symlink_to(self.source)
        except OSError:self.skipTest('Symlink creation is unavailable on this host')
        with self.assertRaises(ValueError):self.archive.ingest_download(self.entry(),link)
        self.assertEqual(self.source.read_bytes(),b'synthetic source')

    def test_status_truthful_configuration_not_model_reachability_success(self):
        status=self.archive.status();self.assertEqual(status['version'],'2.4')
        self.assertEqual(status['sd_auto_download'],'per_camera_configured');self.assertFalse(status['sd_sources'][0]['configured'])
        self.archive.update_camera('front',{'sd_password':'fixture'});self.assertTrue(self.archive.status()['sd_sources'][0]['configured'])


if __name__=='__main__':unittest.main()
