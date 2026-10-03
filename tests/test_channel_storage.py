"""House01 storage channels, safe copy fallback and multipart Local API."""
import io
from dataclasses import replace
from contextlib import closing
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import time
import threading
import unittest
from unittest.mock import Mock, patch

from archive_app.core import Archive, Settings
from archive_app.telegram import ApiRejected, Telegram


class ChannelStorageTests(unittest.TestCase):
    channel = -1001234567890

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mycam-channel-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = Settings(self.root/'state', self.root/'cache', self.root/'input', 'UTC+07:00',
                                 min_free_bytes=0, token='synthetic-not-a-token', enable_upload=True,
                                 owner_user_id=42, allowed_users=(42,43), keep_cache=True)
        self.settings.tenant_id = 'house01'
        self.settings.telegram_destination = 'channel'
        self.settings.storage_channel_id = self.channel
        self.settings.input_dir.mkdir()
        self.archive = Archive(self.settings)
        self.addCleanup(self.archive.close)
        self.telegram = Telegram(self.settings)
        self.calls = []
        self.reply_error = None
        self.copy_result = {'message_id':900}
        self.telegram.request = self.request

    def request(self, method, fields, **kwargs):
        self.calls.append((method,dict(fields),kwargs))
        if method == 'getMe':return {'id':700,'is_bot':True,'username':'House01FixtureBot'}
        if method == 'getChat':return {'id':self.channel,'type':'channel','title':'Synthetic storage'}
        if method == 'getChatMember':return {'status':'administrator','can_post_messages':True,'user':{'id':700,'is_bot':True}}
        if method == 'copyMessage':
            if self.reply_error:raise self.reply_error
            return self.copy_result
        if method in ('sendVideo','sendDocument'):
            field='video' if method=='sendVideo' else 'document'
            recipient=fields['chat_id']
            return {'chat':{'id':recipient,'type':'channel' if recipient<0 else 'private'},
                    'message_id':701,field:{'file_id':'saved-fixture-id','file_unique_id':'saved-fixture-unique'}}
        return True

    def ingest(self, remux=False, camera='front'):
        source=self.settings.input_dir/('original-'+str(time.time_ns())+'.dav')
        source.write_bytes(b'original recording fixture')
        row=self.archive.ingest_entry({'camera':camera,'record_id':source.name,'path':str(source),
                                      'start_time':'2026-10-04T10:00:00+07:00','end_time':'2026-10-04T10:01:00+07:00'})
        if remux:
            old=Path(row['local_path']);mp4=old.with_suffix('.mp4');old.replace(mp4)
            self.archive.conn.execute("UPDATE recordings SET local_path=?,processing_method='remux_copy' WHERE key=?",(str(mp4),row['key']))
            self.archive.conn.commit()
        return row['key']

    def uploaded(self, field='video', legacy=False):
        key=self.ingest()
        self.archive.state('telegram_bot_id','700')
        placement={} if legacy else {'storage_kind':'channel','storage_chat_id':self.channel,'storage_message_id':701}
        self.archive.mark_uploaded(key,42 if legacy else self.channel,701,'saved-fixture-id',
                                   file_unique_id='saved-fixture-unique',media_type=field,bot_id=700,**placement)
        self.calls.clear()
        return key

    def row(self,key):return dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?',(key,)).fetchone())

    def test_channel_upload_never_posts_to_owner_and_does_not_require_owner_start(self):
        key=self.ingest(remux=True)
        self.assertEqual(self.telegram.upload_one(self.archive),'uploaded')
        self.assertEqual([call[0] for call in self.calls],['getMe','getChat','getChatMember','sendVideo'])
        fields=self.calls[-1][1]
        self.assertEqual(fields['chat_id'],self.channel)
        self.assertTrue(fields['supports_streaming'])
        row=self.row(key)
        self.assertEqual((row['storage_kind'],str(row['storage_chat_id']),row['storage_message_id']),('channel',str(self.channel),701))
        self.assertEqual((row['media_type'],row['bot_id']),('video',700))
        self.assertIsNone(self.archive.state('telegram_owner_started:42'))

    def test_channel_raw_file_uses_document_and_verified_channel(self):
        key=self.ingest()
        self.assertEqual(self.telegram.upload_one(self.archive),'uploaded')
        self.assertEqual(self.calls[-1][0],'sendDocument')
        self.assertTrue(self.calls[-1][1]['disable_content_type_detection'])
        self.assertEqual(self.row(key)['media_type'],'document')

    def test_upload_429_persists_shared_pause_before_any_other_camera_claim_or_preflight(self):
        first=self.ingest(camera='front');other=self.ingest(camera='back');original=self.request
        def limited(method,fields,**kwargs):
            if method=='sendDocument':
                self.calls.append((method,dict(fields),kwargs))
                raise ApiRejected(429,120)
            return original(method,fields,**kwargs)
        self.telegram.request=limited
        before=time.time()
        self.assertEqual(self.telegram.upload_one(self.archive,camera='front'),'api_rejected')
        until=self.telegram.upload_retry_until(self.archive)
        self.assertGreaterEqual(until,before+120)
        self.assertEqual(self.row(first)['retry_at'],until)
        self.assertEqual(self.row(first)['status'],'downloaded')
        with closing(Archive(self.settings)) as restarted_archive:
            restarted=Telegram(self.settings)
            restarted.request=Mock(side_effect=AssertionError('Preflight must be paused too'))
            with patch.object(restarted_archive,'claim_upload',wraps=restarted_archive.claim_upload) as claim:
                self.assertEqual(restarted.upload_one(restarted_archive,camera='back'),'rate_limited')
                claim.assert_not_called()
            restarted.request.assert_not_called()
            other_row=dict(restarted_archive.conn.execute('SELECT * FROM recordings WHERE key=?',(other,)).fetchone())
            self.assertEqual(other_row['status'],'downloaded')
            self.assertIsNone(other_row['attempt_id'])
            restarted.request=self.request
            with patch('archive_app.telegram.time.time',return_value=until+1):
                self.assertEqual(restarted.upload_one(restarted_archive,camera='back'),'uploaded')
        self.assertEqual(self.row(other)['status'],'uploaded')
        self.assertEqual(self.row(first)['status'],'downloaded')

    def test_preflight_429_pauses_before_claim_and_persists_for_next_call(self):
        key=self.ingest();original=self.request
        def limited(method,fields,**kwargs):
            if method=='getChat':raise ApiRejected(429,45)
            return original(method,fields,**kwargs)
        self.telegram.request=limited
        self.assertEqual(self.telegram.upload_one(self.archive),'rate_limited')
        self.assertEqual(self.row(key)['status'],'downloaded')
        self.assertIsNone(self.row(key)['attempt_id'])
        self.telegram.request=Mock(side_effect=AssertionError('No API during shared backoff'))
        self.assertEqual(self.telegram.upload_one(self.archive),'rate_limited')
        self.telegram.request.assert_not_called()

    def test_upload_pause_scope_is_tenant_bot_and_destination_not_token_rotation(self):
        self.settings.token='700:synthetic-first-token'
        until=self.telegram._pause_uploads(self.archive,90)
        same_bot=Telegram(replace(self.settings,token='700:synthetic-rotated-token'))
        self.assertEqual(same_bot.upload_retry_until(self.archive),until)
        for settings in (replace(self.settings,tenant_id='house02'),replace(self.settings,token='701:synthetic-other-bot'),
                         replace(self.settings,storage_channel_id=self.channel-1),
                         replace(self.settings,telegram_destination='owner_private',storage_channel_id=0)):
            self.assertEqual(Telegram(settings).upload_retry_until(self.archive),0)
        key=self.telegram._upload_backoff_key()
        self.assertNotIn(self.settings.token,key)
        self.assertNotIn(str(self.channel),key)

    def test_shorter_rate_limit_cannot_shorten_an_existing_shared_pause(self):
        with patch('archive_app.telegram.time.time',return_value=1000):
            first=self.telegram._pause_uploads(self.archive,120)
            second=self.telegram._pause_uploads(self.archive,5)
        self.assertEqual((first,second),(1120,1120))

    def test_shared_upload_pause_does_not_block_requested_channel_replay(self):
        key=self.uploaded()
        self.telegram._pause_uploads(self.archive,60)
        self.calls.clear()
        self.assertEqual(self.telegram.replay(self.archive,key[:32],43,update_id=1),'replayed')
        self.assertEqual([c[0] for c in self.calls],['copyMessage'])

    def test_uncertain_upload_is_not_retried_or_reset_by_backoff_expiry(self):
        key=self.ingest();original=self.request
        def ambiguous(method,fields,**kwargs):
            if method=='sendDocument':raise TimeoutError('synthetic uncertain media POST')
            return original(method,fields,**kwargs)
        self.telegram.request=ambiguous
        self.assertEqual(self.telegram.upload_one(self.archive),'upload_unknown')
        self.telegram._pause_uploads(self.archive,5)
        until=self.telegram.upload_retry_until(self.archive)
        with patch('archive_app.telegram.time.time',return_value=until+1):
            self.assertIsNone(self.telegram.upload_one(self.archive,camera='front'))
        self.assertEqual(self.row(key)['status'],'upload_unknown')

    def test_remux_deployment_never_uploads_stale_raw_cache(self):
        key=self.ingest()
        self.settings.media_mode='remux_copy'
        self.assertEqual(self.telegram.upload_one(self.archive),'needs_review')
        self.assertEqual((self.row(key)['status'],self.row(key)['last_error']),('needs_review','mp4_remux_required'))
        self.assertFalse(any(c[0] in ('sendVideo','sendDocument') for c in self.calls))

    def test_channel_storage_does_not_need_a_private_owner_id(self):
        self.settings.owner_user_id=0
        self.ingest()
        self.assertEqual(self.telegram.upload_one(self.archive),'uploaded')
        self.assertEqual(self.calls[-1][1]['chat_id'],self.channel)

    def test_missing_or_invalid_channel_blocks_without_claim_or_owner_fallback(self):
        key=self.ingest()
        for value in (0,42,True,'-1001234567890'):
            with self.subTest(value=value):
                self.settings.storage_channel_id=value
                self.calls.clear()
                self.assertEqual(self.telegram.upload_one(self.archive),'storage_blocked')
                self.assertEqual(self.row(key)['status'],'downloaded')
                self.assertEqual(self.calls,[])

    def test_nonzero_channel_configuration_overrides_legacy_destination(self):
        self.settings.telegram_destination='owner_private'
        self.archive.state('telegram_owner_started:42','1')
        self.ingest()
        self.assertEqual(self.telegram.upload_one(self.archive),'uploaded')
        self.assertEqual(self.calls[-1][1]['chat_id'],self.channel)

    def test_wrong_bot_channel_or_posting_rights_block_before_claim(self):
        key=self.ingest()
        cases=[('getMe',{'id':True,'is_bot':True}),('getMe',{'id':700,'is_bot':False}),
               ('getChat',{'id':self.channel,'type':'supergroup'}),('getChat',{'id':self.channel-1,'type':'channel'}),
               ('getChat',{'id':self.channel,'type':'channel','username':'public_storage'}),
               ('getChat',{'id':self.channel,'type':'channel','active_usernames':['public_storage']}),
               ('getChatMember',{'status':'member','can_post_messages':True,'user':{'id':700}}),
               ('getChatMember',{'status':'administrator','can_post_messages':False,'user':{'id':700}}),
               ('getChatMember',{'status':'administrator','can_post_messages':True,'user':{'id':700,'is_bot':False}}),
               ('getChatMember',{'status':'administrator','can_post_messages':True,'user':{'id':701}})]
        original=self.request
        for method,response in cases:
            with self.subTest(method=method,response=response):
                self.telegram._storage_verified=None
                self.telegram.request=lambda name,fields,**kwargs: response if name==method else original(name,fields,**kwargs)
                self.assertEqual(self.telegram.upload_one(self.archive),'storage_blocked')
                self.assertEqual(self.row(key)['status'],'downloaded')
                self.assertFalse(any(c[0] in ('sendVideo','sendDocument') for c in self.calls))

    def test_storage_verification_cache_is_bound_to_token_tenant_and_channel(self):
        self.telegram.verify_storage(self.archive)
        self.calls.clear()
        self.telegram.verify_storage(self.archive)
        self.assertEqual(self.calls,[])
        self.settings.tenant_id='house02'
        self.telegram.verify_storage(self.archive)
        self.assertEqual([c[0] for c in self.calls],['getMe','getChat','getChatMember'])
        self.calls.clear()
        self.settings.token='second-synthetic-token'
        self.telegram.verify_storage(self.archive)
        self.assertEqual([c[0] for c in self.calls],['getMe','getChat','getChatMember'])

    def test_wrong_channel_upload_response_is_quarantined_and_cache_retained(self):
        key=self.ingest();original=self.request
        def wrong(method,fields,**kwargs):
            result=original(method,fields,**kwargs)
            if method=='sendDocument':result['chat']={'id':42,'type':'private'}
            return result
        self.telegram.request=wrong
        self.assertEqual(self.telegram.upload_one(self.archive),'upload_unknown')
        self.assertEqual(self.row(key)['status'],'upload_unknown')
        self.assertTrue(Path(self.row(key)['local_path']).exists())

    def test_channel_copy_returns_message_id_without_local_reads_or_catalog_changes(self):
        key=self.uploaded();Path(self.row(key)['local_path']).unlink();before=self.row(key)
        self.assertEqual(self.telegram.replay(self.archive,key[:32],43,update_id=5),'replayed')
        self.assertEqual([c[0] for c in self.calls],['copyMessage'])
        fields=self.calls[0][1]
        self.assertEqual((fields['chat_id'],fields['from_chat_id'],fields['message_id']),(43,self.channel,701))
        self.assertEqual(self.calls[0][2],{})
        self.assertEqual(self.row(key),before)
        saved=json.loads(self.archive.state('telegram_replay_attempt'))
        self.assertEqual((saved['phase'],saved['tenant_id'],saved['recipient'],saved['source_chat_id']),('done','house01',43,self.channel))

    def test_legacy_private_placement_still_replays_matching_file_id(self):
        key=self.uploaded(legacy=True)
        self.assertEqual(self.telegram.replay(self.archive,key[:32],43),'replayed')
        self.assertEqual([c[0] for c in self.calls],['sendVideo'])
        self.assertEqual(self.calls[0][1]['video'],'saved-fixture-id')

    def test_old_private_mode_retains_historical_channel_file_id_replay(self):
        key=self.uploaded()
        self.settings.telegram_destination='owner_private'
        self.settings.storage_channel_id=0
        self.assertEqual(self.telegram.replay(self.archive,key[:32],43),'replayed')
        self.assertEqual([c[0] for c in self.calls],['sendVideo'])
        self.assertEqual(self.calls[0][1]['video'],'saved-fixture-id')

    def test_confirmed_missing_channel_message_falls_back_only_to_matching_file_type(self):
        for field,method in (('video','sendVideo'),('document','sendDocument')):
            with self.subTest(field=field):
                key=self.uploaded(field=field)
                self.reply_error=ApiRejected(400,description='Bad Request: message to copy not found')
                self.assertEqual(self.telegram.replay(self.archive,key[:32],43,update_id=10 if field=='video' else 11),'replayed')
                self.assertEqual([c[0] for c in self.calls],['copyMessage',method])
                self.assertEqual(self.calls[-1][1][field],'saved-fixture-id')
                self.assertEqual(self.calls[-1][2],{})

    def test_copy_timeout_server_error_and_generic_rejection_never_fall_back(self):
        for index,error in enumerate((TimeoutError('synthetic'),ApiRejected(503),ApiRejected(400),ApiRejected(403))):
            with self.subTest(error=type(error).__name__):
                key=self.uploaded();self.reply_error=error
                expected='rejected' if isinstance(error,ApiRejected) and error.code in (400,403) else 'unknown'
                self.assertEqual(self.telegram.replay(self.archive,key[:32],43,update_id=20+index),expected)
                self.assertEqual([c[0] for c in self.calls],['copyMessage'])
                self.assertEqual(self.telegram.replay(self.archive,key[:32],43,update_id=20+index),'consumed_without_retry')
                self.assertEqual(len(self.calls),1)

    def test_copy_rate_limit_keeps_same_event_for_retry_without_fallback(self):
        key=self.uploaded();self.reply_error=ApiRejected(429,12)
        with self.assertRaises(ApiRejected):self.telegram.replay(self.archive,key[:32],43,update_id=30)
        self.assertEqual([c[0] for c in self.calls],['copyMessage'])
        self.assertIsNone(self.archive.state('telegram_offset'))
        self.assertGreater(float(self.archive.state('telegram_replay_retry_at')),time.time())

    def test_fallback_timeout_is_journaled_once_and_never_sent_again_for_same_event(self):
        key=self.uploaded();original=self.request
        def ambiguous(method,fields,**kwargs):
            if method=='sendVideo':
                self.calls.append((method,dict(fields),kwargs))
                raise TimeoutError('synthetic fallback timeout')
            return original(method,fields,**kwargs)
        self.telegram.request=ambiguous
        self.reply_error=ApiRejected(400,description='Bad Request: message to copy not found')
        self.assertEqual(self.telegram.replay(self.archive,key[:32],43,update_id=31),'unknown')
        attempt=json.loads(self.archive.state('telegram_replay_attempt'))
        self.assertEqual((attempt['method'],attempt['phase']),('sendVideo','unknown'))
        self.assertEqual(self.telegram.replay(self.archive,key[:32],43,update_id=31),'consumed_without_retry')
        self.assertEqual([c[0] for c in self.calls],['copyMessage','sendVideo'])

    def test_delete_during_missing_copy_prevents_secondary_file_id_post(self):
        key=self.uploaded();original=self.request
        def removed(method,fields,**kwargs):
            if method=='copyMessage':
                self.calls.append((method,dict(fields),kwargs))
                self.archive.soft_delete(key,43)
                raise ApiRejected(400,description='Bad Request: message to copy not found')
            return original(method,fields,**kwargs)
        self.telegram.request=removed
        self.assertEqual(self.telegram.replay(self.archive,key[:32],43,update_id=32),'unknown')
        self.assertEqual([c[0] for c in self.calls],['copyMessage'])
        self.assertIsNotNone(self.row(key)['deleted_at'])

    def test_invalid_copy_success_is_unknown_not_media_fallback(self):
        for index,result in enumerate(({}, {'message_id':0}, {'message_id':True}, {'message_id':'3'}, {'message_id':3,'video':{}})):
            with self.subTest(result=result):
                key=self.uploaded();self.copy_result=result
                self.assertEqual(self.telegram.replay(self.archive,key[:32],43,update_id=40+index),'unknown')
                self.assertEqual([c[0] for c in self.calls],['copyMessage'])

    def test_forbidden_viewer_deleted_row_and_cross_channel_never_copy(self):
        key=self.uploaded()
        with self.assertRaises(ValueError):self.telegram.replay(self.archive,key[:32],99)
        self.archive.soft_delete(key,43)
        with self.assertRaises(ValueError):self.telegram.replay(self.archive,key[:32],43)
        self.archive.restore_recording(key,43)
        self.settings.storage_channel_id=self.channel-1
        with self.assertRaises(ValueError):self.telegram.replay(self.archive,key[:32],43)
        self.assertEqual(self.calls,[])

    def test_different_bot_identity_blocks_channel_copy(self):
        key=self.uploaded();self.archive.state('telegram_bot_id','701')
        with self.assertRaises(ValueError):self.telegram.replay(self.archive,key[:32],43)
        self.assertEqual(self.calls,[])

    def poll_message(self,actor,text='',**extra):
        original=self.request
        update={'update_id':100,'message':{'from':{'id':actor},'chat':{'id':actor,'type':'private'},'text':text,**extra}}
        def request(method,fields,**kwargs):
            if method=='getUpdates':return [update]
            return original(method,fields,**kwargs)
        self.telegram.request=request
        self.telegram.poll(self.archive)
        return next(c[1]['text'] for c in self.calls if c[0]=='sendMessage')

    def test_owner_channel_command_explains_forwarding_without_configuring_storage(self):
        self.settings.storage_channel_id=0
        text=self.poll_message(42,'/channel')
        self.assertIn('Forward',text)
        self.assertIn('TELEGRAM_STORAGE_CHANNEL_ID',text)
        self.assertEqual(self.settings.storage_channel_id,0)
        self.assertFalse(any(c[0] in ('getChat','sendVideo','sendDocument') for c in self.calls))

    def test_owner_modern_forward_reports_negative_channel_identity_only(self):
        self.settings.storage_channel_id=0
        text=self.poll_message(42,forward_origin={'type':'channel','chat':{'id':self.channel,'type':'channel'},'message_id':1})
        self.assertIn(f'Channel ID: {self.channel}',text)
        self.assertEqual(self.settings.storage_channel_id,0)
        self.assertIsNone(self.archive.state('telegram_storage_channel_id'))

    def test_owner_legacy_forward_reports_channel_identity(self):
        text=self.poll_message(42,forward_from_chat={'id':self.channel,'type':'channel'})
        self.assertIn(f'Channel ID: {self.channel}',text)

    def test_viewer_forward_and_channel_command_do_not_disclose_channel_identity(self):
        text=self.poll_message(43,'/channel',forward_origin={'type':'channel','chat':{'id':self.channel,'type':'channel'}})
        self.assertNotIn(str(self.channel),text)
        with self.assertRaises(ValueError):self.telegram.channel_setup(43)

    def test_setup_rejects_positive_boolean_nonchannel_and_wrong_origin_identity(self):
        for chat in ({'id':42,'type':'channel'},{'id':True,'type':'channel'},
                     {'id':self.channel,'type':'supergroup'},{'id':str(self.channel),'type':'channel'}):
            with self.subTest(chat=chat):
                text,_=self.telegram.channel_setup(42,{'forward_origin':{'type':'channel','chat':chat}})
                self.assertNotIn('Channel ID:',text)
        text,_=self.telegram.channel_setup(42,{'forward_origin':{'type':'user'},'forward_from_chat':{'id':self.channel,'type':'channel'}})
        self.assertNotIn('Channel ID:',text)


class LocalMultipartTests(unittest.TestCase):
    def test_local_api_upload_streams_bytes_not_cross_tenant_file_uri(self):
        with tempfile.TemporaryDirectory(prefix='mycam-multipart-') as temporary:
            root=Path(temporary);path=root/'house01-clip.ps';body=b'original PS fixture bytes'
            path.write_bytes(body)
            settings=Settings(root/'state',root/'cache',root/'input','UTC+07:00',token='synthetic-not-a-token',
                              api_mode='local',api_base='http://telegram-bot-api:8081')
            response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
            response.read.return_value=b'{"ok":true,"result":{"message_id":1}}'
            captured={}
            def urlopen(request,**kwargs):
                captured['request']=request;captured['data']=b''.join(request.data);return response
            with patch('archive_app.telegram.urllib.request.urlopen',side_effect=urlopen):
                Telegram(settings).request('sendDocument',{'chat_id':-100123,'disable_content_type_detection':True},file_path=path,file_field='document')
            self.assertIn(body,captured['data'])
            self.assertNotIn(b'file://',captured['data'])
            self.assertIn(b'filename="house01-clip.ps"',captured['data'])
            self.assertIn(b'Content-Type: application/octet-stream',captured['data'])
            self.assertEqual(int(captured['request'].get_header('Content-length')),len(captured['data']))
            self.assertEqual(path.read_bytes(),body)

    def test_real_local_http_receives_multipart_stream_with_exact_original_bytes(self):
        captured={}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                captured['body']=self.rfile.read(int(self.headers['Content-Length']))
                captured['content_type']=self.headers['Content-Type']
                result=b'{"ok":true,"result":{"message_id":1}}'
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(result)));self.end_headers();self.wfile.write(result)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='mycam-local-http-') as temporary:
                root=Path(temporary);path=root/'clip.ps';body=bytes(range(256))*6000
                path.write_bytes(body)
                settings=Settings(root/'state',root/'cache',root/'input','UTC+07:00',token='synthetic-not-a-token',
                                  api_mode='local',api_base=f'http://127.0.0.1:{server.server_port}')
                self.assertEqual(Telegram(settings).request('sendDocument',{'chat_id':-100123},file_path=path,file_field='document'),{'message_id':1})
                boundary=captured['content_type'].split('boundary=',1)[1].encode()
                part=next(part for part in captured['body'].split(b'--'+boundary) if b'filename="clip.ps"' in part)
                actual=part.split(b'\r\n\r\n',1)[1][:-2]
                self.assertEqual(actual,body)
                self.assertNotIn(b'file://',captured['body'])
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2)

    def test_api_error_parser_only_classifies_exact_definitive_source_missing(self):
        with tempfile.TemporaryDirectory(prefix='mycam-api-rejection-') as temporary:
            root=Path(temporary)
            settings=Settings(root/'state',root/'cache',root/'input','UTC+07:00',token='synthetic-not-a-token')
            for description,missing in [('Bad Request: message to copy not found',True),('Bad Request: chat not found',False),('synthetic private credential text',False)]:
                response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
                response.read.return_value=json.dumps({'ok':False,'error_code':400,'description':description}).encode()
                with patch('archive_app.telegram.urllib.request.urlopen',return_value=response), self.assertRaises(ApiRejected) as caught:
                    Telegram(settings).request('copyMessage',{'chat_id':43,'from_chat_id':-100123,'message_id':1})
                self.assertEqual(caught.exception.source_missing,missing)
                self.assertNotIn(description,str(caught.exception))


if __name__ == '__main__':
    unittest.main()
