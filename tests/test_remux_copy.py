"""MP4 stream-copy mode never encodes or performs full decode validation."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from archive_app.core import Archive, Settings, normalize, record_key


MP4 = b'\x00\x00\x00\x18ftypisom' + b'fixture MP4 packet-copy output' * 2
PS = b'\x00\x00\x01\xba' + b'fixture original encoded PS video and audio' * 8
TS = (b'\x47' + b'\x00'*187)*4


class RemuxCopyTests(unittest.TestCase):
    def setUp(self):
        self.parent = (Path(__file__).parent if os.name == 'nt' else Path(tempfile.gettempdir())).resolve()
        self.root = self.parent / ('.tmp-remux-copy-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.settings = Settings(self.root/'state', self.root/'cache', self.root/'input',
                                 'UTC+07:00', min_free_bytes=0, media_mode='remux_copy')
        self.archive = Archive(self.settings)
        self.settings.input_dir.mkdir()

    def tearDown(self):
        self.archive.close()
        self.assertEqual(self.root.resolve().parent, self.parent)
        shutil.rmtree(self.root)

    def source(self, body=PS, suffix='.source'):
        path = self.settings.input_dir / ('original' + suffix)
        path.write_bytes(body)
        return path

    def entry(self, source, rid='one'):
        return {'camera':'fixture','source':'studio-export','record_id':rid,'path':str(source),
                'start_time':'2026-10-03T10:00:00+07:00','end_time':'2026-10-03T10:01:00+07:00'}

    def ffmpeg(self, command, **kwargs):
        self.assertEqual(command[0], self.settings.ffmpeg)
        Path(command[-1]).write_bytes(MP4)
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    def raw_row(self, rid='one'):
        self.settings.media_mode='raw'
        row=self.archive.ingest_entry(self.entry(self.source(),rid))
        self.settings.media_mode='remux_copy'
        return row

    def test_modes_are_explicit_and_existing_raw_constructor_default_is_unchanged(self):
        self.assertEqual(Settings(Path('state'),Path('cache'),Path('input'),'UTC+07:00').media_mode,'raw')
        with patch.dict(os.environ, {'DISPLAY_TIMEZONE':'UTC+07:00'}, clear=True):
            self.assertEqual(Settings.from_env().media_mode,'raw')
        with patch.dict(os.environ, {'DISPLAY_TIMEZONE':'UTC+07:00','MEDIA_MODE':'remux_copy',
                                    'CACHE_RETENTION_HOURS':'1','KEEP_CACHE':'false'}, clear=True):
            settings=Settings.from_env()
            self.assertEqual(settings.media_mode,'remux_copy')
            self.assertEqual(settings.cache_retention_hours,1)
            self.assertFalse(settings.keep_cache)
        with patch.dict(os.environ, {'DISPLAY_TIMEZONE':'UTC+07:00','MEDIA_MODE':'transcode'}, clear=True), self.assertRaises(ValueError):
            Settings.from_env()

    def test_existing_mp4_is_copied_identically_without_any_subprocess_or_probe(self):
        source=self.source(MP4,'.source')
        self.settings.passthrough_probe=True
        with patch('archive_app.core.subprocess.run',side_effect=AssertionError('No tool for original MP4')):
            info=normalize(source,self.settings.cache_dir/'copy.bin',self.settings)
        self.assertEqual(Path(info['path']).suffix,'.mp4')
        self.assertEqual(Path(info['path']).read_bytes(),MP4)
        self.assertEqual(info['sha256'],hashlib.sha256(MP4).hexdigest())
        self.assertEqual(info['processing_method'],'remux_copy')
        self.assertEqual(info['probe_status'],'disabled')

    def test_ps_command_copies_all_audio_and_one_video_without_encoding_decode_or_ffprobe(self):
        source=self.source()
        self.settings.passthrough_probe=True
        with patch.dict(os.environ,{'LD_LIBRARY_PATH':'/opt/hcnetsdk:/opt/hcnetsdk/HCNetSDKCom'}), patch('archive_app.core.subprocess.run',side_effect=self.ffmpeg) as run:
            info=normalize(source,self.settings.cache_dir/'copy.bin',self.settings)
        command=run.call_args.args[0]
        self.assertEqual(command[command.index('-c')+1],'copy')
        self.assertEqual([command[i+1] for i,item in enumerate(command) if item=='-map'],['0:v:0','0:a?'])
        self.assertIn('+faststart',command)
        self.assertEqual(command[command.index('-probesize')+1],'1048576')
        self.assertEqual(command[command.index('-analyzeduration')+1],'1000000')
        self.assertEqual(run.call_count,1)
        self.assertEqual(run.call_args.kwargs['timeout'],300)
        self.assertNotIn('LD_LIBRARY_PATH',run.call_args.kwargs['env'])
        self.assertFalse(any(item in command for item in ('-an','-vn','-c:a','-c:v','-filter','-vf','-af','null','aac')))
        self.assertEqual(Path(info['path']).suffix,'.mp4')
        self.assertEqual(info['processing_method'],'remux_copy')
        self.assertEqual(info['bytes'],len(MP4))
        self.assertEqual(source.read_bytes(),PS)

    def test_ts_uses_identical_stream_copy_output_path(self):
        source=self.source(TS)
        with patch('archive_app.core.subprocess.run',side_effect=self.ffmpeg) as run:
            info=normalize(source,self.settings.cache_dir/'transport.mp4',self.settings)
        self.assertEqual(run.call_count,1)
        self.assertEqual(Path(info['path']).read_bytes(),MP4)
        self.assertEqual(source.read_bytes(),TS)

    def test_failed_audio_codec_never_falls_back_to_aac_or_strips_audio(self):
        source=self.source()
        retained=self.settings.cache_dir/'failed.mp4'
        retained.write_bytes(b'prior cached file is untouched')
        response=SimpleNamespace(returncode=1,stdout=b'',stderr=b'Codec pcm_mulaw not supported in container')
        with patch('archive_app.core.subprocess.run',return_value=response) as run, self.assertRaisesRegex(ValueError,'remux failed'):
            normalize(source,retained,self.settings)
        self.assertEqual(run.call_count,1)
        self.assertEqual(source.read_bytes(),PS)
        self.assertEqual(retained.read_bytes(),b'prior cached file is untouched')
        self.assertEqual(list(self.settings.cache_dir.glob('*.part.mp4')),[])

    def test_timeout_or_missing_ffmpeg_retains_original_and_removes_own_partial(self):
        source=self.source()
        for error in (FileNotFoundError(),subprocess.TimeoutExpired('ffmpeg',300)):
            with self.subTest(error=type(error).__name__), patch('archive_app.core.subprocess.run',side_effect=error), self.assertRaisesRegex(ValueError,'did not complete'):
                normalize(source,self.settings.cache_dir/'missing.mp4',self.settings)
            self.assertEqual(source.read_bytes(),PS)
            self.assertEqual(list(self.settings.cache_dir.glob('missing*')),[])

    def test_nonempty_mp4_header_is_required_without_decode_validation(self):
        source=self.source()
        for body in (b'',b'not an MP4 container'):
            def bad_output(command,**kwargs):
                Path(command[-1]).write_bytes(body)
                return SimpleNamespace(returncode=0,stdout=b'',stderr=b'')
            with self.subTest(body=body), patch('archive_app.core.subprocess.run',side_effect=bad_output), self.assertRaisesRegex(ValueError,'MP4 container'):
                normalize(source,self.settings.cache_dir/'bad.mp4',self.settings)
            self.assertEqual(list(self.settings.cache_dir.glob('bad*')),[])

    def test_source_changes_during_remux_reject_output_and_preserve_source(self):
        source=self.source()
        def changed(command,**kwargs):
            result=self.ffmpeg(command,**kwargs)
            with source.open('ab') as handle:handle.write(b'concurrent input change')
            return result
        with patch('archive_app.core.subprocess.run',side_effect=changed), self.assertRaisesRegex(ValueError,'Source changed'):
            normalize(source,self.settings.cache_dir/'changed.mp4',self.settings)
        self.assertTrue(source.read_bytes().endswith(b'concurrent input change'))
        self.assertEqual(list(self.settings.cache_dir.glob('changed*')),[])

    def test_unknown_container_does_not_fall_back_to_raw_upload_or_conversion(self):
        source=self.source(b'unknown original proprietary container')
        with patch('archive_app.core.subprocess.run',side_effect=AssertionError('Do not run unknown input')), self.assertRaisesRegex(ValueError,'PS/TS'):
            normalize(source,self.settings.cache_dir/'unknown.mp4',self.settings)
        self.assertEqual(source.read_bytes(),b'unknown original proprietary container')

    def test_preexisting_partial_is_not_overwritten_or_deleted(self):
        source=self.source()
        partial=self.settings.cache_dir/'copy.part.mp4'
        partial.write_bytes(b'unowned retained file')
        with patch('archive_app.core.subprocess.run',side_effect=AssertionError('No process on occupied target')), self.assertRaises(FileExistsError):
            normalize(source,self.settings.cache_dir/'copy.mp4',self.settings)
        self.assertEqual(partial.read_bytes(),b'unowned retained file')

    def test_ingest_preserves_identity_times_and_marks_remux_copy(self):
        source=self.source()
        entry=self.entry(source)
        with patch('archive_app.core.subprocess.run',side_effect=self.ffmpeg):
            row=self.archive.ingest_entry(entry)
        self.assertEqual(row['key'],record_key(entry))
        self.assertEqual(row['duration'],60)
        self.assertEqual(row['processing_method'],'remux_copy')
        self.assertEqual(row['media_extension'],'.mp4')
        self.assertEqual(row['status'],'downloaded')
        self.assertEqual(row['sha256'],hashlib.sha256(MP4).hexdigest())
        self.assertEqual(self.archive.invalidate_legacy_cache(),0)

    def test_sdk_ingest_failure_retains_cached_raw_after_staging_is_deleted(self):
        self.archive.add_camera({'id':'fixture'})
        stage=self.settings.cache_dir/'sd-stage'/'fixture'
        stage.mkdir(parents=True)
        original=stage/('a'*64+'.'+'b'*32+'.source')
        original.write_bytes(PS)
        entry=self.entry(original)
        entry['source']='camera-sd'
        with patch('archive_app.core.subprocess.run',return_value=SimpleNamespace(returncode=1,stdout=b'',stderr=b'unsupported audio')), self.assertRaises(ValueError):
            self.archive.ingest_download(entry,original)
        original.unlink()
        row=self.archive.conn.execute('SELECT * FROM recordings WHERE key=?',(record_key(entry),)).fetchone()
        self.assertEqual((row['status'],row['last_error']),('needs_review','failed_remux'))
        self.assertEqual(row['processing_method'],'passthrough')
        self.assertEqual(Path(row['local_path']).read_bytes(),PS)
        self.assertEqual(row['sha256'],hashlib.sha256(PS).hexdigest())
        self.assertIsNone(self.archive.claim_upload())

    def test_pending_cache_reuses_raw_instead_of_downloading_sd_again(self):
        row=self.raw_row()
        raw=Path(row['local_path'])
        self.assertEqual(self.archive.invalidate_legacy_cache(),1)
        with patch('archive_app.core.subprocess.run',side_effect=self.ffmpeg) as run:
            result=self.archive.remux_pending_cache('fixture')
        self.assertEqual(result,{'converted':1,'failed':0})
        self.assertEqual(Path(run.call_args.args[0][run.call_args.args[0].index('-i')+1]),raw)
        changed=self.archive.conn.execute('SELECT * FROM recordings WHERE key=?',(row['key'],)).fetchone()
        self.assertEqual((changed['status'],changed['processing_method']),('downloaded','remux_copy'))
        self.assertEqual(changed['key'],row['key'])
        self.assertFalse(raw.exists())
        self.assertEqual(self.archive.remux_pending_cache('fixture'),{'converted':0,'failed':0})

    def test_pending_cache_failure_is_review_not_raw_document_retry(self):
        row=self.raw_row()
        raw=Path(row['local_path'])
        with patch('archive_app.core.subprocess.run',return_value=SimpleNamespace(returncode=1,stdout=b'',stderr=b'unsupported audio')):
            self.assertEqual(self.archive.remux_pending_cache(),{'converted':0,'failed':1})
        changed=self.archive.conn.execute('SELECT * FROM recordings WHERE key=?',(row['key'],)).fetchone()
        self.assertEqual((changed['status'],changed['last_error']),('needs_review','failed_remux'))
        self.assertEqual(changed['local_path'],str(raw))
        self.assertEqual(raw.read_bytes(),PS)
        self.assertEqual(self.archive.invalidate_legacy_cache(),0)
        self.assertIsNone(self.archive.claim_upload())

    def test_pending_cache_does_not_touch_posted_uncertain_inflight_deleted_or_ref_rows(self):
        changes=[{'status':'uploaded'},{'status':'upload_unknown'},{'status':'uploading'},
                 {'deleted_at':1},{'file_id':'confirmed-reference'}]
        keys=[]
        for index,values in enumerate(changes):
            row=self.raw_row(str(index))
            keys.append(row['key'])
            assignments=','.join(field+'=?' for field in values)
            self.archive.conn.execute('UPDATE recordings SET '+assignments+' WHERE key=?',(*values.values(),row['key']))
        self.archive.conn.commit()
        before=[dict(row) for row in self.archive.conn.execute('SELECT * FROM recordings ORDER BY key')]
        with patch('archive_app.core.subprocess.run',side_effect=AssertionError('Protected row must not remux')):
            self.assertEqual(self.archive.remux_pending_cache(),{'converted':0,'failed':0})
            self.assertEqual(self.archive.invalidate_legacy_cache(),0)
        after=[dict(row) for row in self.archive.conn.execute('SELECT * FROM recordings ORDER BY key')]
        self.assertEqual(before,after)

    def test_pending_cache_batch_limit_and_raw_mode_no_reverse_conversion(self):
        self.raw_row('first');self.raw_row('second')
        with patch('archive_app.core.subprocess.run',side_effect=self.ffmpeg):
            self.assertEqual(self.archive.remux_pending_cache(limit=1),{'converted':1,'failed':0})
        self.assertEqual(self.archive.conn.execute("SELECT COUNT(*) FROM recordings WHERE processing_method='passthrough'").fetchone()[0],1)
        self.settings.media_mode='raw'
        self.assertEqual(self.archive.remux_pending_cache(),{'converted':0,'failed':0})
        self.assertEqual(self.archive.invalidate_legacy_cache(),0)
        for limit in (0,101,True):
            with self.subTest(limit=limit),self.assertRaises(ValueError):self.archive.remux_pending_cache(limit=limit)

    def test_missing_original_cached_file_is_requeued_for_sd_not_faked_as_conversion(self):
        row=self.raw_row()
        Path(row['local_path']).unlink()
        self.assertEqual(self.archive.remux_pending_cache(),{'converted':0,'failed':1})
        changed=self.archive.conn.execute('SELECT * FROM recordings WHERE key=?',(row['key'],)).fetchone()
        self.assertEqual((changed['status'],changed['last_error']),('failed','raw_reingest_required'))
        self.assertIsNone(changed['local_path'])
        self.assertEqual(self.archive.remux_pending_cache(),{'converted':0,'failed':0})

    def test_pending_remux_cache_budget_blocks_before_claim_and_preserves_raw_row_exactly(self):
        row=self.raw_row()
        self.archive.invalidate_legacy_cache()
        before=dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?',(row['key'],)).fetchone())
        raw=Path(row['local_path'])
        self.settings.cache_max_bytes=raw.stat().st_size*3-1
        with patch('archive_app.core.subprocess.run',side_effect=AssertionError('No remux when cache budget is full')):
            result=self.archive.remux_pending_cache(limit=1)
            second=self.archive.remux_pending_cache(limit=1)
        self.assertEqual(result,{'converted':0,'failed':0,'budget_blocked':1})
        self.assertEqual(second,result)
        after=dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?',(row['key'],)).fetchone())
        self.assertEqual(before,after)
        self.assertEqual(raw.read_bytes(),PS)
        self.assertEqual(list(self.settings.cache_dir.glob('*.part.*')),[])
        self.settings.cache_max_bytes=raw.stat().st_size*3
        with patch('archive_app.core.subprocess.run',side_effect=self.ffmpeg):
            self.assertEqual(self.archive.remux_pending_cache(limit=1),{'converted':1,'failed':0})

    def test_pending_remux_free_space_reserves_twice_source_without_changing_pending_status(self):
        row=self.raw_row()
        raw=Path(row['local_path'])
        before=dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?',(row['key'],)).fetchone())
        self.settings.min_free_bytes=1024
        low=SimpleNamespace(free=raw.stat().st_size*2+1023)
        with patch('archive_app.core.shutil.disk_usage',return_value=low), patch('archive_app.core.subprocess.run',side_effect=AssertionError('No remux when disk headroom is low')):
            self.assertEqual(self.archive.remux_pending_cache(),{'converted':0,'failed':0,'budget_blocked':1})
        after=dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?',(row['key'],)).fetchone())
        self.assertEqual(before,after)
        self.assertEqual(raw.read_bytes(),PS)


if __name__=='__main__':
    unittest.main()
