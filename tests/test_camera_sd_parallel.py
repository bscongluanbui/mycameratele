"""Offline two-file downloads, ordered ingestion and live camera capacity."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from archive_app.core import Archive, Settings
from archive_app.pipeline_runtime import PipelineRuntime, _CACHE_RESERVATIONS
from archive_app.sd_source import SDSource, SDSourceError, HCNetSDKSource, _camera_download_lease
from archive_app.sync import SyncQueue


NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


class CameraSDParallelTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).parent if os.name == 'nt' else Path(tempfile.gettempdir())
        self.root = parent.resolve()/('.tmp-sd-pair-'+uuid.uuid4().hex)
        self.root.mkdir()
        self.settings = Settings(self.root/'state', self.root/'cache', self.root/'input', 'UTC+07:00',
                                 min_free_bytes=0, cache_max_bytes=1024*1024,
                                 media_mode='remux_copy')
        self.archive = Archive(self.settings)
        self.archive.add_camera({'id':'front','name':'Front','sd_download_threads':2})
        self.config = {'id':'front','host':'192.168.2.9','sd_username':'admin','sd_password':'fixture',
                       'sd_channel':1,'sd_timezone':'UTC+07:00','sd_download_threads':2}
        self.files = []
        for number in range(6):
            start = NOW-timedelta(hours=1)+timedelta(minutes=number*2)
            self.files.append({'record_id':f'file-{number}','size':10,
                               'start_time':start.isoformat(),
                               'end_time':(start+timedelta(minutes=1)).isoformat()})
        self.created = []
        self.transfers = []
        self.ingested = []
        self.progress_threads = []
        self.guard = threading.Lock()
        self.active = 0
        self.peak = 0
        self.download_hook = lambda *_: None

    def tearDown(self):
        self.archive.close()
        self.assertFalse(_CACHE_RESERVATIONS)
        shutil.rmtree(self.root)

    def factory(self, source):
        owner = self
        class Provider:
            backend = 'hcnetsdk'
            def __init__(self):
                self.owner = threading.get_ident()
                self.entered = self.closed = False
                owner.created.append(self)
            def __enter__(self):
                owner.assertEqual(self.owner, threading.get_ident())
                self.entered = True
                return self
            def __exit__(self, *_):
                owner.assertEqual(self.owner, threading.get_ident())
                self.closed = True
            def search(self, *_):
                owner.assertEqual(self.owner, threading.get_ident())
                return list(reversed(owner.files))
            def download(self, item, path):
                owner.assertEqual(self.owner, threading.get_ident())
                owner.assertTrue(self.entered)
                with owner.guard:
                    owner.active += 1
                    owner.peak = max(owner.peak, owner.active)
                    owner.transfers.append((item['record_id'],path,self.max_bytes,self.owner))
                try:
                    path.write_bytes(b'x'*10)
                    self.download_progress({'current_file_bytes':10,'current_file_total_bytes':10})
                    owner.download_hook(item,path)
                finally:
                    with owner.guard:owner.active -= 1
        return Provider()

    def normalize(self, source, destination, settings):
        self.assertEqual(threading.get_ident(), self.owner_thread)
        destination.write_bytes(source.read_bytes())
        self.ingested.append(destination.name)
        return {'duration':60,'bytes':10,'processing_method':'remux_copy',
                'container':'mp4','file_extension':'.mp4'}

    def run_source(self, **kwargs):
        self.owner_thread = threading.get_ident()
        with patch.object(SDSource,'_provider',lambda source:self.factory(source)), patch('archive_app.core.normalize',self.normalize):
            return SDSource(self.archive,'front',self.config).sync(now=NOW,**kwargs)

    def test_two_real_distinct_downloads_overlap_and_ingest_chronologically(self):
        barrier = threading.Barrier(2)
        def download(item, path):
            barrier.wait(10)
            if item['record_id']=='file-0':time.sleep(0.05)
        self.download_hook = download
        snapshots = []
        def progress(snapshot):
            self.progress_threads.append(threading.get_ident())
            snapshots.append(snapshot)
        result = self.run_source(max_files=4,progress=progress)
        self.assertEqual((result['downloaded'],result['imported'],result['backlog']),(4,4,2))
        self.assertEqual((result['download_threads'],result['peak_downloads']),(2,2))
        self.assertEqual(self.peak,2)
        self.assertEqual(len({x[1] for x in self.transfers}),4)
        self.assertEqual(len(self.created),3)  # Search + exactly two persistent sessions.
        self.assertTrue(all(p.closed for p in self.created))
        self.assertEqual(len({p.owner for p in self.created}),3)
        self.assertEqual(set(self.progress_threads),{self.owner_thread})
        starts=[r['start_ms'] for r in self.archive.conn.execute('SELECT * FROM recordings ORDER BY created_at,rowid')]
        self.assertEqual(starts,sorted(starts))
        self.assertEqual(len(self.ingested),4)
        self.assertEqual(list((self.settings.cache_dir/'sd-stage'/'front').iterdir()),[])

    def test_limit_one_and_duplicate_search_rows_never_overshoot(self):
        self.files.insert(1,dict(self.files[0],record_id='same-time-another-filename'))
        result=self.run_source(max_files=1)
        self.assertEqual((result['downloaded'],result['imported'],result['backlog']),(1,1,5))
        self.assertEqual(result['already_known'],1)
        self.assertEqual(len(self.transfers),1)
        self.assertEqual(len(self.created),2)

    def test_budget_falls_back_to_one_and_releases_every_reservation(self):
        self.files=self.files[:2]
        self.settings.cache_max_bytes=45  # One raw+MP4 working set fits; two do not.
        result=self.run_source()
        self.assertEqual(result['imported'],2)
        self.assertEqual(self.peak,1)
        self.assertEqual(len(self.created),2)

    def test_budget_rejects_before_any_download(self):
        self.settings.cache_max_bytes=20
        with self.assertRaises(SDSourceError) as error:self.run_source()
        self.assertEqual(error.exception.code,'sd_cache_budget')
        self.assertEqual(self.transfers,[])
        self.assertEqual(self.archive.conn.execute('SELECT count(*) FROM recordings').fetchone()[0],0)

    def test_unknown_sizes_share_reserved_budget_and_are_bounded(self):
        self.files=self.files[:2]
        for item in self.files:item['size']=0
        self.settings.cache_max_bytes=600
        barrier=threading.Barrier(2)
        self.download_hook=lambda *_:barrier.wait(10)
        result=self.run_source()
        self.assertEqual(result['imported'],2)
        self.assertEqual([x[2] for x in self.transfers],[100,100])

    def test_stop_drains_both_received_files_without_starting_next_pair(self):
        stop=threading.Event()
        barrier=threading.Barrier(2)
        def download(*_):
            barrier.wait(10)
            stop.set()
        self.download_hook=download
        result=self.run_source(stop_requested=stop.is_set)
        self.assertTrue(result['interrupted'])
        self.assertEqual((result['imported'],result['backlog']),(2,4))
        self.assertEqual(len(self.transfers),2)
        self.assertEqual(len(list(self.settings.cache_dir.glob('*.mp4'))),2)

    def test_older_failed_transfer_drains_but_never_publishes_newer_clip(self):
        barrier=threading.Barrier(2)
        def download(item,path):
            barrier.wait(10)
            if item['record_id']=='file-0':raise SDSourceError('sd_download_incomplete','Synthetic transfer failure')
        self.download_hook=download
        with self.assertRaises(SDSourceError) as error:self.run_source()
        self.assertEqual(error.exception.code,'sd_download_incomplete')
        self.assertEqual(len(self.transfers),2)
        self.assertEqual(len(self.ingested),0)
        self.assertEqual(self.archive.conn.execute('SELECT count(*) FROM recordings').fetchone()[0],0)
        self.assertTrue(all(p.closed for p in self.created))
        self.assertEqual(list((self.settings.cache_dir/'sd-stage'/'front').iterdir()),[])

    def test_progress_failure_drains_then_stops_and_closes_sessions(self):
        barrier=threading.Barrier(2)
        self.download_hook=lambda *_:barrier.wait(10)
        def progress(snapshot):
            if snapshot['active_downloads'] or snapshot['imported']:
                raise RuntimeError('Synthetic callback failure')
        with self.assertRaises(SDSourceError) as error:self.run_source(progress=progress)
        self.assertIn(error.exception.code,('sd_progress_failed','sd_media_validation_failed'))
        self.assertEqual(len(self.transfers),2)
        self.assertLessEqual(len(self.ingested),1)
        self.assertTrue(all(p.closed for p in self.created))

    def test_cleanup_error_holds_pair_reservation_until_sibling_drains(self):
        cleanup_seen=threading.Event();sibling_entered=threading.Event();release=threading.Event()
        observed=[];original_unlink=Path.unlink;failures=[]
        def download(item,path):
            if item['record_id']=='file-1':
                sibling_entered.set()
                if not release.wait(10):raise RuntimeError('Sibling release timed out')
            else:
                if not sibling_entered.wait(10):raise RuntimeError('Sibling did not start')
        self.download_hook=download
        def unlink(path,*args,**kwargs):
            if path.suffix=='.source' and not failures:
                failures.append(path);cleanup_seen.set()
                raise PermissionError('Synthetic staging cleanup failure')
            return original_unlink(path,*args,**kwargs)
        def observe():
            if cleanup_seen.wait(10):
                observed.append(bool(_CACHE_RESERVATIONS))
                time.sleep(.1)
                observed.append(bool(_CACHE_RESERVATIONS))
            release.set()
        observer=threading.Thread(target=observe);observer.start()
        try:
            with patch.object(Path,'unlink',unlink),self.assertRaises(SDSourceError) as error:
                self.run_source()
            self.assertEqual(error.exception.code,'sd_stage_invalid')
            self.assertEqual(observed,[True,True])
            self.assertEqual(len(self.transfers),2)
            self.assertEqual(len(self.ingested),1)
        finally:release.set();observer.join()

    def test_download_session_login_failure_drains_other_session_without_publish(self):
        original=self.factory
        def factory(source):
            provider=original(source)
            if threading.current_thread().name=='sd-file-front-1':
                def enter():raise SDSourceError('sd_auth_failed','Synthetic login failure')
                provider.__class__.__enter__=lambda session:enter()
            return provider
        with patch.object(self,'factory',factory),self.assertRaises(SDSourceError) as error:
            self.run_source()
        self.assertEqual(error.exception.code,'sd_auth_failed')
        self.assertEqual(self.ingested,[])
        self.assertTrue(all(p.closed for p in self.created))

    def test_invalid_thread_values_rejected_before_provider(self):
        for value in (0,3,True,2.0,'2'):
            with self.subTest(value=value):
                self.config['sd_download_threads']=value
                with self.assertRaises(SDSourceError):self.run_source()
        self.assertEqual(self.created,[])

    def test_same_camera_lease_blocks_second_sync_before_staging_or_provider(self):
        with _camera_download_lease(self.archive,'front'):
            with self.assertRaises(SDSourceError) as error:self.run_source()
        self.assertEqual(error.exception.code,'sd_camera_busy')
        self.assertEqual(self.created,[])
        self.assertEqual(self.run_source(max_files=1)['imported'],1)

    def test_later_failed_transfer_keeps_only_successful_serial_prefix(self):
        barrier=threading.Barrier(2)
        def download(item,path):
            barrier.wait(10)
            if item['record_id']=='file-1':raise SDSourceError('sd_download_incomplete','Synthetic later failure')
        self.download_hook=download
        with self.assertRaises(SDSourceError):self.run_source()
        self.assertEqual(len(self.transfers),2)
        rows=list(self.archive.conn.execute('SELECT * FROM recordings'))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['start_ms'],int(datetime.fromisoformat(self.files[0]['start_time']).timestamp()*1000))

    def test_native_helpers_are_real_distinct_processes_and_close(self):
        self.files=self.files[:2]
        log=self.root/'native-events';log.mkdir()
        markers=self.root/'native-markers';markers.mkdir()
        script="""
import json,os,pathlib,sys,time
events=pathlib.Path(sys.argv[1])/str(os.getpid());markers=pathlib.Path(sys.argv[2]);files=json.loads(sys.argv[3])
for raw in sys.stdin:
 p=json.loads(raw);cmd=p['command']
 with events.open('a') as f:f.write(json.dumps({'pid':os.getpid(),'command':cmd})+'\\n')
 if cmd=='close':break
 if cmd=='search':result=files
 elif cmd=='download':
  path=pathlib.Path(p['destination']);path.write_bytes(b'x'*10)
  (markers/str(os.getpid())).touch();deadline=time.monotonic()+10
  while len(list(markers.iterdir()))<2 and time.monotonic()<deadline:time.sleep(.01)
  if len(list(markers.iterdir()))<2:raise RuntimeError('No second helper')
  result=10
 else:result=True
 print(json.dumps({'ok':True,'result':result}),flush=True)
"""
        command=[sys.executable,'-B','-u','-c',script,str(log),str(markers),json.dumps(self.files)]
        def native(source):
            return HCNetSDKSource('192.168.2.9',8000,'admin','fixture',1,timezone.utc,100,
                                  cache_dir=self.settings.cache_dir,worker_command=command)
        self.owner_thread=threading.get_ident()
        with patch.object(SDSource,'_provider',native),patch('archive_app.core.normalize',self.normalize):
            result=SDSource(self.archive,'front',self.config).sync(now=NOW)
        self.assertEqual(result['imported'],2)
        events=[json.loads(line) for path in log.iterdir() for line in path.read_text().splitlines()]
        self.assertEqual(len({e['pid'] for e in events if e['command']=='init'}),3)
        downloads={e['pid'] for e in events if e['command']=='download'}
        self.assertEqual(len(downloads),2)
        self.assertTrue(downloads <= {e['pid'] for e in events if e['command']=='close'})


class LiveCameraLimitTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).parent if os.name=='nt' else Path(tempfile.gettempdir())
        self.root=parent.resolve()/('.tmp-live-limit-'+uuid.uuid4().hex);self.root.mkdir()
        self.settings=Settings(self.root/'state',self.root/'cache',self.root/'input','UTC+07:00',
                               min_free_bytes=0,sd_download_workers=1)
        self.archive=Archive(self.settings);self.queue=SyncQueue(self.archive)
        for number in range(7):self.archive.add_camera({'id':f'cam{number}','name':f'Cam {number}'})
        self.queue.enqueue()
        self.guard=threading.Lock();self.entered={};self.completed=set();self.peak=0
        self.runtime=PipelineRuntime(self.settings)

    def tearDown(self):
        for release in self.entered.values():release.set()
        self.runtime.stop();self.runtime.join();self.archive.close();shutil.rmtree(self.root)

    def download(self,queue,job,statistics,telegram):
        release=threading.Event()
        with self.guard:
            self.entered[job['camera_id']]=release
            self.peak=max(self.peak,len(self.entered)-len(self.completed))
        if not release.wait(15):raise RuntimeError('Synthetic job timed out')
        queue._finish(job,statistics,'completed','completed','Synthetic camera task')
        with self.guard:self.completed.add(job['camera_id'])

    def wait_for(self,condition):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if condition():return
            time.sleep(.01)
        self.fail('Synthetic concurrency condition timed out')

    def set_limit(self,count):
        self.archive.update_pipeline_settings({'sd_download_workers':count})
        from archive_app.__main__ import refresh_pipeline_limit
        refresh_pipeline_limit(self.archive,self.runtime)
        self.runtime.wake()

    def test_hot_raise_one_to_five_without_restarting_executor(self):
        with patch.object(SyncQueue,'_run',lambda queue,job,statistics,telegram:self.download(queue,job,statistics,telegram)):
            self.runtime.start();self.wait_for(lambda:len(self.entered)==1)
            threads=tuple(self.runtime._threads)
            self.set_limit(5);self.wait_for(lambda:len(self.entered)==5)
            self.assertEqual(tuple(self.runtime._threads),threads)
            self.assertEqual(self.peak,5)
            self.runtime.stop()
            for release in self.entered.values():release.set()
            self.runtime.join()

    def test_hot_lower_drains_existing_jobs_before_admitting_at_new_limit(self):
        self.set_limit(5)
        with patch.object(SyncQueue,'_run',lambda queue,job,statistics,telegram:self.download(queue,job,statistics,telegram)):
            self.runtime.start();self.wait_for(lambda:len(self.entered)==5)
            first=list(self.entered)
            self.set_limit(2)
            for name in first[:3]:self.entered[name].set()
            self.wait_for(lambda:len(self.completed)==3)
            time.sleep(.65)
            self.assertEqual(len(self.entered),5)
            self.entered[first[3]].set()
            self.wait_for(lambda:len(self.entered)==6)
            self.assertLessEqual(len(self.entered)-len(self.completed),2)
            self.runtime.stop()
            for release in self.entered.values():release.set()
            self.runtime.join()

    def test_worker_rechecks_persistent_limit_before_next_job_without_main_refresh(self):
        with patch.object(SyncQueue,'_run',lambda queue,job,statistics,telegram:self.download(queue,job,statistics,telegram)):
            self.runtime.start();self.wait_for(lambda:len(self.entered)==1)
            self.archive.update_pipeline_settings({'sd_download_workers':3})
            next(iter(self.entered.values())).set()
            self.wait_for(lambda:len(self.entered)>=4)
            self.assertLessEqual(self.peak,3)
            self.runtime.stop()
            for release in self.entered.values():release.set()
            self.runtime.join()

    def test_unknown_studio_manifest_registered_and_ingested_by_queued_owner(self):
        from archive_app.__main__ import discover_manifest_cameras
        self.settings.input_dir.mkdir(exist_ok=True)
        source=self.settings.input_dir/'studio.mp4';source.write_bytes(b'fixture video')
        entry={'camera':'studio-new','source':'studio','record_id':'export-1',
               'path':source.name,'start_time':'2026-10-11T10:00:00+07:00',
               'end_time':'2026-10-11T10:01:00+07:00'}
        manifest=self.settings.input_dir/'studio.json'
        manifest.write_text(json.dumps({'recordings':[entry]}))
        self.assertEqual(discover_manifest_cameras(self.archive,self.queue),1)
        self.assertEqual(discover_manifest_cameras(self.archive,self.queue),0)
        self.assertEqual(self.archive.conn.execute('SELECT count(*) FROM recordings').fetchone()[0],0)
        def normalize(source,destination,settings):
            destination.write_bytes(source.read_bytes())
            return {'duration':60,'bytes':source.stat().st_size,'processing_method':'passthrough'}
        class Telegram:
            def upload_one(self,*args,**kwargs):return None
        with patch('archive_app.core.normalize',normalize),patch.object(Archive,'probe_camera',return_value={'tcp':{}}):
            # The seven pre-existing jobs precede the newly registered camera.
            for _ in range(8):self.queue.run_once(Telegram())
        self.assertEqual(self.archive.conn.execute("SELECT count(*) FROM recordings WHERE camera='studio-new'").fetchone()[0],1)
        self.archive.delete_camera('studio-new','studio-new')
        self.assertEqual(discover_manifest_cameras(self.archive,self.queue),0)
        self.assertNotIn('studio-new',{c['id'] for c in self.archive.cameras()})

    def test_invalid_unknown_manifest_entry_does_not_block_later_valid_camera(self):
        from archive_app.__main__ import discover_manifest_cameras
        self.settings.input_dir.mkdir(exist_ok=True)
        source=self.settings.input_dir/'valid.mp4';source.write_bytes(b'fixture video')
        valid={'camera':'valid-new','record_id':'export-2','path':source.name,
               'start_time':'2026-10-11T10:00:00+07:00','end_time':'2026-10-11T10:01:00+07:00'}
        invalid=dict(valid,camera='invalid..slug')
        (self.settings.input_dir/'mixed.json').write_text(json.dumps({'recordings':[invalid,valid]}))
        with patch('archive_app.__main__.emit') as emitted:
            self.assertEqual(discover_manifest_cameras(self.archive,self.queue),1)
        self.assertTrue(emitted.called)
        self.assertIn('valid-new',{camera['id'] for camera in self.archive.cameras()})
        self.assertEqual(self.queue.status('valid-new')['latest']['valid-new']['state'],'queued')


if __name__=='__main__':unittest.main()
