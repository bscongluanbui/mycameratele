"""Per-camera channel dashboard contract; synthetic Telegram, no live sends."""
import http.client
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
import uuid
from unittest.mock import Mock, patch

from archive_app.core import Archive, Settings
from archive_app.dashboard import DashboardServer


class CameraChannelDashboardTests(unittest.TestCase):
    def setUp(self):
        self.parent=(Path(__file__).parent if os.name=='nt' else Path(tempfile.gettempdir())).resolve()
        self.root=self.parent/('.tmp-camera-channel-http-'+uuid.uuid4().hex);self.root.mkdir()
        self.settings=Settings(self.root/'state',self.root/'cache',self.root/'input','UTC+07:00',
                               min_free_bytes=0,multi_channel_routing=True)
        self.archive=Archive(self.settings)
        self.archive.add_camera({'id':'pn','name':'PN','host':'192.168.55.2','enabled':True,
                                 'upload_enabled':True,'sd_password':'synthetic-device-password',
                                 'channel_chat_id':-1001234567890})
        self.telegram=Mock();self.telegram.verify_camera_channel.return_value=(-1001234567890,991)
        self.patch_telegram=patch('archive_app.dashboard.Telegram',return_value=self.telegram)
        self.patch_telegram.start()
        self.directory=Mock();self.directory.list.return_value=[
            {'chat_id':-1001234567890,'name':'Camera PN','bound_camera_id':'pn','private':True,'ready':True}]
        self.directory.refresh.return_value=self.directory.list.return_value
        self.patch_directory=patch('archive_app.dashboard.ChannelDirectory',return_value=self.directory)
        self.patch_directory.start()
        self.server=DashboardServer(('127.0.0.1',0),self.settings)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.02},daemon=True)
        self.thread.start();self.cookie='';self.csrf=''

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)
        self.patch_directory.stop();self.patch_telegram.stop();self.archive.close()
        self.assertEqual(self.root.resolve().parent,self.parent);shutil.rmtree(self.root)

    def request(self,method,path,data=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=30)
        values={'Cookie':self.cookie,'X-CSRF-Token':self.csrf,**(headers or {})}
        if data is not None:values['Content-Type']='application/json';data=json.dumps(data).encode()
        conn.request(method,path,data,values);response=conn.getresponse()
        status=response.status;body=json.loads(response.read());cookie=response.getheader('Set-Cookie','')
        conn.close();return status,body,cookie

    def login(self,setup=True):
        code,payload,cookie=self.request('POST','/api/login',{'username':'admin','password':'admin'})
        self.assertEqual(code,200);self.cookie=cookie.split(';')[0];self.csrf=payload['csrf_token']
        if not setup:return
        self.assertEqual(self.request('POST','/api/account',{'current_password':'admin','username':'fixture_admin',
                                                          'new_password':'synthetic-password'})[0],200)
        code,payload,cookie=self.request('POST','/api/login',{'username':'fixture_admin','password':'synthetic-password'})
        self.assertEqual(code,200);self.cookie=cookie.split(';')[0];self.csrf=payload['csrf_token']

    def camera(self):return next(camera for camera in self.archive.cameras() if camera['id']=='pn')

    def test_channel_routes_require_login_and_initial_password_change(self):
        calls=[('GET','/api/telegram/channels',None),('POST','/api/telegram/channels/refresh',{}),
               ('POST','/api/cameras/pn/channel-check',{})]
        for method,path,data in calls:self.assertEqual(self.request(method,path,data)[0],401)
        self.login(False)
        for method,path,data in calls:self.assertEqual(self.request(method,path,data)[0],409)
        self.telegram.verify_camera_channel.assert_not_called();self.directory.list.assert_not_called()
        self.directory.refresh.assert_not_called()

    def test_channel_mutations_enforce_csrf_and_origin(self):
        self.login()
        for path in ('/api/telegram/channels/refresh','/api/cameras/pn/channel-check'):
            self.assertEqual(self.request('POST',path,{}, {'X-CSRF-Token':'wrong'})[0],403)
            self.assertEqual(self.request('POST',path,{}, {'Origin':'https://unrelated.invalid'})[0],403)
        self.telegram.verify_camera_channel.assert_not_called();self.directory.refresh.assert_not_called()

    def test_camera_list_exposes_mapping_status_and_routing_flags_without_secrets(self):
        self.login();code,payload,_=self.request('GET','/api/cameras')
        self.assertEqual(code,200);self.assertTrue(payload['multi_channel_routing'])
        self.assertTrue(payload['channel_index_enabled'])
        camera=payload['cameras'][0];self.assertEqual(camera['channel_chat_id'],-1001234567890)
        self.assertTrue(camera['channel_enabled']);self.assertEqual(camera['channel_status'],'unconfigured')
        self.assertNotIn('synthetic-device-password',json.dumps(payload));self.assertNotIn('sd_password',camera)

    def test_verify_channel_persists_ready_without_toggling_camera_or_upload(self):
        self.login();code,payload,_=self.request('POST','/api/cameras/pn/channel-check',{})
        self.assertEqual(code,200);self.assertTrue(payload['ready'])
        self.assertEqual(payload['channel_chat_id'],-1001234567890)
        self.assertEqual(self.telegram.verify_camera_channel.call_args.args[1],'pn')
        self.assertEqual(self.telegram.verify_camera_channel.call_args.kwargs,{'require_index':True})
        camera=self.camera();self.assertEqual(camera['channel_status'],'ready')
        self.assertTrue(camera['enabled']);self.assertTrue(camera['upload_enabled'])

    def test_real_channel_check_succeeds_for_paused_camera_without_starting_upload(self):
        from archive_app.telegram import Telegram
        self.login();telegram=Telegram(self.settings)
        telegram.request=Mock(side_effect=lambda method,data: {
            'getMe':{'id':991,'is_bot':True},
            'getChat':{'id':-1001234567890,'type':'channel','title':'Phòng ngủ'},
            'getChatMember':{'user':{'id':991,'is_bot':True},'status':'administrator',
                             'can_post_messages':True,'can_edit_messages':True},
        }[method])
        with patch('archive_app.dashboard.Telegram',return_value=telegram):
            for upload_enabled in (False,True):
                with self.subTest(upload_enabled=upload_enabled):
                    self.archive.update_camera('pn',{'enabled':False,'upload_enabled':upload_enabled,
                                                     'channel_name':'PN · Nhà ba má'})
                    telegram._channel_verified.clear();telegram.request.reset_mock()
                    code,payload,_=self.request('POST','/api/cameras/pn/channel-check',{})
                    self.assertEqual(code,200);self.assertTrue(payload['ready'])
                    self.assertEqual(payload['channel_chat_id'],-1001234567890)
                    camera=payload['camera'];self.assertFalse(camera['enabled'])
                    self.assertEqual(camera['upload_enabled'],upload_enabled)
                    self.assertEqual(camera['channel_status'],'ready');self.assertIsNone(camera['channel_error'])
                    self.assertEqual(camera['channel_name'],'PN · Nhà ba má')
                    self.assertEqual(camera['channel_chat_id'],-1001234567890)
                    self.assertEqual([call.args[0] for call in telegram.request.call_args_list],
                                     ['getMe','getChat','getChatMember'])
                    self.assertFalse(self.camera()['enabled'])
                    self.assertEqual(self.camera()['upload_enabled'],upload_enabled)

    def test_failed_verify_is_sanitized_and_persists_error_without_global_fallback(self):
        self.login();self.telegram.verify_camera_channel.side_effect=RuntimeError('synthetic-token /internal/secret')
        code,payload,_=self.request('POST','/api/cameras/pn/channel-check',{})
        self.assertEqual(code,409);self.assertEqual(payload['code'],'camera_channel_check_failed')
        self.assertNotIn('synthetic-token',json.dumps(payload));self.assertNotIn('/internal/secret',json.dumps(payload))
        camera=self.camera();self.assertEqual(camera['channel_status'],'error')
        self.assertEqual(camera['channel_error'],'camera_channel_check_failed')
        self.assertEqual(camera['channel_chat_id'],-1001234567890);self.assertTrue(camera['upload_enabled'])

    def test_typed_channel_check_failure_returns_specific_safe_code_without_changing_switches(self):
        from archive_app.telegram import ChannelCheckError
        self.login();self.archive.update_camera('pn',{'enabled':False,'upload_enabled':False,
                                                     'channel_name':'Phòng ngủ'})
        known=('channel_invalid_id','channel_bot_identity_invalid','channel_identity_mismatch',
               'channel_not_private','channel_bot_not_admin','channel_post_permission_missing',
               'channel_edit_permission_missing','channel_disabled','channel_changed','channel_check_failed')
        for expected in known:
            with self.subTest(code=expected):
                error=ChannelCheckError(expected)
                error.args=('synthetic-private-token /internal/secret',)
                self.telegram.verify_camera_channel.side_effect=error
                code,payload,_=self.request('POST','/api/cameras/pn/channel-check',{})
                self.assertEqual(code,409);self.assertEqual(payload['code'],expected)
                self.assertTrue(payload['error'])
                self.assertNotIn('synthetic-private-token',json.dumps(payload))
                self.assertNotIn('/internal/secret',json.dumps(payload))
                camera=self.camera();self.assertEqual(camera['channel_status'],'error')
                self.assertEqual(camera['channel_error'],expected)
                self.assertEqual(camera['channel_chat_id'],-1001234567890)
                self.assertEqual(camera['channel_name'],'Phòng ngủ')
                self.assertFalse(camera['enabled']);self.assertFalse(camera['upload_enabled'])

    def test_forged_channel_check_code_is_not_exposed_or_persisted(self):
        from archive_app.telegram import ChannelCheckError
        self.login();error=ChannelCheckError('channel_check_failed')
        error.code='synthetic-private-token /internal/secret'
        self.telegram.verify_camera_channel.side_effect=error
        code,payload,_=self.request('POST','/api/cameras/pn/channel-check',{})
        self.assertEqual(code,409);self.assertEqual(payload['code'],'camera_channel_check_failed')
        self.assertNotIn('synthetic-private-token',json.dumps(payload))
        self.assertNotIn('/internal/secret',json.dumps(payload))
        self.assertEqual(self.camera()['channel_error'],'camera_channel_check_failed')

    def test_channel_api_rejections_return_specific_sanitized_diagnostics(self):
        from archive_app.telegram import ApiRejected
        self.login()
        for api_code,expected in ((429,'channel_rate_limited'),(401,'channel_bot_token_invalid'),
                                  (400,'channel_access_denied'),(403,'channel_access_denied'),
                                  (500,'camera_channel_check_failed')):
            with self.subTest(api_code=api_code):
                self.telegram.verify_camera_channel.side_effect=ApiRejected(
                    api_code,60,'synthetic-private-token /internal/secret')
                code,payload,_=self.request('POST','/api/cameras/pn/channel-check',{})
                self.assertEqual(code,409);self.assertEqual(payload['code'],expected)
                self.assertNotIn('synthetic-private-token',json.dumps(payload))
                self.assertNotIn('/internal/secret',json.dumps(payload))
                self.assertEqual(self.camera()['channel_error'],expected)
                self.assertTrue(self.camera()['enabled']);self.assertTrue(self.camera()['upload_enabled'])

    def test_channel_check_failure_does_not_overwrite_concurrently_changed_binding(self):
        from archive_app.telegram import ChannelCheckError
        self.login()
        def changed(archive,camera,**kwargs):
            archive.update_camera(camera,{'channel_chat_id':-1002234567890})
            raise ChannelCheckError('channel_changed')
        self.telegram.verify_camera_channel.side_effect=changed
        code,payload,_=self.request('POST','/api/cameras/pn/channel-check',{})
        self.assertEqual(code,409);self.assertEqual(payload['code'],'channel_changed')
        camera=self.camera();self.assertEqual(camera['channel_chat_id'],-1002234567890)
        self.assertEqual(camera['channel_status'],'unconfigured');self.assertIsNone(camera['channel_error'])

    def test_channel_check_success_does_not_mark_concurrently_changed_binding_ready(self):
        self.login()
        def changed(archive,camera,**kwargs):
            archive.update_camera(camera,{'channel_chat_id':-1002234567890})
            return -1001234567890,991
        self.telegram.verify_camera_channel.side_effect=changed
        code,payload,_=self.request('POST','/api/cameras/pn/channel-check',{})
        self.assertEqual(code,409);self.assertEqual(payload['code'],'channel_changed')
        camera=self.camera();self.assertEqual(camera['channel_chat_id'],-1002234567890)
        self.assertEqual(camera['channel_status'],'unconfigured');self.assertIsNone(camera['channel_error'])

    def test_missing_mapping_or_disabled_channel_does_not_call_telegram(self):
        self.login()
        for change in ({'channel_enabled':False},{'channel_enabled':True,'channel_chat_id':None}):
            self.archive.update_camera('pn',change)
            code,payload,_=self.request('POST','/api/cameras/pn/channel-check',{})
            self.assertEqual(code,409);self.assertEqual(payload['code'],'camera_channel_unconfigured')
        self.telegram.verify_camera_channel.assert_not_called()

    def test_verify_rejects_unknown_camera_body_fields_and_query(self):
        self.login()
        self.assertEqual(self.request('POST','/api/cameras/missing/channel-check',{})[0],404)
        self.assertEqual(self.request('POST','/api/cameras/pn/channel-check',{'chat_id':-1002234567890})[0],400)
        self.assertEqual(self.request('POST','/api/cameras/pn/channel-check?force=1',{})[0],400)
        self.assertEqual(self.request('POST','/api/cameras/pn/channel-check')[0],400)
        self.telegram.verify_camera_channel.assert_not_called()

    def test_add_camera_accepts_channel_id_and_preserves_manual_flow(self):
        self.login();code,payload,_=self.request('POST','/api/cameras',{
            'id':'door','name':'Cửa','host':'192.168.55.3','enabled':False,
            'channel_chat_id':'-1002234567890','channel_enabled':True})
        self.assertEqual(code,201);self.assertEqual(payload['channel_chat_id'],-1002234567890)
        self.assertEqual(payload['channel_status'],'unconfigured');self.assertFalse(payload['enabled'])
        self.assertTrue(payload['upload_enabled']);self.telegram.verify_camera_channel.assert_not_called()

    def test_edit_mapping_does_not_change_upload_or_sd_fields(self):
        self.login();before=self.camera()
        code,payload,_=self.request('PATCH','/api/cameras/pn',{'channel_chat_id':-1002234567890,'channel_enabled':False})
        self.assertEqual(code,200);self.assertEqual(payload['channel_chat_id'],-1002234567890)
        self.assertFalse(payload['channel_enabled'])
        for field in ('id','name','host','upload_enabled','enabled','sd_password_configured','sd_backend'):
            self.assertEqual(payload[field],before[field])

    def test_add_camera_accepts_trimmed_unicode_channel_display_name(self):
        self.login();code,payload,_=self.request('POST','/api/cameras',{
            'id':'door','name':'Cửa','host':'192.168.55.3','enabled':False,
            'channel_chat_id':'-1002234567890','channel_name':'  Nhà ba má · Cửa trước 🎥  '})
        self.assertEqual(code,201);self.assertEqual(payload['channel_name'],'Nhà ba má · Cửa trước 🎥')
        self.assertEqual(payload['channel_chat_id'],-1002234567890)
        self.assertEqual(payload['channel_status'],'unconfigured');self.assertFalse(payload['enabled'])
        self.assertTrue(payload['upload_enabled']);self.telegram.verify_camera_channel.assert_not_called()
        code,payload,_=self.request('GET','/api/cameras')
        self.assertEqual(code,200)
        self.assertEqual(next(item for item in payload['cameras'] if item['id']=='door')['channel_name'],
                         'Nhà ba má · Cửa trước 🎥')

    def test_alias_only_edit_preserves_ready_mapping_upload_and_device_settings(self):
        self.login();self.archive.update_camera('pn',{'upload_enabled':False})
        self.archive.set_channel_status('pn','ready');before=self.camera()
        code,payload,_=self.request('PATCH','/api/cameras/pn',{'channel_name':'  Phòng ngủ · bama  '})
        self.assertEqual(code,200);self.assertEqual(payload['channel_name'],'Phòng ngủ · bama')
        for field in ('id','name','host','device_port','http_port','rtsp_port','enabled','upload_enabled',
                      'sd_backend','sd_username','sd_channel','sd_timezone','sd_lookback_hours',
                      'sd_password_configured','channel_chat_id','channel_enabled','channel_status','channel_error'):
            self.assertEqual(payload[field],before[field],field)
        self.assertEqual(self.camera()['channel_name'],'Phòng ngủ · bama')
        self.assertEqual(self.telegram.method_calls,[])
        self.directory.refresh.assert_not_called()

    def test_channel_display_name_empty_or_unicode_spaces_clear_alias(self):
        self.login();self.archive.set_channel_status('pn','ready')
        for empty in ('','  ','\u2003\u00a0'):
            with self.subTest(empty=repr(empty)):
                self.archive.update_camera('pn',{'channel_name':'Tên hiển thị'})
                code,payload,_=self.request('PATCH','/api/cameras/pn',{'channel_name':empty})
                self.assertEqual(code,200);self.assertEqual(payload['channel_name'],'')
                self.assertEqual(payload['channel_status'],'ready')
                self.assertEqual(payload['channel_chat_id'],-1001234567890)
        self.assertEqual(self.telegram.method_calls,[])

    def test_channel_display_name_accepts_128_unicode_characters_after_trimming(self):
        self.login();alias='🎥'*128
        code,payload,_=self.request('PATCH','/api/cameras/pn',{'channel_name':'  '+alias+'  '})
        self.assertEqual(code,200);self.assertEqual(payload['channel_name'],alias)
        self.assertEqual(self.camera()['channel_name'],alias)

    def test_invalid_channel_display_names_rejected_without_changing_camera(self):
        self.login();self.archive.update_camera('pn',{'channel_name':'Tên đang dùng'})
        self.archive.set_channel_status('pn','ready');before=self.camera()
        invalid=(None,123,True,[],{},'x'*129,'Nhà\nPN','Nhà\rPN','Nhà\tPN','Nhà\x00PN',
                 'Nhà\x7fPN','Nhà\x85PN')
        for alias in invalid:
            with self.subTest(alias=repr(alias)):
                self.assertEqual(self.request('PATCH','/api/cameras/pn',{'channel_name':alias})[0],400)
                self.assertEqual(self.request('POST','/api/cameras',{
                    'id':'invalid_alias','host':'192.168.55.4','channel_chat_id':-1003234567890,
                    'channel_name':alias})[0],400)
                after=self.camera()
                for field in ('channel_name','channel_chat_id','channel_status','channel_enabled','upload_enabled'):
                    self.assertEqual(after[field],before[field],field)
        self.assertFalse(any(camera['id']=='invalid_alias' for camera in self.archive.cameras()))
        self.assertEqual(self.telegram.method_calls,[])

    def test_channel_display_name_requires_mapping_on_create_and_edit(self):
        self.login()
        self.assertEqual(self.request('POST','/api/cameras',{
            'id':'door','host':'192.168.55.3','channel_name':'Cửa trước'})[0],400)
        self.assertFalse(any(camera['id']=='door' for camera in self.archive.cameras()))
        self.archive.update_camera('pn',{'channel_chat_id':None})
        self.assertEqual(self.request('PATCH','/api/cameras/pn',{'channel_name':'Phòng ngủ'})[0],400)
        self.assertEqual(self.camera()['channel_name'],'');self.assertIsNone(self.camera()['channel_chat_id'])
        self.assertEqual(self.request('PATCH','/api/cameras/pn',{'channel_name':''})[0],200)
        self.assertEqual(self.telegram.method_calls,[])

    def test_channel_display_name_edit_requires_login_and_password_change(self):
        data={'channel_name':'Phòng ngủ'}
        self.assertEqual(self.request('PATCH','/api/cameras/pn',data)[0],401)
        self.login(False)
        self.assertEqual(self.request('PATCH','/api/cameras/pn',data)[0],409)
        self.assertEqual(self.camera()['channel_name'],'');self.assertEqual(self.telegram.method_calls,[])

    def test_channel_display_name_edit_enforces_csrf_and_origin(self):
        self.login();data={'channel_name':'Phòng ngủ'}
        self.assertEqual(self.request('PATCH','/api/cameras/pn',data,{'X-CSRF-Token':'wrong'})[0],403)
        self.assertEqual(self.request('PATCH','/api/cameras/pn',data,{'Origin':'https://unrelated.invalid'})[0],403)
        self.assertEqual(self.camera()['channel_name'],'');self.assertEqual(self.telegram.method_calls,[])
        self.assertEqual(self.request('PATCH','/api/cameras/pn',data)[0],200)

    def test_real_channel_catalog_keeps_local_name_when_telegram_title_changes(self):
        from archive_app.channel_directory import ChannelDirectory
        self.patch_directory.stop();self.login()
        directory=ChannelDirectory(self.archive)
        directory.remember({'id':-1001234567890,'type':'channel','title':'Tên trên Telegram'},status='ready')
        self.assertEqual(self.request('PATCH','/api/cameras/pn',{'channel_name':'PN · Nhà ba má'})[0],200)
        code,payload,_=self.request('GET','/api/telegram/channels')
        self.assertEqual(code,200);channel=payload['channels'][0]
        self.assertEqual(channel['name'],'PN · Nhà ba má');self.assertEqual(channel['channel_name'],'PN · Nhà ba má')
        self.assertEqual(channel['title'],'Tên trên Telegram');self.assertTrue(channel['ready'])
        directory.remember({'id':-1001234567890,'type':'channel','title':'Tên Telegram cập nhật'})
        code,payload,_=self.request('GET','/api/telegram/channels')
        self.assertEqual(code,200);channel=payload['channels'][0]
        self.assertEqual(channel['name'],'PN · Nhà ba má');self.assertEqual(channel['title'],'Tên Telegram cập nhật')
        self.assertEqual(channel['bound_camera_id'],'pn');self.assertTrue(channel['ready'])
        self.assertEqual(self.request('PATCH','/api/cameras/pn',{'channel_name':''})[0],200)
        code,payload,_=self.request('GET','/api/telegram/channels')
        self.assertEqual(code,200);channel=payload['channels'][0]
        self.assertEqual(channel['name'],'Tên Telegram cập nhật');self.assertEqual(channel['channel_name'],'')
        self.assertEqual(self.telegram.method_calls,[])

    def test_manual_channel_alias_visible_before_telegram_discovery(self):
        self.patch_directory.stop();self.login()
        self.assertEqual(self.request('PATCH','/api/cameras/pn',{'channel_name':'Channel gán thủ công'})[0],200)
        code,payload,_=self.request('GET','/api/telegram/channels')
        self.assertEqual(code,200);channel=payload['channels'][0]
        self.assertEqual(channel['name'],'Channel gán thủ công')
        self.assertEqual(channel['title'],'-1001234567890')
        self.assertEqual(channel['chat_id'],-1001234567890);self.assertEqual(channel['bound_camera_id'],'pn')
        self.assertEqual(self.telegram.method_calls,[])

    def test_unique_channel_mapping_rejected_on_add_and_edit(self):
        self.login();self.archive.add_camera({'id':'door','host':'192.168.55.3'})
        for method,path,data in [('POST','/api/cameras',{'id':'other','host':'192.168.55.4','channel_chat_id':-1001234567890}),
                                 ('PATCH','/api/cameras/door',{'channel_chat_id':-1001234567890})]:
            self.assertEqual(self.request(method,path,data)[0],400)
        self.assertIsNone(next(item for item in self.archive.cameras() if item['id']=='door')['channel_chat_id'])

    def test_channel_directory_is_named_and_read_only_on_get(self):
        self.login();code,payload,_=self.request('GET','/api/telegram/channels')
        self.assertEqual(code,200);self.assertEqual(payload['channels'][0]['name'],'Camera PN')
        self.assertEqual(payload['channels'][0]['bound_camera_id'],'pn')
        self.directory.list.assert_called_once_with();self.directory.refresh.assert_not_called()

    def test_directory_refresh_uses_existing_bot_without_creating_channels(self):
        self.login();code,payload,_=self.request('POST','/api/telegram/channels/refresh',{})
        self.assertEqual(code,200);self.assertEqual(payload['channels'],self.directory.list.return_value)
        self.directory.refresh.assert_called_once_with(self.telegram)
        self.telegram.verify_camera_channel.assert_not_called()

    def test_directory_refresh_errors_are_sanitized(self):
        self.login();self.directory.refresh.side_effect=RuntimeError('synthetic-private-api-token')
        code,payload,_=self.request('POST','/api/telegram/channels/refresh',{})
        self.assertEqual(code,409);self.assertEqual(payload['code'],'channel_directory_refresh_failed')
        self.assertNotIn('synthetic-private-api-token',json.dumps(payload))

    def test_directory_rejects_unknown_filters_and_body(self):
        self.login()
        self.assertEqual(self.request('GET','/api/telegram/channels?all=1')[0],400)
        self.assertEqual(self.request('POST','/api/telegram/channels/refresh',{'token':'synthetic'})[0],400)
        self.assertEqual(self.request('POST','/api/telegram/channels/refresh?all=1',{})[0],400)
        self.directory.list.assert_not_called();self.directory.refresh.assert_not_called()


if __name__=='__main__':unittest.main()
