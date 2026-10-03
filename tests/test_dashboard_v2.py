"""Real loopback HTTP tests; synthetic media/Telegram IDs only."""
from pathlib import Path
import http.client,json,os,shutil,tempfile,threading,unittest,uuid
from unittest.mock import patch
from archive_app.core import Archive,Settings,record_key
from archive_app.dashboard import DashboardServer


class DashboardTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).parent if os.name=='nt' else Path(tempfile.gettempdir())
        self.root=parent/('.tmp-dashboard-'+uuid.uuid4().hex);self.parent=parent.resolve()
        self.root.mkdir();(self.root/'input').mkdir()
        self.settings=Settings(self.root/'state',self.root/'cache',self.root/'input','UTC+07:00',min_free_bytes=0)
        self.archive=Archive(self.settings)
        self.server=DashboardServer(('127.0.0.1',0),self.settings,token='synthetic-dashboard-token-123456789')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.cookie='';self.csrf=''

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2);self.archive.close()
        self.assertEqual(self.root.resolve().parent,self.parent);shutil.rmtree(self.root)

    def request(self,method,path,body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=15)
        h={'Cookie':self.cookie,'X-CSRF-Token':self.csrf,**(headers or {})}
        if body is not None:h['Content-Type']='application/json';body=json.dumps(body,ensure_ascii=False).encode()
        conn.request(method,path,body=body,headers=h);reply=conn.getresponse();raw=reply.read()
        result=(reply.status,json.loads(raw) if reply.getheader('Content-Type','').startswith('application/json') else raw,dict(reply.getheaders()))
        conn.close();return result

    def login(self):
        status,body,headers=self.request('POST','/api/login',{'token':'synthetic-dashboard-token-123456789'})
        self.assertEqual(status,200);self.cookie=headers['Set-Cookie'].split(';')[0];self.csrf=body['csrf_token']

    def add(self,id='h6c',name='Phòng khách'):
        return self.archive.add_camera({'id':id,'name':name,'model':'CS-H6c','host':'192.168.1.11'})

    def recording(self,camera='h6c',record_id='one',start='2026-10-03T10:00:00+07:00',end='2026-10-03T10:01:00+07:00'):
        source=self.settings.input_dir/'source.mp4';source.write_bytes(b'synthetic input')
        def fake(source,dest,settings):
            dest.write_bytes(b'synthetic MP4');return {'duration':60,'codec_video':'h264','codec_audio':'aac','bytes':13}
        entry={'camera':camera,'record_id':record_id,'path':str(source),'start_time':start,'end_time':end}
        with patch('archive_app.core.normalize',side_effect=fake):row=self.archive.ingest_entry(entry)
        self.archive.mark_uploaded(row['key'],'-1001234567890',101 if record_id=='one' else 102,'fake-id')
        return row['key']

    def test_health_and_private_api(self):
        self.assertEqual(self.request('GET','/healthz')[0],200)
        self.assertEqual(self.request('GET','/api/cameras')[0],401)
        self.assertEqual(self.request('GET','/../core.py')[0],404)

    def test_login_wrong_token_and_cookie_flags(self):
        self.assertEqual(self.request('POST','/api/login',{'token':'wrong'})[0],401)
        status,body,headers=self.request('POST','/api/login',{'token':'synthetic-dashboard-token-123456789'})
        self.assertEqual(status,200);self.assertIn('HttpOnly',headers['Set-Cookie']);self.assertIn('SameSite=Strict',headers['Set-Cookie'])

    def test_create_rename_persists_after_connection_reopens(self):
        self.login()
        code,camera,_=self.request('POST','/api/cameras',{'id':'h6c','name':'Phòng khách','host':'192.168.1.11','model':'CS-H6c'})
        self.assertEqual(code,201);self.assertEqual(camera['device_port'],8000)
        code,renamed,_=self.request('PATCH','/api/cameras/h6c',{'name':'Cửa trước'})
        self.assertEqual(code,200);self.assertEqual(renamed['id'],'h6c')
        self.assertEqual(self.request('GET','/api/cameras')[1]['cameras'][0]['name'],'Cửa trước')

    def test_csrf_and_cross_origin_rejected_without_change(self):
        self.login();self.add()
        self.csrf='bad'
        self.assertEqual(self.request('PATCH','/api/cameras/h6c',{'name':'bad'})[0],403)
        self.assertEqual(self.request('GET','/api/cameras',headers={'Origin':'http://malicious.example'})[0],403)
        self.assertEqual(self.archive.camera_name('h6c'),'Phòng khách')

    def test_bad_camera_and_immutable_id(self):
        self.login();self.add()
        self.assertEqual(self.request('POST','/api/cameras',{'id':123,'name':'Numeric ID'})[0],400)
        self.assertIsNone(self.archive.conn.execute('SELECT id FROM cameras WHERE id=?',('123',)).fetchone())
        for body in ({'id':'new-id'},{'device_port':True},{'enabled':'false'},{'name':''},{'host':'http://example.com/path'}):
            with self.subTest(body=body):self.assertEqual(self.request('PATCH','/api/cameras/h6c',body)[0],400)
        self.assertEqual(self.request('POST','/api/cameras',{'id':'h6c','name':'Duplicate'})[0],400)

    def test_rename_preserves_key_files_telegram_identity(self):
        self.add();key=self.recording()
        before=dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?',(key,)).fetchone())
        self.archive.update_camera('h6c',{'name':'Tên mới'})
        after=dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?',(key,)).fetchone())
        self.assertEqual(before,after);self.assertTrue(Path(after['local_path']).exists())
        self.assertEqual(self.archive.browse(camera='h6c')['recordings'][0]['camera_name'],'Tên mới')

    def test_camera_first_archive_sort_and_isolation(self):
        self.add();self.add('c6n','Sân nhà')
        first=self.recording();second=self.recording(record_id='two',start='2026-10-03T11:00:00+07:00',end='2026-10-03T11:01:00+07:00')
        self.recording(camera='c6n',record_id='other')
        self.login()
        asc=self.request('GET','/api/archive?camera=h6c&year=2026&month=10&day=3&order=asc')[1]
        desc=self.request('GET','/api/archive?camera=h6c&year=2026&month=10&day=3&order=desc')[1]
        self.assertEqual([r['key'] for r in asc['recordings']],[first,second])
        self.assertEqual([r['key'] for r in desc['recordings']],[second,first])
        self.assertEqual(asc['total'],2);self.assertEqual(asc['recordings'][0]['telegram_url'],'https://t.me/c/1234567890/101')
        self.assertNotIn('local_path',asc['recordings'][0]);self.assertNotIn('file_id',asc['recordings'][0])

    def test_pagination_and_bad_filters(self):
        self.add();self.recording();self.recording(record_id='two');self.login()
        page=self.request('GET','/api/archive?camera=h6c&limit=1&offset=1')[1]
        self.assertEqual(page['total'],2);self.assertEqual(len(page['recordings']),1)
        for query in ('order=hack','year=2026&month=0','year=2026&month=2&day=30','limit=0','offset=-1','day=3','unknown=1'):
            with self.subTest(query=query):self.assertEqual(self.request('GET','/api/archive?'+query)[0],400)

    def test_calendar_cross_midnight_and_exclusive_end(self):
        self.add();self.recording(start='2026-12-31T23:59:00+07:00',end='2027-01-01T00:01:00+07:00')
        tree=self.archive.calendar('h6c')
        self.assertEqual(tree,{'years':[{'year':2026,'months':[{'month':12,'days':[31]}]},{'year':2027,'months':[{'month':1,'days':[1]}]}]})
        self.add('end','Exactly midnight');self.recording(camera='end',start='2026-12-31T23:59:00+07:00',end='2027-01-01T00:00:00+07:00')
        self.assertEqual(len(self.archive.calendar('end')['years']),1)

    def test_disabled_camera_pauses_ingest_and_upload(self):
        self.add();key=self.recording();self.archive.conn.execute("UPDATE recordings SET status='downloaded' WHERE key=?",(key,));self.archive.conn.commit()
        self.archive.update_camera('h6c',{'enabled':False});self.assertIsNone(self.archive.claim_upload())
        self.archive.update_camera('h6c',{'enabled':True});self.assertEqual(self.archive.claim_upload()['key'],key)

    def test_migration_registers_legacy_slugs_and_preserves_identity(self):
        key=self.recording(camera='legacy_camera');self.archive.conn.execute('DELETE FROM cameras');self.archive.conn.commit()
        reopened=Archive(self.settings)
        try:
            self.assertEqual(reopened.cameras()[0]['id'],'legacy_camera');self.assertEqual(reopened.cameras()[0]['name'],'legacy_camera')
            self.assertEqual(reopened.browse()['recordings'][0]['key'],key)
        finally:reopened.close()

    def test_probe_bounded_and_reports_sd_unverified(self):
        self.add()
        address=[(2,1,6,'',('192.168.1.11',0))]
        with patch('archive_app.core.socket.getaddrinfo',return_value=address),patch('archive_app.core.socket.create_connection') as connect:
            result=self.archive.probe_camera('h6c')
            self.assertEqual(connect.call_count,3);self.assertTrue(all(call.kwargs['timeout']==3 for call in connect.call_args_list))
            self.assertEqual(result['sd_download'],'unverified')
        self.archive.update_camera('h6c',{'host':'192.168.1.10'});self.assertIsNone(self.archive.cameras()[0]['probe'])

    def test_logout_invalidates_session(self):
        self.login();self.assertEqual(self.request('POST','/api/logout',{})[0],200)
        self.assertEqual(self.request('GET','/api/status')[0],401)


if __name__=='__main__':unittest.main()
