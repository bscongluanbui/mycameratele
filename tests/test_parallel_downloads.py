"""Real SQLite camera claims and synthetic download/upload concurrency."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from archive_app.core import Archive, Settings, record_key
from archive_app.pipeline_runtime import (PipelineRuntime, cache_used_bytes,
                                          reserve_cache, serialized_uploads, SerializedUploads)
from archive_app.sync import SyncQueue
from archive_app.sd_source import SDSource
from archive_app.telegram import Telegram


class ParallelDownloadsTests(unittest.TestCase):
    def setUp(self):
        self.parent = (Path(__file__).parent if os.name == 'nt' else Path(tempfile.gettempdir())).resolve()
        self.root = self.parent / ('.tmp-parallel-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.settings = Settings(self.root / 'state', self.root / 'cache', self.root / 'input',
                                 'UTC+07:00', min_free_bytes=0, sd_download_workers=5)
        self.archive = Archive(self.settings)
        self.queue = SyncQueue(self.archive)
        for number in range(5):
            self.archive.add_camera({'id': 'camera-' + str(number), 'name': 'Camera ' + str(number)})

    def tearDown(self):
        self.archive.close()
        self.assertEqual(self.root.resolve().parent, self.parent)
        shutil.rmtree(self.root)

    def test_five_camera_downloads_overlap_with_thread_owned_sqlite_connections(self):
        self.queue.enqueue()
        entered = threading.Barrier(6)
        release = threading.Event()
        done = threading.Event()
        lock = threading.Lock()
        active = set()
        connections = set()
        threads = set()
        completed = []
        failures = []

        def download(queue, job, statistics, telegram):
            # A connection accidentally created on the main thread raises here.
            queue.conn.execute('SELECT count(*) FROM cameras').fetchone()
            with lock:
                if job['camera_id'] in active:
                    failures.append('same camera ran concurrently')
                active.add(job['camera_id'])
                connections.add(id(queue.conn))
                threads.add(threading.get_ident())
            entered.wait(15)
            if not release.wait(15):
                raise RuntimeError('Synthetic download release timed out')
            queue._finish(job, statistics, 'completed', 'completed', 'Synthetic download complete')
            with lock:
                active.remove(job['camera_id'])
                completed.append(job['camera_id'])
                if len(completed) == 5:
                    done.set()

        runtime = PipelineRuntime(self.settings)
        try:
            with patch.object(SyncQueue, '_run', download):
                runtime.start()
                entered.wait(15)
                self.assertEqual(len(active), 5)
                self.assertEqual(len(connections), 5)
                self.assertEqual(len(threads), 5)
                self.assertNotIn(id(self.archive.conn), connections)
                running = self.archive.conn.execute("SELECT count(*) FROM sync_jobs WHERE state='running'").fetchone()[0]
                self.assertEqual(running, 5)
                release.set()
                self.assertTrue(done.wait(15))
        finally:
            release.set()
            runtime.stop()
            runtime.join()
        self.assertFalse(runtime.failed)
        self.assertEqual(failures, [])
        self.assertEqual(len(completed), 5)

    def test_five_workers_claim_only_one_active_job_for_same_camera(self):
        original = self.queue.enqueue('camera-0')['jobs'][0]['id']
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def download(queue, job, statistics, telegram):
            calls.append(job['id'])
            entered.set()
            release.wait(15)
            queue._finish(job, statistics, 'completed', 'completed', 'Synthetic download complete')

        runtime = PipelineRuntime(self.settings)
        try:
            with patch.object(SyncQueue, '_run', download):
                runtime.start()
                self.assertTrue(entered.wait(15))
                for _ in range(10):
                    self.assertEqual(self.queue.enqueue('camera-0')['jobs'][0]['id'], original)
                self.assertEqual(calls, [original])
                runtime.stop()
                release.set()
                runtime.join()
        finally:
            release.set()
            runtime.stop()
            runtime.join()
        self.assertEqual(calls, [original])

    def test_five_sd_downloads_real_ingest_and_global_upload_cleanup_no_duplicates(self):
        self.settings.enable_upload = True
        self.settings.token = 'synthetic-token'
        self.settings.owner_user_id = 42
        self.settings.keep_cache = False
        self.settings.media_mode = 'remux_copy'
        self.archive.state('telegram_owner_started:42', '1')
        self.queue.enqueue()
        entered = threading.Barrier(6)
        release = threading.Event()
        uploader_stop = threading.Event()
        errors = []
        sent = []
        normalized = []
        now = datetime.now(timezone.utc) - timedelta(minutes=10)

        class Provider:
            backend = 'hcnetsdk'

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def search(self, start, end):
                return [{'record_id': 'synthetic-closed-file', 'size': 20,
                         'start_time': now.isoformat(),
                         'end_time': (now + timedelta(minutes=1)).isoformat()}]

            def download(self, recording, destination):
                destination.write_bytes(b'x' * 20)
                entered.wait(15)
                if not release.wait(15):
                    raise RuntimeError('Synthetic download release timed out')
                return 20

        def normalize(source, destination, settings):
            destination.write_bytes(source.read_bytes())
            normalized.append(destination.name)
            return {'duration': 60, 'codec_video': 'h264', 'codec_audio': None,
                    'bytes': 20, 'processing_method': 'remux_copy',
                    'container': 'mp4', 'file_extension': '.mp4'}

        def request(telegram, method, fields, **kwargs):
            if method != 'sendVideo':
                raise AssertionError('Unexpected Telegram method ' + method)
            path = Path(kwargs['file_path'])
            if not path.is_file():
                raise AssertionError('Upload source disappeared before confirmed send')
            sent.append(path.name)
            return {'message_id': len(sent), 'chat': {'id': 42, 'type': 'private'},
                    'video': {'file_id': 'synthetic-file-' + str(len(sent)),
                              'file_unique_id': 'synthetic-unique-' + str(len(sent))}}

        original_ingest = Archive.ingest_download

        def ingest_and_race_cleanup(archive, entry, source):
            result = original_ingest(archive, entry, source)
            # Reproduce the main uploader claiming the committed MP4 and
            # cleaning its staged PS before SDSource's ingest call returns.
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                status = archive.conn.execute('SELECT status FROM recordings WHERE key=?',
                                              (result['key'],)).fetchone()[0]
                if status == 'uploaded':
                    return result
                time.sleep(0.005)
            raise RuntimeError('Synthetic global upload did not finish')

        def global_upload():
            archive = Archive(self.settings)
            telegram = serialized_uploads(Telegram(self.settings))
            try:
                while not uploader_stop.is_set():
                    telegram.upload_one(archive)
                    uploader_stop.wait(0.005)
            except Exception as error:
                errors.append(type(error).__name__)
            finally:
                archive.close()

        runtime = PipelineRuntime(self.settings)
        uploader = threading.Thread(target=global_upload)
        try:
            with patch.object(SDSource, '_provider', lambda source: Provider()), \
                 patch.object(Archive, 'probe_camera', return_value={'tcp': {'8000': 'open'}}), \
                 patch('archive_app.core.normalize', normalize), \
                 patch.object(Telegram, 'request', request), \
                 patch.object(Archive, 'ingest_download', ingest_and_race_cleanup):
                runtime.start()
                entered.wait(15)
                self.assertEqual(len(list((self.settings.cache_dir / 'sd-stage').glob('*/*.part'))), 5)
                uploader.start()
                release.set()
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    count = self.archive.conn.execute("SELECT count(*) FROM sync_jobs WHERE state='completed'").fetchone()[0]
                    if count == 5:
                        break
                    time.sleep(0.02)
                self.assertEqual(count, 5, self.queue.status()['jobs'])
                runtime.stop()
                runtime.join()
                uploader_stop.set()
                uploader.join()
        finally:
            release.set()
            runtime.stop()
            runtime.join()
            uploader_stop.set()
            if uploader.ident is not None:
                uploader.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(normalized), 5)
        self.assertEqual(len(sent), 5)
        self.assertEqual(len(set(sent)), 5)
        self.assertEqual(self.archive.conn.execute("SELECT count(*) FROM recordings WHERE status='uploaded'").fetchone()[0], 5)
        self.assertEqual(list(self.settings.cache_dir.glob('*.mp4')), [])
        self.assertEqual(list((self.settings.cache_dir / 'sd-stage').glob('*/*.source')), [])
        self.assertEqual(list((self.settings.cache_dir / 'sd-stage').glob('*/*.part')), [])

    def test_media_uploads_serialize_across_all_telegram_instances(self):
        barrier = threading.Barrier(6)
        guard = threading.Lock()
        active = 0
        peak = 0
        calls = []

        class FakeTelegram:
            def upload_one(self, number):
                nonlocal active, peak
                with guard:
                    active += 1
                    peak = max(active, peak)
                time.sleep(0.01)
                with guard:
                    calls.append(number)
                    active -= 1
                return 'uploaded'

        def upload(number):
            telegram = serialized_uploads(FakeTelegram())
            barrier.wait(10)
            return telegram.upload_one(number)

        with ThreadPoolExecutor(max_workers=6) as executor:
            self.assertEqual(list(executor.map(upload, range(6))), ['uploaded'] * 6)
        self.assertEqual(peak, 1)
        self.assertEqual(sorted(calls), list(range(6)))

    def test_parallel_global_upload_cleanup_during_legacy_remux_keeps_receipt(self):
        self.settings.enable_upload = True
        self.settings.token = 'synthetic-token'
        self.settings.owner_user_id = 42
        self.settings.keep_cache = False
        self.settings.media_mode = 'remux_copy'
        self.archive.state('telegram_owner_started:42', '1')
        key = record_key({'camera': 'camera-0', 'record_id': 'legacy-raw', 'source': 'camera-sd'})
        raw = self.settings.cache_dir / (key + '.ps')
        raw.write_bytes(b'original raw recording')
        with self.archive.conn:
            self.archive.conn.execute('''INSERT INTO recordings
                (key,camera,record_id,start_ms,end_ms,source_path,local_path,status,
                 processing_method,file_size,created_at)
                VALUES(?,'camera-0','legacy-raw',1790996400000,1790996460000,?,?,'downloaded','passthrough',?,?)''',
                (key, str(raw), str(raw), raw.stat().st_size, time.time()))

        def normalize(source, destination, settings):
            destination.write_bytes(source.read_bytes())
            return {'duration': 60, 'bytes': destination.stat().st_size,
                    'processing_method': 'remux_copy', 'container': 'mp4', 'file_extension': '.mp4'}

        def request(telegram, method, fields, **kwargs):
            return {'message_id': 73, 'chat': {'id': 42, 'type': 'private'},
                    'video': {'file_id': 'confirmed-legacy', 'file_unique_id': 'unique-legacy'}}

        def upload_elsewhere():
            archive = Archive(self.settings)
            try:
                return serialized_uploads(Telegram(self.settings)).upload_one(archive)
            finally:
                archive.close()

        original_store = Archive._store_media

        def store_then_confirm(archive, *args, **kwargs):
            saved = original_store(archive, *args, **kwargs)
            with ThreadPoolExecutor(max_workers=1) as executor:
                self.assertEqual(executor.submit(upload_elsewhere).result(timeout=15), 'uploaded')
            self.assertFalse(raw.exists())
            self.assertFalse(saved.exists())
            return saved

        with patch('archive_app.core.normalize', normalize), \
             patch.object(Archive, '_store_media', store_then_confirm), \
             patch.object(Telegram, 'request', request):
            result = self.archive.remux_pending_cache(camera='camera-0', limit=1)
        row = dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?', (key,)).fetchone())
        self.assertEqual(result['converted'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(row['status'], 'uploaded')
        self.assertEqual(row['message_id'], 73)
        self.assertEqual(row['file_id'], 'confirmed-legacy')
        self.assertIsNotNone(row['local_path'])

    def test_upload_guard_does_not_block_poll_and_delegates_retry_state(self):
        entered = threading.Event()
        release = threading.Event()

        class FakeTelegram:
            def upload_one(self):
                entered.set()
                release.wait(10)
                return 'uploaded'

            def poll(self):
                return 'commands responsive'

            def upload_retry_until(self, archive):
                return 123

        telegram = serialized_uploads(FakeTelegram())
        thread = threading.Thread(target=telegram.upload_one)
        thread.start()
        try:
            self.assertTrue(entered.wait(10))
            self.assertEqual(telegram.poll(), 'commands responsive')
            self.assertEqual(telegram.upload_retry_until(None), 123)
            self.assertIs(serialized_uploads(telegram), telegram)
        finally:
            release.set()
            thread.join()

    def test_shutdown_finishes_active_upload_but_waiter_never_claims(self):
        stop = threading.Event()
        entered = threading.Event()
        waiting = threading.Event()
        release = threading.Event()
        calls = []
        results = {}

        class ObservedLock:
            def __init__(self):
                self.lock = threading.RLock()

            def __enter__(self):
                if threading.current_thread().name == 'waiting-upload':
                    waiting.set()
                self.lock.acquire()

            def __exit__(self, *_):
                self.lock.release()

        class FakeTelegram:
            def upload_one(self, number):
                calls.append(number)
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('Active upload release timed out')
                return 'uploaded'

        telegram = SerializedUploads(FakeTelegram(), ObservedLock(), stop_event=stop)
        active = threading.Thread(target=lambda: results.update(active=telegram.upload_one(1)))
        waiter = threading.Thread(target=lambda: results.update(waiter=telegram.upload_one(2)),
                                  name='waiting-upload')
        active.start()
        try:
            self.assertTrue(entered.wait(10))
            waiter.start()
            self.assertTrue(waiting.wait(10))
            stop.set()
            release.set()
            active.join(10)
            waiter.join(10)
            self.assertFalse(active.is_alive())
            self.assertFalse(waiter.is_alive())
            self.assertEqual(results, {'active': 'uploaded', 'waiter': None})
            self.assertEqual(calls, [1])
            self.assertIsNone(telegram.upload_one(3))
            self.assertEqual(calls, [1])
        finally:
            release.set()
            active.join()
            if waiter.ident is not None:
                waiter.join()

    def test_stop_event_can_be_added_to_previously_wrapped_uploads(self):
        calls = []

        class FakeTelegram:
            def upload_one(self):
                calls.append('claimed')
                return 'uploaded'

        original = serialized_uploads(FakeTelegram())
        stop = threading.Event()
        guarded = serialized_uploads(original, stop_event=stop)
        self.assertIs(serialized_uploads(guarded, stop_event=stop), guarded)
        self.assertEqual(guarded.upload_one(), 'uploaded')
        stop.set()
        self.assertIsNone(guarded.upload_one())
        self.assertEqual(calls, ['claimed'])

    def test_shutdown_during_five_downloads_keeps_ingested_media_without_new_uploads(self):
        self.settings.enable_upload = True
        self.settings.token = 'synthetic-token'
        self.settings.owner_user_id = 42
        self.settings.keep_cache = False
        self.settings.media_mode = 'remux_copy'
        self.archive.state('telegram_owner_started:42', '1')
        self.queue.enqueue()
        entered = threading.Barrier(6)
        release = threading.Event()
        now = datetime.now(timezone.utc) - timedelta(minutes=10)

        class Provider:
            backend = 'hcnetsdk'

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def search(self, start, end):
                return [{'record_id': 'shutdown-closed-file', 'size': 20,
                         'start_time': now.isoformat(),
                         'end_time': (now + timedelta(minutes=1)).isoformat()}]

            def download(self, recording, destination):
                destination.write_bytes(b'x' * 20)
                entered.wait(15)
                if not release.wait(15):
                    raise RuntimeError('Synthetic download release timed out')
                return 20

        def normalize(source, destination, settings):
            destination.write_bytes(source.read_bytes())
            return {'duration': 60, 'bytes': 20, 'processing_method': 'remux_copy',
                    'container': 'mp4', 'file_extension': '.mp4'}

        runtime = PipelineRuntime(self.settings)
        try:
            with patch.object(SDSource, '_provider', lambda source: Provider()), \
                 patch.object(Archive, 'probe_camera', return_value={'tcp': {'8000': 'open'}}), \
                 patch('archive_app.core.normalize', normalize), \
                 patch.object(Telegram, 'upload_one', return_value='uploaded') as upload:
                runtime.start()
                entered.wait(15)
                runtime.stop()
                release.set()
                runtime.join()
                upload.assert_not_called()
        finally:
            release.set()
            runtime.stop()
            runtime.join()
        self.assertFalse(runtime.failed)
        self.assertEqual(self.archive.conn.execute(
            "SELECT count(*) FROM sync_jobs WHERE state='blocked' AND code='sd_interrupted'"
        ).fetchone()[0], 5)
        self.assertEqual(self.archive.conn.execute(
            "SELECT count(*) FROM recordings WHERE status='downloaded'"
        ).fetchone()[0], 5)
        self.assertEqual(len(list(self.settings.cache_dir.glob('*.mp4'))), 5)
        self.assertEqual(list((self.settings.cache_dir / 'sd-stage').glob('*/*.part')), [])
        self.assertEqual(list((self.settings.cache_dir / 'sd-stage').glob('*/*.source')), [])

    def test_shutdown_gates_new_jobs_and_finishes_active_download(self):
        self.settings.sd_download_workers = 1
        self.queue.enqueue()
        entered = threading.Event()
        release = threading.Event()
        calls = []
        closed = []

        class OwnedArchive(Archive):
            def close(self):
                self.conn.execute('SELECT 1').fetchone()
                closed.append(threading.get_ident())
                super().close()

        def download(queue, job, statistics, telegram):
            calls.append(job['camera_id'])
            entered.set()
            release.wait(15)
            queue._finish(job, statistics, 'completed', 'completed', 'Current file finished')

        runtime = PipelineRuntime(self.settings, archive_factory=OwnedArchive)
        try:
            with patch.object(SyncQueue, '_run', download):
                runtime.start()
                self.assertTrue(entered.wait(15))
                runtime.stop()
                self.assertEqual(closed, [])
                release.set()
                runtime.join()
        finally:
            release.set()
            runtime.stop()
            runtime.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(closed), 1)
        self.assertNotEqual(closed[0], threading.get_ident())
        queued = self.archive.conn.execute("SELECT count(*) FROM sync_jobs WHERE state='queued'").fetchone()[0]
        self.assertEqual(queued, 4)

    def test_stopped_pool_does_not_claim_any_job(self):
        self.queue.enqueue()
        stop = threading.Event()
        stop.set()
        runtime = PipelineRuntime(self.settings, stop_event=stop)
        runtime.start()
        runtime.join()
        running = self.archive.conn.execute("SELECT count(*) FROM sync_jobs WHERE state!='queued'").fetchone()[0]
        self.assertEqual(running, 0)

    def test_invalid_worker_count_rejected_and_duplicate_start_rejected(self):
        for value in (0, 6, True, 2.0, '5'):
            self.settings.sd_download_workers = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                PipelineRuntime(self.settings)
        self.settings.sd_download_workers = 1
        runtime = PipelineRuntime(self.settings)
        runtime.stop()
        runtime.start()
        try:
            with self.assertRaises(RuntimeError):
                runtime.start()
        finally:
            runtime.join()

    def test_worker_initialization_failure_stops_pool_without_claiming(self):
        self.queue.enqueue()

        def broken_archive(settings):
            raise RuntimeError('Synthetic initialization failure')

        events = []
        runtime = PipelineRuntime(self.settings, archive_factory=broken_archive,
                                  emit=lambda event, **fields: events.append((event, fields)))
        runtime.start()
        runtime.join()
        self.assertTrue(runtime.failed)
        self.assertTrue(runtime.stop_event.is_set())
        self.assertTrue(any(event == 'sync_worker_error' for event, fields in events))
        self.assertEqual(self.archive.conn.execute("SELECT count(*) FROM sync_jobs WHERE state='queued'").fetchone()[0], 5)

    def test_cache_reservations_aggregate_across_threads_and_release(self):
        self.settings.cache_max_bytes = 100
        entered = threading.Event()
        release = threading.Event()
        admissions = []

        def download():
            with reserve_cache(self.settings, 60) as allowed:
                admissions.append(allowed)
                entered.set()
                release.wait(10)

        with patch('archive_app.pipeline_runtime.shutil.disk_usage', return_value=shutil._ntuple_diskusage(1000, 0, 1000)):
            thread = threading.Thread(target=download)
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                with reserve_cache(self.settings, 50) as allowed:
                    self.assertFalse(allowed)
                with reserve_cache(self.settings, 40) as allowed:
                    self.assertTrue(allowed)
            finally:
                release.set()
                thread.join()
            with reserve_cache(self.settings, 100) as allowed:
                self.assertTrue(allowed)
        self.assertEqual(admissions, [True])

    def test_nested_ingest_reuses_outer_sd_reservation_and_reserves_only_delta(self):
        self.settings.cache_max_bytes = 100
        with patch('archive_app.pipeline_runtime.shutil.disk_usage', return_value=shutil._ntuple_diskusage(1000, 0, 1000)):
            with reserve_cache(self.settings, 60) as outer:
                self.assertTrue(outer)
                # The downloaded raw file now exists, but its remux room was
                # already included in the outer download reservation.
                (self.settings.cache_dir / 'download.source').write_bytes(b'x' * 20)
                with reserve_cache(self.settings, 40) as nested:
                    self.assertTrue(nested)
                with reserve_cache(self.settings, 80) as expanded:
                    self.assertTrue(expanded)
                with reserve_cache(self.settings, 81) as expanded:
                    self.assertFalse(expanded)
            with reserve_cache(self.settings, 80) as allowed:
                self.assertTrue(allowed)

    def test_cache_reservation_release_after_exception_and_disk_floor(self):
        self.settings.cache_max_bytes = 1000
        self.settings.min_free_bytes = 20
        with patch('archive_app.pipeline_runtime.shutil.disk_usage', return_value=shutil._ntuple_diskusage(100, 0, 100)):
            with self.assertRaisesRegex(RuntimeError, 'synthetic'):
                with reserve_cache(self.settings, 80) as allowed:
                    self.assertTrue(allowed)
                    raise RuntimeError('synthetic')
            with reserve_cache(self.settings, 81) as allowed:
                self.assertFalse(allowed)
            with reserve_cache(self.settings, 80) as allowed:
                self.assertTrue(allowed)

    def test_reservations_reject_invalid_size_and_escaped_directory(self):
        for value in (-1, True, 1.5, '10'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                with reserve_cache(self.settings, value):
                    pass
        with self.assertRaises(ValueError):
            with reserve_cache(self.settings, 1, self.root.parent):
                pass

    def test_cache_usage_tolerates_concurrent_removal_not_permission_errors(self):
        first = self.settings.cache_dir / 'a.source'
        second = self.settings.cache_dir / 'b.mp4'
        first.write_bytes(b'123')
        second.write_bytes(b'12345')
        self.assertEqual(cache_used_bytes(self.settings.cache_dir), 8)
        original = Path.stat

        def removed(path, *args, **kwargs):
            if path == first:
                raise FileNotFoundError('Concurrent confirmed upload cleanup')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'stat', removed):
            self.assertEqual(cache_used_bytes(self.settings.cache_dir), 5)

        def denied(path, *args, **kwargs):
            if path == first:
                raise PermissionError('Synthetic disk error')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'stat', denied), self.assertRaises(PermissionError):
            cache_used_bytes(self.settings.cache_dir)


if __name__ == '__main__':
    unittest.main()
