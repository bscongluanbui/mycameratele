"""Authenticated sync controls use real loopback HTTP and synthetic jobs."""
import http.client,json,os,shutil,tempfile,threading,unittest,uuid
from pathlib import Path
from archive_app.core import Archive,Settings
from archive_app.dashboard import DashboardServer
from archive_app.sync import SyncQueue

class SyncHTTPTests(unittest.TestCase):
    def setUp(self):
        self.parent=(Path(__file__).parent if os.name=='nt' else Path(tempfile.gettempdir())).resolve()
        self.root=self.parent/('.tmp-sync-http-'+uuid.uuid4().hex);self.root.mkdir()
        self.settings=Settings(self.root/'state',self.root/'cache',self.root/'input','UTC+07:00',min_free_bytes=0)
        self.archive=Archive(self.settings);self.server=DashboardServer(('127.0.0.1',0),self.settings)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.02},daemon=True);self.thread.start()
        self.cookie='';self.csrf=''
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2);self.archive.close()
        self.assertEqual(self.root.resolve().parent,self.parent);shutil.rmtree(self.root)
    def request(self,method,path,data=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=30)
        h={'Cookie':self.cookie,'X-CSRF-Token':self.csrf,**(headers or {})}
        if data is not None:h['Content-Type']='application/json';data=json.dumps(data).encode()
        conn.request(method,path,data,h);res=conn.getresponse();body=json.loads(res.read());cookie=res.getheader('Set-Cookie','');conn.close()
        return res.status,body,cookie
    def login(self,setup=True):
        user='fixture_admin' if setup else 'admin';password='synthetic-password' if setup else 'admin'
        if setup:
            self.login(False)
            self.assertEqual(self.request('POST','/api/account',{'current_password':'admin','username':user,'new_password':password})[0],200)
        code,body,cookie=self.request('POST','/api/login',{'username':user,'password':password})
        self.assertEqual(code,200);self.cookie=cookie.split(';')[0];self.csrf=body['csrf_token']
    def test_new_routes_require_auth_setup_csrf_origin(self):
        self.assertEqual(self.request('POST','/api/sync',{})[0],401)
        self.assertEqual(self.request('GET','/api/sync')[0],401)
        self.login(False)
        self.assertEqual(self.request('POST','/api/sync',{})[0],409)
        self.request('POST','/api/account',{'current_password':'admin','username':'fixture_admin','new_password':'synthetic-password'})
        code,body,cookie=self.request('POST','/api/login',{'username':'fixture_admin','password':'synthetic-password'})
        self.cookie=cookie.split(';')[0];self.csrf=body['csrf_token']
        self.archive.add_camera({'id':'c6n'})
        self.assertEqual(self.request('POST','/api/sync',{}, {'X-CSRF-Token':'wrong'})[0],403)
        self.assertEqual(self.request('POST','/api/sync',{}, {'Origin':'http://elsewhere.example'})[0],403)
        self.assertEqual(SyncQueue(self.archive).status()['jobs'],[])
    def test_add_autoqueues_enabled_but_not_disabled_and_defaults_upload_true(self):
        self.login()
        code,cam,_=self.request('POST','/api/cameras',{'id':'c6n','name':'PN'})
        self.assertEqual(code,201);self.assertTrue(cam['upload_enabled']);self.assertEqual(cam['sync']['state'],'queued')
        self.assertEqual(cam['sync']['source'],'camera-added');self.assertFalse(cam['worker_alive'])
        code,cam,_=self.request('POST','/api/cameras',{'id':'paused','enabled':False})
        self.assertEqual(code,201);self.assertNotIn('sync',cam)
        code,allcams,_=self.request('GET','/api/cameras')
        self.assertEqual(code,200);self.assertEqual(next(c for c in allcams['cameras'] if c['id']=='c6n')['sync']['state'],'queued')
        self.assertEqual(len(self.request('POST','/api/sync',{})[1]['jobs']),1)
    def test_camera_scoped_start_dedupes_and_toggle_is_persistent(self):
        self.login();self.archive.add_camera({'id':'c6n'});self.archive.add_camera({'id':'h6c'})
        first=self.request('POST','/api/sync',{'camera_id':'h6c'})
        self.assertEqual(first[0],202);job=first[1]['jobs'][0]
        again=self.request('POST','/api/sync',{'camera_id':'h6c'})[1]['jobs'][0]
        self.assertEqual(job['id'],again['id'])
        self.assertEqual(list(self.request('GET','/api/sync?camera=h6c')[1]['latest']),['h6c'])
        self.assertEqual(self.request('PATCH','/api/cameras/h6c',{'upload_enabled':False})[0],200)
        self.assertFalse(self.request('GET','/api/cameras')[1]['cameras'][1]['upload_enabled'])
        self.assertEqual(self.request('PATCH','/api/cameras/h6c',{'upload_enabled':'false'})[0],400)
    def test_bad_filters_unknown_and_disabled_camera(self):
        self.login();self.archive.add_camera({'id':'paused','enabled':False})
        for body in ({'camera_id':''},{'camera_id':True},{'camera_id':'../x'},{'force':True}):
            self.assertEqual(self.request('POST','/api/sync',body)[0],400)
        self.assertEqual(self.request('POST','/api/sync',{'camera_id':'missing'})[0],404)
        self.assertEqual(self.request('POST','/api/sync',{'camera_id':'paused'})[0],400)
        for query in ('?camera=','?limit=0','?limit=101','?camera=a&camera=b','?unknown=1'):
            self.assertEqual(self.request('GET','/api/sync'+query)[0],400)
    def test_device_secret_never_echoed_by_camera_or_job(self):
        self.login();secret='synthetic-device-password'
        code,cam,_=self.request('POST','/api/cameras',{'id':'c6n','sd_password':secret})
        self.assertEqual(code,201);self.assertTrue(cam['sd_password_configured'])
        for payload in (cam,self.request('GET','/api/cameras')[1],self.request('GET','/api/sync')[1]):
            self.assertNotIn(secret,json.dumps(payload));self.assertNotIn('"sd_password":',json.dumps(payload))
        self.assertEqual(self.archive.camera_sd_config('c6n')['sd_password'],secret)
