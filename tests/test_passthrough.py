"""Original bytes are archived intact; no real camera or Telegram is used."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from archive_app.core import Archive, Settings, normalize, record_key
from archive_app.sd_source import SDSource


class PassthroughTests(unittest.TestCase):
    def setUp(self):
        self.parent = (Path(__file__).parent if os.name == 'nt' else Path(tempfile.gettempdir())).resolve()
        self.root = self.parent / ('.tmp-passthrough-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.settings = Settings(self.root/'state', self.root/'cache', self.root/'input',
                                 'UTC+07:00', min_free_bytes=0)
        self.archive = Archive(self.settings)
        self.settings.input_dir.mkdir()

    def tearDown(self):
        self.archive.close()
        self.assertEqual(self.root.resolve().parent, self.parent)
        shutil.rmtree(self.root)

    def source(self, suffix='.dav', body=b'synthetic original proprietary recording'):
        path = self.settings.input_dir / ('original' + suffix)
        path.write_bytes(body)
        return path

    def entry(self, path, rid='one'):
        return {'camera': 'fixture', 'source': 'studio-export', 'record_id': rid,
                'path': str(path), 'start_time': '2026-10-03T10:00:00+07:00',
                'end_time': '2026-10-03T10:01:00+07:00'}

    def test_environment_and_direct_settings_disable_probe_by_default(self):
        self.assertFalse(self.settings.passthrough_probe)
        with patch.dict(os.environ, {'DISPLAY_TIMEZONE':'UTC+07:00'}, clear=True):
            self.assertFalse(Settings.from_env().passthrough_probe)
        with patch.dict(os.environ, {'DISPLAY_TIMEZONE':'UTC+07:00',
                                    'PASSTHROUGH_PROBE_METADATA':'true'}, clear=True):
            self.assertTrue(Settings.from_env().passthrough_probe)

    def test_original_all_bytes_and_extension_are_preserved_without_subprocess(self):
        body = bytes(range(256))*10000 + b'all original streams and proprietary trailer'
        source = self.source(body=body)
        before = source.stat()
        with patch('archive_app.core.subprocess.run', side_effect=AssertionError('No media process is allowed')):
            info = normalize(source, self.settings.cache_dir/'copy.mp4', self.settings)
        cached = Path(info['path'])
        self.assertEqual(cached.suffix, '.dav')
        self.assertEqual(cached.read_bytes(), body)
        self.assertEqual(source.read_bytes(), body)
        self.assertEqual(source.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(info['sha256'], hashlib.sha256(body).hexdigest())
        self.assertEqual(info['bytes'], len(body))
        self.assertEqual(info['probe_status'], 'disabled')
        self.assertEqual(info['processing_method'], 'passthrough')

    def test_sdk_staging_mpeg_ps_is_not_mislabeled_as_mp4(self):
        body = b'\x00\x00\x01\xba' + b'original camera PS bytes'
        source = self.source('.source', body)
        with patch('archive_app.core.subprocess.run', side_effect=AssertionError('No FFmpeg or FFprobe')):
            info = normalize(source, self.settings.cache_dir/'raw.mp4', self.settings)
        self.assertEqual(Path(info['path']).suffix, '.ps')
        self.assertEqual(info['container'], 'mpeg')
        self.assertEqual(Path(info['path']).read_bytes(), body)

    def test_unknown_sdk_container_is_retained_as_binary_not_rejected(self):
        source = self.source('.source')
        with patch('archive_app.core.subprocess.run', side_effect=AssertionError('No codec tools')):
            info = normalize(source, self.settings.cache_dir/'unknown.mp4', self.settings)
        self.assertEqual(Path(info['path']).suffix, '.bin')
        self.assertIsNone(info['container'])
        self.assertEqual(Path(info['path']).read_bytes(), source.read_bytes())

    def test_sdk_true_mp4_has_bounded_header_naming_without_conversion(self):
        body = b'\x00\x00\x00\x18ftypisom' + bytes(range(128))
        source = self.source('.source', body)
        info = normalize(source, self.settings.cache_dir/'sample.mp4', self.settings)
        self.assertEqual(Path(info['path']).suffix, '.mp4')
        self.assertEqual(Path(info['path']).read_bytes(), body)

    def test_optional_probe_is_only_bounded_metadata_and_does_not_gate_copy(self):
        source = self.source('.source')
        self.settings.passthrough_probe = True
        response = SimpleNamespace(returncode=0, stderr=b'unsupported audio warning', stdout=json.dumps({
            'format': {'format_name':'mpeg', 'duration':'61.2'},
            'streams': [{'codec_type':'video','codec_name':'hevc'},
                        {'codec_type':'audio','codec_name':'adpcm_g726'}],
        }).encode())
        with patch('archive_app.core.subprocess.run', return_value=response) as run:
            info = normalize(source, self.settings.cache_dir/'probe.mp4', self.settings)
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], self.settings.ffprobe)
        self.assertEqual(kwargs['timeout'], 5)
        self.assertIn('-probesize', args[0])
        self.assertIn('-show_entries', args[0])
        self.assertNotIn('-i', args[0])
        self.assertNotIn('-c', args[0])
        self.assertEqual(info['codec_audio'], 'adpcm_g726')
        self.assertEqual(info['duration'], 61.2)
        self.assertEqual(Path(info['path']).read_bytes(), source.read_bytes())

    def test_optional_probe_failure_timeout_and_missing_binary_still_copy_raw(self):
        self.settings.passthrough_probe = True
        for error in (FileNotFoundError(), subprocess.TimeoutExpired('ffprobe', 5),
                      SimpleNamespace(returncode=1, stderr=b'unsupported', stdout=b''),
                      SimpleNamespace(returncode=0, stderr=b'', stdout=b'not-json')):
            with self.subTest(error=type(error).__name__):
                source = self.source('.source')
                kwargs = {'side_effect':error} if isinstance(error, Exception) else {'return_value':error}
                with patch('archive_app.core.subprocess.run', **kwargs):
                    info = normalize(source, self.settings.cache_dir/'retry.mp4', self.settings)
                self.assertEqual(info['probe_status'], 'unavailable')
                self.assertEqual(Path(info['path']).read_bytes(), source.read_bytes())

    def test_manifest_ingest_uses_original_suffix_sha_and_camera_duration(self):
        source = self.source('.h265')
        manifest = self.settings.input_dir/'manifest.json'
        manifest.write_text(json.dumps({'recordings':[self.entry(source)]}), encoding='utf-8')
        with patch('archive_app.core.subprocess.run', side_effect=AssertionError('No media conversion')):
            row = self.archive.ingest_manifest(manifest)[0]
        self.assertEqual(row['status'], 'downloaded')
        self.assertEqual(row['processing_method'], 'passthrough')
        self.assertEqual(row['media_extension'], '.h265')
        self.assertEqual(row['duration'], 60)
        self.assertIsNone(row['codec_video'])
        self.assertEqual(row['sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(Path(row['local_path']).read_bytes(), source.read_bytes())
        self.assertEqual(self.archive.ingest_entry(self.entry(source))['key'], row['key'])

    def test_zero_bytes_are_not_accepted_as_successful_download(self):
        source = self.source(body=b'')
        with self.assertRaises(ValueError):
            normalize(source, self.settings.cache_dir/'empty.mp4', self.settings)
        self.assertEqual(list(self.settings.cache_dir.glob('empty*')), [])

    def test_source_changes_during_copy_are_detected_and_partial_removed(self):
        source = self.source(body=b'original before concurrent write')
        real_fsync = os.fsync
        def changed(fd):
            real_fsync(fd)
            with source.open('ab') as handle:
                handle.write(b'concurrent changed source')
        with patch('archive_app.core.os.fsync', side_effect=changed), self.assertRaisesRegex(ValueError, 'Source changed'):
            normalize(source, self.settings.cache_dir/'changed.mp4', self.settings)
        self.assertEqual(list(self.settings.cache_dir.glob('changed*')), [])
        self.assertIn(b'concurrent changed source', source.read_bytes())

    def test_copy_integrity_mismatch_is_rejected_without_replacing_existing_cache(self):
        source = self.source()
        existing = self.settings.cache_dir/'copy.dav'
        existing.write_bytes(b'previous retained cache')
        with patch('archive_app.core.hashlib.file_digest', return_value=SimpleNamespace(hexdigest=lambda:'0'*64)), self.assertRaisesRegex(ValueError, 'integrity mismatch'):
            normalize(source, self.settings.cache_dir/'copy.mp4', self.settings)
        self.assertEqual(existing.read_bytes(), b'previous retained cache')
        self.assertEqual(list(self.settings.cache_dir.glob('copy.part.*')), [])

    def test_unowned_preexisting_partial_is_not_removed(self):
        source = self.source()
        partial = self.settings.cache_dir/'copy.part.dav'
        partial.write_bytes(b'unowned or interrupted artifact')
        with self.assertRaises(FileExistsError):
            normalize(source, self.settings.cache_dir/'copy.mp4', self.settings)
        self.assertEqual(partial.read_bytes(), b'unowned or interrupted artifact')

    def test_legacy_migration_preserves_posted_uncertain_inflight_and_tombstones(self):
        rows = [('a','downloaded',False,False), ('b','failed',False,False),
                ('c','upload_unknown',False,False), ('d','uploaded',False,True),
                ('e','downloaded',True,False), ('f','uploading',False,False),
                ('g','downloaded',False,True)]
        for character, status, deleted, telegram in rows:
            self.archive.conn.execute('''INSERT INTO recordings(key,camera,record_id,start_ms,end_ms,source_path,
                status,created_at,deleted_at,file_id,message_id,chat_id) VALUES(?, 'fixture', ?,1,2,'source',?,?, ?,?,?,?)''',
                (character*64, character, status, time.time(), 1 if deleted else None,
                 'keep-file' if telegram else None, 42 if telegram else None, '42' if telegram else None))
        self.archive.conn.commit()
        before = [dict(row) for row in self.archive.conn.execute("SELECT * FROM recordings WHERE key NOT IN (?,?) ORDER BY key", ('a'*64,'b'*64))]
        self.assertEqual(self.archive.invalidate_legacy_cache('fixture'), 2)
        self.assertEqual(self.archive.invalidate_legacy_cache('fixture'), 0)
        after = [dict(row) for row in self.archive.conn.execute("SELECT * FROM recordings WHERE key NOT IN (?,?) ORDER BY key", ('a'*64,'b'*64))]
        self.assertEqual(before, after)
        for character in ('a','b'):
            row = self.archive.conn.execute('SELECT * FROM recordings WHERE key=?', (character*64,)).fetchone()
            self.assertEqual((row['status'],row['last_error']), ('failed','raw_reingest_required'))

    def test_current_raw_cache_is_not_marked_for_legacy_reingest(self):
        row = self.archive.ingest_entry(self.entry(self.source()))
        self.assertEqual(self.archive.invalidate_legacy_cache(), 0)
        self.assertEqual(self.archive.conn.execute('SELECT status FROM recordings WHERE key=?', (row['key'],)).fetchone()[0], 'downloaded')

    def test_restart_recovers_generated_partial_for_any_original_format_only(self):
        key = 'a'*64
        self.archive.conn.execute("INSERT INTO recordings(key,camera,record_id,start_ms,end_ms,source_path,status,created_at) VALUES(?,'fixture','one',1,2,'source','ingesting',?)", (key,time.time()))
        self.archive.conn.commit()
        expected = [self.settings.cache_dir/(key+'.part'+ext) for ext in ('.ps','.bin','.mp4')]
        for path in expected:
            path.write_bytes(b'interrupted unposted original copy')
        unrelated = self.settings.cache_dir/(key+'.part.user-notes')
        unrelated.write_bytes(b'unknown user file retained')
        final = self.settings.cache_dir/(key+'.ps')
        final.write_bytes(b'durable original final file')
        self.assertEqual(self.archive.recover_ingests(),1)
        self.assertTrue(all(not path.exists() for path in expected))
        self.assertTrue(unrelated.exists())
        self.assertTrue(final.exists())

    def test_sd_provider_to_archive_preserves_proprietary_bytes_without_media_process(self):
        body = b'\x00\x00\x01\xba' + bytes(range(256))*100
        source_rows = [{'record_id':'synthetic-sdk-name', 'start_time':'2026-10-03T10:00:00+07:00',
                        'end_time':'2026-10-03T10:01:00+07:00', 'size':len(body)}]
        class Provider:
            backend = 'hcnetsdk'
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def search(self,*args): return source_rows
            def download(self,item,path): path.write_bytes(body)
        self.archive.add_camera({'id':'fixture','host':'192.168.1.2','sd_password':'synthetic-only'})
        source = SDSource(self.archive, 'fixture')
        now = datetime(2026,10,3,4,0,tzinfo=timezone.utc)
        with patch.object(source,'_provider',return_value=Provider()), patch('archive_app.core.subprocess.run',side_effect=AssertionError('No FFmpeg or FFprobe')):
            result = source.sync(now=now)
            second = source.sync(now=now)
        self.assertEqual((result['downloaded'],result['imported']), (1,1))
        self.assertEqual(second['already_known'],1)
        row = self.archive.conn.execute('SELECT * FROM recordings').fetchone()
        self.assertEqual(Path(row['local_path']).suffix,'.ps')
        self.assertEqual(Path(row['local_path']).read_bytes(),body)
        self.assertEqual(row['sha256'],hashlib.sha256(body).hexdigest())
        self.assertEqual(list((self.settings.cache_dir/'sd-stage'/'fixture').iterdir()),[])


if __name__ == '__main__':
    unittest.main()
