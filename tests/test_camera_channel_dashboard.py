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

    def test_failed_verify_is_sanitized_and_persists_error_without_global_fallback(self):
        self.login();self.telegram.verify_camera_channel.side_effect=RuntimeError('synthetic-token /internal/secret')
        code,payload,_=self.request('POST','/api/cameras/pn/channel-check',{})
        self.assertEqual(code,409);self.assertEqual(payload['code'],'camera_channel_check_failed')
        self.assertNotIn('synthetic-token',json.dumps(payload));self.assertNotIn('/internal/secret',json.dumps(payload))
        camera=self.camera();self.assertEqual(camera['channel_status'],'error')
        self.assertEqual(camera['channel_error'],'camera_channel_check_failed')
        self.assertEqual(camera['channel_chat_id'],-1001234567890);self.assertTrue(camera['upload_enabled'])

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
