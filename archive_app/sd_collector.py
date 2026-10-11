"""Bounded LAN-native SD collector: original bytes only, no catalog or Telegram.

The bearer-authenticated endpoint belongs on a private/Tailscale address. Camera
credentials remain in request memory and the anonymous native-helper pipe.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import threading
import time
import uuid

from .core import get_zone, parse_time
from .sd_source import (HCNetSDKSource, MAX_NATIVE_FILE, MAX_SEARCH_RESULTS,
                       NATIVE_INIT_TIMEOUT, SDSourceError, _download_speed)

MAX_BODY = 64 * 1024
SESSION_ID = re.compile(r'[a-f0-9]{32}')
NATIVE_CODES = frozenset(('sd_sdk_missing', 'sd_sdk_arch_mismatch', 'sd_sdk_load_failed',
    'sd_sdk_incompatible', 'sd_sdk_path_invalid', 'sd_auth_failed', 'sd_native_unsupported',
    'sd_native_error', 'sd_search_timeout', 'sd_search_limit', 'sd_protocol_error',
    'sd_size_limit', 'sd_download_incomplete', 'sd_download_timeout', 'sd_stage_invalid',
    'sd_native_worker_failed', 'sd_native_worker_timeout'))


class CollectorError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


class _NativeSessionOwner:
    """Keep the native helper's Linux creator thread alive for its session.

    PR_SET_PDEATHSIG is intentionally retained in HCNetSDKSource. Linux watches
    the thread that spawned that child, not merely the collector process. HTTP
    handler threads therefore submit bounded commands to one persistent owner.
    """
    def __init__(self, *args, **kwargs):
        self._arguments, self._keywords = args, kwargs
        self.max_bytes = args[6]
        self.download_speed_requested_kbps = kwargs.get('download_speed_kbps', 0)
        self.download_speed_status = 'pending' if self.download_speed_requested_kbps else 'off'
        self.download_speed_error_code = None
        self._commands = queue.Queue(maxsize=1)
        self._closing, self._stopped, self._busy = threading.Event(), threading.Event(), threading.Event()
        self._provider, self._thread = None, None

    @staticmethod
    def _failed(code='sd_native_worker_failed'):
        return SDSourceError(code, 'Collector native session stopped or exceeded its bounded operation timeout.')

    def _snapshot(self):
        for name in ('download_speed_requested_kbps', 'download_speed_status', 'download_speed_error_code'):
            setattr(self, name, getattr(self._provider, name))

    def _run(self, initialized):
        try:
            self._busy.set()
            try:
                self._provider = HCNetSDKSource(*self._arguments, **self._keywords)
                # Remove the proxy's duplicate credential reference after construction.
                self._arguments, self._keywords = (), {}
                if self._closing.is_set():
                    raise self._failed()
                self._provider.__enter__()
                self._snapshot()
                initialized.put_nowait((True, None))
            except Exception as error:
                initialized.put_nowait((False, error if isinstance(error, SDSourceError) else self._failed()))
                return
            finally:
                self._busy.clear()
            while not self._closing.is_set():
                try:
                    operation, arguments, maximum, result = self._commands.get(timeout=0.2)
                except queue.Empty:
                    continue
                self._busy.set()
                try:
                    if self._closing.is_set():
                        raise self._failed()
                    self._provider.max_bytes = maximum
                    value = getattr(self._provider, operation)(*arguments)
                    self._snapshot()
                    result.put_nowait((True, value))
                except Exception as error:
                    result.put_nowait((False, error if isinstance(error, SDSourceError) else self._failed()))
                finally:
                    self._busy.clear()
        finally:
            self._arguments, self._keywords = (), {}
            if self._provider is not None:
                try:
                    self._provider.__exit__(None, None, None)
                except Exception:
                    pass
            self._stopped.set()
            # Wake a submitted request if shutdown raced with queue admission.
            while True:
                try:
                    *_, result = self._commands.get_nowait()
                    result.put_nowait((False, self._failed()))
                except queue.Empty:
                    break

    def _wait(self, result, timeout):
        deadline = time.monotonic() + timeout
        while True:
            try:
                successful, value = result.get(timeout=min(0.2, max(0.001, deadline-time.monotonic())))
                if successful:
                    return value
                raise value
            except queue.Empty:
                if self._stopped.is_set():
                    raise self._failed()
                if time.monotonic() >= deadline:
                    self.__exit__(None, None, None)
                    raise self._failed('sd_native_worker_timeout')

    def __enter__(self):
        if self._thread is not None or self._closing.is_set():
            raise self._failed()
        initialized = queue.Queue(maxsize=1)
        self._thread = threading.Thread(target=self._run, args=(initialized,),
            name='sd-sdk-owner-' + uuid.uuid4().hex, daemon=False)
        self._thread.start()
        self._wait(initialized, NATIVE_INIT_TIMEOUT + 10)
        return self

    def _call(self, operation, arguments, timeout):
        if self._thread is None or self._closing.is_set() or self._stopped.is_set():
            raise self._failed()
        result = queue.Queue(maxsize=1)
        try:
            self._commands.put((operation, arguments, self.max_bytes, result), timeout=1)
        except queue.Full:
            raise self._failed() from None
        return self._wait(result, timeout)

    def search(self, start, end):
        return self._call('search', (start, end), 135)

    def download(self, recording, destination):
        return self._call('download', (recording, destination), 1810)

    def __exit__(self, *_):
        self._closing.set()
        # Do not queue close behind a potentially 1800-second active download.
        # Killing only its isolated child wakes _exchange; SDK exit itself still
        # runs on the owner thread. Its parent-death signal remains enabled.
        if self._busy.is_set() and self._provider is not None:
            process = getattr(self._provider, 'process', None)
            if process is not None:
                try:
                    if process.poll() is None:
                        process.kill()
                except OSError:
                    pass
        if self._thread is not None and self._thread is not threading.current_thread():
            # Leave cleanup margin beyond native init if shutdown raced spawn.
            self._thread.join(timeout=NATIVE_INIT_TIMEOUT + 10)
            if self._thread.is_alive():
                raise self._failed('sd_native_worker_timeout')
        return False


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise CollectorError('sd_collector_invalid_request')
    return value


def _env_integer(name, default, minimum, maximum):
    try:
        return _integer(int(os.environ.get(name, str(default))), minimum, maximum)
    except (ValueError, CollectorError):
        raise ValueError(f'Invalid collector setting: {name}') from None


@dataclass(frozen=True)
class CollectorConfig:
    token: str = field(repr=False)
    cache_dir: Path
    allowed_cidrs: tuple = (ipaddress.ip_network('192.168.2.0/24'),)
    allowed_ports: tuple = (8000,)
    cache_max_bytes: int = 2 * 1024**3
    min_free_bytes: int = 2 * 1024**3
    idle_timeout: int = 900
    max_sessions: int = 10
    per_camera_sessions: int = 2

    def __post_init__(self):
        if (not isinstance(self.token, str) or not self.token.isascii() or
                not 32 <= len(self.token) <= 512 or any(ch.isspace() for ch in self.token)):
            raise ValueError('Collector token requires 32 to 512 non-whitespace ASCII characters')
        if not self.allowed_cidrs or not self.allowed_ports:
            raise ValueError('Collector camera allowlist must not be empty')
        for network in self.allowed_cidrs:
            if (not isinstance(network, (ipaddress.IPv4Network, ipaddress.IPv6Network)) or
                    not network.is_private or network.prefixlen == 0 or network.network_address.is_multicast or
                    network.network_address.is_unspecified):
                raise ValueError('Invalid collector camera subnet')
        try:
            for port in self.allowed_ports:
                _integer(port, 1, 65535)
            _integer(self.cache_max_bytes, 1, 16 * 1024**3)
            _integer(self.min_free_bytes, 0, 16 * 1024**3)
            _integer(self.idle_timeout, 1, 3600)
            _integer(self.max_sessions, 1, 10)
            _integer(self.per_camera_sessions, 1, 2)
        except CollectorError:
            raise ValueError('Invalid collector budget, timeout or session limit') from None

    @classmethod
    def from_env(cls):
        token = os.environ.get('SD_COLLECTOR_TOKEN', '')
        token_file = os.environ.get('SD_COLLECTOR_TOKEN_FILE', '')
        if token_file:
            if token:
                raise ValueError('Set one collector token source')
            try:
                with Path(token_file).open(encoding='utf-8') as handle:
                    token = handle.read(514).strip()
                if len(token) > 512:
                    raise ValueError()
            except (OSError, UnicodeError, ValueError):
                raise ValueError('Invalid collector token file') from None
        try:
            networks = tuple(ipaddress.ip_network(item.strip(), strict=True) for item in
                os.environ.get('COLLECTOR_ALLOWED_CIDRS', '192.168.2.0/24').split(',') if item.strip())
            ports = tuple(int(item.strip()) for item in
                os.environ.get('COLLECTOR_ALLOWED_PORTS', '8000').split(',') if item.strip())
        except ValueError:
            raise ValueError('Invalid collector camera allowlist') from None
        return cls(token=token, cache_dir=Path(os.environ.get('COLLECTOR_CACHE_DIR', os.environ.get('CACHE_DIR', './cache'))),
            allowed_cidrs=networks, allowed_ports=ports,
            cache_max_bytes=_env_integer('COLLECTOR_CACHE_MAX_GB', 2, 1, 16) * 1024**3,
            min_free_bytes=_env_integer('COLLECTOR_MIN_FREE_GB', 2, 0, 16) * 1024**3,
            idle_timeout=_env_integer('COLLECTOR_IDLE_TIMEOUT_SECONDS', 900, 1, 3600),
            max_sessions=_env_integer('COLLECTOR_MAX_SESSIONS', 10, 1, 10),
            per_camera_sessions=_env_integer('COLLECTOR_PER_CAMERA_SESSIONS', 2, 1, 2))


@dataclass
class _Session:
    identity: tuple
    scope: tuple
    maximum: int
    provider: object = None
    lock: object = field(default_factory=threading.Lock)
    touched: float = field(default_factory=time.monotonic)
    recordings: dict = field(default_factory=dict)


class CollectorState:
    def __init__(self, config, provider_factory=None, clock=time.monotonic):
        self.config, self.clock = config, clock
        self.provider_factory = provider_factory or _NativeSessionOwner
        self.lock, self.sessions, self.reservations = threading.RLock(), {}, {}
        # Serialize expensive vendor initialization with bounded admission.
        # Admission never queues HTTP handlers: clients
        # retry this short busy response, while established transfers continue.
        self.init_gate = threading.Lock()
        self.metadata, self.active_paths = {}, set()
        self.closing = False
        cache = Path(config.cache_dir)
        if cache.is_symlink() or getattr(cache, 'is_junction', lambda: False)():
            raise ValueError('Collector cache must not be a link')
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cache = cache.resolve()
        stage = self.cache / 'sd-stage'
        self.root = stage / 'collector'
        for path in (stage, self.root):
            if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
                raise ValueError('Collector staging must not be a link')
            path.mkdir(exist_ok=True, mode=0o700)
        if not self.root.resolve().is_relative_to(self.cache):
            raise ValueError('Invalid collector staging path')
        # Only this collector's random staging files are eligible on restart.
        for path in self.root.iterdir():
            if re.fullmatch(r'[a-f0-9]{32}\.part', path.name) and path.is_file() and not path.is_symlink():
                path.unlink()

    def _camera(self, value):
        expected = {'id', 'host', 'device_port', 'sd_username', 'sd_password', 'sd_channel', 'sd_timezone'}
        if not isinstance(value, dict) or set(value) != expected:
            raise CollectorError('sd_collector_invalid_request')
        if not isinstance(value['id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value['id']):
            raise CollectorError('sd_collector_invalid_request')
        try:
            address = ipaddress.ip_address(value['host'])
            if (not address.is_private or address.is_multicast or address.is_unspecified or
                    address.is_loopback or address.is_link_local or
                    not any(address in net for net in self.config.allowed_cidrs if address.version == net.version)):
                raise ValueError()
            zone = get_zone(value['sd_timezone'])
        except (TypeError, ValueError, KeyError):
            raise CollectorError('sd_collector_camera_not_allowed') from None
        port = _integer(value['device_port'], 1, 65535)
        if port not in self.config.allowed_ports:
            raise CollectorError('sd_collector_camera_not_allowed')
        channel = _integer(value['sd_channel'], 1, 9999)
        for name in ('sd_username', 'sd_password'):
            text = value[name]
            if not isinstance(text, str) or not text or '\x00' in text or len(text.encode()) > 64:
                raise CollectorError('sd_collector_invalid_request')
        return str(address), port, channel, zone

    def create(self, body):
        if not isinstance(body, dict) or set(body) != {'camera', 'max_bytes', 'download_speed_kbps'}:
            raise CollectorError('sd_collector_invalid_request')
        camera = body['camera']
        address, port, channel, zone = self._camera(camera)
        maximum = _integer(body['max_bytes'], 1, MAX_NATIVE_FILE)
        try:
            speed = _download_speed(body['download_speed_kbps'])
        except SDSourceError:
            raise CollectorError('sd_collector_invalid_request') from None
        if not self.init_gate.acquire(blocking=False):
            raise CollectorError('sd_collector_session_busy', 409)
        try:
            identity, sid = (address, port, channel), uuid.uuid4().hex
            fingerprint = hmac.digest(self.config.token.encode(),
                json.dumps([camera['sd_username'], camera['sd_password']], separators=(',', ':')).encode(), 'sha256')
            session = _Session(identity=identity, scope=(*identity, fingerprint), maximum=maximum, touched=self.clock())
            session.lock.acquire()
            try:
                with self.lock:
                    if self.closing:
                        raise CollectorError('sd_collector_stopping', 503)
                    if (len(self.sessions) >= self.config.max_sessions or
                            sum(item.identity == identity for item in self.sessions.values()) >= self.config.per_camera_sessions):
                        raise CollectorError('sd_collector_session_limit', 409)
                    self.sessions[sid] = session
                session.provider = self.provider_factory(address, port, camera['sd_username'], camera['sd_password'],
                    channel, zone, maximum, cache_dir=self.cache, download_speed_kbps=speed)
                session.provider.__enter__()
                with self.lock:
                    if self.closing or self.sessions.get(sid) is not session:
                        raise CollectorError('sd_collector_stopping', 503)
                return sid
            except BaseException:
                with self.lock:
                    self.sessions.pop(sid, None)
                if session.provider is not None:
                    session.provider.__exit__(None, None, None)
                raise
            finally:
                session.touched = self.clock()
                session.lock.release()
        finally:
            self.init_gate.release()

    @contextmanager
    def use(self, sid):
        with self.lock:
            session = self.sessions.get(sid)
            if session is None or self.closing:
                raise CollectorError('sd_collector_session_missing', 404)
            if not session.lock.acquire(blocking=False):
                raise CollectorError('sd_collector_session_busy', 409)
        try:
            yield session
        finally:
            session.touched = self.clock()
            session.lock.release()

    def close_session(self, sid):
        with self.use(sid) as session:
            with self.lock:
                self.sessions.pop(sid, None)
            session.provider.__exit__(None, None, None)

    @staticmethod
    def _recording(value):
        if not isinstance(value, dict) or set(value) != {'record_id', 'start_time', 'end_time', 'size'}:
            raise CollectorError('sd_collector_invalid_recording')
        rid = value['record_id']
        if not isinstance(rid, str) or not rid or len(rid.encode()) >= 100 or any(ord(ch) < 32 for ch in rid):
            raise CollectorError('sd_collector_invalid_recording')
        try:
            first, last = parse_time(value['start_time']), parse_time(value['end_time'])
            if not isinstance(value['start_time'], str) or not isinstance(value['end_time'], str) or last <= first:
                raise ValueError()
        except (ValueError, TypeError):
            raise CollectorError('sd_collector_invalid_recording') from None
        _integer(value['size'], 0, MAX_NATIVE_FILE)
        return rid, value['start_time'], value['end_time'], value['size']

    def search(self, session, body):
        if not isinstance(body, dict) or set(body) != {'start', 'end'}:
            raise CollectorError('sd_collector_invalid_request')
        try:
            start, end = parse_time(body['start']), parse_time(body['end'])
            if end <= start or (end - start).total_seconds() > 31 * 86400:
                raise ValueError()
        except (TypeError, ValueError):
            raise CollectorError('sd_collector_invalid_request') from None
        results = session.provider.search(start, end)
        if not isinstance(results, list) or len(results) > MAX_SEARCH_RESULTS:
            raise CollectorError('sd_protocol_error', 502)
        canonical = {}
        for item in results:
            key = self._recording(item)
            if key in canonical:
                raise CollectorError('sd_protocol_error', 502)
            canonical[key] = dict(item)
        session.recordings = canonical
        with self.lock:
            self.metadata[session.scope] = (self.clock(), canonical)
            # Parallel SD sync deliberately closes its search session before
            # opening two download sessions. Reuse only authenticated, bounded,
            # credential-scoped canonical metadata, never arbitrary filenames.
            while len(self.metadata) > self.config.max_sessions:
                oldest = min(self.metadata, key=lambda scope: self.metadata[scope][0])
                self.metadata.pop(oldest)
        return list(canonical.values())

    @contextmanager
    def downloaded(self, session, body):
        if not isinstance(body, dict) or set(body) != {'recording', 'max_bytes'}:
            raise CollectorError('sd_collector_invalid_request')
        key = self._recording(body['recording'])
        with self.lock:
            cached = self.metadata.get(session.scope)
            canonical = cached[1] if cached and self.clock() - cached[0] < self.config.idle_timeout else {}
            recording = canonical.get(key)
            if recording is not None:
                self.metadata[session.scope] = (self.clock(), canonical)
        if recording is None:
            raise CollectorError('sd_collector_recording_not_searched', 409)
        maximum = min(_integer(body['max_bytes'], 1, MAX_NATIVE_FILE), session.maximum)
        if recording['size'] > maximum:
            raise CollectorError('sd_size_limit', 413)
        reserved = recording['size'] or maximum
        path = self.root / f'{uuid.uuid4().hex}.part'
        with self.lock:
            used = sum(item.stat().st_size for item in self.root.iterdir()
                if item.is_file() and not item.is_symlink() and item not in self.reservations)
            outstanding = sum(self.reservations.values())
            if (used + outstanding + reserved > self.config.cache_max_bytes or
                    shutil.disk_usage(self.root).free - outstanding - reserved < self.config.min_free_bytes):
                raise CollectorError('sd_cache_budget', 507)
            self.reservations[path] = reserved
            self.active_paths.add(path)
        try:
            session.provider.max_bytes = reserved
            session.provider.download(recording, path)
            if path.is_symlink() or not path.is_file():
                raise CollectorError('sd_download_incomplete', 502)
            size = path.stat().st_size
            if size <= 0 or size > reserved or recording['size'] and size != recording['size']:
                raise CollectorError('sd_download_incomplete', 502)
            yield path, size
            with self.lock:
                cached = self.metadata.get(session.scope)
                if cached:
                    self.metadata[session.scope] = (self.clock(), cached[1])
        finally:
            # Retain reservation if cleanup fails: subsequent requests must
            # not mistake an undeleted staging file for free disk capacity.
            try:
                path.unlink(missing_ok=True)
            finally:
                with self.lock:
                    self.active_paths.discard(path)
                    if not path.exists():
                        self.reservations.pop(path, None)

    def expire(self):
        now = self.clock()
        closing = []
        with self.lock:
            for scope, (touched, _) in list(self.metadata.items()):
                if now - touched >= self.config.idle_timeout:
                    if any(session.scope == scope and session.lock.locked() for session in self.sessions.values()):
                        self.metadata[scope] = (now, self.metadata[scope][1])
                    else:
                        self.metadata.pop(scope)
            for sid, session in list(self.sessions.items()):
                if now - session.touched < self.config.idle_timeout or not session.lock.acquire(blocking=False):
                    continue
                self.sessions.pop(sid)
                closing.append(session)
        for session in closing:
            try:
                session.provider.__exit__(None, None, None)
            except (OSError, ValueError, SDSourceError):
                pass
            finally:
                session.lock.release()
        with self.lock:
            for path in self.root.iterdir():
                if (path in self.active_paths or not re.fullmatch(r'[a-f0-9]{32}\.part', path.name) or
                        path.is_symlink() or not path.is_file()):
                    continue
                if path not in self.reservations and time.time() - path.stat().st_mtime < self.config.idle_timeout:
                    continue
                try:
                    path.unlink()
                    self.reservations.pop(path, None)
                except OSError:
                    pass

    def close(self):
        with self.lock:
            self.closing = True
            sessions = list(self.sessions.values())
            self.sessions.clear()
        # Native helper exit closes/kills its child within bounded seconds.
        # Closing its pipes also wakes a request currently waiting for data.
        for session in sessions:
            if session.provider is not None:
                try:
                    session.provider.__exit__(None, None, None)
                except (OSError, ValueError, SDSourceError):
                    pass


class CollectorServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, address, state):
        self.state = state
        super().__init__(address, CollectorHandler)

    def handle_error(self, request, client_address):
        # No request bodies, credentials, SDK errors or raw media in logs.
        pass


class CollectorHandler(BaseHTTPRequestHandler):
    server_version, sys_version = 'SDCollector', ''
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *_):
        pass

    def _json(self, status, value):
        raw = json.dumps(value, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        self.wfile.write(raw)

    def _auth(self):
        expected = ('Bearer ' + self.server.state.config.token).encode()
        actual = self.headers.get('Authorization', '').encode()
        if len(self.headers.get_all('Authorization', [])) != 1 or not hmac.compare_digest(actual, expected):
            raise CollectorError('sd_collector_unauthorized', 401)

    def _body(self):
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') or len(lengths) != 1 or not re.fullmatch(r'[0-9]+', lengths[0]):
            raise CollectorError('sd_collector_invalid_request')
        length = int(lengths[0])
        if length > MAX_BODY:
            raise CollectorError('sd_collector_body_too_large', 413)
        if self.headers.get_content_type() != 'application/json':
            raise CollectorError('sd_collector_invalid_request')
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise CollectorError('sd_collector_invalid_request')
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise CollectorError('sd_collector_invalid_request') from None

    def handle_expect_100(self):
        # Authenticate before acknowledging or reading an upload body.
        try:
            self._auth()
        except CollectorError as error:
            self._json(error.status, {'error': error.code})
            return False
        return super().handle_expect_100()

    def do_GET(self):
        self._run('GET')

    def do_POST(self):
        self._run('POST')

    def do_DELETE(self):
        self._run('DELETE')

    def _run(self, method):
        started_response = False
        try:
            if method == 'GET' and self.path == '/health':
                self._json(200, {'ok': True})
                return
            self._auth()
            state = self.server.state
            if method == 'POST' and self.path == '/v1/sessions':
                self._json(200, {'session_id': state.create(self._body())})
                return
            match = re.fullmatch(r'/v1/sessions/([a-f0-9]{32})(?:/(search|download))?', self.path)
            if not match:
                raise CollectorError('sd_collector_not_found', 404)
            sid, action = match.groups()
            if method == 'DELETE' and action is None:
                state.close_session(sid)
                self._json(200, {'closed': True})
                return
            if method != 'POST' or action not in ('search', 'download'):
                raise CollectorError('sd_collector_not_found', 404)
            body = self._body()
            with state.use(sid) as session:
                if action == 'search':
                    self._json(200, {'recordings': state.search(session, body)})
                    return
                with state.downloaded(session, body) as (path, size):
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Length', str(size))
                    self.send_header('Cache-Control', 'no-store')
                    status = getattr(session.provider, 'download_speed_status', 'off')
                    if status not in ('off', 'applied', 'unsupported', 'rejected'):
                        status = 'off'
                    code = getattr(session.provider, 'download_speed_error_code', None)
                    self.send_header('X-SD-Speed-Status', status)
                    self.send_header('X-SD-Speed-Error', str(code) if type(code) is int and 0 <= code <= 0xffffffff else '')
                    self.send_header('X-SD-Speed-Kbps', str(session.provider.download_speed_requested_kbps))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    started_response, self.close_connection = True, True
                    with path.open('rb') as handle:
                        while chunk := handle.read(1024 * 1024):
                            self.wfile.write(chunk)
                    self.wfile.flush()
        except CollectorError as error:
            if not started_response:
                self._json(error.status, {'error': error.code})
        except SDSourceError as error:
            if not started_response:
                self._json(502, {'error': error.code if error.code in NATIVE_CODES else 'sd_native_error'})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            if not started_response:
                self._json(500, {'error': 'sd_collector_error'})
            else:
                self.close_connection = True


def serve(host, port):
    address = ipaddress.ip_address(host)
    # A bridge container listens on 0.0.0.0 internally; Compose publishes
    # only the configured Tailscale host IP. Reject public literal binds.
    if address.is_multicast or (not address.is_unspecified and not address.is_private
            and address not in ipaddress.ip_network('100.64.0.0/10')):
        raise ValueError('Bind collector inside its container or on a private/Tailscale address')
    _integer(port, 1, 65535)
    if address.version != 4:
        raise ValueError('Collector listener requires an IPv4 address')
    state = CollectorState(CollectorConfig.from_env())
    stopped = threading.Event()
    server = None
    previous = {}
    def janitor():
        while not stopped.wait(30):
            try:
                state.expire()
            except OSError:
                pass
    def stop(*_):
        raise KeyboardInterrupt()
    try:
        server = CollectorServer((host, port), state)
        thread = threading.Thread(target=janitor, daemon=True)
        thread.start()
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, stop)
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        state.close()
        if server is not None:
            server.server_close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
