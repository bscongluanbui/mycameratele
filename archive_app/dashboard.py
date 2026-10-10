"""Authenticated, no-dependency LAN dashboard; one SQLite connection per request."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit,parse_qs,unquote
import hmac
import ipaddress
import json
import os
import secrets
import ssl
import threading
import time

from .core import Archive,Settings
from .dashboard_auth import DashboardAuth
from .discovery import DiscoveryManager
from .network_routes import load_subnets
from .sync import SyncQueue
from .telegram import Telegram
from .channel_directory import ChannelDirectory


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
        self.player_slots=threading.BoundedSemaphore(8)
        self.discovery=DiscoveryManager()
        self.camera_catalog_lock=threading.Lock()
        self.discovery_routes_file=Path(os.environ.get('DISCOVERY_ROUTES_FILE','/network/subnets.json'))
        self.web=Path(__file__).parent/'web'
        with_archive=Archive(settings);with_archive.close()
        super().__init__(address,DashboardHandler)

    def server_close(self):
        self.discovery.close()
        super().server_close()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version='EZVIZDashboard/2.4'
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

    def discovery_snapshot(self,snapshot):
        """Mark known devices using IP identity, never camera credentials."""
        archive=Archive(self.server.settings)
        try:cameras=archive.cameras()
        finally:archive.close()
        existing={}
        for camera in cameras:
            try:address=str(ipaddress.ip_address(camera['host']))
            except ValueError:continue  # Do not resolve arbitrary saved hostnames during a scan.
            existing.setdefault((address,camera['device_port']),camera['id'])
        result={**snapshot,'results':[]}
        for candidate in snapshot['results']:
            try:address=str(ipaddress.ip_address(candidate['host']))
            except ValueError:continue
            result['results'].append({**candidate,'existing_camera_id':existing.get((address,candidate['device_port']))})
        return result

    def add_camera(self,archive,data,actor):
        """Normal manual adds remain unchanged; discovered selections deduplicate."""
        discovered='discovery_scan_id' in data
        data=dict(data)
        if discovered:
            scan_id=data.pop('discovery_scan_id')
            if not isinstance(scan_id,str) or not scan_id or len(scan_id)>128:raise ValueError('Invalid discovery selection')
            try:snapshot=self.server.discovery.snapshot(scan_id)
            except KeyError:return (404,{'error':'Phiên quét đã hết hạn; quét lại','code':'discovery_not_found'})
            try:host=str(ipaddress.IPv4Address(data.get('host','')))
            except (ValueError,TypeError):raise ValueError('Invalid discovery host') from None
            port=data.get('device_port',8000)
            if type(port) is not int:raise ValueError('Invalid discovery port')
            if not any(candidate['host']==host and candidate['device_port']==port for candidate in snapshot['results']):
                raise ValueError('Camera was not found by this discovery scan')
        # Add and edit requests share one process-wide catalog lock. Re-check
        # known hosts here rather than trusting an earlier scan/UI snapshot.
        with self.server.camera_catalog_lock:
            if discovered:
                for camera in archive.cameras():
                    try:known_host=str(ipaddress.ip_address(camera['host']))
                    except ValueError:continue
                    if known_host==host and camera['device_port']==port:
                        return (409,{'error':'Camera đã có trong danh sách','code':'camera_already_exists','existing_camera_id':camera['id']})
            camera=archive.add_camera(data)
            if camera['enabled']:
                result=SyncQueue(archive).enqueue(camera['id'],source='camera-added',actor=actor)
                camera.update(sync=result['jobs'][0],worker_alive=result['worker_alive'])
        return (201,camera)

    def check_camera_channel(self,archive,slug):
        """Read-only Telegram permission check; never change upload switches."""
        if self.read_json():raise ValueError('Channel check does not accept fields')
        camera=next((item for item in archive.cameras() if item['id']==slug),None)
        if camera is None:raise KeyError(slug)
        if not camera.get('channel_chat_id') or camera.get('channel_enabled') is False:
            return (409,{'error':'Nhập Channel ID và bật channel trước khi kiểm tra',
                         'code':'camera_channel_unconfigured','camera':camera})
        try:
            chat_id,_bot_id=Telegram(self.server.settings).verify_camera_channel(archive,slug,require_index=True)
        except Exception:
            # SDK / HTTP errors may include URLs or tokens. Keep raw exceptions
            # out of both client replies and persisted camera metadata.
            archive.set_channel_status(slug,'error','camera_channel_check_failed')
            camera=next(item for item in archive.cameras() if item['id']==slug)
            return (409,{'error':'Kiểm tra Channel ID và quyền admin đăng, sửa, ghim bài của bot',
                         'code':'camera_channel_check_failed','camera':camera})
        archive.set_channel_status(slug,'ready')
        camera=next(item for item in archive.cameras() if item['id']==slug)
        return (200,{'camera':camera,'channel_chat_id':chat_id,'ready':True})

    def handle_discovery(self,method,path,query):
        """All callers have passed the ordinary login, setup, Origin and CSRF gates."""
        if query:raise ValueError('Discovery filters are not supported')
        if path=='/api/discovery/subnets' and method=='GET':
            routes=load_subnets(self.server.discovery_routes_file)
            subnets=[{**subnet,'label':subnet['cidr']+' · '+('Tailscale' if subnet['source']=='tailscale' else 'LAN')+' · '+subnet['interface']} for subnet in routes['subnets']]
            self.send(200,{**routes,'subnets':subnets,'available':bool(subnets) and not routes['stale']});return
        if path=='/api/discovery/scans' and method=='POST':
            data=self.read_json()
            if set(data)!={'target'}:raise ValueError('A scan target is required')
            snapshot=self.server.discovery.start(data['target'])
            self.send(202,{'scan':self.discovery_snapshot(snapshot)});return
        prefix='/api/discovery/scans/'
        if path.startswith(prefix):
            pieces=path[len(prefix):].split('/')
            # Scan IDs are opaque; they are never filesystem paths or IP targets.
            if not pieces[0] or len(pieces[0])>128:raise KeyError('Unknown scan')
            if len(pieces)==1 and method=='GET':
                snapshot=self.server.discovery.snapshot(pieces[0]);status=200
            elif len(pieces)==2 and pieces[1]=='cancel' and method=='POST':
                if self.read_json():raise ValueError('Cancel does not accept fields')
                snapshot=self.server.discovery.cancel(pieces[0]);status=202
            else:self.send(404,{'error':'Not found'});return
            self.send(status,{'scan':self.discovery_snapshot(snapshot)});return
        self.send(404,{'error':'Not found'})

    def do_GET(self):self.handle_request('GET')
    def do_HEAD(self):self.handle_request('HEAD')
    def do_POST(self):self.handle_request('POST')
    def do_PATCH(self):self.handle_request('PATCH')

    def handle_request(self,method):
        parsed=urlsplit(self.path);path=parsed.path
        try:
            if path.startswith('/player/') and method in ('GET','HEAD'):
                from .telegram_player import TelegramPlayer
                if not self.server.player_slots.acquire(blocking=False):
                    self.send(503,{'error':'Thử lại sau'});return
                try:
                    archive=Archive(self.server.settings)
                    try:TelegramPlayer(self.server.settings).handle(self,archive,path,head=method=='HEAD')
                    finally:archive.close()
                finally:self.server.player_slots.release()
                return
            if path=='/healthz' and method=='GET':
                self.send(200,{'healthy':True,'service':'dashboard','version':'2.4'});return
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
            if path=='/api/discovery' or path.startswith('/api/discovery/'):
                try:self.handle_discovery(method,path,parsed.query)
                except KeyError:self.send(404,{'error':'Không tìm thấy phiên quét','code':'discovery_not_found'})
                except (ValueError,TypeError):self.send(400,{'error':'Dải IP không hợp lệ, quá lớn, hoặc đang có phiên quét chạy','code':'discovery_invalid_request'})
                return
            archive=Archive(self.server.settings)
            try:
                if path=='/api/status' and method=='GET':
                    heartbeat=self.server.settings.state_dir/'heartbeat'
                    status=archive.status()
                    status.update(csrf_token=session['csrf_token'],heartbeat={'worker_alive':heartbeat.exists() and time.time()-heartbeat.stat().st_mtime<max(120,self.server.settings.interval*4)})
                    response=(200,status)
                elif path=='/api/cameras' and method=='GET':
                    sync=SyncQueue(archive).status()
                    response=(200,{'cameras':[{**camera,'sync':sync['latest'].get(camera['id'])} for camera in archive.cameras()],
                                   'worker_alive':sync['worker_alive'],
                                   'multi_channel_routing':self.server.settings.multi_channel_routing,
                                   'channel_index_enabled':self.server.settings.channel_index_enabled})
                elif path=='/api/cameras' and method=='POST':
                    response=self.add_camera(archive,self.read_json(),account['username'])
                elif path=='/api/telegram/channels' and method=='GET':
                    if parsed.query:raise ValueError('Channel directory filters are not supported')
                    response=(200,{'channels':ChannelDirectory(archive).list()})
                elif path=='/api/telegram/channels/refresh' and method=='POST':
                    if parsed.query or self.read_json():raise ValueError('Channel refresh does not accept fields')
                    try:response=(200,{'channels':ChannelDirectory(archive).refresh(Telegram(self.server.settings))})
                    except Exception:response=(409,{'error':'Kiểm tra kết nối Telegram và quyền admin của bot trong channel','code':'channel_directory_refresh_failed'})
                elif path=='/api/sync' and method=='POST':
                    data=self.read_json()
                    if set(data)-{'camera_id'}:raise ValueError('Unknown sync field')
                    camera=data.get('camera_id')
                    if camera is not None and (not isinstance(camera,str) or not camera):raise ValueError('Invalid camera')
                    response=(202,SyncQueue(archive).enqueue(camera,source='dashboard',actor=account['username']))
                elif path=='/api/sync' and method=='GET':
                    query=parse_qs(parsed.query,keep_blank_values=True)
                    if set(query)-{'camera','limit'} or any(len(values)!=1 for values in query.values()):raise ValueError('Unknown sync filter')
                    camera=query.get('camera',[None])[0]
                    if camera=='':raise ValueError('Select a camera')
                    response=(200,SyncQueue(archive).status(camera,int(query.get('limit',['20'])[0])))
                elif path.startswith('/api/cameras/'):
                    pieces=path[len('/api/cameras/'):].split('/');slug=unquote(pieces[0])
                    if len(pieces)==1 and method=='PATCH':
                        data=self.read_json()
                        with self.server.camera_catalog_lock:response=(200,archive.update_camera(slug,data))
                    elif len(pieces)==2 and pieces[1]=='probe' and method=='POST':response=(200,archive.probe_camera(slug))
                    elif len(pieces)==2 and pieces[1]=='channel-check' and method=='POST':
                        if parsed.query:raise ValueError('Channel check filters are not supported')
                        with self.server.camera_catalog_lock:response=self.check_camera_channel(archive,slug)
                    else:response=(404,{'error':'Not found'})
                elif path=='/api/calendar' and method=='GET':
                    query=parse_qs(parsed.query);camera=query.get('camera',[''])[0]
                    if not camera:raise ValueError('Select a camera')
                    response=(200,archive.calendar(camera))
                elif path=='/api/archive' and method=='GET':
                    query=parse_qs(parsed.query)
                    allowed={'camera','year','month','day','order','status','offset','limit'}
                    if set(query)-allowed:raise ValueError('Unknown filter')
                    args={k:v[0] for k,v in query.items()}
                    for key in ('year','month','day','offset','limit'):
                        if key in args:args[key]=int(args[key])
                    response=(200,archive.browse(**args))
                else:response=(404,{'error':'Not found'})
            finally:archive.close()
            # Release SQLite handles before the client can receive/act on the
            # response (important for Windows teardown and concurrent readers).
            self.send(*response)
        except (ValueError,TypeError,UnicodeError,json.JSONDecodeError):self.send(400,{'error':'Dữ liệu đầu vào không hợp lệ'})
        except KeyError:self.send(404,{'error':'Không tìm thấy camera'})
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception:self.send(500,{'error':'Lỗi xử lý; kiểm tra cấu hình/dịch vụ'})


def serve(settings,host='0.0.0.0',port=8080):
    server=DashboardServer((host,port),settings)
    server.serve_forever(poll_interval=0.5)
