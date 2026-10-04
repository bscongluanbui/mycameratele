"""Direct MP4 transport uses synthetic files/HTTP, never a live bot/camera."""
from contextlib import nullcontext
import copy
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
import uuid
from unittest.mock import patch

from archive_app.core import Settings
from archive_app.local_upload import local_mp4_uri, validate_upload_root, LocalUploadError
from archive_app.telegram import Telegram, ApiRejected
from archive_app.upload_spool import check_upload_spool, SpoolBudgetError
from tests import test_telegram as fixtures


class LocalFilePathTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parent if os.name=='nt' else Path(tempfile.gettempdir()).resolve()
        self.root=parent/('.tmp-direct-path-'+uuid.uuid4().hex);self.root.mkdir()
        self.cache=self.root/'cache';self.cache.mkdir();self.key='a'*64
        self.path=self.cache/(self.key+'.mp4');self.path.write_bytes(b'synthetic-mp4')
        self.settings=Settings(self.root/'state',self.cache,self.root/'input','UTC+07:00',
            api_mode='local',upload_transport='local_file',local_upload_root='/cache')

    def tearDown(self):shutil.rmtree(self.root)

    def test_managed_path_maps_without_reading_or_hashing_bytes(self):
        with patch('pathlib.Path.open',side_effect=AssertionError('No file reads')):
            self.assertEqual(local_mp4_uri(self.settings,self.path,expected_key=self.key),
                             'file:///cache/'+self.key+'.mp4')

    def test_api_mount_root_is_uri_encoded(self):
        self.settings.local_upload_root='/cache space/nhà#%'
        self.assertEqual(local_mp4_uri(self.settings,self.path),
                         'file:///cache%20space/nh%C3%A0%23%25/'+self.key+'.mp4')

    def test_invalid_mount_roots_are_rejected(self):
        for value in ('','/','relative','//server/cache','/cache/../other','/cache/./x',
                      'file:///cache','/cache\\x','/cache\x00x','/cache\nx','/cache\ud800'):
            with self.subTest(value=repr(value)),self.assertRaises(LocalUploadError):validate_upload_root(value)

    def test_local_transport_requires_local_api_and_explicit_selection(self):
        for field,value in (('api_mode','cloud'),('upload_transport','multipart')):
            before=getattr(self.settings,field);setattr(self.settings,field,value)
            with self.assertRaises(LocalUploadError):local_mp4_uri(self.settings,self.path)
            setattr(self.settings,field,before)

    def test_rejects_missing_empty_non_mp4_and_wrong_key(self):
        self.path.unlink()
        with self.assertRaises(LocalUploadError):local_mp4_uri(self.settings,self.path)
        self.path.touch()
        with self.assertRaises(LocalUploadError):local_mp4_uri(self.settings,self.path)
        self.path.write_bytes(b'fixture')
        for path,key in ((self.path,'b'*64),(self.cache/'clip.mp4',None),
                         (self.cache/(self.key+'.ps'),self.key)):
            with self.subTest(path=path),self.assertRaises(LocalUploadError):
                local_mp4_uri(self.settings,path,expected_key=key)

    def test_rejects_file_outside_root_and_changed_original_root(self):
        outside=self.root/self.path.name;outside.write_bytes(b'outside')
        with self.assertRaises(LocalUploadError):local_mp4_uri(self.settings,outside)
        with self.assertRaises(LocalUploadError):local_mp4_uri(self.settings,self.path,cache_root=self.root/'original')

    def test_symlink_is_rejected_without_following_file_bytes(self):
        outside=self.root/'outside';outside.write_bytes(b'private fixture');self.path.unlink()
        try:self.path.symlink_to(outside)
        except (OSError,NotImplementedError):self.skipTest('Symlinks unavailable on fixture host')
        with self.assertRaises(LocalUploadError):local_mp4_uri(self.settings,self.path)
        self.assertEqual(outside.read_bytes(),b'private fixture')

    def test_fifo_is_rejected_without_opening_or_blocking(self):
        if not hasattr(os,'mkfifo'):self.skipTest('POSIX FIFO fixture')
        self.path.unlink();os.mkfifo(self.path)
        with self.assertRaises(LocalUploadError):local_mp4_uri(self.settings,self.path)

    def test_env_defaults_and_explicit_local_mode(self):
        with patch.dict(os.environ,{'DISPLAY_TIMEZONE':'UTC+07:00'},clear=True):
            self.assertEqual(Settings.from_env().upload_transport,'multipart')
        with patch.dict(os.environ,{'TELEGRAM_API_MODE':'local','TELEGRAM_UPLOAD_TRANSPORT':'local_file',
                                   'TELEGRAM_LOCAL_UPLOAD_ROOT':'/cache','DISPLAY_TIMEZONE':'UTC+07:00'},clear=True):
            s=Settings.from_env();self.assertEqual((s.upload_transport,s.local_upload_root),('local_file','/cache'))
        for env in ({'TELEGRAM_UPLOAD_TRANSPORT':'local_file','TELEGRAM_LOCAL_UPLOAD_ROOT':'/cache'},
                    {'TELEGRAM_UPLOAD_TRANSPORT':'invalid'},
                    {'TELEGRAM_API_MODE':'local','TELEGRAM_UPLOAD_TRANSPORT':'local_file'}):
            with self.subTest(env=env),patch.dict(os.environ,env,clear=True),self.assertRaises(ValueError):Settings.from_env()

    def test_json_request_has_no_media_body_and_keeps_upload_timeout(self):
        self.settings.token='fixture';t=Telegram(self.settings);response=io.BytesIO(b'{"ok":true,"result":{"message_id":1}}')
        with (patch('archive_app.telegram.urllib.request.urlopen',return_value=response) as request,
              patch('pathlib.Path.open',side_effect=AssertionError('Do not read MP4'))):
            self.assertEqual(t.request('sendVideo',{'chat_id':42},file_path=self.path,file_field='video',local_file=True),{'message_id':1})
        req=request.call_args.args[0]
        self.assertEqual(req.get_header('Content-type'),'application/json')
        self.assertEqual(json.loads(req.data),{'chat_id':42,'video':'file:///cache/'+self.path.name})
        self.assertLess(len(req.data),200);self.assertEqual(request.call_args.kwargs['timeout'],1800)

    def test_non_video_direct_request_rejected_before_http(self):
        self.settings.token='fixture'
        with patch('archive_app.telegram.urllib.request.urlopen') as request,self.assertRaises(LocalUploadError):
            Telegram(self.settings).request('sendDocument',{},file_path=self.path,file_field='document',local_file=True)
        request.assert_not_called()

    def test_real_local_http_receives_json_only_for_large_mp4(self):
        self.path.write_bytes(b'synthetic-video-payload'*100000);captured={}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                captured['body']=self.rfile.read(int(self.headers['Content-Length']))
                captured['type']=self.headers['Content-Type']
                data=b'{"ok":true,"result":{"message_id":1}}';self.send_response(200)
                self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            self.settings.token='fixture';self.settings.api_base=f'http://127.0.0.1:{server.server_port}'
            Telegram(self.settings).request('sendVideo',{'chat_id':42},file_path=self.path,file_field='video',local_file=True)
            self.assertEqual(captured['type'],'application/json');self.assertLess(len(captured['body']),200)
            self.assertEqual(json.loads(captured['body'])['video'],'file:///cache/'+self.path.name)
            self.assertGreater(self.path.stat().st_size,2000000)
        finally:server.shutdown();server.server_close();thread.join(timeout=2)

    def test_direct_does_not_reserve_a_new_spool_file_but_checks_usage_and_free(self):
        spool=self.root/'spool';spool.mkdir();(spool/'active').write_bytes(b'x'*100)
        self.settings.bot_api_spool_root=spool;self.settings.bot_api_spool_max_bytes=100;self.settings.min_free_bytes=0
        self.assertEqual(check_upload_spool(self.settings,1000,local_file=True)['reserved_bytes'],0)
        with self.assertRaises(SpoolBudgetError):check_upload_spool(self.settings,1)
        with patch('archive_app.upload_spool._used_bytes',return_value=(101,1000)),self.assertRaises(SpoolBudgetError):
            check_upload_spool(self.settings,1,local_file=True)
        self.settings.min_free_bytes=100
        with patch('archive_app.upload_spool._used_bytes',return_value=(0,99)),self.assertRaises(SpoolBudgetError):
            check_upload_spool(self.settings,1,local_file=True)


class LocalFileLifecycleTests(unittest.TestCase):
    ingest=fixtures.TelegramTests.ingest
    row=fixtures.TelegramTests.row
    tearDown=fixtures.TelegramTests.tearDown

    def setUp(self):
        fixtures.TelegramTests.setUp(self)
        self.settings.api_mode='local';self.settings.upload_transport='local_file'
        self.settings.local_upload_root='/cache';self.settings.media_mode='remux_copy'
        self.request.return_value={'chat':{'id':42,'type':'private'},'message_id':17,
                                   'video':{'file_id':'fixture-file','file_unique_id':'fixture-unique'}}

    def fake_normalize(self,source,destination,settings):
        info=fixtures.TelegramTests.fake_normalize(self,source,destination,settings)
        info['processing_method']='remux_copy';return info

    def test_confirmed_commit_precedes_mp4_and_raw_alias_cleanup(self):
        key=self.ingest();path=Path(self.row(key)['local_path']);raw=self.settings.cache_dir/(key+'.ps');raw.write_bytes(b'raw')
        mark=self.archive.mark_uploaded
        def committed(*args,**kwargs):
            self.assertTrue(path.exists());self.assertTrue(raw.exists());self.assertEqual(self.row(key)['status'],'uploading')
            mark(*args,**kwargs);self.assertEqual(self.row(key)['status'],'uploaded')
        with patch.object(self.archive,'mark_uploaded',side_effect=committed):
            self.assertEqual(self.telegram.upload_one(self.archive),'uploaded')
        self.assertFalse(path.exists());self.assertFalse(raw.exists())
        self.assertTrue(self.request.call_args.kwargs['local_file'])
        self.assertEqual(self.request.call_args.kwargs['file_path'],path)

    def test_source_is_present_during_entire_request(self):
        key=self.ingest();path=Path(self.row(key)['local_path']);reply=copy.deepcopy(self.request.return_value)
        def sending(*args,**kwargs):
            self.assertTrue(path.exists());self.assertEqual(self.row(key)['status'],'uploading');return reply
        self.request.side_effect=sending
        self.assertEqual(self.telegram.upload_one(self.archive),'uploaded')

    def test_timeout_server_error_invalid_response_and_commit_error_keep_file(self):
        for name,error in (('timeout',TimeoutError()),('server',ApiRejected(500)),('invalid',None),('commit',RuntimeError('fixture'))):
            with self.subTest(name=name):
                key=self.ingest(record_id=name);path=Path(self.row(key)['local_path'])
                self.request.side_effect=error if name in ('timeout','server') else None
                reply=copy.deepcopy(self.request.return_value)
                if name=='invalid':self.request.return_value={'message_id':17}
                manager=patch.object(self.archive,'mark_uploaded',side_effect=error) if name=='commit' else nullcontext()
                with manager:self.telegram.upload_one(self.archive)
                row=self.row(key);self.assertEqual(row['status'],'upload_unknown');self.assertTrue(path.exists())
                self.request.return_value=reply;self.request.reset_mock()
                self.assertIsNone(self.telegram.upload_one(self.archive));self.request.assert_not_called()

    def test_definitive_400_preserves_and_never_falls_back_to_multipart(self):
        key=self.ingest();path=Path(self.row(key)['local_path']);self.request.side_effect=ApiRejected(400)
        self.assertEqual(self.telegram.upload_one(self.archive),'api_rejected')
        self.assertEqual(self.row(key)['status'],'needs_review');self.assertTrue(path.exists())
        self.assertEqual(self.request.call_count,1);self.assertTrue(self.request.call_args.kwargs['local_file'])

    def test_429_preserves_pending_file_and_global_backoff(self):
        key=self.ingest();path=Path(self.row(key)['local_path']);self.request.side_effect=ApiRejected(429,60)
        self.assertEqual(self.telegram.upload_one(self.archive),'api_rejected')
        self.assertEqual(self.row(key)['status'],'downloaded');self.assertTrue(path.exists())
        self.assertGreater(self.telegram.upload_retry_until(self.archive),0)

    def test_invalid_path_is_known_unsent_review(self):
        key=self.ingest();outside=self.input/(key+'.mp4');outside.write_bytes(b'fixture')
        with self.archive.conn:self.archive.conn.execute('UPDATE recordings SET local_path=? WHERE key=?',(str(outside),key))
        self.assertEqual(self.telegram.upload_one(self.archive),'needs_review')
        self.assertEqual(self.row(key)['last_error'],'local_upload_path');self.request.assert_not_called()

    def test_path_recheck_failure_before_post_is_not_ambiguous(self):
        key=self.ingest();self.request.side_effect=LocalUploadError('fixture')
        self.assertEqual(self.telegram.upload_one(self.archive),'needs_review')
        self.assertEqual(self.row(key)['last_error'],'local_upload_path')
        self.assertTrue(Path(self.row(key)['local_path']).exists())

    def test_cloud_and_explicit_multipart_keep_original_transport(self):
        for api,transport in (('cloud','multipart'),('local','multipart')):
            self.settings.api_mode=api;self.settings.upload_transport=transport
            key=self.ingest(record_id=api)
            self.assertEqual(self.telegram.upload_one(self.archive),'uploaded')
            self.assertNotIn('local_file',self.request.call_args.kwargs)

    def test_raw_document_uses_multipart_even_with_local_setting(self):
        key=self.ingest()
        with self.archive.conn:self.archive.conn.execute("UPDATE recordings SET processing_method='passthrough' WHERE key=?",(key,))
        self.settings.media_mode='raw'
        self.request.return_value={'chat':{'id':42,'type':'private'},'message_id':17,
                                   'document':{'file_id':'fixture-file','file_unique_id':'fixture-unique'}}
        self.assertEqual(self.telegram.upload_one(self.archive),'uploaded')
        self.assertEqual(self.request.call_args.args[0],'sendDocument');self.assertNotIn('local_file',self.request.call_args.kwargs)

    def test_channel_success_has_confirmed_channel_placement(self):
        channel=-1001234567890
        self.settings.storage_channel_id=channel;self.settings.telegram_destination='channel'
        self.request.return_value['chat']={'id':channel,'type':'channel'}
        key=self.ingest()
        with patch.object(self.telegram,'verify_storage',return_value=(channel,123)):
            self.assertEqual(self.telegram.upload_one(self.archive),'uploaded')
        row=self.row(key)
        self.assertEqual((row['storage_kind'],row['storage_chat_id'],row['storage_message_id']),('channel',channel,17))
        self.assertEqual(row['bot_id'],123);self.assertTrue(self.request.call_args.kwargs['local_file'])

    def test_wrong_destination_response_is_ambiguous_and_retains_mp4(self):
        key=self.ingest();self.request.return_value['chat']['id']=43
        self.assertEqual(self.telegram.upload_one(self.archive),'upload_unknown')
        self.assertTrue(Path(self.row(key)['local_path']).exists());self.assertIsNone(self.row(key)['file_id'])
