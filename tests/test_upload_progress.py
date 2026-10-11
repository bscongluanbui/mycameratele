"""Confirmed throughput, local-file waiting state and owner-thread telemetry."""
from pathlib import Path
from types import SimpleNamespace
import json
import threading
import unittest
from unittest.mock import patch

from archive_app.pipeline_runtime import SerializedUploads
from archive_app.sync import SyncQueue
from tests import test_telegram as fixtures


class UploadProgressTests(unittest.TestCase):
    setUp=fixtures.TelegramTests.setUp
    tearDown=fixtures.TelegramTests.tearDown
    ingest=fixtures.TelegramTests.ingest
    row=fixtures.TelegramTests.row
    fake_normalize=fixtures.TelegramTests.fake_normalize

    def reply(self, field='document'):
        return {'chat':{'id':42,'type':'private'},'message_id':17,
                field:{'file_id':'confirmed-file','file_unique_id':'confirmed-unique'}}

    def clock(self, elapsed=2.0):
        values=iter((100.0,100.0+elapsed))
        return SimpleNamespace(time=lambda:1700000000.0,monotonic=lambda:next(values))

    def test_confirmed_bytes_are_reported_only_after_durable_receipt(self):
        key=self.ingest();path=Path(self.row(key)['local_path']);size=path.stat().st_size
        events=[];owner=threading.get_ident()
        def progress(snapshot):
            self.assertEqual(threading.get_ident(),owner)
            if snapshot['upload_state']=='sending':
                self.assertEqual(self.row(key)['status'],'uploading')
                self.assertTrue(path.exists())
                self.assertNotIn('upload_last_file_bytes',snapshot)
            if snapshot['upload_state']=='confirmed':
                self.assertEqual(self.row(key)['status'],'uploaded')
                self.assertTrue(path.exists())
            events.append(snapshot)
        self.request.return_value=self.reply()
        with patch('archive_app.telegram.time',self.clock()):
            self.assertEqual(self.telegram.upload_one_with_progress(self.archive,progress=progress),'uploaded')
        self.assertEqual([e['upload_state'] for e in events],['sending','confirmed'])
        self.assertEqual(events[0]['upload_current_file_total_bytes'],size)
        self.assertEqual(events[0]['upload_current_file_started_at'],1700000000.0)
        self.assertEqual(events[0]['upload_transport'],'multipart')
        self.assertEqual(events[-1]['upload_last_file_bytes'],size)
        self.assertEqual(events[-1]['upload_last_file_seconds'],2.0)
        self.assertEqual(events[-1]['upload_last_file_bytes_per_second'],size/2)
        self.assertFalse(path.exists())

    def test_local_file_reports_waiting_size_not_invented_live_byte_speed(self):
        self.settings.api_mode='local';self.settings.upload_transport='local_file'
        self.settings.local_upload_root='/cache'
        key=self.ingest()
        with self.archive.conn:
            self.archive.conn.execute("UPDATE recordings SET processing_method='remux_copy' WHERE key=?",(key,))
        events=[]
        def sending(*args,**kwargs):
            self.assertTrue(kwargs['local_file'])
            self.assertEqual(events[-1]['upload_state'],'sending')
            self.assertNotIn('upload_current_file_bytes',events[-1])
            self.assertNotIn('upload_current_file_bytes_per_second',events[-1])
            return self.reply('video')
        self.request.side_effect=sending
        with patch('archive_app.telegram.time',self.clock(4)):
            self.assertEqual(self.telegram.upload_one_with_progress(self.archive,progress=events.append),'uploaded')
        self.assertEqual(events[0]['upload_transport'],'local_file')
        self.assertEqual(events[-1]['upload_last_file_seconds'],4)

    def test_ambiguous_or_invalid_receipt_never_counts_confirmed_bytes(self):
        for record,reply in (('timeout',TimeoutError()),('invalid',{'message_id':17})):
            with self.subTest(record=record):
                key=self.ingest(record_id=record);events=[]
                self.request.side_effect=reply if isinstance(reply,Exception) else None
                self.request.return_value=reply
                self.assertEqual(self.telegram.upload_one_with_progress(self.archive,progress=events.append),'upload_unknown')
                self.assertEqual(self.row(key)['status'],'upload_unknown')
                self.assertEqual(events[-1]['upload_state'],'error')
                self.assertFalse(any('upload_last_file_bytes' in event for event in events))
                self.assertTrue(Path(self.row(key)['local_path']).is_file())

    def test_telemetry_callback_failure_does_not_change_confirmed_delivery(self):
        key=self.ingest();self.request.return_value=self.reply()
        def broken(_):raise RuntimeError('telemetry fixture')
        self.assertEqual(self.telegram.upload_one_with_progress(self.archive,progress=broken),'uploaded')
        self.assertEqual(self.row(key)['status'],'uploaded');self.request.assert_called_once()

    def test_sync_retains_confirmed_speed_and_persists_sending_before_request(self):
        key=self.ingest(camera='Front_Camera');self.archive.update_camera('Front_Camera',{'host':'192.168.1.2'})
        queue=SyncQueue(self.archive);queue.enqueue('Front_Camera');size=Path(self.row(key)['local_path']).stat().st_size
        def sending(*args,**kwargs):
            status=queue.status('Front_Camera')['latest']['Front_Camera']
            self.assertEqual(status['phase'],'uploading')
            self.assertEqual(status['statistics']['upload_state'],'sending')
            self.assertEqual(status['statistics']['upload_current_file_total_bytes'],size)
            return self.reply()
        self.request.side_effect=sending
        with (patch.object(self.archive,'probe_camera',return_value={'tcp':{'8000':'open'}}),
              patch('archive_app.sd_source.SDSource.sync',return_value={'backend':'isapi'}),
              patch('archive_app.telegram.time',self.clock(2))):
            self.assertTrue(queue.run_once(self.telegram))
        status=queue.status('Front_Camera')['latest']['Front_Camera'];s=status['statistics']
        self.assertEqual(status['state'],'completed')
        self.assertEqual(s['upload_state'],'confirmed')
        self.assertEqual(s['upload_confirmed_bytes'],size)
        self.assertEqual(s['upload_request_seconds'],2.0)
        self.assertEqual(s['upload_bytes_per_second'],size/2)
        self.assertEqual(s['upload_last_file_confirmed_at'],1700000000.0)
        # A freshly queued/running batch retains last-file telemetry without
        # pretending its bytes belong to the new batch's cumulative average.
        queued=queue.enqueue('Front_Camera')['jobs'][0]['statistics']
        self.assertEqual(queued['upload_last_file_bytes_per_second'],size/2)
        self.assertEqual(queued['upload_last_file_confirmed_at'],1700000000.0)
        self.assertEqual(queued['upload_confirmed_bytes'],0)
        self.assertEqual(queued['upload_request_seconds'],0.0)
        with (patch.object(self.archive,'probe_camera',return_value={'tcp':{'8000':'open'}}),
              patch('archive_app.sd_source.SDSource.sync',return_value={'backend':'isapi'})):
            self.assertTrue(queue.run_once(self.telegram))
        latest=queue.status('Front_Camera')['latest']['Front_Camera']['statistics']
        self.assertEqual(latest['upload_last_file_bytes_per_second'],size/2)
        self.assertEqual(latest['upload_confirmed_bytes'],0)

    def test_invalid_saved_telemetry_never_breaks_queue_or_publishes_nonfinite_rate(self):
        self.archive.add_camera({'id':'front'})
        queue=SyncQueue(self.archive)
        for value in ('not json','[]',json.dumps({'upload_last_file_bytes':True,
                'upload_last_file_seconds':2,'upload_last_file_confirmed_at':1700000000}),
                json.dumps({'upload_last_file_bytes':1e308,'upload_last_file_seconds':1e-308,
                            'upload_last_file_confirmed_at':1700000000})):
            with self.subTest(value=value):
                self.archive.state('upload_telemetry_v1:front',value)
                self.assertEqual(queue._job_statistics('front')['upload_last_file_bytes_per_second'],0.0)

    def test_sync_legacy_strict_upload_adapter_remains_compatible(self):
        key=self.ingest(camera='Front_Camera');queue=SyncQueue(self.archive);queue.enqueue('Front_Camera')
        archive=self.archive
        class Legacy:
            def upload_one(self, value, camera=None):
                self.called=(value,camera)
                archive.mark_uploaded(key,42,17,'confirmed-file')
                return 'uploaded'
        legacy=Legacy()
        with (patch.object(self.archive,'probe_camera',return_value={'tcp':{'8000':'open'}}),
              patch('archive_app.sd_source.SDSource.sync',return_value={'backend':'isapi'})):
            self.assertTrue(queue.run_once(SerializedUploads(legacy)))
        self.assertEqual(legacy.called,(self.archive,'Front_Camera'))
        status=queue.status('Front_Camera')['latest']['Front_Camera']
        self.assertEqual(status['state'],'completed')
        self.assertEqual(status['statistics']['upload_state'],'idle')
        self.assertEqual(status['statistics']['upload_confirmed_bytes'],0)


class UploadQueueProgressTests(unittest.TestCase):
    def test_waiting_queue_is_excluded_from_request_speed(self):
        events=[];clock={'value':0}
        class Lock:
            def __enter__(self):
                clock['value']+=1000
            def __exit__(self,*_):pass
        class Fake:
            def upload_one_with_progress(self, archive, camera=None, *, progress=None):
                self.start=clock['value'];clock['value']+=2
                progress({'upload_state':'confirmed','upload_last_file_bytes':2000000,
                          'upload_last_file_seconds':clock['value']-self.start})
                return 'uploaded'
        fake=Fake();wrapped=SerializedUploads(fake,Lock())
        self.assertEqual(wrapped.upload_one_with_progress(None,camera='front',progress=events.append),'uploaded')
        self.assertEqual([event['upload_state'] for event in events],['waiting','confirmed'])
        self.assertEqual(fake.start,1000)
        self.assertEqual(events[-1]['upload_last_file_seconds'],2)

    def test_shutdown_waiter_reports_idle_without_claim(self):
        stop=threading.Event();events=[]
        class Lock:
            def __enter__(self):stop.set()
            def __exit__(self,*_):pass
        class Fake:
            def upload_one(self,*_):raise AssertionError('No claim after stop')
        wrapped=SerializedUploads(Fake(),Lock(),stop_event=stop)
        self.assertIsNone(wrapped.upload_one_with_progress(None,progress=events.append))
        self.assertEqual([e['upload_state'] for e in events],['waiting','idle'])


if __name__=='__main__':unittest.main()
