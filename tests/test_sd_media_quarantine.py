"""A bad SD clip is retained, not sent, and does not strand good neighbors."""
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import uuid

from archive_app import core
from archive_app.core import Archive, Settings
from archive_app.sd_source import SDSource, SDSourceError
from archive_app.sync import SyncQueue, _statistics


NOW = datetime(2026, 10, 3, 5, tzinfo=timezone.utc)
MP4 = b'\x00\x00\x00\x18ftypisom' + b'intact original MP4 bytes' * 5
BAD = b'\x56\x2e\xf7\xbc' + b'not a recognized complete media container' * 5


class SDMediaQuarantineTests(unittest.TestCase):
    def setUp(self):
        self.parent = (Path(__file__).parent if os.name == 'nt' else Path(tempfile.gettempdir())).resolve()
        self.root = self.parent / ('.tmp-sd-media-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.settings = Settings(self.root/'state', self.root/'cache', self.root/'input',
                                 'UTC+07:00', min_free_bytes=0, media_mode='remux_copy')
        self.archive = Archive(self.settings)
        self.archive.add_camera({'id': 'front'})
        self.config = {'id': 'front', 'host': '192.168.2.8', 'sd_username': 'admin',
                       'sd_password': 'synthetic-secret', 'sd_channel': 1,
                       'sd_timezone': 'UTC+07:00', 'sd_download_threads': 1}
        self.files = []
        for number in range(5):
            start = NOW - timedelta(hours=1) + timedelta(minutes=number*2)
            self.files.append({'record_id': str(number), 'size': len(BAD if number == 1 else MP4),
                               'start_time': start.isoformat(),
                               'end_time': (start+timedelta(minutes=1)).isoformat()})
        self.downloads = []

    def tearDown(self):
        self.archive.close()
        self.assertEqual(self.root.resolve().parent, self.parent)
        shutil.rmtree(self.root)

    def provider(self):
        owner = self
        class Provider:
            backend = 'hcnetsdk'
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def search(self, *_):
                return list(owner.files)
            def download(self, recording, path):
                owner.downloads.append(recording['record_id'])
                path.write_bytes(BAD if recording['record_id'] == '1' else MP4)
        return Provider()

    def run_source(self, **kwargs):
        with patch.object(SDSource, '_provider', side_effect=self.provider):
            return SDSource(self.archive, 'front', self.config).sync(now=NOW, max_files=3, **kwargs)

    def test_recognized_bad_media_keeps_raw_and_imports_good_neighbors_serial_and_parallel(self):
        for threads in (1, 2):
            with self.subTest(threads=threads):
                self.archive.conn.execute('DELETE FROM recordings')
                self.archive.conn.commit()
                self.config['sd_download_threads'] = threads
                result = self.run_source()
                self.assertEqual((result['downloaded'], result['imported'], result['media_failed'], result['backlog']), (3, 2, 1, 2))
                self.assertEqual(result['media_error_code'], 'sd_remux_input_unsupported')
                rows = self.archive.conn.execute('SELECT * FROM recordings ORDER BY start_ms').fetchall()
                self.assertEqual([row['status'] for row in rows], ['downloaded', 'needs_review', 'downloaded'])
                bad = rows[1]
                self.assertEqual(bad['last_error'], 'failed_remux')
                self.assertEqual(bad['processing_method'], 'passthrough')
                self.assertEqual(Path(bad['local_path']).read_bytes(), BAD)
                self.assertEqual(bad['sha256'], hashlib.sha256(BAD).hexdigest())
                self.assertEqual(result['media_failed_keys'], [bad['key']])
                self.assertTrue(self.archive.retained_remux_failure(bad['key']))
                self.assertIsNone(bad['file_id'])
                self.assertIsNone(bad['message_id'])
                self.assertIsNone(bad['attempt_id'])
                self.assertEqual(list((self.settings.cache_dir/'sd-stage'/'front').iterdir()), [])
                with self.archive.conn:
                    self.archive.conn.execute("UPDATE recordings SET status='uploaded' WHERE status='downloaded'")
                self.assertIsNone(self.archive.claim_upload())

    def test_generic_ingest_failure_still_blocks_both_modes_and_keeps_raw(self):
        original = core.normalize
        def normalize(source, dest, settings):
            if source.read_bytes() == BAD:
                raise ValueError('synthetic-secret: unknown storage/database failure')
            return original(source, dest, settings)
        for threads in (1, 2):
            with self.subTest(threads=threads):
                self.archive.conn.execute('DELETE FROM recordings')
                self.archive.conn.commit()
                self.config['sd_download_threads'] = threads
                with patch('archive_app.core.normalize', side_effect=normalize), self.assertRaises(SDSourceError) as caught:
                    self.run_source()
                self.assertEqual(caught.exception.code, 'sd_media_validation_failed')
                self.assertNotIn('synthetic-secret', str(caught.exception))
                bad = self.archive.conn.execute("SELECT * FROM recordings WHERE status='needs_review'").fetchone()
                self.assertEqual(Path(bad['local_path']).read_bytes(), BAD)
                self.assertEqual(self.archive.conn.execute('SELECT count(*) FROM recordings').fetchone()[0], 2)

    def test_missing_durable_raw_copy_never_skips_failed_recording(self):
        with patch.object(self.archive, '_retain_raw_remux_failure', side_effect=OSError('retention unavailable')), self.assertRaises(SDSourceError) as caught:
            self.run_source()
        self.assertEqual(caught.exception.code, 'sd_remux_input_unsupported')
        row = self.archive.conn.execute("SELECT * FROM recordings WHERE status='needs_review'").fetchone()
        self.assertFalse(self.archive.retained_remux_failure(row['key']))

    def test_tool_and_timeout_errors_are_not_treated_as_corrupt_media(self):
        original = core.normalize
        for code in ('sd_remux_tool_unavailable', 'sd_remux_timeout', 'sd_remux_source_changed', 'sd_remux_failed'):
            with self.subTest(code=code):
                self.archive.conn.execute('DELETE FROM recordings')
                self.archive.conn.commit()
                def normalize(source, dest, settings):
                    if source.read_bytes() == BAD:
                        raise core.MediaRemuxError(code)
                    return original(source, dest, settings)
                with patch('archive_app.core.normalize', side_effect=normalize), self.assertRaises(SDSourceError) as caught:
                    self.run_source()
                self.assertEqual(caught.exception.code, code)

    def test_same_job_quarantine_requires_exact_verified_keys_and_no_send_references(self):
        queue = SyncQueue(self.archive)
        result = self.run_source()
        bad_key = result['media_failed_keys'][0]
        bad = self.archive.conn.execute('SELECT * FROM recordings WHERE key=?', (bad_key,)).fetchone()
        started = bad['created_at'] - 1
        stats = {**_statistics(), 'sd_backlog': 2, 'sd_downloaded': 3, 'sd_imported': 2,
                 'uploaded': 2, 'needs_review': 1, 'pending': 1,
                 'sd_media_failed': 1, 'sd_media_failed_keys': [bad_key]}
        job = {'camera_id': 'front', 'started_at': started}
        self.assertEqual(queue._quarantined_reviews(job, stats), 1)
        self.assertEqual(queue._quarantined_reviews(job, {**stats, 'sd_media_failed_keys': ['a'*64]}), 0)
        self.assertEqual(queue._quarantined_reviews(job, {**stats, 'sd_media_failed': 0}), 0)
        for field, value in (('attempt_id', 'uncertain-attempt'), ('file_id', 'uncertain-send'), ('message_id', 42)):
            with self.subTest(field=field), self.archive.conn:
                self.archive.conn.execute('UPDATE recordings SET '+field+'=? WHERE key=?', (value, bad_key))
                self.assertEqual(queue._quarantined_reviews(job, stats), 0)
                self.archive.conn.execute('UPDATE recordings SET '+field+'=NULL WHERE key=?', (bad_key,))
        Path(bad['local_path']).unlink()
        self.assertEqual(queue._quarantined_reviews(job, stats), 0)

    def test_ffmpeg_details_are_classified_without_leaking_paths_or_input(self):
        for stderr, code in ((b'/private/secret sample rate not set', 'sd_remux_parameters_missing'),
                             (b'Codec pcm_mulaw not supported in container', 'sd_remux_codec_unsupported'),
                             (b'non monotonically increasing dts secret', 'sd_remux_timestamps_invalid'),
                             (b'secret unknown reason', 'sd_remux_failed')):
            with self.subTest(code=code):
                self.assertEqual(core._remux_failure_code(stderr), code)
                self.assertNotIn('secret', str(core.MediaRemuxError(code)))

    def test_operational_ffmpeg_failure_takes_precedence_over_media_warnings(self):
        for message in ('No space left on device', 'Disk quota exceeded', 'Permission denied',
                        'Read-only file system', 'Input/output error', 'Too many open files',
                        'Cannot allocate memory', 'Error opening output'):
            with self.subTest(message=message):
                stderr = ('Could not find codec parameters; sample rate not set; ' + message).encode()
                self.assertEqual(core._remux_failure_code(stderr), 'sd_remux_failed')


if __name__ == '__main__':
    unittest.main()
