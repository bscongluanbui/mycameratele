"""Read-only Linux route collector including policy-routing tables used by Tailscale.

The collector needs the host network namespace, but no root user, Docker socket,
Tailscale credentials, NET_ADMIN or other capabilities. It only issues a
NETLINK_ROUTE dump request and writes sanitized subnet metadata to a volume.
"""
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import struct
import threading
import time

from .discovery import PRIVATE_NETWORKS


_NL_HEADER=struct.Struct('=IHHII')
_RT_HEADER=struct.Struct('=BBBBBBBBI')
_ATTR_HEADER=struct.Struct('=HH')
_CONTAINER_PREFIXES=('docker','br-','veth','virbr','cni','flannel','kube')


def _aligned(length):return (length+3)&~3


def _attributes(data):
    offset=0
    while offset+_ATTR_HEADER.size<=len(data):
        length,kind=_ATTR_HEADER.unpack_from(data,offset)
        if length<_ATTR_HEADER.size or offset+length>len(data):raise ValueError('Malformed route attribute')
        yield kind&0x3fff,data[offset+_ATTR_HEADER.size:offset+length]
        offset+=_aligned(length)
    if offset<len(data) and any(data[offset:]):raise ValueError('Malformed route padding')


def _parse_route(payload,interface_name):
    if len(payload)<_RT_HEADER.size:raise ValueError('Malformed route message')
    family,prefix,_,_,table,_,_,route_type,_=_RT_HEADER.unpack_from(payload)
    if family!=socket.AF_INET or route_type!=1 or not 1<=prefix<32:return None
    destination=None;interface_index=None
    for kind,value in _attributes(payload[_RT_HEADER.size:]):
        if kind==1 and len(value)==4:destination=socket.inet_ntoa(value)
        elif kind==4 and len(value)==4:interface_index=struct.unpack('=I',value)[0]
        elif kind==15 and len(value)==4:table=struct.unpack('=I',value)[0]
    if destination is None or interface_index is None:return None
    try:interface=interface_name(interface_index)
    except (OSError,ValueError):return None
    return {'cidr':str(ipaddress.IPv4Network((destination,prefix),strict=False)),
            'interface':interface,'table':table}


def normalize_subnets(routes):
    """Filter and deduplicate only RFC1918 non-container subnet routes."""
    selected={}
    for route in routes:
        if not isinstance(route,dict):continue
        try:
            network=ipaddress.IPv4Network(route.get('cidr',''),strict=False)
            interface=route.get('interface','');table=route.get('table',254)
            if not 1<=network.prefixlen<32 or not any(network.subnet_of(private) for private in PRIVATE_NETWORKS):continue
            if not isinstance(interface,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}',interface):continue
            if interface=='lo' or interface.lower().startswith(_CONTAINER_PREFIXES):continue
            if not isinstance(table,int) or not 0<=table<=0xffffffff:continue
            source='tailscale' if interface.startswith('tailscale') else 'lan'
            item={'cidr':str(network),'interface':interface,'source':source,'table':table}
            previous=selected.get(str(network))
            # A route advertised through Tailscale is the most useful label
            # when the same prefix also has an ordinary-table entry.
            if previous is None or (source=='tailscale' and previous['source']!='tailscale'):
                selected[str(network)]=item
        except (ValueError,TypeError):continue
    return sorted(selected.values(),key=lambda item:(int(ipaddress.IPv4Network(item['cidr']).network_address),ipaddress.IPv4Network(item['cidr']).prefixlen))


def collect_subnets(*,socket_factory=None,interface_name=None):
    """Dump all IPv4 Linux route tables, including Tailscale table 52."""
    if not hasattr(socket,'AF_NETLINK'):raise OSError('Linux route discovery is unavailable')
    socket_factory=socket_factory or socket.socket
    interface_name=interface_name or socket.if_indextoname
    sequence=(time.monotonic_ns()&0xffffffff) or 1
    payload=_RT_HEADER.pack(socket.AF_INET,0,0,0,0,0,0,0,0)
    request=_NL_HEADER.pack(_NL_HEADER.size+len(payload),26,0x305,sequence,0)+payload
    routes=[]
    with socket_factory(socket.AF_NETLINK,socket.SOCK_RAW,0) as connection:
        connection.settimeout(3)
        connection.bind((0,0));connection.sendto(request,(0,0))
        for _ in range(256):
            packet,_,flags,sender=connection.recvmsg(65536)
            if flags&getattr(socket,'MSG_TRUNC',0x20):raise OSError('Truncated route dump')
            if not sender or sender[0]!=0:continue
            offset=0
            while offset+_NL_HEADER.size<=len(packet):
                length,kind,message_flags,seq,_pid=_NL_HEADER.unpack_from(packet,offset)
                if length<_NL_HEADER.size or offset+length>len(packet):raise OSError('Malformed route dump')
                body=packet[offset+_NL_HEADER.size:offset+length];offset+=_aligned(length)
                if seq!=sequence:continue
                if message_flags&0x10:raise OSError('Interrupted route dump')
                if kind==4:raise OSError('Route dump overrun')
                if kind==2:
                    if len(body)<4:raise OSError('Malformed route error')
                    code=struct.unpack_from('=i',body)[0]
                    if code:raise OSError(-code,'Route dump failed')
                elif kind==3:
                    if len(body)>=4 and struct.unpack_from('=i',body)[0]:raise OSError('Route dump failed')
                    return normalize_subnets(routes)
                elif kind==24:
                    try:route=_parse_route(body,interface_name)
                    except ValueError:raise OSError('Malformed route dump') from None
                    if route is not None:routes.append(route)
        raise OSError('Route dump exceeded limit')


def write_snapshot(path,subnets,*,error=None,now=None):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    payload={'version':1,'updated_at':time.time() if now is None else float(now),
             'subnets':normalize_subnets(subnets),'error':'route_collection_failed' if error else None}
    temporary=path.with_name('.'+path.name+'.'+str(os.getpid())+'.tmp')
    try:
        with temporary.open('w',encoding='utf-8') as handle:
            json.dump(payload,handle,ensure_ascii=True,separators=(',',':'))
            handle.flush();os.fsync(handle.fileno())
        os.replace(temporary,path)
    finally:
        try:temporary.unlink(missing_ok=True)
        except OSError:pass
    return payload


def load_subnets(path,*,max_age=180,now=None):
    current=time.time() if now is None else float(now)
    empty={'subnets':[],'updated_at':None,'stale':True,'error':'route_snapshot_unavailable'}
    try:
        path=Path(path)
        if path.stat().st_size>262144:return empty
        payload=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(payload,dict) or payload.get('version')!=1:return empty
        stamp=payload.get('updated_at');routes=payload.get('subnets')
        if not isinstance(stamp,(int,float)) or isinstance(stamp,bool) or not 0<=stamp<=current+60:return empty
        if not isinstance(routes,list) or len(routes)>2048:return empty
        stale=current-stamp>max_age
        error='route_collection_failed' if payload.get('error') else 'route_snapshot_stale' if stale else None
        return {'subnets':normalize_subnets(routes),'updated_at':stamp,'stale':stale,'error':error}
    except (OSError,ValueError,TypeError):return empty


def collect_loop(path,interval=30,stop_event=None):
    interval=float(interval)
    if not 1<=interval<=3600:raise ValueError('Route refresh interval must be between 1 and 3600 seconds')
    stop_event=stop_event or threading.Event()
    while not stop_event.is_set():
        try:write_snapshot(path,collect_subnets())
        except (OSError,ValueError):write_snapshot(path,[],error='route_collection_failed')
        stop_event.wait(interval)
