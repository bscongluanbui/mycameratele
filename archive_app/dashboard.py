"""Authenticated, no-dependency LAN dashboard; one SQLite connection per request."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit,parse_qs,unquote
import hmac
import json
import os
import secrets
import ssl
import threading
import time

from .core import Archive,Settings
from .dashboard_auth import DashboardAuth


class DashboardServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,settings,token=None):
        self.settings=settings
        settings.state_dir.mkdir(parents=True,exist_ok=True)
        # The obsolete constructor argument remains accepted for callers only;
        # neither it, DASHBOARD_TOKEN, nor dashboard_token grants access.
        self.auth=DashboardAuth(settings.state_dir)
        self.cookie_secure=os.environ.get('DASHBOARD_COOKIE_SECURE','false').strip().lower()=='true'
        self.sessions={};self.login_attempts={};self.account_attempts={};self.session_lock=threading.Lock()
        self.auth_lock=threading.Lock()
        self.web=Path(__file__).parent/'web'
        with_archive=Archive(settings);with_archive.close()
        super().__init__(address,DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version='EZVIZDashboard/2.3'
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
            session=self.server.sessions.get(sid)
        if session and session['version']!=self.server.auth.account()['version']:
            with self.server.session_lock:self.server.sessions.pop(sid,None)
            return None
        return session

    def session_cookie(self,sid='',max_age=0):
        cookie='ezviz_session='+sid+'; HttpOnly; SameSite=Strict; Path=/; Max-Age='+str(max_age)
        # Forwarded headers are client-controlled unless a trusted proxy strips
        # them. Only actual TLS or an explicit deployment setting enables this.
        if self.server.cookie_secure or isinstance(self.connection,ssl.SSLSocket):cookie+='; Secure'
        return cookie

    def rate_limited(self,store):
        peer=self.client_address[0];now=time.time()
        with self.server.session_lock:
            for address,timestamps in list(store.items()):
                filtered=[stamp for stamp in timestamps if now-stamp<60]
                if filtered:store[address]=filtered
                else:store.pop(address,None)
            attempts=store.setdefault(peer,[])
            if len(attempts)>=10:return True
            # Bound the amount of remote address state retained by the service.
            if len(store)>10000:store.clear();attempts=store.setdefault(peer,[])
            attempts.append(now)
        return False

    @staticmethod
    def public_account(account):
        return {'username':account['username'],'password_change_required':account['password_change_required']}

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
                self.send(200,{'healthy':True,'service':'dashboard','version':'2.3'});return
            if not path.startswith('/api/'):
                assets={'/':('index.html','text/html'),'/app.js':('app.js','text/javascript'),'/style.css':('style.css','text/css')}
                if method!='GET' or path not in assets:self.send(404,{'error':'Not found'});return
                filename,mime=assets[path]
                self.send(200,body=(self.server.web/filename).read_bytes(),mime=mime);return
            if not self.same_origin():self.send(403,{'error':'Origin rejected'});return
            if path=='/api/login' and method=='POST':
                data=self.read_json()
                peer=self.client_address[0];now=time.time()
                if self.rate_limited(self.server.login_attempts):
                    self.send(429,{'error':'Thử lại sau một phút','code':'rate_limited'});return
                with self.server.auth_lock:
                    account=self.server.auth.authenticate(data.get('username'),data.get('password'))
                    if not account or account['version']!=self.server.auth.account()['version']:
                        self.send(401,{'error':'Tên đăng nhập hoặc mật khẩu không đúng','code':'invalid_credentials'});return
                    sid=secrets.token_urlsafe(32)
                    session={'csrf_token':secrets.token_urlsafe(24),'expires':now+43200,'version':account['version']}
                    with self.server.session_lock:
                        if len(self.server.sessions)>=100:self.server.sessions.clear()
                        self.server.sessions[sid]=session;self.server.login_attempts.pop(peer,None)
                self.send(200,{'authenticated':True,'csrf_token':session['csrf_token'],'account':self.public_account(account)},cookie=self.session_cookie(sid,43200));return
            session=self.session()
            if not session:self.send(401,{'error':'Đăng nhập dashboard trước','code':'authentication_required'});return
            account=self.server.auth.account()
            if account['version']!=session['version']:
                self.send(401,{'error':'Đăng nhập dashboard trước','code':'authentication_required'});return
            allowed_setup=(path=='/api/account' and method in ('GET','POST')) or (path=='/api/logout' and method=='POST')
            if account['password_change_required'] and not allowed_setup:
                self.send(409,{'error':'Đổi mật khẩu ban đầu trước khi sử dụng dashboard','code':'password_change_required'});return
            if method!='GET' and not hmac.compare_digest(self.headers.get('X-CSRF-Token','').encode('utf-8'),session['csrf_token'].encode('utf-8')):
                self.send(403,{'error':'CSRF token required'});return
            if path=='/api/logout' and method=='POST':
                with self.server.session_lock:
                    for sid,item in list(self.server.sessions.items()):
                        if item is session:self.server.sessions.pop(sid,None)
                self.send(200,{'authenticated':False},cookie=self.session_cookie());return
            if path=='/api/account' and method=='GET':
                self.send(200,{**self.public_account(account),'csrf_token':session['csrf_token']});return
            if path=='/api/account' and method=='POST':
                data=self.read_json()
                if self.rate_limited(self.server.account_attempts):
                    self.send(429,{'error':'Thử lại sau một phút','code':'rate_limited'});return
                with self.server.auth_lock:
                    try:
                        self.server.auth.change(data.get('current_password'),data.get('username'),data.get('new_password'),session['version'])
                    except PermissionError:
                        self.send(401,{'error':'Mật khẩu hiện tại không đúng','code':'current_password_invalid'});return
                    with self.server.session_lock:
                        self.server.sessions.clear();self.server.account_attempts.pop(self.client_address[0],None)
                self.send(200,{'authenticated':False,'credentials_updated':True},cookie=self.session_cookie());return
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
