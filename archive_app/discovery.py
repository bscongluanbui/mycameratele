"""Bounded, unauthenticated unicast discovery for explicitly selected LAN ranges.

Discovery only gathers camera candidates. It never downloads recordings, tries
credentials, or automatically modifies the camera catalog.
"""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import copy
import ipaddress
import re
import secrets
import socket
import threading
import time
import xml.etree.ElementTree as ET


PRIVATE_NETWORKS=tuple(ipaddress.IPv4Network(value) for value in
                       ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))
DEFAULT_PORTS=(8000,554,80)


def _private(address):
    return isinstance(address,ipaddress.IPv4Address) and any(address in network for network in PRIVATE_NETWORKS)


def parse_targets(target,max_hosts=1024):
    """Return a normalized range and bounded RFC1918 IPv4 host addresses."""
    if not isinstance(target,str) or not target.strip() or len(target)>100:
        raise ValueError('Enter a private IPv4 CIDR, address, or first-last range')
    if not isinstance(max_hosts,int) or isinstance(max_hosts,bool) or not 1<=max_hosts<=4096:
        raise ValueError('Discovery host limit is invalid')
    target=target.strip()
    try:
        if '-' in target:
            parts=target.split('-')
            if len(parts)!=2:raise ValueError
            first,last=(ipaddress.IPv4Address(value.strip()) for value in parts)
            if not _private(first) or not _private(last) or int(first)>int(last):raise ValueError
            # Endpoints alone cannot authorize a range spanning public space.
            if not any(first in network and last in network for network in PRIVATE_NETWORKS):raise ValueError
            count=int(last)-int(first)+1
            normalized=str(first)+'-'+str(last)
            addresses=lambda:(ipaddress.IPv4Address(number) for number in range(int(first),int(last)+1))
        else:
            network=ipaddress.IPv4Network(target,strict=False)
            if not any(network.subnet_of(private) for private in PRIVATE_NETWORKS):raise ValueError
            count=network.num_addresses-(2 if network.prefixlen<31 else 0)
            normalized=str(network) if '/' in target else str(network.network_address)
            addresses=network.hosts
    except (ValueError,TypeError):
        raise ValueError('Only private LAN IPv4 ranges are accepted') from None
    if count>max_hosts:
        raise ValueError('Range contains too many hosts; choose at most '+str(max_hosts))
    return normalized,[str(address) for address in addresses()]


def _safe_text(value,limit=96):
    if not isinstance(value,str):return ''
    return ''.join(character for character in value.strip() if character.isprintable())[:limit]


def _device_info(raw):
    """Extract only non-secret identification fields from a bounded XML reply."""
    model=vendor=''
    if b'\r\n\r\n' not in raw:return vendor,model
    headers,body=raw.split(b'\r\n\r\n',1)
    if not re.match(br'HTTP/1\.[01] 200(?: |\r|$)',headers):return vendor,model
    if b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():return vendor,model
    try:
        root=ET.fromstring(body)
    except (ET.ParseError,ValueError):return vendor,model
    if root.tag.rsplit('}',1)[-1].lower()!='deviceinfo':return vendor,model
    for element in root.iter():
        tag=element.tag.rsplit('}',1)[-1].lower()
        if tag=='model':model=_safe_text(element.text)
        elif tag in ('manufacturer','manufacturername'):vendor=_safe_text(element.text,48)
    return vendor,model


def _bounded_reply(connection,limit=8192,timeout=0.6):
    chunks=[];remaining=limit;deadline=time.monotonic()+timeout
    while remaining:
        left=deadline-time.monotonic()
        if left<=0:break
        connection.settimeout(left)
        try:data=connection.recv(min(2048,remaining))
        except (OSError,TimeoutError):break
        if not data:break
        chunks.append(data);remaining-=len(data)
        joined=b''.join(chunks)
        if joined.startswith(b'RTSP/') and b'\r\n\r\n' in joined:break
        if b'\r\n\r\n' in joined:
            headers,body=joined.split(b'\r\n\r\n',1)
            match=re.search(br'(?im)^content-length:\s*(\d+)\s*$',headers)
            if match and len(match.group(1))<=9 and len(body)>=int(match.group(1)):break
    return b''.join(chunks)


def probe_host(host,timeout=0.6,connector=None):
    """Inspect three standard ports, returning sanitized evidence or None.

    Ports alone never claim a particular vendor/model. RTSP OPTIONS and the
    unauthenticated ISAPI device-info endpoint do not change device state.
    """
    try:address=ipaddress.IPv4Address(host)
    except (ValueError,TypeError):raise ValueError('Discovery requires a private IPv4 address') from None
    if not _private(address):raise ValueError('Discovery requires a private IPv4 address')
    connector=connector or socket.create_connection
    ports=[];evidence=[];vendor=model='';identified=False
    for port in DEFAULT_PORTS:
        try:
            with connector((str(address),port),timeout=timeout) as connection:
                connection.settimeout(timeout)
                ports.append(port)
                if port==554:
                    connection.sendall(('OPTIONS rtsp://'+str(address)+'/ RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: MyCameraTele-Discovery\r\n\r\n').encode('ascii'))
                    reply=_bounded_reply(connection,timeout=timeout)
                    if re.match(br'RTSP/1\.[01] \d{3}(?: |\r|$)',reply):
                        evidence.append('rtsp_response')
                        lowered=reply.lower()
                        if b'ezviz' in lowered:vendor='EZVIZ';identified=True
                        elif b'hikvision' in lowered:vendor='Hikvision';identified=True
                elif port==80:
                    connection.sendall(('GET /ISAPI/System/deviceInfo HTTP/1.1\r\nHost: '+str(address)+'\r\nConnection: close\r\nUser-Agent: MyCameraTele-Discovery\r\n\r\n').encode('ascii'))
                    found_vendor,found_model=_device_info(_bounded_reply(connection,timeout=timeout))
                    if found_vendor or found_model:
                        vendor=found_vendor or vendor;model=found_model
                        evidence.append('isapi_device_info');identified=True
        except (OSError,TimeoutError):pass
    if 8000 in ports:evidence.append('device_port_open')
    if 554 in ports:evidence.append('rtsp_port_open')
    if not evidence:return None
    return {'host':str(address),'device_port':8000,'rtsp_port':554,'http_port':80,
            'ports':sorted(ports),'vendor':vendor,'model':model,
            'confidence':'identified' if identified else 'candidate','evidence':evidence}


class DiscoveryManager:
    """One cancellable, bounded asynchronous scan per dashboard process."""
    def __init__(self,*,probe=None,max_hosts=1024,workers=32,timeout=0.6,ttl=900,clock=None):
        self.probe=probe or probe_host
        self.max_hosts=max_hosts
        self.workers=max(1,min(32,int(workers)))
        self.timeout=max(0.1,min(2.0,float(timeout)))
        self.ttl=max(1,float(ttl));self.clock=clock or time.time
        self.lock=threading.Lock();self.jobs={};self.active=None;self.closed=False

    def _prune(self):
        now=self.clock()
        for job_id,job in list(self.jobs.items()):
            if job['data']['finished_at'] is not None and now-job['data']['finished_at']>self.ttl:
                self.jobs.pop(job_id,None)

    def start(self,target):
        normalized,hosts=parse_targets(target,self.max_hosts)
        with self.lock:
            self._prune()
            if self.closed:raise ValueError('Discovery service is closed')
            if self.active is not None:raise ValueError('A discovery scan is already running')
            while len(self.jobs)>=16:
                oldest=min(self.jobs,key=lambda key:self.jobs[key]['data']['started_at'])
                self.jobs.pop(oldest,None)
            job_id=secrets.token_urlsafe(18)
            data={'id':job_id,'target':normalized,'state':'running','total':len(hosts),'scanned':0,
                  'results':[],'started_at':self.clock(),'finished_at':None,'error':None}
            job={'data':data,'cancel':threading.Event(),'thread':None}
            thread=threading.Thread(target=self._run,args=(job_id,hosts),daemon=True,name='camera-discovery')
            job['thread']=thread;self.jobs[job_id]=job;self.active=job_id
            result=copy.deepcopy(data)
            thread.start()
            return result

    def snapshot(self,job_id):
        with self.lock:
            self._prune()
            if job_id not in self.jobs:raise KeyError(job_id)
            return copy.deepcopy(self.jobs[job_id]['data'])

    def cancel(self,job_id):
        with self.lock:
            self._prune()
            if job_id not in self.jobs:raise KeyError(job_id)
            self.jobs[job_id]['cancel'].set()
            return copy.deepcopy(self.jobs[job_id]['data'])

    def close(self):
        with self.lock:
            self.closed=True
            for job in self.jobs.values():job['cancel'].set()

    def _run(self,job_id,hosts):
        job=self.jobs[job_id];cancel=job['cancel'];pending={};index=0;failed=False
        executor=ThreadPoolExecutor(max_workers=self.workers,thread_name_prefix='camera-probe')
        try:
            while (index<len(hosts) or pending) and not cancel.is_set():
                while index<len(hosts) and len(pending)<self.workers and not cancel.is_set():
                    host=hosts[index];index+=1
                    pending[executor.submit(self.probe,host,timeout=self.timeout)]=host
                if not pending:break
                completed,_=wait(pending,timeout=0.1,return_when=FIRST_COMPLETED)
                for future in completed:
                    pending.pop(future,None)
                    # Unexpected probe failures are reduced to a fixed status;
                    # neither exception text nor a remote banner is exposed.
                    try:result=future.result()
                    except Exception:result=None;failed=True
                    with self.lock:
                        job['data']['scanned']+=1
                        if result is not None:job['data']['results'].append(copy.deepcopy(result))
                        job['data']['results'].sort(key=lambda value:int(ipaddress.IPv4Address(value['host'])))
        except Exception:failed=True
        finally:
            for future in pending:future.cancel()
            # At most `workers` probes remain; each real socket operation has a
            # bounded timeout. Keep the active slot until they have finished.
            executor.shutdown(wait=True,cancel_futures=True)
            with self.lock:
                data=job['data'];data['finished_at']=self.clock()
                data['state']='cancelled' if cancel.is_set() else 'failed' if failed else 'completed'
                data['error']='probe_failed' if failed and not cancel.is_set() else None
                if self.active==job_id:self.active=None
