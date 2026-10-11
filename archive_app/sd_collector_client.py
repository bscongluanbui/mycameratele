"""Authenticated LAN SDK sessions; only bounded recording bytes cross the VPN.

This adapter never touches the archive database or Telegram.  A configured
collector is authoritative: errors do not silently retry through the WAN SDK.
"""
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import re
import socket
import ssl
import time
from urllib.parse import urlsplit

from .core import parse_time, validate_sd_collector_url
from .sd_source import (DOWNLOAD_PROGRESS_INTERVAL, MAX_NATIVE_FILE,
                        MAX_SEARCH_RESULTS, SDSourceError, _download_speed,
                        _file_progress)


MAX_JSON = 2_000_000
SESSION_ID = re.compile(r'[a-f0-9]{32}')
CAMERA_FIELDS = ('id', 'host', 'device_port', 'sd_username', 'sd_password',
                 'sd_channel', 'sd_timezone')
ERROR_MESSAGES = {
    'sd_collector_auth_failed': 'LAN collector rejected its access token.',
    'sd_collector_network_error': 'LAN collector connection failed or timed out.',
    'sd_collector_protocol_error': 'LAN collector returned an invalid response.',
    'sd_collector_redirect_rejected': 'LAN collector redirected its request.',
    'sd_collector_session_missing': 'LAN collector session expired; retry the camera job.',
    'sd_collector_recording_not_searched': 'LAN collector recording metadata expired; search the camera again.',
    'sd_collector_busy': 'LAN collector is busy; retry the camera job.',
    'sd_collector_error': 'LAN collector did not complete the SDK operation.',
    'sd_collector_budget': 'LAN collector staging budget is full; retry after active files finish.',
    'sd_sdk_missing': 'Official Linux HCNetSDK package is missing on the LAN collector.',
    'sd_sdk_arch_mismatch': 'LAN collector HCNetSDK architecture does not match its host.',
    'sd_sdk_load_failed': 'LAN collector HCNetSDK dependencies did not load.',
    'sd_sdk_incompatible': 'LAN collector HCNetSDK lacks a required recording function.',
    'sd_sdk_path_invalid': 'LAN collector SDK path is invalid.',
    'sd_credentials_missing': 'Set the camera SD username and verification code.',
    'sd_auth_failed': 'Camera rejected the SD credentials or recording permission.',
    'sd_native_unsupported': 'Camera rejected the native SD protocol or channel.',
    'sd_native_error': 'LAN camera native SD operation failed.',
    'sd_native_worker_failed': 'LAN collector native SDK helper stopped unexpectedly.',
    'sd_native_worker_timeout': 'LAN collector native SDK operation timed out.',
    'sd_search_timeout': 'LAN camera SD search timed out.',
    'sd_search_limit': 'LAN camera search exceeded its recording limit.',
    'sd_protocol_error': 'LAN camera returned invalid recording metadata.',
    'sd_size_limit': 'SD recording exceeded its staging limit.',
    'sd_download_incomplete': 'SD recording transfer ended with missing bytes.',
    'sd_download_timeout': 'SD recording transfer timed out.',
    'sd_stage_invalid': 'SD recording staging path is invalid.',
}


def _error(code):
    return SDSourceError(code, ERROR_MESSAGES.get(code, ERROR_MESSAGES['sd_collector_error']))


def collector_endpoint(value):
    """IP literals only: no redirects, environment proxies or DNS rebinding."""
    try:
        endpoint = validate_sd_collector_url(value)
        if not endpoint:
            raise ValueError
        parsed = urlsplit(endpoint)
        return parsed.scheme, parsed.hostname, parsed.port
    except (ValueError, TypeError):
        raise SDSourceError('sd_config_invalid', 'LAN collector URL must use a private or Tailscale IP endpoint.') from None


class SDCollectorSource:
    backend = 'hcnetsdk-collector'

    def __init__(self, url, token, camera, max_bytes, *, cache_dir,
                 download_speed_kbps=0, timeout=1900):
        self.scheme, self.host, self.port = collector_endpoint(url)
        if (not isinstance(token, str) or not 32 <= len(token) <= 512
                or not token.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in token)):
            raise SDSourceError('sd_config_invalid', 'Configure a valid LAN collector access token.')
        if (type(max_bytes) is not int or not 0 < max_bytes <= MAX_NATIVE_FILE
                or type(timeout) not in (int, float) or not 0 < timeout <= 1900):
            raise SDSourceError('sd_config_invalid', 'Invalid LAN collector transfer limit.')
        self.token, self.max_bytes, self.timeout = token, max_bytes, float(timeout)
        self.camera = {field: camera[field] for field in CAMERA_FIELDS}
        self.cache_dir = Path(cache_dir).resolve()
        self.session_id = None
        self.download_progress = None
        self.download_speed_requested_kbps = _download_speed(download_speed_kbps)
        self.download_speed_status = 'pending' if download_speed_kbps else 'off'
        self.download_speed_error_code = None

    @staticmethod
    def _read(response, connection, size, deadline):
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        transport = connection.sock
        if transport is None:
            transport = getattr(getattr(response.fp, 'raw', None), '_sock', None)
        if transport is not None:
            transport.settimeout(remaining)
        return response.read(size)

    @contextmanager
    def _request(self, method, path, payload=None, *, timeout=None):
        # http.client deliberately has no environment-proxy or redirect support.
        duration = min(self.timeout, timeout or self.timeout)
        deadline = time.monotonic()+duration
        connection_type = http.client.HTTPSConnection if self.scheme == 'https' else http.client.HTTPConnection
        connection = connection_type(self.host, self.port, timeout=duration)
        response = None
        try:
            data = json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode() if payload is not None else None
            connection.request(method, path, body=data, headers={
                'Authorization': 'Bearer '+self.token, 'Content-Type': 'application/json',
                'Accept': '*/*', 'Accept-Encoding': 'identity', 'Connection': 'close'})
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise _error('sd_collector_redirect_rejected')
            if response.status != 200:
                code = {401: 'sd_collector_auth_failed', 403: 'sd_collector_auth_failed',
                        404: 'sd_collector_session_missing', 409: 'sd_collector_busy',
                        413: 'sd_size_limit', 507: 'sd_collector_budget'}.get(response.status, 'sd_collector_error')
                if response.status not in (401, 403):
                    try:
                        detail = json.loads(self._read(response, connection, 8193, deadline))
                        reported = detail.get('error') if isinstance(detail, dict) else None
                        if reported in ERROR_MESSAGES:
                            code = reported
                    except (ValueError, TypeError):
                        pass
                raise _error(code)
            yield response, connection, deadline
        except SDSourceError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException, ssl.SSLError, socket.timeout):
            raise _error('sd_collector_network_error') from None
        except (ValueError, TypeError):
            raise _error('sd_collector_protocol_error') from None
        finally:
            if response is not None:
                response.close()
            connection.close()

    def _json(self, method, path, payload=None, *, timeout=130):
        with self._request(method, path, payload, timeout=timeout) as (response, connection, deadline):
            if response.getheader('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                raise _error('sd_collector_protocol_error')
            raw = self._read(response, connection, MAX_JSON+1, deadline)
            if len(raw) > MAX_JSON:
                raise _error('sd_collector_protocol_error')
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeError):
                raise _error('sd_collector_protocol_error') from None
            if not isinstance(result, dict):
                raise _error('sd_collector_protocol_error')
            return result

    def __enter__(self):
        if self.session_id is not None:
            raise _error('sd_collector_protocol_error')
        try:
            result = self._json('POST', '/v1/sessions', {
                'camera': self.camera, 'max_bytes': self.max_bytes,
                'download_speed_kbps': self.download_speed_requested_kbps}, timeout=40)
        finally:
            self.camera['sd_password'] = ''
        session = result.get('session_id')
        if not isinstance(session, str) or SESSION_ID.fullmatch(session) is None:
            raise _error('sd_collector_protocol_error')
        self.session_id = session
        return self

    def __exit__(self, *_):
        session, self.session_id = self.session_id, None
        self.camera['sd_password'] = ''
        if session is not None:
            try:
                self._json('DELETE', '/v1/sessions/'+session, timeout=10)
            except SDSourceError:
                # A close failure must not hide an earlier transfer failure or
                # undo an already verified download; collector TTL is bounded.
                pass
        return False

    def _path(self, operation):
        if self.session_id is None:
            raise _error('sd_collector_session_missing')
        return '/v1/sessions/'+self.session_id+'/'+operation

    def search(self, start, end):
        result = self._json('POST', self._path('search'), {'start': start.isoformat(), 'end': end.isoformat()})
        recordings = result.get('recordings')
        if not isinstance(recordings, list) or len(recordings) > MAX_SEARCH_RESULTS:
            raise _error('sd_collector_protocol_error')
        seen = set()
        for recording in recordings:
            try:
                rid = recording['record_id']
                first, last = parse_time(recording['start_time']), parse_time(recording['end_time'])
                size = recording['size']
                if (not isinstance(rid, str) or not rid or len(rid.encode()) >= 100
                        or '\x00' in rid or type(size) is not int or not 0 <= size <= MAX_NATIVE_FILE
                        or last <= first or (rid, first) in seen):
                    raise ValueError
                seen.add((rid, first))
            except (KeyError, TypeError, ValueError, OverflowError):
                raise _error('sd_collector_protocol_error') from None
        return recordings

    def _speed_headers(self, response):
        status = response.getheader('X-SD-Speed-Status', '')
        raw_error = response.getheader('X-SD-Speed-Error', '')
        raw_speed = response.getheader('X-SD-Speed-Kbps', '')
        try:
            error = int(raw_error) if raw_error else None
            if (status not in ('off', 'applied', 'unsupported', 'rejected')
                    or not re.fullmatch(r'[0-9]+', raw_speed)
                    or int(raw_speed) != self.download_speed_requested_kbps
                    or error is not None and not 0 <= error <= 0xffffffff
                    or status in ('off', 'applied') and error is not None
                    or status in ('unsupported', 'rejected') and error is None
                    or status == 'unsupported' and error != 23):
                raise ValueError
        except (ValueError, TypeError):
            raise _error('sd_collector_protocol_error') from None
        self.download_speed_status, self.download_speed_error_code = status, error

    def download(self, recording, destination):
        destination = Path(destination)
        if (not destination.is_absolute() or destination.is_symlink() or destination.exists()
                or not destination.parent.resolve().is_relative_to(self.cache_dir/'sd-stage')):
            raise _error('sd_stage_invalid')
        if type(self.max_bytes) is not int or not 0 < self.max_bytes <= MAX_NATIVE_FILE:
            raise _error('sd_size_limit')
        started, last_progress = time.monotonic(), None
        created, complete, received = False, False, 0
        try:
            with self._request('POST', self._path('download'), {
                    'recording': recording, 'max_bytes': self.max_bytes}) as (response, connection, deadline):
                lengths = response.headers.get_all('Content-Length', [])
                if (len(lengths) != 1 or re.fullmatch(r'[0-9]+', lengths[0]) is None
                        or response.getheader('Transfer-Encoding') is not None
                        or response.getheader('Content-Encoding', 'identity') != 'identity'
                        or response.getheader('Content-Type', '').split(';')[0].strip().lower() != 'application/octet-stream'):
                    raise _error('sd_collector_protocol_error')
                length = int(lengths[0])
                if length <= 0 or length > self.max_bytes:
                    raise _error('sd_size_limit')
                if recording.get('size', 0) > 0 and length != recording['size']:
                    raise _error('sd_download_incomplete')
                self._speed_headers(response)
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                created = True
                with os.fdopen(descriptor, 'wb') as output:
                    while received < length:
                        block = self._read(response, connection, min(1024*1024, length-received), deadline)
                        if not block:
                            raise _error('sd_download_incomplete')
                        output.write(block)
                        received += len(block)
                        now = time.monotonic()
                        if callable(self.download_progress) and (last_progress is None or now-last_progress >= DOWNLOAD_PROGRESS_INTERVAL):
                            output.flush()
                            self.download_progress(dict(_file_progress(destination, recording, started),
                                download_speed_requested_kbps=self.download_speed_requested_kbps,
                                download_speed_status=self.download_speed_status,
                                download_speed_error_code=self.download_speed_error_code))
                            last_progress = now
                    output.flush()
                    os.fsync(output.fileno())
                if destination.stat().st_size != length:
                    raise _error('sd_download_incomplete')
                complete = True
            return received
        finally:
            if created and not complete:
                destination.unlink(missing_ok=True)
