"""MAC identity metadata from an explicitly configured LAN observer.

No camera credentials or media ever pass through this client/observer protocol.
The worker keeps its direct routed SD connection; only its destination is bound.
"""
from __future__ import annotations

import ipaddress
import json
import math
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

MAX_AGE = 120
PRIVATE = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))


class CameraIdentityError(Exception):
    def __init__(self, code, message='LAN camera identity is not verified'):
        self.code = code
        super().__init__(message)


def normalize_mac(value):
    if not isinstance(value,str):raise ValueError('MAC must be text')
    value=value.strip().lower()
    if not value:return ''
    if re.fullmatch(r'[0-9a-f]{12}',value):compact=value
    elif re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}',value):compact=value.replace(':','')
    elif re.fullmatch(r'(?:[0-9a-f]{2}-){5}[0-9a-f]{2}',value):compact=value.replace('-','')
    else:raise ValueError('MAC needs six hexadecimal octets')
    if compact=='000000000000' or int(compact[:2],16)&1:raise ValueError('MAC needs a nonzero unicast address')
    return ':'.join(compact[n:n+2] for n in range(0,12,2))


def normalize_subnet(value):
    if not isinstance(value,str):raise ValueError('Identity subnet must be text')
    value=value.strip()
    if not value:return ''
    try:
        if '/' not in value:raise ValueError
        net=ipaddress.IPv4Network(value,strict=False)
        if not 24<=net.prefixlen<=30 or not any(net.subnet_of(n) for n in PRIVATE):raise ValueError
    except ValueError:raise ValueError('Identity subnet needs a private IPv4 CIDR /24 to /30') from None
    return str(net)


def validate_url(value):
    if not value:return ''
    try:
        parts=urlsplit(value);address=ipaddress.ip_address(parts.hostname or '')
        allowed=(*PRIVATE,ipaddress.ip_network('100.64.0.0/10'),ipaddress.ip_network('127.0.0.0/8'))
        if (parts.scheme not in ('http','https') or not any(address in n for n in allowed)
                or not parts.port or parts.username is not None or parts.password is not None
                or parts.path not in ('','/') or parts.query or parts.fragment or '%' in value):raise ValueError
    except (ValueError,TypeError):raise ValueError('LAN_IDENTITY_URL needs a private/tailnet IPv4 and explicit port') from None
    return value.rstrip('/')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def _fresh(value,now):
    return type(value) in (int,float) and math.isfinite(value) and -5<=now-value<=MAX_AGE


def validate_snapshot(data,now=None):
    now=time.time() if now is None else now
    try:
        if not isinstance(data,dict) or type(data.get('version')) is not int or data['version']!=1:raise ValueError
        subnet=normalize_subnet(data['subnet']);net=ipaddress.IPv4Network(subnet)
        if not subnet or not _fresh(data['observed_at'],now) or data.get('error'):raise ValueError
        records=data['records']
        if not isinstance(records,list) or len(records)>254 or data.get('conflicts'):raise ValueError
        mappings={};ips=set();clean=[]
        for row in records:
            if not isinstance(row,dict) or not _fresh(row['observed_at'],now):raise ValueError
            mac=normalize_mac(row['mac']);host=str(ipaddress.IPv4Address(row['host']))
            if (not mac or ipaddress.IPv4Address(host) not in net or host in (str(net.network_address),str(net.broadcast_address))
                    or mac in mappings or host in ips or row.get('evidence') not in ('tcp+netlink','icmp+netlink')):raise ValueError
            mappings[mac]=host;ips.add(host)
            clean.append({'host':host,'mac':mac,'observed_at':row['observed_at'],'evidence':row['evidence']})
        return {'version':1,'configured':True,'subnet':subnet,'observed_at':data['observed_at'],
                'records':clean,'mappings':mappings,'conflicts':[]}
    except (ValueError,KeyError,TypeError,OverflowError):
        raise CameraIdentityError('sd_identity_invalid','LAN identity snapshot is stale, ambiguous or invalid') from None


class IdentityClient:
    def __init__(self,url,token_file,*,clock=None):
        self.url=validate_url(url);self.token_file=token_file
        self.clock=clock or time.time;self.lock=threading.RLock()
        self.cached=None;self.cached_at=0;self.verified={}

    def _request(self,path,body=None):
        try:
            if Path(self.token_file).stat().st_size>4096 or not Path(self.token_file).is_file():raise ValueError
            token=Path(self.token_file).read_text(encoding='utf-8').strip()
            if not 32<=len(token)<=512 or any(not 33<=ord(c)<=126 for c in token):raise ValueError
            payload=None if body is None else json.dumps(body,separators=(',',':')).encode('utf-8')
            req=Request(self.url+path,data=payload,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},method='GET' if body is None else 'POST')
            # Never leak the observer token to environment HTTP proxies or redirects.
            with build_opener(ProxyHandler({}),_NoRedirect()).open(req,timeout=5) as response:
                data=response.read(131073)
                if len(data)>131072:raise ValueError
                return json.loads(data)
        except (OSError,ValueError,TypeError,HTTPError,URLError):
            raise CameraIdentityError('sd_identity_unavailable','LAN identity observer did not return verified metadata') from None

    def snapshot(self,force=False):
        with self.lock:
            now=self.clock()
            if force or self.cached is None or now-self.cached_at>=5:
                self.cached=validate_snapshot(self._request('/v1/neighbors'),now)
                self.cached_at=now
            # Revalidate the timestamp even for a cached response.
            return validate_snapshot(self.cached,now)

    def verify(self,host,mac):
        key=(host,mac)
        with self.lock:
            now=self.clock()
            if 0<=now-self.verified.get(key,-math.inf)<1:return
            reply=self._request('/v1/verify',{'host':host,'mac':mac})
            if (not isinstance(reply,dict) or type(reply.get('version')) is not int or reply['version']!=1 or reply.get('verified') is not True
                    or reply.get('host')!=host or reply.get('mac')!=mac
                    or not _fresh(reply.get('observed_at'),now) or abs(now-reply['observed_at'])>5):
                raise CameraIdentityError('sd_identity_changed','Camera MAC no longer matches its bound IP')
            self.verified[key]=self.clock()


_clients={};_clients_lock=threading.Lock()


def client_for(settings):
    url=getattr(settings,'lan_identity_url','')
    token=getattr(settings,'lan_identity_token_file','')
    if not url or not token:raise CameraIdentityError('sd_identity_unconfigured','Configure the LAN metadata observer before enabling automatic IP')
    key=(url,token)
    with _clients_lock:
        if key not in _clients:
            if len(_clients)>=32:_clients.clear()
            try:_clients[key]=IdentityClient(url,token)
            except ValueError:raise CameraIdentityError('sd_identity_unconfigured') from None
        return _clients[key]


def public_snapshot(settings):
    if not getattr(settings,'lan_identity_url',''):return {'configured':False,'records':[]}
    snapshot=client_for(settings).snapshot()
    return {key:value for key,value in snapshot.items() if key!='mappings'}


def resolve_camera(archive,camera_id):
    camera=archive.conn.execute('SELECT * FROM cameras WHERE id=? AND deleted_at IS NULL',(camera_id,)).fetchone()
    if camera is None:raise KeyError(camera_id)
    if not camera['auto_ip']:return
    try:
        snapshot=client_for(archive.settings).snapshot()
        if camera['identity_subnet']!=snapshot['subnet']:
            raise CameraIdentityError('sd_identity_subnet','LAN observer belongs to a different subnet')
        try:archive.apply_camera_identity(snapshot['subnet'],snapshot['mappings'],snapshot['observed_at'])
        except ValueError:raise CameraIdentityError('sd_identity_stale') from None
        camera=archive.conn.execute('SELECT * FROM cameras WHERE id=? AND deleted_at IS NULL',(camera_id,)).fetchone()
        if camera['identity_status']!='verified':
            raise CameraIdentityError(camera['identity_error'] or 'sd_identity_not_found')
    except CameraIdentityError as error:
        if error.code not in ('sd_identity_conflict','sd_identity_not_found','sd_identity_stale'):
            with archive.conn:
                archive.conn.execute("""UPDATE cameras SET identity_status='unavailable',identity_error=?
                    WHERE id=? AND deleted_at IS NULL AND auto_ip=1 AND mac_address=? AND identity_subnet=?""",
                    (error.code,camera_id,camera['mac_address'],camera['identity_subnet']))
        raise


def guard_camera(config,*,settings):
    if not config.get('auto_ip'):return
    try:
        mac=normalize_mac(config['mac_address']);subnet=normalize_subnet(config['identity_subnet'])
    except (KeyError,ValueError):raise CameraIdentityError('sd_identity_invalid') from None
    snapshot=client_for(settings).snapshot()
    if snapshot['subnet']!=subnet:raise CameraIdentityError('sd_identity_subnet')
    if snapshot['mappings'].get(mac)!=config['host']:raise CameraIdentityError('sd_identity_changed')
    client_for(settings).verify(config['host'],mac)
