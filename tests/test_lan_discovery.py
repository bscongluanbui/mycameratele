"""Synthetic unicast discovery probes; no real LAN scan or credentials."""
import socket
import threading
import time
import unittest
from unittest.mock import patch

from archive_app.discovery import DiscoveryManager, parse_targets, probe_host


class TargetTests(unittest.TestCase):
    def test_cidr_masks_host_bits_and_excludes_network_broadcast(self):
        target,hosts=parse_targets(' 192.168.31.166/24 ')
        self.assertEqual(target,'192.168.31.0/24')
        self.assertEqual(len(hosts),254)
        self.assertEqual(hosts[0],'192.168.31.1')
        self.assertEqual(hosts[-1],'192.168.31.254')

    def test_single_address(self):
        self.assertEqual(parse_targets('10.0.0.9'),('10.0.0.9',['10.0.0.9']))

    def test_inclusive_range(self):
        self.assertEqual(parse_targets('192.168.2.10 - 192.168.2.12'),
                         ('192.168.2.10-192.168.2.12',['192.168.2.10','192.168.2.11','192.168.2.12']))

    def test_point_to_point_and_host_prefixes(self):
        self.assertEqual(len(parse_targets('172.16.0.0/31')[1]),2)
        self.assertEqual(parse_targets('172.16.0.9/32')[1],['172.16.0.9'])

    def test_reject_public_loopback_metadata_ipv6_and_cgnat(self):
        for target in ('8.8.8.8','127.0.0.1','169.254.169.254','100.76.59.88','::1','fd00::/120','0.0.0.0/0','192.0.0.0/8'):
            with self.subTest(target=target),self.assertRaises(ValueError):parse_targets(target)

    def test_range_cannot_span_nonprivate_addresses(self):
        with self.assertRaises(ValueError):parse_targets('10.255.255.254-172.16.0.1')

    def test_reversed_or_malformed_ranges(self):
        for target in ('192.168.1.2-192.168.1.1','192.168.1.1-192.168.1.2-192.168.1.3',
                       '192.168.1.999','router.lan','','192.168.1.1/99',None,['192.168.1.1']):
            with self.subTest(target=target),self.assertRaises(ValueError):parse_targets(target)

    def test_host_limit_before_materializing_large_network(self):
        with self.assertRaisesRegex(ValueError,'at most 1024'):parse_targets('10.0.0.0/8')
        with self.assertRaisesRegex(ValueError,'at most 1024'):parse_targets('192.168.0.1-192.168.255.254')
        self.assertEqual(len(parse_targets('10.0.0.0/22')[1]),1022)

    def test_configurable_limit_is_bounded(self):
        with self.assertRaises(ValueError):parse_targets('10.0.0.1',4097)
        with self.assertRaises(ValueError):parse_targets('10.0.0.1',0)
        with self.assertRaises(ValueError):parse_targets('10.0.0.1',True)
        with self.assertRaises(ValueError):parse_targets('10.0.0.0/24',10)


class FakeConnection:
    def __init__(self,chunks):self.chunks=list(chunks);self.sent=[];self.timeouts=[];self.closed=False
    def __enter__(self):return self
    def __exit__(self,*args):self.closed=True
    def settimeout(self,value):self.timeouts.append(value)
    def sendall(self,value):self.sent.append(value)
    def recv(self,size):
        if not self.chunks:return b''
        value=self.chunks.pop(0)
        if isinstance(value,Exception):raise value
        if len(value)>size:self.chunks.insert(0,value[size:])
        return value[:size]


class ProbeTests(unittest.TestCase):
    def connector(self,responses):
        connections={};calls=[]
        def connect(address,timeout):
            calls.append((address,timeout))
            if address[1] not in responses:raise OSError('synthetic closed')
            connection=FakeConnection(responses[address[1]])
            connections[address[1]]=connection
            return connection
        return connect,connections,calls

    def probe(self,responses):
        connector,connections,calls=self.connector(responses)
        result=probe_host('192.168.31.166',connector=connector)
        self.assertEqual([call[0][1] for call in calls],[8000,554,80])
        self.assertTrue(all(connection.closed for connection in connections.values()))
        return result,connections

    def test_all_closed_is_not_candidate(self):
        self.assertIsNone(self.probe({})[0])

    def test_web_only_host_is_not_camera_candidate(self):
        self.assertIsNone(self.probe({80:[b'HTTP/1.1 200 OK\r\n\r\n<html>generic router</html>']})[0])

    def test_device_port_alone_is_uncertain(self):
        result,_=self.probe({8000:[]})
        self.assertEqual(result['confidence'],'candidate')
        self.assertEqual(result['vendor'],'')
        self.assertEqual(result['model'],'')
        self.assertEqual(result['ports'],[8000])
        self.assertEqual(result['evidence'],['device_port_open'])

    def test_rtsp_response_uses_options_without_credentials(self):
        result,connections=self.probe({554:[b'RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\n\r\n']})
        self.assertEqual(result['confidence'],'candidate')
        self.assertIn('rtsp_response',result['evidence'])
        request=connections[554].sent[0]
        self.assertTrue(request.startswith(b'OPTIONS rtsp://192.168.31.166/ RTSP/1.0'))
        self.assertNotIn(b'Authorization',request)
        self.assertNotIn(b'PLAY',request)

    def test_vendor_from_rtsp_is_identified_without_guessing_model(self):
        result,_=self.probe({554:[b'RTSP/1.0 200 OK\r\nServer: Hikvision\r\n\r\n']})
        self.assertEqual((result['vendor'],result['model'],result['confidence']),('Hikvision','','identified'))

    def test_ezviz_rtsp_vendor(self):
        result,_=self.probe({554:[b'RTSP/1.0 200 OK\r\nServer: EZVIZ\r\n\r\n']})
        self.assertEqual(result['vendor'],'EZVIZ')

    def test_xml_extracts_model_vendor_only(self):
        body=b'<DeviceInfo xmlns="http://www.hikvision.com/ver20/XMLSchema"><model>CS-H6c-R105-1L3WF</model><manufacturer>EZVIZ</manufacturer><serialNumber>secret-serial</serialNumber></DeviceInfo>'
        result,connections=self.probe({80:[b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body]})
        self.assertEqual(result['model'],'CS-H6c-R105-1L3WF')
        self.assertEqual(result['vendor'],'EZVIZ')
        self.assertEqual(result['confidence'],'identified')
        self.assertNotIn('secret-serial',str(result))
        self.assertIn(b'GET /ISAPI/System/deviceInfo',connections[80].sent[0])

    def test_redirect_and_authentication_response_not_followed(self):
        for status in (b'302 Found',b'401 Unauthorized'):
            response=b'HTTP/1.1 '+status+b'\r\nLocation: http://8.8.8.8/\r\n\r\n<DeviceInfo><model>fake</model></DeviceInfo>'
            result,connections=self.probe({8000:[],80:[response]})
            self.assertEqual(result['model'],'')
            self.assertEqual(len(connections[80].sent),1)

    def test_xml_entities_and_malformed_xml_rejected(self):
        for body in (b'<!DOCTYPE x [<!ENTITY y "secret">]><DeviceInfo><model>&y;</model></DeviceInfo>',
                     b'<DeviceInfo><model>broken',b'<Other><model>not-device-info</model></Other>'):
            result,_=self.probe({8000:[],80:[b'HTTP/1.1 200 OK\r\n\r\n'+body]})
            self.assertEqual(result['model'],'')

    def test_remote_banner_never_becomes_public_evidence(self):
        result,_=self.probe({554:[b'RTSP/1.0 401 Unauthorized\r\nWWW-Authenticate: secret-challenge\r\n\r\n']})
        self.assertNotIn('secret-challenge',str(result))

    def test_peer_timeout_after_port_open_keeps_candidate(self):
        result,_=self.probe({554:[socket.timeout('synthetic')]})
        self.assertEqual(result['ports'],[554])
        self.assertEqual(result['evidence'],['rtsp_port_open'])

    def test_reply_read_is_byte_bounded(self):
        result,connections=self.probe({554:[b'x'*20000]})
        self.assertEqual(sum(len(part) for part in connections[554].chunks),20000-8192)
        self.assertEqual(result['confidence'],'candidate')

    def test_reply_read_has_whole_reply_deadline(self):
        connector,connections,_=self.connector({554:[b'R',b'T',b'S',b'P']})
        with patch('archive_app.discovery.time.monotonic',side_effect=[0,0.1,0.4,0.7]):
            result=probe_host('192.168.31.166',timeout=.6,connector=connector)
        self.assertEqual(result['confidence'],'candidate')
        self.assertEqual(connections[554].chunks,[b'S',b'P'])

    def test_absurd_content_length_is_bounded_without_integer_parse_failure(self):
        result,_=self.probe({8000:[],80:[b'HTTP/1.1 200 OK\r\nContent-Length: '+b'9'*5000+b'\r\n\r\n']})
        self.assertEqual(result['confidence'],'candidate')

    def test_invalid_address_never_connects(self):
        for host in ('8.8.8.8','localhost','::1','169.254.169.254'):
            with self.subTest(host=host),self.assertRaises(ValueError):
                probe_host(host,connector=lambda *args,**kwargs:self.fail('unexpected connection'))


class ManagerTests(unittest.TestCase):
    def terminal(self,manager,job_id):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            job=manager.snapshot(job_id)
            if job['state']!='running':return job
            time.sleep(.005)
        self.fail('scan failed to reach terminal state')

    def test_start_async_progress_and_results_sorted(self):
        def probe(host,timeout):
            return {'host':host,'ports':[8000]}
        manager=DiscoveryManager(probe=probe,workers=2)
        try:
            initial=manager.start('192.168.1.10-192.168.1.12')
            self.assertEqual(initial['state'],'running')
            final=self.terminal(manager,initial['id'])
            self.assertEqual((final['state'],final['scanned'],final['total']),('completed',3,3))
            self.assertEqual([row['host'] for row in final['results']],['192.168.1.10','192.168.1.11','192.168.1.12'])
            self.assertIsNotNone(final['finished_at'])
        finally:manager.close()

    def test_no_candidates_is_successful_completed_scan(self):
        manager=DiscoveryManager(probe=lambda *args,**kwargs:None)
        try:
            final=self.terminal(manager,manager.start('10.0.0.1')['id'])
            self.assertEqual(final['state'],'completed')
            self.assertEqual(final['results'],[])
        finally:manager.close()

    def test_second_scan_rejected_while_running(self):
        release=threading.Event()
        def probe(*args,**kwargs):release.wait(2)
        manager=DiscoveryManager(probe=probe,workers=1)
        initial=manager.start('10.0.0.1')
        try:
            with self.assertRaisesRegex(ValueError,'already running'):manager.start('10.0.0.2')
        finally:release.set();self.terminal(manager,initial['id']);manager.close()

    def test_cancel_stops_scheduling_and_releases_slot(self):
        release=threading.Event();entered=threading.Event();calls=[]
        def probe(host,timeout):calls.append(host);entered.set();release.wait(2)
        manager=DiscoveryManager(probe=probe,workers=1)
        initial=manager.start('10.0.0.1-10.0.0.100')
        try:
            self.assertTrue(entered.wait(1));manager.cancel(initial['id']);release.set()
            final=self.terminal(manager,initial['id'])
            self.assertEqual(final['state'],'cancelled')
            self.assertEqual(len(calls),1)
            self.assertEqual(final['error'],None)
            second=manager.start('10.0.0.2');self.terminal(manager,second['id'])
        finally:release.set();manager.close()

    def test_worker_count_is_bounded(self):
        entered=threading.Event();release=threading.Event();lock=threading.Lock();active=peak=0
        def probe(*args,**kwargs):
            nonlocal active,peak
            with lock:
                active+=1;peak=max(active,peak)
                if active==3:entered.set()
            release.wait(2)
            with lock:active-=1
        manager=DiscoveryManager(probe=probe,workers=3)
        initial=manager.start('10.0.0.1-10.0.0.20')
        try:
            self.assertTrue(entered.wait(1));manager.cancel(initial['id']);release.set()
            self.terminal(manager,initial['id']);self.assertEqual(peak,3)
        finally:release.set();manager.close()

    def test_snapshots_cannot_modify_internal_results(self):
        manager=DiscoveryManager(probe=lambda host,**kwargs:{'host':host,'ports':[8000]})
        try:
            final=self.terminal(manager,manager.start('10.0.0.1')['id'])
            final['results'][0]['ports'].append(9999)
            self.assertEqual(manager.snapshot(final['id'])['results'][0]['ports'],[8000])
        finally:manager.close()

    def test_probe_failure_is_sanitized_and_all_hosts_attempted(self):
        def probe(*args,**kwargs):raise RuntimeError('secret exception detail')
        manager=DiscoveryManager(probe=probe)
        try:
            final=self.terminal(manager,manager.start('10.0.0.1-10.0.0.2')['id'])
            self.assertEqual((final['state'],final['scanned'],final['error']),('failed',2,'probe_failed'))
            self.assertNotIn('secret',str(final))
        finally:manager.close()

    def test_unknown_ids_raise_keyerror(self):
        manager=DiscoveryManager()
        try:
            with self.assertRaises(KeyError):manager.snapshot('unknown')
            with self.assertRaises(KeyError):manager.cancel('unknown')
        finally:manager.close()

    def test_completed_jobs_expire(self):
        now=[100.0];manager=DiscoveryManager(probe=lambda *args,**kwargs:None,clock=lambda:now[0],ttl=2)
        try:
            job_id=manager.start('10.0.0.1')['id'];self.terminal(manager,job_id)
            now[0]=103
            with self.assertRaises(KeyError):manager.snapshot(job_id)
        finally:manager.close()

    def test_retained_job_count_is_bounded(self):
        manager=DiscoveryManager(probe=lambda *args,**kwargs:None)
        try:
            oldest=manager.start('10.0.0.1')['id'];self.terminal(manager,oldest)
            for _ in range(16):self.terminal(manager,manager.start('10.0.0.1')['id'])
            self.assertLessEqual(len(manager.jobs),16)
            with self.assertRaises(KeyError):manager.snapshot(oldest)
        finally:manager.close()

    def test_close_cancels_current_and_prevents_restart(self):
        release=threading.Event()
        def probe(*args,**kwargs):release.wait(2)
        manager=DiscoveryManager(probe=probe,workers=1)
        initial=manager.start('10.0.0.1-10.0.0.2')
        manager.close();release.set()
        self.assertEqual(self.terminal(manager,initial['id'])['state'],'cancelled')
        with self.assertRaisesRegex(ValueError,'closed'):manager.start('10.0.0.2')


if __name__=='__main__':unittest.main()
