"""Authenticated discovery HTTP contract; no camera/network probing in tests."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import unittest
import uuid
from unittest.mock import Mock, patch

from archive_app.core import Archive, Settings
from archive_app.dashboard import DashboardServer


def scan_fixture():
    return {'id':'fixture-scan', 'target':'192.168.55.0/24', 'state':'running',
            'total':254, 'scanned':2, 'started_at':1728000000.0,
            'finished_at':None, 'error':'', 'results':[
                {'host':'192.168.55.2', 'device_port':8000, 'rtsp_port':554,
                 'http_port':80, 'ports':[554,8000], 'vendor':'', 'model':'',
                 'confidence':'candidate', 'evidence':['SDK and RTSP ports open']}]}


class DiscoveryHTTPTests(unittest.TestCase):
    def setUp(self):
        self.parent=(Path(__file__).parent if os.name=='nt' else Path(tempfile.gettempdir())).resolve()
        self.root=self.parent/('.tmp-discovery-http-'+uuid.uuid4().hex)
        self.root.mkdir()
        self.settings=Settings(self.root/'state',self.root/'cache',self.root/'input','UTC+07:00',min_free_bytes=0)
        self.archive=Archive(self.settings)
        self.manager=Mock()
        self.manager.start.side_effect=lambda target:deepcopy(scan_fixture())
        self.manager.snapshot.side_effect=lambda ident:deepcopy(scan_fixture())
        self.manager.cancel.side_effect=lambda ident:{**deepcopy(scan_fixture()),'state':'cancelled'}
        self.routes_file=self.root/'routes.json'
        with patch('archive_app.dashboard.DiscoveryManager',return_value=self.manager),patch.dict(os.environ,{'DISCOVERY_ROUTES_FILE':str(self.routes_file)}):
            self.server=DashboardServer(('127.0.0.1',0),self.settings)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.02},daemon=True)
        self.thread.start()
        self.cookie='';self.csrf=''

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)
        self.archive.close()
        self.assertEqual(self.root.resolve().parent,self.parent)
        shutil.rmtree(self.root)

    def request(self,method,path,data=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=30)
        request_headers={'Cookie':self.cookie,'X-CSRF-Token':self.csrf,**(headers or {})}
        if data is not None:
            request_headers['Content-Type']='application/json';data=json.dumps(data).encode()
        conn.request(method,path,data,request_headers)
        response=conn.getresponse();payload=json.loads(response.read())
        cookie=response.getheader('Set-Cookie','')
        status=response.status;conn.close()
        return status,payload,cookie

    def login(self,setup=True):
        code,payload,cookie=self.request('POST','/api/login',{'username':'admin','password':'admin'})
        self.assertEqual(code,200);self.cookie=cookie.split(';')[0];self.csrf=payload['csrf_token']
        if not setup:return
        self.assertEqual(self.request('POST','/api/account',{
            'current_password':'admin','username':'fixture_admin','new_password':'synthetic-password'})[0],200)
        code,payload,cookie=self.request('POST','/api/login',{
            'username':'fixture_admin','password':'synthetic-password'})
        self.assertEqual(code,200);self.cookie=cookie.split(';')[0];self.csrf=payload['csrf_token']

    def test_all_discovery_routes_require_login_and_initial_password_change(self):
        requests=[('GET','/api/discovery/subnets',None),
                  ('POST','/api/discovery/scans',{'target':'192.168.55.0/24'}),
                  ('GET','/api/discovery/scans/fixture-scan',None),
                  ('POST','/api/discovery/scans/fixture-scan/cancel',{})]
        for method,path,data in requests:
            with self.subTest(path=path):self.assertEqual(self.request(method,path,data)[0],401)
        self.login(False)
        for method,path,data in requests:
            with self.subTest(path=path):self.assertEqual(self.request(method,path,data)[0],409)
        self.manager.start.assert_not_called();self.manager.snapshot.assert_not_called();self.manager.cancel.assert_not_called()

    def test_start_and_cancel_enforce_csrf_and_origin_without_probing(self):
        self.login()
        for path,data in [('/api/discovery/scans',{'target':'192.168.55.0/24'}),
                          ('/api/discovery/scans/fixture-scan/cancel',{})]:
            self.assertEqual(self.request('POST',path,data,{'X-CSRF-Token':'wrong'})[0],403)
            self.assertEqual(self.request('POST',path,data,{'Origin':'http://unrelated.invalid'})[0],403)
        self.assertEqual(self.request('GET','/api/discovery/subnets',headers={'Origin':'http://unrelated.invalid'})[0],403)
        self.manager.start.assert_not_called();self.manager.cancel.assert_not_called()

    def test_start_poll_cancel_contract_and_existing_camera_detection(self):
        self.login()
        self.archive.add_camera({'id':'pn','host':'192.168.55.2','sd_password':'synthetic-secret'})
        self.archive.add_camera({'id':'other-port','host':'192.168.55.2','device_port':8001})
        code,payload,_=self.request('POST','/api/discovery/scans',{'target':'192.168.55.0/24'})
        self.assertEqual(code,202);self.assertEqual(payload['scan']['state'],'running')
        self.assertEqual(payload['scan']['results'][0]['existing_camera_id'],'pn')
        self.manager.start.assert_called_once_with('192.168.55.0/24')
        code,payload,_=self.request('GET','/api/discovery/scans/fixture-scan')
        self.assertEqual(code,200);self.assertEqual(payload['scan']['id'],'fixture-scan')
        self.manager.snapshot.assert_called_once_with('fixture-scan')
        code,payload,_=self.request('POST','/api/discovery/scans/fixture-scan/cancel',{})
        self.assertEqual(code,202);self.assertEqual(payload['scan']['state'],'cancelled')
        self.manager.cancel.assert_called_once_with('fixture-scan')
        self.assertNotIn('synthetic-secret',json.dumps(payload))
        self.assertNotIn('sd_password',json.dumps(payload))

    def test_known_devices_are_refreshed_without_mutating_manager_snapshot(self):
        self.login();raw=scan_fixture()
        self.manager.snapshot.side_effect=None;self.manager.snapshot.return_value=raw
        first=self.request('GET','/api/discovery/scans/fixture-scan')[1]
        self.assertIsNone(first['scan']['results'][0]['existing_camera_id'])
        self.archive.add_camera({'id':'new-camera','host':'192.168.55.2','enabled':False})
        second=self.request('GET','/api/discovery/scans/fixture-scan')[1]
        self.assertEqual(second['scan']['results'][0]['existing_camera_id'],'new-camera')
        self.assertNotIn('existing_camera_id',raw['results'][0])

    def test_saved_hostnames_are_not_resolved_during_candidate_decoration(self):
        self.login();self.archive.add_camera({'id':'named','host':'camera.fixture'})
        original_resolver=socket.getaddrinfo
        def only_loopback(host,*args,**kwargs):
            if host!='127.0.0.1':raise AssertionError('No camera DNS during decoration')
            return original_resolver(host,*args,**kwargs)
        with patch('socket.getaddrinfo',side_effect=only_loopback):
            code,payload,_=self.request('GET','/api/discovery/scans/fixture-scan')
        self.assertEqual(code,200);self.assertIsNone(payload['scan']['results'][0]['existing_camera_id'])

    def test_invalid_payloads_and_filters_are_rejected_before_manager(self):
        self.login()
        for data in ({},{'target':'192.168.55.0/24','password':'ignored'},{'targets':['192.168.55.0/24']}):
            with self.subTest(data=data):self.assertEqual(self.request('POST','/api/discovery/scans',data)[0],400)
        for path in ('/api/discovery/subnets?secret=value','/api/discovery/scans?target=192.168.55.0/24',
                     '/api/discovery/scans/fixture-scan?unexpected=1'):
            with self.subTest(path=path):self.assertEqual(self.request('GET',path)[0],400)
        self.assertEqual(self.request('POST','/api/discovery/scans/fixture-scan/cancel',{'target':'other'})[0],400)
        self.assertEqual(self.request('POST','/api/discovery/scans/fixture-scan/cancel')[0],400)
        self.manager.start.assert_not_called();self.manager.snapshot.assert_not_called();self.manager.cancel.assert_not_called()

    def test_backend_validation_and_unknown_scan_errors_do_not_echo_input(self):
        self.login()
        self.manager.start.side_effect=ValueError('synthetic-private-error-token')
        code,payload,_=self.request('POST','/api/discovery/scans',{'target':'invalid'})
        self.assertEqual(code,400);self.assertEqual(payload['code'],'discovery_invalid_request')
        self.assertNotIn('synthetic-private-error-token',json.dumps(payload))
        self.manager.snapshot.side_effect=KeyError('synthetic-private-error-token')
        self.manager.cancel.side_effect=KeyError('synthetic-private-error-token')
        for method,path,data in [('GET','/api/discovery/scans/missing',None),('POST','/api/discovery/scans/missing/cancel',{})]:
            code,payload,_=self.request(method,path,data)
            self.assertEqual(code,404);self.assertEqual(payload['code'],'discovery_not_found')
            self.assertNotIn('synthetic-private-error-token',json.dumps(payload))

    def test_route_list_labels_and_staleness(self):
        self.login()
        routes={'subnets':[{'cidr':'192.168.55.0/24','interface':'tailscale0','source':'tailscale','table':52},
                           {'cidr':'10.10.20.0/24','interface':'eth0','source':'lan','table':254}],
                'updated_at':1728000000.0,'stale':False,'error':''}
        with patch('archive_app.dashboard.load_subnets',return_value=routes) as load:
            code,payload,_=self.request('GET','/api/discovery/subnets')
        self.assertEqual(code,200);self.assertTrue(payload['available'])
        self.assertEqual(payload['subnets'][0]['label'],'192.168.55.0/24 · Tailscale · tailscale0')
        self.assertEqual(payload['subnets'][1]['label'],'10.10.20.0/24 · LAN · eth0')
        load.assert_called_once_with(self.routes_file)
        with patch('archive_app.dashboard.load_subnets',return_value={**routes,'stale':True}):
            self.assertFalse(self.request('GET','/api/discovery/subnets')[1]['available'])

    def test_missing_routes_file_does_not_prevent_manual_target_scan(self):
        self.login()
        code,payload,_=self.request('GET','/api/discovery/subnets')
        self.assertEqual(code,200);self.assertFalse(payload['available']);self.assertEqual(payload['subnets'],[])
        self.assertNotIn(str(self.routes_file),json.dumps(payload))
        self.assertEqual(self.request('POST','/api/discovery/scans',{'target':'192.168.55.2'})[0],202)

    def test_unknown_methods_and_nested_paths_do_not_start_scan(self):
        self.login()
        for method,path,data in [('GET','/api/discovery/scans',None),
                                 ('POST','/api/discovery/subnets',{}),
                                 ('PATCH','/api/discovery/scans',{}),
                                 ('POST','/api/discovery/scans/fixture-scan/delete',{}),
                                 ('GET','/api/discovery/scans/fixture-scan/nested/path',None)]:
            with self.subTest(method=method,path=path):self.assertEqual(self.request(method,path,data)[0],404)
        self.manager.start.assert_not_called();self.manager.cancel.assert_not_called()

    def test_manual_add_and_disabled_discovered_drafts_keep_existing_behavior(self):
        self.login()
        code,draft,_=self.request('POST','/api/cameras',{
            'id':'cam-192-168-55-2','name':'Camera mới','host':'192.168.55.2','enabled':False,'upload_enabled':True})
        self.assertEqual(code,201);self.assertFalse(draft['enabled']);self.assertTrue(draft['upload_enabled'])
        self.assertNotIn('sync',draft)
        code,manual,_=self.request('POST','/api/cameras',{'id':'manual-camera','host':'192.168.55.3'})
        self.assertEqual(code,201);self.assertTrue(manual['enabled']);self.assertEqual(manual['sync']['state'],'queued')
        self.assertEqual(self.request('GET','/api/cameras')[0],200)

    def test_discovered_add_requires_valid_scan_hit_and_still_hides_credentials(self):
        self.login()
        data={'id':'discovered','name':'Camera mới','host':'192.168.55.2','enabled':False,
              'upload_enabled':True,'discovery_scan_id':'fixture-scan','sd_password':'synthetic-discovery-secret'}
        code,camera,_=self.request('POST','/api/cameras',data)
        self.assertEqual(code,201);self.assertNotIn('discovery_scan_id',camera)
        self.assertTrue(camera['sd_password_configured']);self.assertNotIn('synthetic-discovery-secret',json.dumps(camera))
        self.assertEqual(self.archive.camera_sd_config('discovered')['sd_password'],'synthetic-discovery-secret')
        for patch_data in ({'host':'192.168.55.3'},{'device_port':8001},{'device_port':True},
                           {'host':'camera.fixture'},{'discovery_scan_id':False},{'discovery_scan_id':''}):
            code,_,_=self.request('POST','/api/cameras',{**data,'id':'invalid',**patch_data})
            self.assertEqual(code,400)
        self.manager.snapshot.side_effect=KeyError('expired')
        code,payload,_=self.request('POST','/api/cameras',{**data,'id':'expired'})
        self.assertEqual(code,404);self.assertEqual(payload['code'],'discovery_not_found')

    def test_discovered_add_checks_fresh_known_host_not_only_generated_id(self):
        self.login();self.archive.add_camera({'id':'manually-named','host':'192.168.55.2'})
        code,payload,_=self.request('POST','/api/cameras',{
            'id':'generated-name','host':'192.168.55.2','enabled':False,'discovery_scan_id':'fixture-scan'})
        self.assertEqual(code,409);self.assertEqual(payload['code'],'camera_already_exists')
        self.assertEqual(payload['existing_camera_id'],'manually-named')
        self.assertEqual(len(self.archive.cameras()),1)
        # Manual host duplication is an existing explicit configuration choice.
        self.assertEqual(self.request('POST','/api/cameras',{
            'id':'manual-same-host','host':'192.168.55.2','enabled':False})[0],201)

    def test_concurrent_discovered_adds_have_one_winner_and_one_duplicate(self):
        self.login()
        def add(index):
            return self.request('POST','/api/cameras',{
                'id':'discovered-'+str(index),'host':'192.168.55.2','enabled':False,
                'discovery_scan_id':'fixture-scan'})[:2]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(add,range(2)))
        self.assertEqual(sorted(code for code,payload in results),[201,409])
        cameras=self.archive.cameras();self.assertEqual(len(cameras),1)
        duplicate=next(payload for code,payload in results if code==409)
        self.assertEqual(duplicate['existing_camera_id'],cameras[0]['id'])


class DiscoveryServerLifecycleTests(unittest.TestCase):
    def test_closing_dashboard_closes_discovery_manager(self):
        parent=(Path(__file__).parent if os.name=='nt' else Path(tempfile.gettempdir())).resolve()
        root=parent/('.tmp-discovery-lifecycle-'+uuid.uuid4().hex);root.mkdir()
        manager=Mock()
        try:
            settings=Settings(root/'state',root/'cache',root/'input','UTC+07:00',min_free_bytes=0)
            with patch('archive_app.dashboard.DiscoveryManager',return_value=manager):
                server=DashboardServer(('127.0.0.1',0),settings)
            server.server_close();manager.close.assert_called_once_with()
        finally:
            if 'server' in locals():server.socket.close()
            self.assertEqual(root.resolve().parent,parent);shutil.rmtree(root)


if __name__=='__main__':unittest.main()
