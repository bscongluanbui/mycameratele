"""Bounded pipeline work, lifecycle and no-media synthetic performance checks."""
from datetime import timedelta
import io
import queue
import unittest
from unittest.mock import Mock,patch

from tests import test_sd_source as fixtures
from archive_app.core import record_key
from archive_app.sd_source import HCNetSDKSource,SDSource,SDSourceError,_utc
from archive_app.sync import SyncQueue,_statistics


def known_files(archive,count=1000):
    files=[];rows=[]
    for index in range(count):
        first=fixtures.NOW-timedelta(days=3)+timedelta(minutes=index)
        last=first+timedelta(seconds=30)
        item=fixtures.recording(rid='file-'+str(index),start=first.isoformat(),end=last.isoformat())
        entry={'camera':'PN','source':'camera-sd','record_id':'ch1:'+_utc(first)}
        rows.append((record_key(entry),entry['record_id'],int(first.timestamp()*1000),int(last.timestamp()*1000)))
        files.append(item)
    archive.conn.executemany("INSERT INTO recordings(key,camera,record_id,start_ms,end_ms,source_path,status,created_at) VALUES(?,'PN',?,?,?,'synthetic','uploaded',0)",rows)
    archive.conn.commit()
    return files


class NativeBudgetTests(unittest.TestCase):
    def provider(self):
        p=HCNetSDKSource('192.168.1.10',8000,'admin','synthetic',1,fixtures.get_zone('UTC+07:00'),1024,
                         worker_command=['synthetic-not-executed'],session_timeout=1800)
        p.process=Mock(stdin=io.BytesIO(),stdout=io.BytesIO())
        p.process.poll.return_value=0
        p.deadline=1  # A session that began long ago is still healthy.
        p.inbox=Mock();p.inbox.get.return_value=b'{"ok":true,"result":8}\n'
        return p

    def test_healthy_request_after_old_session_deadline_still_completes(self):
        p=self.provider()
        with patch('archive_app.sd_source.time.monotonic',return_value=7200):
            self.assertEqual(p._exchange({'command':'download'},1805),8)
            self.assertEqual(p._exchange({'command':'download'},1805),8)
        self.assertEqual([c.kwargs['timeout'] for c in p.inbox.get.call_args_list],[1800,1800])

    def test_search_retains_its_shorter_operation_bound(self):
        p=self.provider();p._exchange({'command':'search'},130)
        self.assertEqual(p.inbox.get.call_args.kwargs['timeout'],130)

    def test_hung_operation_still_stops_the_native_process(self):
        p=self.provider();p.inbox.get.side_effect=queue.Empty
        with self.assertRaises(SDSourceError) as caught:p._exchange({'command':'download'},1805)
        self.assertEqual(caught.exception.code,'sd_native_worker_timeout')
        self.assertIsNone(p.process)


class OptimizedSDTests(unittest.TestCase):
    setUp=fixtures.SDOrchestrationTests.setUp
    source=fixtures.SDOrchestrationTests.source
    normalize=fixtures.SDOrchestrationTests.normalize
    sync=fixtures.SDOrchestrationTests.sync

    def test_thousand_known_records_only_publish_boundary_snapshots(self):
        provider=fixtures.FakeSource(files=known_files(self.archive))
        snapshots=[]
        with patch('archive_app.sd_source.time.monotonic',return_value=10):
            result=self.sync(provider,progress=snapshots.append)
        self.assertEqual(len(snapshots),3)
        self.assertEqual(snapshots[-1]['already_known'],1000)
        self.assertEqual(result['downloaded'],0)
        self.assertEqual(provider.downloads,[])

    def test_each_import_notifies_even_inside_throttle_window_and_releases_stage(self):
        items=[fixtures.recording(),fixtures.recording('second',start='2026-10-03T10:02:00+07:00',end='2026-10-03T10:03:00+07:00')]
        imported=[]
        def progress(snapshot):
            if snapshot['imported'] and (not imported or imported[-1]!=snapshot['imported']):
                imported.append(snapshot['imported'])
                self.assertEqual(list((self.archive.settings.cache_dir/'sd-stage'/'PN').iterdir()),[])
                self.assertTrue(all(__import__('pathlib').Path(r[0]).is_file() for r in self.archive.conn.execute('SELECT local_path FROM recordings')))
        with patch('archive_app.sd_source.time.monotonic',return_value=10):
            result=self.sync(fixtures.FakeSource(files=items),progress=progress)
        self.assertEqual(imported,[1,2]);self.assertEqual(result['download_bytes'],16)
        for field in ('connect_seconds','search_seconds','download_seconds','ingest_seconds'):
            self.assertEqual(result[field],0)

    def test_remux_reservation_rejects_before_download_not_after_staging(self):
        self.archive.settings.media_mode='remux_copy';self.archive.settings.cache_max_bytes=20
        provider=fixtures.FakeSource()
        with self.assertRaises(SDSourceError) as caught:self.sync(provider)
        self.assertEqual(caught.exception.code,'sd_size_limit')
        self.assertEqual(provider.downloads,[])
        self.assertEqual(self.archive.conn.execute('SELECT count(*) FROM recordings').fetchone()[0],0)

    def test_slow_known_scan_still_publishes_heartbeat_progress(self):
        provider=fixtures.FakeSource(files=known_files(self.archive,3));snapshots=[]
        tick=iter(range(100))
        with patch('archive_app.sd_source.time.monotonic',side_effect=lambda:next(tick)):
            self.sync(provider,progress=snapshots.append)
        self.assertEqual([s['already_known'] for s in snapshots if s['already_known']],[1,2,3,3])

    def test_sql_counts_match_visible_deleted_and_retry_semantics(self):
        known_files(self.archive,8)
        keys=[r[0] for r in self.archive.conn.execute('SELECT key FROM recordings ORDER BY start_ms')]
        values=[('uploaded',None,0),('downloaded',None,None),('downloaded',None,200),
                ('needs_review',None,0),('upload_unknown',None,0),('ingesting',None,0),
                ('downloaded',0,0),('failed',None,0)]
        with self.archive.conn:
            for key,(status,deleted,retry) in zip(keys,values):
                self.archive.conn.execute('UPDATE recordings SET status=?,deleted_at=?,retry_at=? WHERE key=?',(status,deleted,retry,key))
        q=SyncQueue(self.archive);stats=_statistics()
        with patch('archive_app.sync.time.time',return_value=100):result=q._counts('PN',stats)
        self.assertEqual(result,(8,1))
        self.assertEqual({k:stats[k] for k in ('ready','pending','needs_review','upload_unknown','deleted','failed_records')},
                         {'ready':1,'pending':6,'needs_review':1,'upload_unknown':1,'deleted':1,'failed_records':2})
        plan=self.archive.conn.execute('EXPLAIN QUERY PLAN SELECT status,deleted_at,retry_at FROM recordings WHERE camera=?',('PN',)).fetchall()
        self.assertTrue(any('by_camera_pipeline' in r[3] for r in plan))

    def test_empty_camera_counts_are_zero_not_null(self):
        q=SyncQueue(self.archive);stats=_statistics()
        self.assertEqual(q._counts('PN',stats),(0,0))
        self.assertTrue(all(stats[k]==0 for k in ('ready','pending','needs_review','upload_unknown','deleted','failed_records')))


if __name__=='__main__':unittest.main()
