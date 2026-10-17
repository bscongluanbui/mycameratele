"""Synthetic neighbour observations; no camera, credentials or media are read."""
from dataclasses import replace
import http.client
import ipaddress
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from archive_app.lan_identity import (IdentityServer, IdentityState, ObserverConfig,
                                     _probe, collect_neighbors, scan_identity,
                                     serve_identity, verify_identity)


TOKEN = 'synthetic-lan-observer-token-1234567890'
MAC = '02:ab:cd:ef:01:07'


def attr(kind, data):
    raw = struct.pack('=HH', len(data) + 4, kind) + data
    return raw + b'\0' * ((-len(raw)) % 4)


def neighbor(host='192.168.2.7', mac=MAC, index=2, state=2, flags=0, age=0.01, kind=1, family=socket.AF_INET):
    raw = struct.pack('=BBHiHBB', family, 0, 0, index, state, flags, kind)
    return raw + attr(1, socket.inet_aton(host)) + attr(2, bytes.fromhex(mac.replace(':', ''))) + attr(3, struct.pack('=IIII', int(age * 100), 0, 0, 1))


class FakeNetlink:
    def __init__(self, packets):
        self.packets, self.closed = packets, False
    def __enter__(self):
        return self
    def __exit__(self, *_):
        self.closed = True
    def settimeout(self, value):
        self.timeout = value
    def bind(self, value):
        self.bound = value
    def sendto(self, request, address):
        self.request, self.address = request, address
    def recvmsg(self, _):
        messages, flags, sender = self.packets.pop(0)
        sequence = struct.unpack_from('=IHHII', self.request)[3]
        packet = b''
        for kind, payload, message_flags, seq_offset in messages:
            raw = struct.pack('=IHHII', 16 + len(payload), kind, message_flags, sequence + seq_offset, 0) + payload
            packet += raw + b'\0' * ((-len(raw)) % 4)
        return packet, [], flags, sender


class ConfigTests(unittest.TestCase):
    def test_normalizes_private_subnet(self):
        self.assertEqual(ObserverConfig(TOKEN, subnet='192.168.2.9/24').subnet, '192.168.2.0/24')
        self.assertEqual(ObserverConfig(TOKEN, subnet='10.1.2.0/30').subnet, '10.1.2.0/30')

    def test_rejects_unbounded_or_public_subnet(self):
        for subnet in ('0.0.0.0/0', '192.168.0.0/16', '192.168.2.0/31', '8.8.8.0/24', '100.64.0.0/24', '::/0'):
            with self.subTest(subnet=subnet), self.assertRaises(ValueError):
                ObserverConfig(TOKEN, subnet=subnet)

    def test_bind_is_tailscale_only(self):
        for address in ('', '0.0.0.0', '127.0.0.1', '192.168.2.1', '8.8.8.8', '::1'):
            with self.subTest(address=address), self.assertRaises(ValueError):
                ObserverConfig(TOKEN, bind=address)

    def test_token_interface_and_worker_limits(self):
        self.assertNotIn(TOKEN, repr(ObserverConfig(TOKEN)))
        for change in ({'token': 'short'}, {'token': TOKEN + '\n'}, {'token': 'é' * 40},
                       {'interface': 'tailscale0'}, {'interface': 'eth0;id'}, {'interface': 'a' * 16},
                       {'workers': 17}, {'workers': True}, {'port': 80}, {'port': True},
                       {'interval': float('nan')}, {'scan_timeout': 16}, {'probe_timeout': 0}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(ObserverConfig(TOKEN), **change)

    def test_environment_reads_only_token_file(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent if os.name == 'nt' else None) as directory:
            path = Path(directory) / 'token'
            path.write_text(TOKEN + '\n', encoding='ascii')
            env = {'LAN_IDENTITY_TOKEN_FILE': str(path), 'LAN_IDENTITY_BIND': '100.92.56.98',
                   'LAN_IDENTITY_SUBNET': '192.168.2.0/24', 'LAN_IDENTITY_INTERFACE': 'eth0', 'LAN_IDENTITY_PORT': '8790'}
            with patch.dict(os.environ, env, clear=True):
                config = ObserverConfig.from_env()
            self.assertEqual((config.token, config.bind, config.port), (TOKEN, '100.92.56.98', 8790))
            path.write_text('x' * 4097)
            with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError):
                ObserverConfig.from_env()


class NetlinkTests(unittest.TestCase):
    def collect(self, messages, *, flags=0, sender=(0, 0), **kwargs):
        connection = FakeNetlink([(messages, flags, sender)])
        with patch.object(socket, 'AF_NETLINK', 777, create=True):
            result = collect_neighbors('eth0', '192.168.2.0/24', socket_factory=lambda *args: connection,
                                       if_index=lambda name: 2, ticks=100, **kwargs)
        self.assertTrue(connection.closed)
        self.assertEqual(connection.bound, (0, 0))
        self.assertEqual(connection.address, (0, 0))
        length, kind, flags, _, pid = struct.unpack_from('=IHHII', connection.request)
        self.assertEqual((length, kind, flags, pid), (28, 30, 0x301, 0))
        return result

    def test_confirmed_fresh_direct_neighbor(self):
        rows = self.collect([(28, neighbor(age=0.5), 2, 0), (3, b'\0' * 4, 2, 0)])
        self.assertEqual(rows, [{'host': '192.168.2.7', 'mac': MAC, 'confirmed_age': 0.5}])

    def test_filters_stale_failed_permanent_proxy_and_wrong_scope(self):
        payloads = [neighbor(state=value) for value in (1, 4, 16, 32, 64, 128)]
        payloads += [neighbor(index=3), neighbor(flags=8), neighbor(kind=2), neighbor(family=socket.AF_INET6),
                     neighbor(host='192.168.3.7'), neighbor(host='192.168.2.0'), neighbor(host='192.168.2.255'),
                     neighbor(age=121), neighbor(mac='00:00:00:00:00:00'), neighbor(mac='ff:ff:ff:ff:ff:ff')]
        self.assertEqual(self.collect([(28, raw, 2, 0) for raw in payloads] + [(3, b'\0' * 4, 2, 0)]), [])

    def test_delay_with_older_confirmed_counter_is_candidate_not_proof(self):
        rows = self.collect([(28, neighbor(state=8, age=20), 2, 0), (3, b'\0' * 4, 2, 0)])
        self.assertEqual(rows, [{'host': '192.168.2.7', 'mac': MAC, 'confirmed_age': 20}])

    def test_other_sequence_is_ignored(self):
        self.assertEqual(self.collect([(28, neighbor(), 2, 1), (3, b'\0' * 4, 2, 0)]), [])

    def test_non_kernel_sender_is_ignored(self):
        connection = FakeNetlink([([(28, neighbor(), 2, 0)], 0, (123, 0)), ([(3, b'\0' * 4, 2, 0)], 0, (0, 0))])
        with patch.object(socket, 'AF_NETLINK', 777, create=True):
            self.assertEqual(collect_neighbors('eth0', '192.168.2.0/24', socket_factory=lambda *args: connection,
                                               if_index=lambda _: 2, ticks=100), [])

    def test_malformed_dump_fails_closed(self):
        for payload in (b'x', neighbor() + struct.pack('=HH', 100, 1) + b'x',
                        struct.pack('=BBHiHBB', socket.AF_INET, 0, 0, 2, 2, 0, 1) + attr(1, b'x') + attr(2, b'123456') + attr(3, b'0' * 16)):
            with self.subTest(payload=payload), self.assertRaises(OSError):
                self.collect([(28, payload, 2, 0), (3, b'\0' * 4, 2, 0)])

    def test_truncated_interrupted_and_error_dump_fail_closed(self):
        for messages, flags in (([(3, b'\0' * 4, 2, 0)], getattr(socket, 'MSG_TRUNC', 0x20)),
                                ([(3, b'\0' * 4, 0x10, 0)], 0),
                                ([(4, b'', 0, 0)], 0),
                                ([(2, struct.pack('=i', -1), 0, 0)], 0),
                                ([(3, b'x', 2, 0)], 0)):
            with self.subTest(messages=messages), self.assertRaises(OSError):
                self.collect(messages, flags=flags)

    def test_dump_deadline_is_bounded(self):
        with self.assertRaisesRegex(OSError, 'timed out'):
            self.collect([(3, b'\0' * 4, 2, 0)], monotonic=Mock(side_effect=[0, 3]))


class ScanTests(unittest.TestCase):
    def scan(self, rows, *, config=None, probe=None, **kwargs):
        config = config or ObserverConfig(TOKEN, subnet='192.168.2.0/29')
        return scan_identity(config, probe=probe or (lambda host, *_: host == '192.168.2.3'),
                             neighbors=lambda *args, **kw: rows,
                             interface_lookup=lambda _: ('192.168.2.1', ipaddress.IPv4Network('192.168.2.0/24')),
                             clock=lambda: 1000, **kwargs)

    def test_successful_echo_and_fresh_netlink_are_both_required(self):
        result = self.scan([{'host': '192.168.2.3', 'mac': MAC, 'confirmed_age': 0.01},
                            {'host': '192.168.2.4', 'mac': '02:ab:cd:ef:01:08', 'confirmed_age': 0.01}])
        self.assertEqual(result['records'], [{'host': '192.168.2.3', 'mac': MAC, 'observed_at': 1000, 'evidence': 'icmp+netlink'}])
        self.assertEqual((result['version'], result['subnet'], result['interface'], result['observed_at']),
                         (1, '192.168.2.0/29', 'eth0', 1000))
        self.assertEqual(len(result['scan_id']), 32)
        self.assertIsNone(result['error'])

    def test_ambiguous_mac_and_ip_are_never_published(self):
        rows = [{'host': host, 'mac': mac, 'confirmed_age': 0.01} for host, mac in
                [('192.168.2.3', MAC), ('192.168.2.4', MAC), ('192.168.2.3', '02:ab:cd:ef:01:08')]]
        result = self.scan(rows, probe=lambda *_: True)
        self.assertEqual(result['records'], [])
        self.assertEqual(result['conflicts'], [MAC, '02:ab:cd:ef:01:08'])

    def test_duplicate_identical_rows_are_deduplicated(self):
        row = {'host': '192.168.2.3', 'mac': MAC, 'confirmed_age': 0.01}
        result = self.scan([row, row])
        self.assertEqual(len(result['records']), 1)
        self.assertEqual(result['conflicts'], [])

    def test_invalid_old_overscope_or_multicast_identity_is_ignored(self):
        rows = [None, {}, {'host': '192.168.3.3', 'mac': MAC, 'confirmed_age': 0},
                {'host': '192.168.2.3', 'mac': 'ff:ff:ff:ff:ff:ff', 'confirmed_age': 0},
                {'host': '192.168.2.3', 'mac': MAC, 'confirmed_age': 121},
                {'host': '192.168.2.3', 'mac': MAC, 'confirmed_age': float('nan')},
                {'host': '192.168.2.3', 'mac': MAC, 'confirmed_age': True}]
        self.assertEqual(self.scan(rows)['records'], [])

    def test_routed_subnet_is_not_treated_as_directly_attached(self):
        with self.assertRaisesRegex(ValueError, 'directly attached'):
            self.scan([], config=ObserverConfig(TOKEN, subnet='192.168.3.0/24'))

    def test_old_delay_counter_uses_current_echo_timestamp(self):
        row = {'host': '192.168.2.3', 'mac': MAC, 'confirmed_age': 20}
        result = self.scan([row])
        self.assertEqual(result['records'][0]['observed_at'], 1000)
        self.assertEqual(self.scan([row], probe=lambda *_: False)['records'], [])

    def test_workers_and_scope_are_bounded(self):
        lock, active, peak, calls = threading.Lock(), [0], [0], []
        def probe(host, local_address, timeout):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
                calls.append(host)
            time.sleep(0.001)
            with lock:
                active[0] -= 1
            self.assertEqual((local_address, timeout), ('192.168.2.1', 0.35))
            return False
        self.scan([], config=ObserverConfig(TOKEN), probe=probe)
        self.assertEqual(len(calls), 253)
        self.assertLessEqual(peak[0], 16)
        self.assertNotIn('192.168.2.0', calls)
        self.assertNotIn('192.168.2.255', calls)
        self.assertNotIn('192.168.2.1', calls)

    def test_scan_deadline_stops_scheduling_new_hosts(self):
        probe = Mock(return_value=True)
        ticks = iter([0, 14])
        self.scan([], probe=probe, monotonic=lambda: next(ticks, 14))
        probe.assert_not_called()

    def test_missing_connected_host_is_reprobed_then_fresh_delay_is_included(self):
        calls = []
        def probe(host, *_):
            calls.append(host)
            return host == '192.168.2.3'
        rows = [{'host': '192.168.2.3', 'mac': MAC, 'confirmed_age': 20}]
        dump = Mock(side_effect=[[], rows])
        config = ObserverConfig(TOKEN, subnet='192.168.2.0/29')
        result = scan_identity(config, probe=probe, neighbors=dump,
                               interface_lookup=lambda _: ('192.168.2.1', ipaddress.IPv4Network('192.168.2.0/24')),
                               clock=Mock(side_effect=[1000, 1001, 1002]))
        self.assertEqual(result['records'], [{'host': '192.168.2.3', 'mac': MAC, 'observed_at': 1001, 'evidence': 'icmp+netlink'}])
        self.assertEqual(calls.count('192.168.2.3'), 2)
        self.assertEqual(calls.count('192.168.2.2'), 1)
        self.assertEqual(dump.call_count, 2)

    def test_missing_candidate_refresh_is_limited_to_sixteen_hosts(self):
        calls = []
        def probe(host, *_):
            calls.append(host)
            return True
        result = self.scan([], config=ObserverConfig(TOKEN), probe=probe)
        self.assertEqual(result['records'], [])
        self.assertEqual(len(set(calls)), 253)
        self.assertEqual(len(calls), 253 + 16)
        self.assertLessEqual(max(calls.count(host) for host in calls), 2)

    def test_reprobe_is_skipped_when_original_scan_deadline_is_near(self):
        now, calls = [0], []
        def probe(host, *_):
            calls.append(host)
            return host == '192.168.2.3'
        def dump(*args, **kwargs):
            now[0] = 14.9
            return []
        result = scan_identity(ObserverConfig(TOKEN, subnet='192.168.2.0/29'), probe=probe, neighbors=dump,
                               interface_lookup=lambda _: ('192.168.2.1', ipaddress.IPv4Network('192.168.2.0/24')),
                               monotonic=lambda: now[0])
        self.assertEqual(result['records'], [])
        self.assertEqual(calls.count('192.168.2.3'), 1)

    def test_no_echo_success_never_triggers_reprobe_or_publishes(self):
        dump = Mock(return_value=[{'host': '192.168.2.3', 'mac': MAC, 'confirmed_age': 20}])
        result = scan_identity(ObserverConfig(TOKEN, subnet='192.168.2.0/29'), probe=lambda *_: False, neighbors=dump,
                               interface_lookup=lambda _: ('192.168.2.1', ipaddress.IPv4Network('192.168.2.0/24')))
        self.assertEqual(result['records'], [])
        self.assertEqual(dump.call_count, 1)

    def test_reprobe_failure_discards_previous_tcp_success(self):
        calls = [0]
        def probe(host, *_):
            if host != '192.168.2.3':
                return False
            calls[0] += 1
            return calls[0] == 1
        dump = Mock(side_effect=[[], [{'host': '192.168.2.3', 'mac': MAC, 'confirmed_age': 20}]])
        result = scan_identity(ObserverConfig(TOKEN, subnet='192.168.2.0/29'), probe=probe, neighbors=dump,
                               interface_lookup=lambda _: ('192.168.2.1', ipaddress.IPv4Network('192.168.2.0/24')))
        self.assertEqual(result['records'], [])

    def test_stopped_scan_does_not_publish_snapshot(self):
        stop = threading.Event()
        stop.set()
        self.assertIsNone(self.scan([], stop=stop))

    def test_probe_uses_source_bound_echo_without_opening_camera_ports(self):
        with patch('archive_app.lan_identity.echo_probe', return_value=True) as echo, \
             patch('archive_app.lan_identity.socket.socket') as connection:
            self.assertTrue(_probe('192.168.2.3', '192.168.2.1', 0.35))
        echo.assert_called_once_with('192.168.2.3', '192.168.2.1', 0.35)
        connection.assert_not_called()

    def test_probe_echo_failure_does_not_fall_back_to_sdk_tcp(self):
        with patch('archive_app.lan_identity.echo_probe', return_value=False), \
             patch('archive_app.lan_identity.socket.socket') as connection:
            self.assertFalse(_probe('192.168.2.3', '192.168.2.1', 0.35))
        connection.assert_not_called()


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.state = IdentityState(ObserverConfig(TOKEN), scanner=Mock())
        self.server = IdentityServer(('127.0.0.1', 0), self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01})
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.state.close()

    def request(self, path='/v1/neighbors', token=TOKEN, duplicate=False):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.putrequest('GET', path)
            if token is not None:
                connection.putheader('Authorization', 'Bearer ' + token)
                if duplicate:
                    connection.putheader('Authorization', 'Bearer ' + token)
            connection.endheaders()
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_authorized_cached_snapshot_never_starts_scans(self):
        for _ in range(3):
            code, payload = self.request()
            self.assertEqual(code, 200)
            self.assertEqual(payload['error'], 'observer_pending')
            self.assertEqual(payload['records'], [])
            self.assertNotIn(TOKEN, json.dumps(payload))
        self.state.scanner.assert_not_called()

    def test_missing_wrong_or_duplicate_authorization_rejected(self):
        for token, duplicate in ((None, False), ('wrong', False), (TOKEN, True)):
            self.assertEqual(self.request(token=token, duplicate=duplicate), (401, {'error': 'observer_unauthorized'}))

    def test_query_and_arbitrary_network_are_not_supported(self):
        for path in ('/v1/neighbors?subnet=10.0.0.0/8', '/v1/scan', '/v1/neighbors/'):
            self.assertEqual(self.request(path)[0], 404)
        self.state.scanner.assert_not_called()

    def test_health_contains_no_metadata(self):
        self.assertEqual(self.request('/health', token=None), (200, {'ok': True}))

    def test_snapshot_is_copied(self):
        value = self.state.snapshot()
        value['records'].append({'host': 'invalid'})
        self.assertEqual(self.state.snapshot()['records'], [])

    def post(self, raw, *, token=TOKEN, content_type='application/json', headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=4)
        supplied = {'Authorization': 'Bearer ' + token, 'Content-Type': content_type, **(headers or {})}
        try:
            connection.request('POST', '/v1/verify', body=raw, headers=supplied)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_post_authentication_precedes_parsing_and_scan(self):
        with patch('archive_app.lan_identity.verify_identity') as verify:
            self.assertEqual(self.post('invalid', token='wrong')[0], 401)
        verify.assert_not_called()

    def test_post_exact_body_delegates_single_host_verification(self):
        result = {'version': 1, 'host': '192.168.2.7', 'mac': MAC, 'verified': True, 'observed_at': 1000}
        with patch('archive_app.lan_identity.verify_identity', return_value=result) as verify:
            self.assertEqual(self.post(json.dumps({'host': '192.168.2.7', 'mac': MAC})), (200, result))
        verify.assert_called_once_with(self.state.config, '192.168.2.7', MAC, stop=self.state.stop)

    def test_post_rejects_extra_fields_oversize_and_bad_content_type(self):
        with patch('archive_app.lan_identity.verify_identity') as verify:
            for raw in ('invalid', '[]', '{}', json.dumps({'host': '192.168.2.7', 'mac': MAC, 'port': 80}),
                        json.dumps({'host': ['192.168.2.7'], 'mac': MAC}),
                        '{"host":"192.168.2.7","host":"192.168.2.8","mac":"' + MAC + '"}'):
                self.assertEqual(self.post(raw)[0], 400)
            self.assertEqual(self.post('x' * 1025)[0], 413)
            self.assertEqual(self.post('{}', content_type='text/plain')[0], 400)
            self.assertEqual(self.post('{}', headers={'Transfer-Encoding': 'chunked'})[0], 400)
        verify.assert_not_called()

    def test_post_overscope_and_unavailable_are_sanitized(self):
        with patch('archive_app.lan_identity.verify_identity', side_effect=ValueError('secret detail')):
            self.assertEqual(self.post(json.dumps({'host': '8.8.8.8', 'mac': MAC})), (400, {'error': 'observer_invalid_request'}))
        with patch('archive_app.lan_identity.verify_identity', side_effect=OSError('secret detail')):
            self.assertEqual(self.post(json.dumps({'host': '192.168.2.7', 'mac': MAC})), (503, {'error': 'observer_verification_unavailable'}))


class VerificationTests(unittest.TestCase):
    def verify(self, *, host='192.168.2.7', mac=MAC, rows=None, probe=None):
        if rows is None:
            rows = [{'host': host, 'mac': MAC, 'confirmed_age': 0.01}]
        return verify_identity(ObserverConfig(TOKEN), host, mac, probe=probe or (lambda *_: True),
                               neighbors=lambda *args, **kwargs: rows,
                               interface_lookup=lambda _: ('192.168.2.1', ipaddress.IPv4Network('192.168.2.0/24')),
                               clock=lambda: 1000)

    def test_fresh_positive_single_host_proof(self):
        result = self.verify()
        self.assertEqual(result, {'version': 1, 'host': '192.168.2.7', 'mac': MAC, 'verified': True, 'observed_at': 1000})

    def test_wrong_mac_never_verifies(self):
        result = self.verify(mac='02:ab:cd:ef:01:08')
        self.assertFalse(result['verified'])
        self.assertEqual(result['mac'], MAC)

    def test_outside_scope_local_network_and_broadcast_rejected(self):
        for host in ('192.168.3.7', '192.168.2.0', '192.168.2.255', '192.168.2.1', '8.8.8.8', 'camera.example'):
            with self.subTest(host=host), self.assertRaises(ValueError):
                self.verify(host=host)

    def test_ambiguous_neighbor_proof_is_rejected(self):
        result = self.verify(rows=[{'host': '192.168.2.7', 'mac': MAC, 'confirmed_age': 0},
                                   {'host': '192.168.2.7', 'mac': '02:ab:cd:ef:01:08', 'confirmed_age': 0}])
        self.assertFalse(result['verified'])
        self.assertEqual(result['mac'], '')

    def test_same_mac_on_multiple_fresh_hosts_is_rejected(self):
        result = self.verify(rows=[{'host': '192.168.2.7', 'mac': MAC, 'confirmed_age': 0},
                                   {'host': '192.168.2.8', 'mac': MAC, 'confirmed_age': 0}])
        self.assertFalse(result['verified'])
        self.assertEqual(result['mac'], '')

    def test_no_echo_or_stale_neighbor_proof_is_rejected(self):
        stop = threading.Event()
        stop.wait = Mock(return_value=False)
        with patch('archive_app.lan_identity.threading.Event', return_value=stop):
            self.assertFalse(self.verify(probe=lambda *_: False)['verified'])
            self.assertFalse(self.verify(rows=[{'host': '192.168.2.7', 'mac': MAC, 'confirmed_age': 121}])['verified'])

    def test_confirmed_age_twenty_seconds_is_valid_with_current_echo(self):
        result = self.verify(rows=[{'host': '192.168.2.7', 'mac': MAC, 'confirmed_age': 20}])
        self.assertTrue(result['verified'])
        self.assertEqual(result['observed_at'], 1000)

    def test_mac_change_between_before_and_after_dump_fails_closed(self):
        before = [{'host': '192.168.2.7', 'mac': MAC, 'confirmed_age': 20}]
        after = [{'host': '192.168.2.7', 'mac': '02:ab:cd:ef:01:08', 'confirmed_age': 0}]
        lookup = lambda _: ('192.168.2.1', ipaddress.IPv4Network('192.168.2.0/24'))
        dump = Mock(side_effect=[before, after])
        result = verify_identity(ObserverConfig(TOKEN), '192.168.2.7', MAC,
                                 probe=lambda *_: True, neighbors=dump, interface_lookup=lookup)
        self.assertFalse(result['verified'])
        self.assertEqual(result['mac'], '')
        self.assertEqual(dump.call_count, 2)


class StateTests(unittest.TestCase):
    def test_failure_publishes_only_fixed_error_and_close_stops_wait(self):
        called = threading.Event()
        def fail(*args, **kwargs):
            called.set()
            raise RuntimeError('SENSITIVE-CAMERA-OR-TOKEN')
        state = IdentityState(ObserverConfig(TOKEN), scanner=fail)
        state.start()
        self.assertTrue(called.wait(2))
        state.close()
        self.assertFalse(state.thread.is_alive())
        self.assertEqual(state.snapshot()['error'], 'observer_scan_failed')
        self.assertNotIn('SENSITIVE', json.dumps(state.snapshot()))

    def test_start_is_idempotent_and_snapshot_replaced(self):
        called = threading.Event()
        data = {'version': 1, 'records': [], 'error': None}
        def scan(*args, **kwargs):
            called.set()
            return data
        state = IdentityState(ObserverConfig(TOKEN), scanner=scan)
        state.start()
        worker = state.thread
        state.start()
        self.assertIs(state.thread, worker)
        self.assertTrue(called.wait(2))
        state.close()
        self.assertEqual(state.snapshot(), data)

    def test_pre_stopped_server_starts_and_closes_cleanly(self):
        stop = threading.Event()
        stop.set()
        with patch('archive_app.lan_identity.ObserverConfig.from_env', return_value=ObserverConfig(TOKEN)), \
             patch('archive_app.lan_identity.IdentityState') as state_type, \
             patch('archive_app.lan_identity.IdentityServer') as server_type:
            serve_identity(stop)
        state_type.return_value.close.assert_called_once()
        server_type.return_value.server_close.assert_called_once()
        server_type.return_value.handle_request.assert_not_called()


class ProofCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.state = IdentityState(ObserverConfig(TOKEN), clock=lambda: self.now)

    def tearDown(self):
        self.state.close()

    def proof(self, host='192.168.2.7', mac=MAC, stamp=None, verified=True):
        return {'version': 1, 'host': host, 'mac': mac, 'verified': verified,
                'observed_at': self.now if stamp is None else stamp}

    def scan(self, records=None, conflicts=None):
        return {'version': 1, 'subnet': '192.168.2.0/24', 'interface': 'eth0',
                'observed_at': self.now, 'scan_id': 'synthetic', 'error': None,
                'records': records or [], 'conflicts': conflicts or []}

    def test_successful_post_proof_seeds_fresh_directory_before_first_scan(self):
        self.state.remember_proof(self.proof(), MAC)
        result = self.state.snapshot()
        self.assertEqual(result['records'], [{'host': '192.168.2.7', 'mac': MAC, 'observed_at': 1000, 'evidence': 'icmp+netlink'}])
        self.assertIsNone(result['error'])
        self.assertEqual(result['observed_at'], 1000)

    def test_expected_mac_case_and_separator_share_one_normalized_retry_hint(self):
        self.state.remember_proof(self.proof(), MAC.upper().replace(':', '-'))
        self.assertEqual(set(self.state.known_targets), {MAC})
        self.assertEqual(self.state.snapshot()['records'][0]['mac'], MAC)

    def test_missing_broad_scan_pair_is_refreshed_by_targeted_guard(self):
        self.state.remember_proof(self.proof(), MAC)
        self.now = 1060
        with patch('archive_app.lan_identity.verify_identity', return_value=self.proof()) as verify:
            self.state._apply_scan(self.scan())
        verify.assert_called_once_with(self.state.config, '192.168.2.7', MAC, stop=self.state.stop)
        self.assertEqual(self.state.snapshot()['records'][0]['observed_at'], 1060)

    def test_cached_proof_expires_after_one_hundred_twenty_seconds(self):
        self.state.remember_proof(self.proof(), MAC)
        self.now = 1120
        self.assertEqual(len(self.state.snapshot()['records']), 1)
        self.now = 1120.01
        self.assertEqual(self.state.snapshot()['records'], [])

    def test_failed_targeted_proof_does_not_reuse_old_pair(self):
        self.state.remember_proof(self.proof(), MAC)
        with patch('archive_app.lan_identity.verify_identity', return_value=self.proof(verified=False)):
            self.state._apply_scan(self.scan())
        self.assertEqual(self.state.snapshot()['records'], [])

    def test_transient_failure_retains_only_retry_hint_and_recovers_next_scan(self):
        self.state.remember_proof(self.proof(), MAC)
        self.now = 1060
        with patch('archive_app.lan_identity.verify_identity', return_value=self.proof(verified=False)):
            self.state._apply_scan(self.scan())
        self.assertEqual(self.state.snapshot()['records'], [])
        self.assertEqual(self.state.known_targets[MAC]['observed_at'], 1000)
        self.now = 1121
        self.assertEqual(self.state.snapshot()['records'], [])
        with patch('archive_app.lan_identity.verify_identity', return_value=self.proof()) as verify:
            self.state._apply_scan(self.scan())
        verify.assert_called_once_with(self.state.config, '192.168.2.7', MAC, stop=self.state.stop)
        self.assertEqual(self.state.snapshot()['records'][0]['observed_at'], 1121)

    def test_expired_proof_is_not_published_but_can_receive_strict_retry(self):
        self.state.remember_proof(self.proof(), MAC)
        self.now = 1121
        self.assertEqual(self.state.snapshot()['records'], [])
        with patch('archive_app.lan_identity.verify_identity', return_value=self.proof(verified=False)) as verify:
            self.state._apply_scan(self.scan())
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(self.state.snapshot()['records'], [])

    def test_retry_exception_revokes_proof_without_forgetting_target(self):
        self.state.remember_proof(self.proof(), MAC)
        with patch('archive_app.lan_identity.verify_identity', side_effect=OSError('temporary')):
            self.state._apply_scan(self.scan())
        self.assertEqual(self.state.snapshot()['records'], [])
        self.assertIn(MAC, self.state.known_targets)
        with patch('archive_app.lan_identity.verify_identity', return_value=self.proof()) as verify:
            self.state._apply_scan(self.scan())
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(len(self.state.snapshot()['records']), 1)

    def test_known_hint_expires_after_twenty_four_hours_without_success(self):
        self.state.remember_proof(self.proof(), MAC)
        self.now = 87400.01
        with patch('archive_app.lan_identity.verify_identity') as verify:
            self.state._apply_scan(self.scan())
        verify.assert_not_called()
        self.assertEqual(self.state.known_targets, {})
        self.assertEqual(self.state.snapshot()['records'], [])

    def test_backward_clock_jump_forgets_future_hints_and_proofs(self):
        self.state.remember_proof(self.proof(), MAC)
        self.now = 994
        with patch('archive_app.lan_identity.verify_identity') as verify:
            self.state._apply_scan(self.scan())
        verify.assert_not_called()
        self.assertEqual(self.state.known_targets, {})
        self.assertEqual(self.state.snapshot()['records'], [])

    def test_different_proven_owner_forgets_old_hint_after_proof_expired(self):
        replacement = '02:ab:cd:ef:01:08'
        self.state.remember_proof(self.proof(), MAC)
        self.now = 1121
        self.assertEqual(self.state.snapshot()['records'], [])
        self.state.remember_proof(self.proof(mac=replacement), replacement)
        self.state.remember_proof(self.proof(stamp=1120), MAC)
        self.assertEqual(set(self.state.known_targets), {replacement})
        self.assertEqual([row['mac'] for row in self.state.snapshot()['records']], [replacement])

    def test_wrong_mac_response_forgets_old_pair_but_not_new_host(self):
        replacement = '02:ab:cd:ef:01:08'
        self.state.remember_proof(self.proof(), MAC)
        self.state.remember_proof(self.proof(mac=replacement, verified=False), MAC)
        self.assertNotIn(MAC, self.state.known_targets)
        self.state.remember_proof(self.proof(host='192.168.2.17'), MAC)
        self.state.remember_proof(self.proof(mac=replacement, verified=False), MAC)
        self.assertEqual(self.state.known_targets[MAC]['host'], '192.168.2.17')
        self.assertEqual(self.state.snapshot()['records'][0]['host'], '192.168.2.17')

    def test_newer_proven_owner_replaces_old_mac_on_same_host(self):
        replacement = '02:ab:cd:ef:01:08'
        self.state.remember_proof(self.proof(), MAC)
        self.now = 1001
        self.state.remember_proof(self.proof(mac=replacement), replacement)
        self.assertEqual([row['mac'] for row in self.state.snapshot()['records']], [replacement])

    def test_late_older_scan_cannot_evict_newer_post_proven_owner(self):
        replacement = '02:ab:cd:ef:01:08'
        self.state.remember_proof(self.proof(mac=replacement, stamp=1000), replacement)
        self.now = 1001
        self.state.remember_proof(self.proof(), MAC)
        old_row = {'host': '192.168.2.7', 'mac': replacement, 'observed_at': 1000, 'evidence': 'icmp+netlink'}
        with patch('archive_app.lan_identity.verify_identity', return_value=self.proof()):
            self.state._apply_scan(self.scan([old_row]))
        result = self.state.snapshot()['records']
        self.assertEqual([(row['host'], row['mac'], row['observed_at']) for row in result], [('192.168.2.7', MAC, 1001)])

    def test_reported_mac_conflict_removes_cache_instead_of_masking_it(self):
        self.state.remember_proof(self.proof(), MAC)
        self.state._apply_scan(self.scan(conflicts=[MAC]))
        self.assertEqual(self.state.snapshot()['records'], [])
        self.assertEqual(self.state.snapshot()['conflicts'], [MAC])
        self.assertNotIn(MAC, self.state.known_targets)
        with patch('archive_app.lan_identity.verify_identity') as verify:
            self.state._apply_scan(self.scan())
        verify.assert_not_called()

    def test_missing_known_pair_refresh_is_limited_to_sixteen(self):
        for index in range(20):
            mac = f'02:ab:cd:ef:01:{index:02x}'
            self.state.remember_proof(self.proof(host=f'192.168.2.{index + 2}', mac=mac), mac)
        def verify(config, host, mac, **kwargs):
            return self.proof(host, mac)
        with patch('archive_app.lan_identity.verify_identity', side_effect=verify) as callback:
            self.state._apply_scan(self.scan())
        self.assertEqual(callback.call_count, 16)
        self.assertEqual(len(self.state.snapshot()['records']), 20)

    def test_bounded_retry_rotates_targets_instead_of_starving_later_cameras(self):
        for index in range(20):
            mac = f'02:ab:cd:ef:01:{index:02x}'
            self.state.remember_proof(self.proof(host=f'192.168.2.{index + 2}', mac=mac), mac)
        def failed(config, host, mac, **kwargs):
            return self.proof(host, mac, verified=False)
        with patch('archive_app.lan_identity.verify_identity', side_effect=failed) as verify:
            self.state._apply_scan(self.scan())
            self.now += 60
            self.state._apply_scan(self.scan())
        self.assertEqual(verify.call_count, 32)
        self.assertEqual(len({call.args[2] for call in verify.call_args_list}), 20)
        self.assertEqual(self.state.snapshot()['records'], [])

    def test_known_targets_are_bounded_by_subnet_and_replace_address_owner(self):
        for index in range(1, 255):
            mac = f'02:ab:cd:ef:01:{index:02x}'
            self.state.remember_proof(self.proof(host=f'192.168.2.{index}', mac=mac), mac)
        self.assertEqual(len(self.state.known_targets), 254)
        replacement = '02:ab:cd:ef:02:01'
        self.now += 1
        self.state.remember_proof(self.proof(host='192.168.2.1', mac=replacement), replacement)
        self.assertEqual(len(self.state.known_targets), 254)
        self.assertNotIn('02:ab:cd:ef:01:01', self.state.known_targets)

    def test_failed_old_host_proof_does_not_remove_new_host_for_same_mac(self):
        self.state.remember_proof(self.proof(host='192.168.2.17'), MAC)
        self.state.remember_proof(self.proof(verified=False), MAC)
        self.assertEqual(self.state.snapshot()['records'][0]['host'], '192.168.2.17')

    def test_invalid_or_expired_proofs_do_not_populate_cache(self):
        for proof in (self.proof(host='8.8.8.8'), self.proof(stamp=0), self.proof(stamp=1010),
                      self.proof(mac='ff:ff:ff:ff:ff:ff')):
            self.state.remember_proof(proof, proof['mac'])
        self.assertEqual(self.state.snapshot()['records'], [])


if __name__ == '__main__':
    unittest.main()
