"""Synthetic policy-route Netlink dumps and atomic metadata snapshots."""
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from archive_app.network_routes import (collect_loop, collect_subnets, load_subnets,
                                       normalize_subnets, write_snapshot)


def attribute(kind,data):
    value=struct.pack('=HH',4+len(data),kind)+data
    return value+b'\0'*((-len(value))%4)


def route(cidr='192.168.31.0/24',index=2,table=52,route_type=1):
    address,prefix=cidr.split('/')
    payload=struct.pack('=BBBBBBBBI',socket.AF_INET,int(prefix),0,0,table if table<256 else 0,0,0,route_type,0)
    payload+=attribute(1,socket.inet_aton(address))+attribute(4,struct.pack('=I',index))
    if table>=256:payload+=attribute(15,struct.pack('=I',table))
    return payload


class FakeNetlink:
    def __init__(self,packets):self.packets=packets;self.request=None;self.address=None;self.closed=False
    def __enter__(self):return self
    def __exit__(self,*args):self.closed=True
    def settimeout(self,timeout):self.timeout=timeout
    def bind(self,address):self.bound=address
    def sendto(self,request,address):self.request=request;self.address=address
    def recvmsg(self,size):
        if not self.packets:raise socket.timeout('synthetic')
        messages,flags,sender=self.packets.pop(0)
        sequence=struct.unpack_from('=IHHII',self.request)[3]
        packet=b''
        for kind,payload,message_flags,seq_offset in messages:
            value=struct.pack('=IHHII',16+len(payload),kind,message_flags,sequence+seq_offset,0)+payload
            packet+=value+b'\0'*((-len(value))%4)
        return packet,[],flags,sender


class RouteFilterTests(unittest.TestCase):
    def test_real_vps_route_shape_includes_tailscale_and_main_lan(self):
        values=[{'cidr':prefix,'interface':'tailscale0','table':52} for prefix in
                ('192.168.1.0/24','192.168.2.0/24','192.168.21.0/24','192.168.31.0/24')]
        values.append({'cidr':'10.0.0.0/24','interface':'enp0s6','table':254})
        self.assertEqual(len(normalize_subnets(values)),5)
        self.assertEqual(normalize_subnets(values)[0]['source'],'lan')
        self.assertTrue(all(row['source']=='tailscale' for row in normalize_subnets(values)[1:]))

    def test_excludes_default_node_cgnat_docker_and_loopback(self):
        values=[{'cidr':'0.0.0.0/0','interface':'eth0'},
                {'cidr':'192.168.1.2/32','interface':'tailscale0'},
                {'cidr':'100.76.59.88/32','interface':'tailscale0'},
                {'cidr':'172.17.0.0/16','interface':'docker0'},
                {'cidr':'172.18.0.0/16','interface':'br-deadbeef'},
                {'cidr':'192.168.1.0/24','interface':'veth123'},
                {'cidr':'10.0.0.0/8','interface':'lo'},
                {'cidr':'8.8.8.0/24','interface':'eth0'},
                {'cidr':'169.254.0.0/16','interface':'eth0'}]
        self.assertEqual(normalize_subnets(values),[])

    def test_deduplicates_and_prefers_tailscale_label(self):
        values=[{'cidr':'192.168.1.9/24','interface':'eth0','table':254},
                {'cidr':'192.168.1.0/24','interface':'tailscale0','table':52}]
        self.assertEqual(normalize_subnets(values),[{'cidr':'192.168.1.0/24','interface':'tailscale0','source':'tailscale','table':52}])

    def test_rejects_malformed_untrusted_snapshot_entries(self):
        values=[None,{},'route',{'cidr':'::/0','interface':'eth0'},
                {'cidr':'192.168.1.0/24','interface':'<script>'},
                {'cidr':'192.168.1.0/24','interface':'eth0','table':-1},
                {'cidr':'192.168.1.0/24','interface':'eth0','table':'52'}]
        self.assertEqual(normalize_subnets(values),[])


class NetlinkTests(unittest.TestCase):
    def collect(self,packets,names=None):
        connection=FakeNetlink(packets)
        def factory(*args):
            self.assertEqual(args,(777,socket.SOCK_RAW,0))
            return connection
        with patch.object(socket,'AF_NETLINK',777,create=True):
            result=collect_subnets(socket_factory=factory,interface_name=lambda index:(names or {2:'tailscale0'})[index])
        self.assertTrue(connection.closed)
        self.assertEqual(connection.bound,(0,0))
        self.assertEqual(connection.address,(0,0))
        length,kind,flags,sequence,pid=struct.unpack_from('=IHHII',connection.request)
        self.assertEqual((length,kind,flags,pid),(28,26,0x305,0))
        self.assertGreater(sequence,0)
        self.assertEqual(connection.request[20],0) # table 0 requests all policy tables
        return result

    def test_policy_table52_route_dump(self):
        result=self.collect([([(24,route(),2,0),(3,b'\0'*4,2,0)],0,(0,0))])
        self.assertEqual(result,[{'cidr':'192.168.31.0/24','interface':'tailscale0','source':'tailscale','table':52}])

    def test_u32_table_attribute_and_multiple_packets(self):
        result=self.collect([([(24,route(table=1000),2,0)],0,(0,0)),
                             ([(3,b'\0'*4,2,0)],0,(0,0))])
        self.assertEqual(result[0]['table'],1000)

    def test_ack_success_ignored_until_done(self):
        self.assertEqual(self.collect([([(2,b'\0'*4,0,0),(3,b'\0'*4,2,0)],0,(0,0))]),[])

    def test_messages_with_other_sequence_ignored(self):
        self.assertEqual(self.collect([([(24,route(),2,1),(3,b'\0'*4,2,0)],0,(0,0))]),[])

    def test_messages_from_non_kernel_sender_ignored(self):
        self.assertEqual(self.collect([([(24,route(),2,0)],0,(123,0)),
                                       ([(3,b'\0'*4,2,0)],0,(0,0))]),[])

    def test_nonunicast_routes_skipped(self):
        self.assertEqual(self.collect([([(24,route(route_type=2),2,0),(3,b'\0'*4,2,0)],0,(0,0))]),[])

    def test_permission_error_is_oserror(self):
        with self.assertRaises(OSError):self.collect([([(2,struct.pack('=i',-1),0,0)],0,(0,0))])

    def test_dump_interrupted_rejected(self):
        with self.assertRaisesRegex(OSError,'Interrupted'):self.collect([([(3,b'\0'*4,0x10,0)],0,(0,0))])

    def test_truncated_packet_rejected(self):
        with self.assertRaisesRegex(OSError,'Truncated'):self.collect([([(3,b'\0'*4,2,0)],getattr(socket,'MSG_TRUNC',0x20),(0,0))])

    def test_socket_overrun_rejected(self):
        with self.assertRaisesRegex(OSError,'overrun'):self.collect([([(4,b'',0,0)],0,(0,0))])

    def test_malformed_route_attribute_rejected(self):
        payload=route()+struct.pack('=HH',20,1)+b'x'
        with self.assertRaisesRegex(OSError,'Malformed'):self.collect([([(24,payload,2,0)],0,(0,0))])

    def test_short_route_payload_rejected(self):
        with self.assertRaisesRegex(OSError,'Malformed'):self.collect([([(24,b'x',2,0)],0,(0,0))])

    def test_short_error_payload_rejected(self):
        with self.assertRaisesRegex(OSError,'Malformed'):self.collect([([(2,b'x',0,0)],0,(0,0))])

    def test_done_error_rejected(self):
        with self.assertRaises(OSError):self.collect([([(3,struct.pack('=i',-1),2,0)],0,(0,0))])

    def test_platform_unavailable_has_clear_error(self):
        with patch('archive_app.network_routes.hasattr',return_value=False,create=True):
            with self.assertRaisesRegex(OSError,'unavailable'):collect_subnets()


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.parent=(Path(__file__).parent if os.name=='nt' else Path(tempfile.gettempdir())).resolve()
        self.root=self.parent/('.tmp-network-routes-'+uuid.uuid4().hex);self.root.mkdir()
        self.path=self.root/'subnets.json'
        self.routes=[{'cidr':'192.168.31.0/24','interface':'tailscale0','table':52}]

    def tearDown(self):
        self.assertEqual(self.root.resolve().parent,self.parent);shutil.rmtree(self.root)

    def test_atomic_file_is_reopened_and_loaded(self):
        payload=write_snapshot(self.path,self.routes,now=100)
        self.assertEqual(json.loads(self.path.read_text()),payload)
        loaded=load_subnets(self.path,now=101)
        self.assertEqual(loaded['subnets'],normalize_subnets(self.routes))
        self.assertFalse(loaded['stale']);self.assertIsNone(loaded['error'])
        self.assertEqual(list(self.root.iterdir()),[self.path])

    def test_replacement_refreshes_not_appends(self):
        write_snapshot(self.path,self.routes,now=100);write_snapshot(self.path,[],now=102)
        self.assertEqual(load_subnets(self.path,now=103)['subnets'],[])

    def test_stale_snapshot_remains_labeled_stale(self):
        write_snapshot(self.path,self.routes,now=100)
        loaded=load_subnets(self.path,now=281)
        self.assertTrue(loaded['stale']);self.assertEqual(loaded['error'],'route_snapshot_stale')

    def test_collector_failure_has_fixed_error(self):
        write_snapshot(self.path,[],error='route_collection_failed',now=100)
        loaded=load_subnets(self.path,now=101)
        self.assertEqual(loaded['error'],'route_collection_failed')

    def test_missing_malformed_and_future_snapshot(self):
        self.assertEqual(load_subnets(self.path)['error'],'route_snapshot_unavailable')
        for payload in ('not-json','[]','{"version":2}',
                        '{"version":1,"updated_at":NaN,"subnets":[]}',
                        '{"version":1,"updated_at":true,"subnets":[]}',
                        '{"version":1,"updated_at":10000,"subnets":[]}'):
            self.path.write_text(payload)
            self.assertEqual(load_subnets(self.path,now=100)['error'],'route_snapshot_unavailable')

    def test_oversized_snapshot_not_read(self):
        self.path.write_text('x'*262145)
        self.assertEqual(load_subnets(self.path)['subnets'],[])

    def test_collect_loop_once_stops_and_writes(self):
        event=threading.Event()
        def collect():event.set();return self.routes
        with patch('archive_app.network_routes.collect_subnets',side_effect=collect):collect_loop(self.path,stop_event=event)
        self.assertEqual(load_subnets(self.path)['subnets'],normalize_subnets(self.routes))

    def test_collect_loop_error_sanitized_and_keeps_running_until_stop(self):
        event=threading.Event()
        def collect():event.set();raise OSError('secret platform detail')
        with patch('archive_app.network_routes.collect_subnets',side_effect=collect):collect_loop(self.path,stop_event=event)
        self.assertNotIn('secret',self.path.read_text())
        self.assertEqual(load_subnets(self.path)['error'],'route_collection_failed')

    def test_collect_loop_interval_validation(self):
        for value in (0,3601):
            with self.assertRaises(ValueError):collect_loop(self.path,interval=value)


if __name__=='__main__':unittest.main()
