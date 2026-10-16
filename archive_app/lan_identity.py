"""Small, read-only LAN identity observer; it never opens or transfers media.

Run in the LAN host network namespace without Linux capabilities.  A successful
TCP connection and a bounded-age neighbour entry must agree before an IP/MAC
pair is published.  Cached ARP rows alone are deliberately insufficient.
"""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
import copy
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import struct
import threading
import time
import uuid

from .network_routes import _aligned, _attributes


_NL_HEADER = struct.Struct('=IHHII')
_NEIGH_HEADER = struct.Struct('=BBHiHBB')
_PRIVATE = tuple(map(ipaddress.IPv4Network, ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')))
_TAILNET = ipaddress.IPv4Network('100.64.0.0/10')


def _mac(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{2}(?:[:-][0-9a-fA-F]{2}){5}', value):
        raise ValueError('Invalid MAC address')
    raw = bytes.fromhex(value.replace(':', '').replace('-', ''))
    if raw == b'\0' * 6 or raw[0] & 1:
        raise ValueError('Invalid MAC address')
    return ':'.join(f'{part:02x}' for part in raw)


@dataclass(frozen=True)
class ObserverConfig:
    token: str = field(repr=False)
    subnet: str = '192.168.2.0/24'
    interface: str = 'eth0'
    bind: str = '100.64.0.1'
    port: int = 8790
    interval: float = 60
    workers: int = 16
    probe_timeout: float = 0.35
    scan_timeout: float = 15

    def __post_init__(self):
        if not isinstance(self.token, str) or not 32 <= len(self.token) <= 512 or not all(33 <= ord(c) <= 126 for c in self.token):
            raise ValueError('Identity observer requires a private token of at least 32 characters')
        network = ipaddress.IPv4Network(self.subnet, strict=False)
        if not 24 <= network.prefixlen <= 30 or not any(network.subnet_of(item) for item in _PRIVATE):
            raise ValueError('Identity observer requires a private /24 to /30 subnet')
        object.__setattr__(self, 'subnet', str(network))
        if not isinstance(self.interface, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', self.interface) or self.interface.lower().startswith(('lo', 'tailscale', 'docker', 'veth', 'br-')):
            raise ValueError('Identity observer requires an explicit LAN interface')
        if ipaddress.IPv4Address(self.bind) not in _TAILNET:
            raise ValueError('Identity observer must bind its Tailscale IPv4 address')
        if type(self.port) is not int or not 1024 <= self.port <= 65535:
            raise ValueError('Invalid identity observer port')
        if type(self.workers) is not int or not 1 <= self.workers <= 16:
            raise ValueError('Invalid identity observer worker count')
        for value, minimum, maximum in ((self.interval, 30, 3600), (self.probe_timeout, 0.05, 1), (self.scan_timeout, 3, 15)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError('Invalid identity observer timing')

    @classmethod
    def from_env(cls):
        path = Path(os.environ.get('LAN_IDENTITY_TOKEN_FILE', '/run/secrets/lan-identity-token'))
        if path.stat().st_size > 4096:
            raise ValueError('Invalid identity observer token file')
        token = path.read_text(encoding='ascii').strip()
        return cls(token, os.environ.get('LAN_IDENTITY_SUBNET', '192.168.2.0/24'),
                   os.environ.get('LAN_IDENTITY_INTERFACE', 'eth0'),
                   os.environ.get('LAN_IDENTITY_BIND', ''), int(os.environ.get('LAN_IDENTITY_PORT', '8790')))


def interface_network(interface):
    """Read an interface's directly attached IPv4 address and mask, without caps."""
    import fcntl
    request = struct.pack('256s', interface.encode('ascii'))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        address = socket.inet_ntoa(fcntl.ioctl(connection, 0x8915, request)[20:24])
        mask = socket.inet_ntoa(fcntl.ioctl(connection, 0x891b, request)[20:24])
    return address, ipaddress.IPv4Network((address, mask), strict=False)


def _parse_neighbor(payload, index, network, max_age, ticks):
    if len(payload) < _NEIGH_HEADER.size:
        raise ValueError('Malformed neighbour message')
    family, _, _, ifindex, state, flags, kind = _NEIGH_HEADER.unpack_from(payload)
    if family != socket.AF_INET or ifindex != index or state not in (2, 8) or flags & 8 or kind != 1:
        return None
    attrs = dict(_attributes(payload[_NEIGH_HEADER.size:]))
    address, hardware, cache = attrs.get(1), attrs.get(2), attrs.get(3)
    if address is None or hardware is None or cache is None:
        return None
    if len(address) != 4 or len(hardware) != 6 or len(cache) != 16:
        raise ValueError('Malformed neighbour identity')
    host = ipaddress.IPv4Address(address)
    if host not in network or host in (network.network_address, network.broadcast_address):
        return None
    age = struct.unpack('=IIII', cache)[0] / ticks
    if age > max_age:
        return None
    try:
        mac = _mac(':'.join(f'{part:02x}' for part in hardware))
    except ValueError:
        return None
    return {'host': str(host), 'mac': mac, 'confirmed_age': age}


def collect_neighbors(interface, subnet, *, max_age=120, timeout=2, socket_factory=None, if_index=None, ticks=None, monotonic=None):
    """Read bounded-age candidates; callers still require a current TCP proof.

    DELAY follows use of a stale-but-valid cache entry.  A short TCP handshake
    does not necessarily refresh its confirmed counter before the ARP timer.
    """
    if not hasattr(socket, 'AF_NETLINK'):
        raise OSError('Linux neighbour discovery is unavailable')
    network = ipaddress.IPv4Network(subnet, strict=False)
    index = (if_index or socket.if_nametoindex)(interface)
    ticks = os.sysconf('SC_CLK_TCK') if ticks is None else ticks
    if not isinstance(ticks, (int, float)) or ticks <= 0:
        raise ValueError('Invalid neighbour clock frequency')
    monotonic = monotonic or time.monotonic
    sequence = (time.monotonic_ns() & 0xffffffff) or 1
    payload = _NEIGH_HEADER.pack(socket.AF_INET, 0, 0, index, 0, 0, 0)
    request = _NL_HEADER.pack(16 + len(payload), 30, 0x301, sequence, 0) + payload
    records, deadline = [], monotonic() + min(2, max(0.05, timeout))
    with (socket_factory or socket.socket)(socket.AF_NETLINK, socket.SOCK_RAW, 0) as connection:
        connection.bind((0, 0))
        connection.sendto(request, (0, 0))
        for _ in range(128):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise OSError('Neighbour dump timed out')
            connection.settimeout(remaining)
            packet, _, flags, sender = connection.recvmsg(65536)
            if flags & getattr(socket, 'MSG_TRUNC', 0x20):
                raise OSError('Truncated neighbour dump')
            if not sender or sender[0] != 0:
                continue
            offset = 0
            while offset + 16 <= len(packet):
                length, kind, message_flags, seq, _ = _NL_HEADER.unpack_from(packet, offset)
                if length < 16 or offset + length > len(packet):
                    raise OSError('Malformed neighbour dump')
                body = packet[offset + 16:offset + length]
                offset += _aligned(length)
                if seq != sequence:
                    continue
                if message_flags & 0x10 or kind == 4:
                    raise OSError('Interrupted neighbour dump')
                if kind in (2, 3):
                    if len(body) < 4 or struct.unpack_from('=i', body)[0]:
                        raise OSError('Neighbour dump failed')
                    if kind == 3:
                        return records
                elif kind == 28:
                    try:
                        record = _parse_neighbor(body, index, network, max_age, ticks)
                    except ValueError:
                        raise OSError('Malformed neighbour dump') from None
                    if record is not None:
                        records.append(record)
                        if len(records) > 1024:
                            raise OSError('Neighbour dump exceeded limit')
            if offset < len(packet) and any(packet[offset:]):
                raise OSError('Malformed neighbour dump')
    raise OSError('Neighbour dump exceeded limit')


def _probe(host, local_address, timeout):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.bind((local_address, 0))
            connection.connect((host, 8000))
            return True
    except OSError:
        return False


def scan_identity(config, *, stop=None, probe=None, neighbors=None, interface_lookup=None, clock=None, monotonic=None):
    stop = stop or threading.Event()
    clock, monotonic = clock or time.time, monotonic or time.monotonic
    started = monotonic()
    local_address, attached = (interface_lookup or interface_network)(config.interface)
    network = ipaddress.IPv4Network(config.subnet)
    if not network.subnet_of(attached) or ipaddress.IPv4Address(local_address) not in attached:
        raise ValueError('Configured subnet is not directly attached to the LAN interface')
    hosts = iter(str(host) for host in network.hosts() if str(host) != local_address)
    connected, pending, exhausted = {}, {}, False
    with ThreadPoolExecutor(max_workers=config.workers, thread_name_prefix='lan-identity-probe') as pool:
        while not stop.is_set() and monotonic() - started < config.scan_timeout - 2 - config.probe_timeout:
            while not exhausted and len(pending) < config.workers:
                host = next(hosts, None)
                if host is None:
                    exhausted = True
                    break
                pending[pool.submit(probe or _probe, host, local_address, config.probe_timeout)] = host
            if not pending:
                break
            completed, _ = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
            for future in completed:
                host = pending.pop(future)
                if future.result() is True:
                    connected[host] = clock()
        for future in pending:
            future.cancel()
    if stop.is_set():
        return None

    def read_rows():
        remaining = started + config.scan_timeout - monotonic()
        if remaining <= 0.05:
            return []
        values = (neighbors or collect_neighbors)(config.interface, config.subnet, max_age=120, timeout=min(2, remaining))
        if not isinstance(values, list) or len(values) > 1024:
            raise ValueError('Invalid neighbour snapshot')
        validated = []
        for row in values:
            if not isinstance(row, dict):
                continue
            try:
                host = ipaddress.IPv4Address(row.get('host', ''))
                mac = _mac(row.get('mac'))
            except (TypeError, ValueError):
                continue
            age = row.get('confirmed_age')
            if host not in network or host in (network.network_address, network.broadcast_address):
                continue
            if isinstance(age, bool) or not isinstance(age, (int, float)) or not math.isfinite(age) or not 0 <= age <= 120:
                continue
            validated.append({'host': str(host), 'mac': mac, 'confirmed_age': age})
        return validated

    rows = read_rows()
    # Early successes can become STALE while the rest of the /24 is scanned.
    # Refresh only missing, already-connected candidates, never the whole LAN.
    missing = sorted(set(connected) - {row['host'] for row in rows}, key=ipaddress.IPv4Address)[:config.workers]
    if missing and not stop.is_set() and monotonic() - started < config.scan_timeout - 2 - config.probe_timeout:
        with ThreadPoolExecutor(max_workers=config.workers, thread_name_prefix='lan-identity-refresh') as pool:
            refreshed = {pool.submit(probe or _probe, host, local_address, config.probe_timeout): host for host in missing}
            for future, host in refreshed.items():
                connected.pop(host, None)
                if future.result() is True:
                    connected[host] = clock()
        # Retain earlier rows as well so an observed MAC change stays a conflict.
        rows.extend(read_rows())
    if stop.is_set():
        return None
    observed = clock()
    records, by_host, by_mac = [], {}, {}
    for row in rows:
        host, mac = row['host'], row['mac']
        by_host.setdefault(host, set()).add(mac)
        by_mac.setdefault(mac, set()).add(host)
    conflicting_hosts = {host for host, values in by_host.items() if len(values) != 1}
    conflicting_macs = {mac for mac, values in by_mac.items() if len(values) != 1}
    for row in rows:
        host, mac = row['host'], row['mac']
        if host not in connected or host in conflicting_hosts or mac in conflicting_macs:
            continue
        if not any(item['host'] == host for item in records):
            records.append({'host': host, 'mac': mac, 'observed_at': connected[host], 'evidence': 'tcp+netlink'})
    return {'version': 1, 'subnet': config.subnet, 'interface': config.interface,
            'observed_at': observed, 'scan_id': uuid.uuid4().hex,
            'records': sorted(records, key=lambda item: int(ipaddress.IPv4Address(item['host']))),
            'conflicts': sorted(conflicting_macs | {mac for host in conflicting_hosts for mac in by_host[host]}), 'error': None}


def verify_identity(config, host, mac, *, stop=None, probe=None, neighbors=None,
                    interface_lookup=None, clock=None, monotonic=None):
    """Fresh, bounded proof for one configured-subnet host before SDK login."""
    network = ipaddress.IPv4Network(config.subnet)
    address = ipaddress.IPv4Address(host)
    if address not in network or address in (network.network_address, network.broadcast_address):
        raise ValueError('Verification host is outside the configured LAN subnet')
    expected = _mac(mac)
    local_address, attached = (interface_lookup or interface_network)(config.interface)
    if not network.subnet_of(attached) or ipaddress.IPv4Address(local_address) not in attached or str(address) == local_address:
        raise ValueError('Verification requires a directly attached LAN host')
    stop, clock, monotonic = stop or threading.Event(), clock or time.time, monotonic or time.monotonic
    deadline, actual, verified, proof_at = monotonic() + 3, '', False, None

    def proof():
        remaining = deadline - monotonic()
        if remaining <= 0.05:
            return None
        rows = (neighbors or collect_neighbors)(config.interface, config.subnet, max_age=120, timeout=min(0.5, remaining))
        found, mac_hosts = set(), {}
        if not isinstance(rows, list) or len(rows) > 1024:
            raise ValueError('Invalid neighbour snapshot')
        for row in rows:
            if not isinstance(row, dict):
                continue
            age = row.get('confirmed_age')
            if isinstance(age, bool) or not isinstance(age, (int, float)) or not math.isfinite(age) or not 0 <= age <= 120:
                continue
            try:
                row_host = ipaddress.IPv4Address(row.get('host', ''))
                row_mac = _mac(row.get('mac'))
            except (TypeError, ValueError):
                continue
            if row_host not in network or row_host in (network.network_address, network.broadcast_address):
                continue
            mac_hosts.setdefault(row_mac, set()).add(str(row_host))
            if row_host == address:
                found.add(row_mac)
        return found, mac_hosts

    for _ in range(4):
        if stop.is_set() or deadline - monotonic() <= 0.1:
            break
        before = proof()
        remaining = deadline - monotonic()
        if before is None or remaining <= 0.05:
            break
        connected = (probe or _probe)(str(address), local_address, min(0.5, remaining)) is True
        if connected:
            proof_at = clock()
        after = proof()
        if after is None:
            break
        old_macs, old_hosts = before
        new_macs, new_hosts = after
        if len(old_macs) > 1 or len(new_macs) > 1 or (old_macs and new_macs and old_macs != new_macs):
            break
        if len(old_macs) == len(new_macs) == 1 and connected:
            candidate = next(iter(new_macs))
            if len(old_hosts[candidate]) != 1 or len(new_hosts[candidate]) != 1:
                break
            actual, verified = candidate, candidate == expected
            break
        if stop.wait(min(0.2, max(0, deadline - monotonic()))):
            break
    return {'version': 1, 'host': str(address), 'mac': actual, 'verified': verified,
            'observed_at': proof_at if proof_at is not None else clock()}


class IdentityState:
    def __init__(self, config, *, scanner=None, clock=None):
        self.config, self.scanner = config, scanner or scan_identity
        self.clock, self.proven = clock or time.time, {}
        self.stop = threading.Event()
        self.lock, self.thread = threading.Lock(), None
        self.data = {'version': 1, 'subnet': config.subnet, 'interface': config.interface,
                     'observed_at': 0, 'scan_id': '', 'records': [], 'conflicts': [], 'error': 'observer_pending'}

    def snapshot(self):
        with self.lock:
            self._prune_proofs()
            result = copy.deepcopy(self.data)
            result['records'] = sorted(copy.deepcopy(list(self.proven.values())), key=lambda row: ipaddress.IPv4Address(row['host']))
            if result['records'] and not result.get('conflicts'):
                result['observed_at'] = max(row['observed_at'] for row in result['records'])
                result['error'] = None
            return result

    def _prune_proofs(self):
        now = self.clock()
        self.proven = {mac: row for mac, row in self.proven.items() if -5 <= now - row['observed_at'] <= 120}

    def _remember_records(self, records):
        self._prune_proofs()
        network = ipaddress.IPv4Network(self.config.subnet)
        if not isinstance(records, list) or len(records) > 254:
            return
        for row in records:
            try:
                host = ipaddress.IPv4Address(row['host'])
                mac, stamp = _mac(row['mac']), row['observed_at']
                if host not in network or host in (network.network_address, network.broadcast_address):
                    continue
                if type(stamp) not in (int, float) or not math.isfinite(stamp) or not -5 <= self.clock() - stamp <= 120 or row.get('evidence') != 'tcp+netlink':
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            if any(old['host'] == str(host) and old_mac != mac and old['observed_at'] > stamp
                   for old_mac, old in self.proven.items()):
                continue
            # A newer proved owner of an address replaces its older association.
            for old_mac, old in list(self.proven.items()):
                if old['host'] == str(host) and old_mac != mac:
                    self.proven.pop(old_mac)
            previous = self.proven.get(mac)
            if previous is None or stamp >= previous['observed_at']:
                self.proven[mac] = {'host': str(host), 'mac': mac, 'observed_at': stamp, 'evidence': 'tcp+netlink'}

    def remember_proof(self, proof, expected_mac):
        with self.lock:
            if not isinstance(proof, dict) or proof.get('version') != 1:
                return
            if proof.get('verified') is True:
                if proof.get('mac') != expected_mac:
                    return
                self._remember_records([{**proof, 'evidence': 'tcp+netlink'}])
            else:
                previous = self.proven.get(expected_mac)
                if previous is not None and previous['host'] == proof.get('host'):
                    self.proven.pop(expected_mac)

    def _apply_scan(self, result):
        with self.lock:
            self._remember_records(result.get('records', []))
            conflicts = result.get('conflicts', [])
            for mac in conflicts:
                self.proven.pop(mac, None)
            seen = {row.get('mac') for row in result.get('records', []) if isinstance(row, dict)}
            missing = [dict(row) for mac, row in self.proven.items() if mac not in seen][:16]
        # Directory scans may miss a short-lived neighbour-state transition.
        # Refresh only previously proved missing pairs using the strict guard.
        if missing and not self.stop.is_set():
            with ThreadPoolExecutor(max_workers=self.config.workers, thread_name_prefix='lan-identity-known') as pool:
                pending = {pool.submit(verify_identity, self.config, row['host'], row['mac'], stop=self.stop): row for row in missing}
                for future, row in pending.items():
                    try:
                        proof = future.result()
                    except Exception:
                        continue
                    self.remember_proof(proof, row['mac'])
        with self.lock:
            self.data = result

    def _run(self):
        while not self.stop.is_set():
            try:
                result = self.scanner(self.config, stop=self.stop)
            except Exception:
                result = {'version': 1, 'subnet': self.config.subnet, 'interface': self.config.interface,
                          'observed_at': time.time(), 'scan_id': uuid.uuid4().hex,
                          'records': [], 'conflicts': [], 'error': 'observer_scan_failed'}
            if result is not None:
                self._apply_scan(result)
            self.stop.wait(self.config.interval)

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True, name='lan-identity')
            self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(self.config.scan_timeout + 2)


class IdentityServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, state):
        self.state, self.slots = state, threading.BoundedSemaphore(8)
        super().__init__(address, IdentityHandler)

    def process_request(self, request, client_address):
        request.settimeout(3)
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Neither authorization values nor remote exception text reach logs.
        pass


class IdentityHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _json(self, status, payload):
        raw = json.dumps(payload, separators=(',', ':'), ensure_ascii=True).encode('ascii')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        self.wfile.write(raw)

    def _auth(self):
        headers = self.headers.get_all('Authorization', [])
        expected = ('Bearer ' + self.server.state.config.token).encode('ascii')
        if len(headers) != 1 or not hmac.compare_digest(headers[0].encode('utf-8'), expected):
            self._json(401, {'error': 'observer_unauthorized'})
            return False
        return True

    def do_GET(self):
        if self.path == '/health':
            self._json(200, {'ok': True})
            return
        if not self._auth():
            return
        if self.path != '/v1/neighbors':
            self._json(404, {'error': 'observer_not_found'})
        else:
            self._json(200, self.server.state.snapshot())

    def do_POST(self):
        if not self._auth():
            return
        if self.path != '/v1/verify':
            self._json(404, {'error': 'observer_not_found'})
            return
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') or len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,5}', lengths[0]):
            self._json(400, {'error': 'observer_invalid_request'})
            return
        length = int(lengths[0])
        if length > 1024:
            self._json(413, {'error': 'observer_body_too_large'})
            return
        if self.headers.get_content_type() != 'application/json':
            self._json(400, {'error': 'observer_invalid_request'})
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('Incomplete verification request')
            def unique_fields(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('Duplicate verification field')
                    result[key] = value
                return result
            body = json.loads(raw, object_pairs_hook=unique_fields)
            if not isinstance(body, dict) or set(body) != {'host', 'mac'} or not all(isinstance(value, str) for value in body.values()):
                raise ValueError('Invalid verification request')
            result = verify_identity(self.server.state.config, body['host'], body['mac'], stop=self.server.state.stop)
        except (ValueError, TypeError, UnicodeError):
            self._json(400, {'error': 'observer_invalid_request'})
        except OSError:
            self._json(503, {'error': 'observer_verification_unavailable'})
        else:
            self.server.state.remember_proof(result, body['mac'])
            self._json(200, result)


def serve_identity(stop=None):
    config = ObserverConfig.from_env()
    state = IdentityState(config)
    stop, previous = stop or threading.Event(), {}
    server = IdentityServer((config.bind, config.port), state)
    server.timeout = 0.5
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        state.start()
        while not stop.is_set():
            server.handle_request()
    finally:
        server.server_close()
        state.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
