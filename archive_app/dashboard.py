"""Authenticated, no-dependency LAN dashboard; one SQLite connection per request."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit,parse_qs,unquote
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from .core import Archive,Settings,secret


class DashboardServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,settings,token=None):
        self.settings=settings
        settings.state_dir.mkdir(parents=True,exist_ok=True)
        if token is None:
            token=secret('DASHBOARD_TOKEN')
            if not token:
                path=settings.state_dir/'dashboard_token'
                try:
                    with path.open('x',encoding='utf-8') as handle:
                        os.chmod(path,0o600);handle.write(secrets.token_urlsafe(32)+'\n')
                except FileExistsError:pass
                token=path.read_text(encoding='utf-8').strip()
        if not isinstance(token,str) or len(token)<24:raise ValueError('Dashboard token must contain at least 24 characters')
        self.token_hash=hashlib.sha256(token.encode()).digest()
        self.sessions={};self.login_attempts={};self.session_lock=threading.Lock()
        self.web=Path(__file__).parent/'web'
        with_archive=Archive(settings);with_archive.close()
        super().__init__(address,DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version='EZVIZDashboard/2.2'
    def setup(self):
        super().setup();self.connection.settimeout(15)
    def log_message(self,*args):pass  # Requests can contain cookies; do not log headers/tokens.

    def send(self,status,payload=None,*,body=None,mime='application/json',cookie=None):
        if body is None:body=json.dumps(payload,ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type',mime+'; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if cookie:self.send_header('Set-Cookie',cookie)
        self.end_headers()
        self.wfile.write(body)

    def session(self):
        try:
            cookies=SimpleCookie(self.headers.get('Cookie',''))
            sid=cookies['ezviz_session'].value if 'ezviz_session' in cookies else ''
        except Exception:return None
        now=time.time()
        with self.server.session_lock:
            expired=[key for key,value in self.server.sessions.items() if value['expires']<=now]
            for key in expired:self.server.sessions.pop(key,None)
            return self.server.sessions.get(sid)

    def read_json(self):
        if self.headers.get('Transfer-Encoding'):raise ValueError('Chunked JSON is not accepted')
        length=int(self.headers.get('Content-Length','0'))
        if length<=0 or length>16384:raise ValueError('JSON body must be between 1 and 16384 bytes')
        if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':raise ValueError('JSON Content-Type required')
        result=json.loads(self.rfile.read(length))
        if not isinstance(result,dict):raise ValueError('JSON object required')
        return result

    def same_origin(self):
        origin=self.headers.get('Origin')
        if origin:
            parsed=urlsplit(origin)
            if parsed.scheme not in ('http','https') or parsed.netloc!=self.headers.get('Host'):return False
        return True

    def do_GET(self):self.handle_request('GET')
    def do_POST(self):self.handle_request('POST')
    def do_PATCH(self):self.handle_request('PATCH')

    def handle_request(self,method):
        parsed=urlsplit(self.path);path=parsed.path
        try:
            if path=='/healthz' and method=='GET':
                self.send(200,{'healthy':True,'service':'dashboard','version':'2.2'});return
            if not path.startswith('/api/'):
                assets={'/':('index.html','text/html'),'/app.js':('app.js','text/javascript'),'/style.css':('style.css','text/css')}
                if method!='GET' or path not in assets:self.send(404,{'error':'Not found'});return
                filename,mime=assets[path]
                self.send(200,body=(self.server.web/filename).read_bytes(),mime=mime);return
            if not self.same_origin():self.send(403,{'error':'Origin rejected'});return
            if path=='/api/login' and method=='POST':
                data=self.read_json();token=data.get('token','')
                if not isinstance(token,str) or len(token)>512:raise ValueError('Invalid login token')
                peer=self.client_address[0];now=time.time()
                with self.server.session_lock:
                    attempts=[t for t in self.server.login_attempts.get(peer,[]) if now-t<60]
                    self.server.login_attempts[peer]=attempts
                    if len(attempts)>=10:self.send(429,{'error':'Thử lại sau một phút'});return
                    attempts.append(now)
                if not hmac.compare_digest(hashlib.sha256(token.encode()).digest(),self.server.token_hash):
                    self.send(401,{'error':'Token không đúng'});return
                sid=secrets.token_urlsafe(32);session={'csrf_token':secrets.token_urlsafe(24),'expires':now+43200}
                with self.server.session_lock:
                    if len(self.server.sessions)>=100:self.server.sessions.clear()
                    self.server.sessions[sid]=session;self.server.login_attempts.pop(peer,None)
                self.send(200,{'authenticated':True,'csrf_token':session['csrf_token']},cookie='ezviz_session='+sid+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200');return
            session=self.session()
            if not session:self.send(401,{'error':'Đăng nhập dashboard trước'});return
            if method!='GET' and not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),session['csrf_token']):
                self.send(403,{'error':'CSRF token required'});return
            if path=='/api/logout' and method=='POST':
                with self.server.session_lock:
                    for sid,item in list(self.server.sessions.items()):
                        if item is session:self.server.sessions.pop(sid,None)
                self.send(200,{'authenticated':False},cookie='ezviz_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0');return
            archive=Archive(self.server.settings)
            try:
                if path=='/api/status' and method=='GET':
                    heartbeat=self.server.settings.state_dir/'heartbeat'
                    status=archive.status()
                    status.update(csrf_token=session['csrf_token'],heartbeat={'worker_alive':heartbeat.exists() and time.time()-heartbeat.stat().st_mtime<max(120,self.server.settings.interval*4)})
                    self.send(200,status)
                elif path=='/api/cameras' and method=='GET':self.send(200,{'cameras':archive.cameras()})
                elif path=='/api/cameras' and method=='POST':self.send(201,archive.add_camera(self.read_json()))
                elif path.startswith('/api/cameras/'):
                    pieces=path[len('/api/cameras/'):].split('/');slug=unquote(pieces[0])
                    if len(pieces)==1 and method=='PATCH':self.send(200,archive.update_camera(slug,self.read_json()))
                    elif len(pieces)==2 and pieces[1]=='probe' and method=='POST':self.send(200,archive.probe_camera(slug))
                    else:self.send(404,{'error':'Not found'})
                elif path=='/api/calendar' and method=='GET':
                    query=parse_qs(parsed.query);camera=query.get('camera',[''])[0]
                    if not camera:raise ValueError('Select a camera')
                    self.send(200,archive.calendar(camera))
                elif path=='/api/archive' and method=='GET':
                    query=parse_qs(parsed.query)
                    allowed={'camera','year','month','day','order','status','offset','limit'}
                    if set(query)-allowed:raise ValueError('Unknown filter')
                    args={k:v[0] for k,v in query.items()}
                    for key in ('year','month','day','offset','limit'):
                        if key in args:args[key]=int(args[key])
                    self.send(200,archive.browse(**args))
                else:self.send(404,{'error':'Not found'})
            finally:archive.close()
        except (ValueError,TypeError,UnicodeError,json.JSONDecodeError):self.send(400,{'error':'Dữ liệu đầu vào không hợp lệ'})
        except KeyError:self.send(404,{'error':'Không tìm thấy camera'})
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception:self.send(500,{'error':'Lỗi xử lý; kiểm tra cấu hình/dịch vụ'})


def serve(settings,host='0.0.0.0',port=8080):
    server=DashboardServer((host,port),settings)
    server.serve_forever(poll_interval=0.5)
