"""SD recording sources, not live-stream capture.

The native protocol uses a separately supplied, architecture-matching official
HCNetSDK. ISAPI is used only when the device returns a genuine CMSearchResult.
Neither an open TCP port nor the camera's model name establishes compatibility.
Only finished downloads enter Archive's existing remux/decode/upload pipeline.
"""
from __future__ import annotations

import ctypes as C
import json
import ipaddress
import os
from pathlib import Path
import platform
import re
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import (HTTPDigestAuthHandler, HTTPPasswordMgrWithDefaultRealm,
                            HTTPRedirectHandler, ProxyHandler, Request, build_opener)
import uuid
import xml.etree.ElementTree as ET

from .core import get_zone, parse_time, record_key


MAX_XML = 2_000_000
MAX_SEARCH_RESULTS = 4000
MAX_NATIVE_FILE = 1024 * 1024 * 1024 - 1  # Avoid SDK's implicit split-file mode.
SETTLE_SECONDS = 120
SDK_LOCK = threading.RLock()
NS = 'http://www.hikvision.com/ver20/XMLSchema'
STAGING_NAME = re.compile(r'[a-f0-9]{64}\.[a-f0-9]{32}\.(?:part|source)')


class SDSourceError(Exception):
    """Only fixed, non-secret descriptions may reach job status/logs."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def recover_staging(archive):
    """Remove abandoned adapter files at exclusive worker startup only.

    Original-byte cached recordings, catalog metadata and unknown user files
    are never touched.
    Root/camera component links are rejected instead of traversed, including
    Windows junctions. Linux native helpers die with the original worker.
    """
    cache = archive.settings.cache_dir.resolve()
    root = cache / 'sd-stage'
    linked = lambda path: path.is_symlink() or getattr(path, 'is_junction', lambda: False)()
    if linked(root):
        raise SDSourceError('sd_stage_invalid', 'SD staging directory must not be a symlink or junction.')
    if not root.exists():
        return 0
    if not root.is_dir() or root.resolve() != root:
        raise SDSourceError('sd_stage_invalid', 'SD staging root escaped the cache directory.')
    count = 0
    for camera in root.iterdir():
        if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', camera.name) is None:
            continue
        if linked(camera):
            raise SDSourceError('sd_stage_invalid', 'Camera SD staging directory must not be a symlink or junction.')
        if not camera.is_dir():
            continue
        if camera.resolve() != camera or not camera.is_relative_to(root):
            raise SDSourceError('sd_stage_invalid', 'Camera SD staging directory escaped the cache directory.')
        for candidate in camera.iterdir():
            if STAGING_NAME.fullmatch(candidate.name) and not linked(candidate) and candidate.is_file():
                candidate.unlink()
                count += 1
    return count


def _private_address(host):
    if not isinstance(host, str) or not host or len(host) > 128:
        raise SDSourceError('sd_host_missing', 'Camera LAN address is required.')
    try:
        addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)))
        if not addresses or any(not ipaddress.ip_address(item).is_private or
                                ipaddress.ip_address(item).is_loopback or
                                ipaddress.ip_address(item).is_unspecified or
                                ipaddress.ip_address(item).is_multicast for item in addresses):
            raise SDSourceError('sd_host_invalid', 'Camera address must resolve exclusively to private LAN addresses.')
        return addresses[0]
    except (OSError, ValueError):
        raise SDSourceError('sd_network_error', 'Camera LAN address did not resolve; check the subnet route.') from None


def _xml(raw):
    # Device XML is ASCII/UTF-8. Removing NULs also catches DTD/entity tokens
    # encoded as UTF-16 before ElementTree sees their expansion definitions.
    guard = raw.replace(b'\x00', b'').upper()
    if len(raw) > MAX_XML or b'<!DOCTYPE' in guard or b'<!ENTITY' in guard:
        raise SDSourceError('sd_protocol_error', 'Camera returned an invalid or oversized XML response.')
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        raise SDSourceError('sd_protocol_error', 'Camera returned a non-XML SD response.') from None


def _tag(node):
    return node.tag.rsplit('}', 1)[-1]


def _value(node, name, default=None):
    return next((item.text or '' for item in node.iter() if _tag(item) == name), default)


def _utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SDSourceError('sd_redirect_rejected', 'Camera redirected its SD API request.')


class _ClosedDigest(HTTPDigestAuthHandler):
    def http_error_401(self, request, response, code, message, headers):
        try:
            return super().http_error_401(request, response, code, message, headers)
        finally:
            response.close()


class ISAPISource:
    backend = 'isapi'

    def __init__(self, address, port, username, password, channel, zone, max_bytes):
        literal = f'[{address}]' if ':' in address else address
        self.base = f'http://{literal}:{port}'
        self.address, self.channel, self.zone, self.max_bytes = address, channel, zone, max_bytes
        manager = HTTPPasswordMgrWithDefaultRealm()
        manager.add_password(None, self.base, username, password)
        # Never send credentials to an environment HTTP proxy or a redirect.
        self.opener = build_opener(ProxyHandler({}), _NoRedirect(), _ClosedDigest(manager))

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def _request(self, path, data, method, timeout=30):
        request = Request(self.base + path, data=data, method=method,
                          headers={'Content-Type': 'application/xml', 'Accept': '*/*'})
        try:
            return self.opener.open(request, timeout=timeout)
        except HTTPError as error:
            error.close()
            if error.code in (401, 403):
                raise SDSourceError('sd_auth_failed', 'Camera rejected the SD username/password or recording permission.') from None
            if error.code in (404, 405, 501):
                raise SDSourceError('sd_isapi_unsupported', 'Camera did not expose the ISAPI SD-history API; select HCNetSDK.') from None
            raise SDSourceError('sd_http_error', f'Camera SD API returned HTTP {int(error.code)}.') from None
        except (URLError, OSError, TimeoutError):
            raise SDSourceError('sd_network_error', 'SD API connection failed; check camera HTTP port and Tailscale subnet routing.') from None
        except ValueError:
            raise SDSourceError('sd_protocol_error', 'Camera authentication challenge or HTTP protocol was invalid.') from None

    def search(self, start, end):
        search_id, position, results, seen = uuid.uuid4().hex, 0, [], set()
        while position < MAX_SEARCH_RESULTS:
            root = ET.Element('CMSearchDescription', {'version': '1.0', 'xmlns': NS})
            ET.SubElement(root, 'searchID').text = search_id
            ET.SubElement(ET.SubElement(root, 'trackIDList'), 'trackID').text = str(self.channel * 100 + 1)
            span = ET.SubElement(ET.SubElement(root, 'timeSpanList'), 'timeSpan')
            ET.SubElement(span, 'startTime').text = _utc(start)
            ET.SubElement(span, 'endTime').text = _utc(end)
            ET.SubElement(root, 'maxResults').text = '40'
            # Vendor schema deliberately spells Postion, not Position.
            ET.SubElement(root, 'searchResultPostion').text = str(position)
            ET.SubElement(ET.SubElement(root, 'metadataList'), 'metadataDescriptor').text = '//recordType.meta.std-cgi.com'
            with self._request('/ISAPI/ContentMgmt/search', ET.tostring(root), 'POST') as response:
                result = _xml(response.read(MAX_XML + 1))
            if _tag(result) != 'CMSearchResult':
                raise SDSourceError('sd_isapi_unsupported', 'Camera response did not confirm the ISAPI SD-history API.')
            status = (_value(result, 'responseStatusStrg', '') or '').upper()
            items = [item for item in result.iter() if _tag(item) == 'searchMatchItem']
            if _value(result, 'responseStatus', 'true').lower() not in ('true', '1') and status != 'NO MATCHES':
                raise SDSourceError('sd_search_failed', 'Camera rejected its SD search criteria.')
            previous_count = len(results)
            for item in items:
                if _value(item, 'trackID') not in (None, str(self.channel * 100 + 1)):
                    continue
                uri = _value(item, 'playbackURI', '')
                try:
                    parsed = urlsplit(uri)
                    # URI is passed back as XML, never executed/fetched as a URL.
                    if parsed.scheme != 'rtsp' or parsed.username or parsed.password or parsed.hostname != self.address:
                        raise ValueError('Invalid playback URI')
                    query = parse_qs(parsed.query)
                    rid = query.get('name', [''])[0]
                    first = parse_time(_value(item, 'startTime'))
                    last = parse_time(_value(item, 'endTime'))
                    size = int(query.get('size', ['0'])[0])
                    if not rid or len(rid) > 200 or '\x00' in rid or last <= first or size < 0:
                        raise ValueError('Invalid recording')
                except (ValueError, TypeError):
                    raise SDSourceError('sd_protocol_error', 'Camera returned invalid SD recording metadata.') from None
                identity = (rid, _utc(first))
                if identity not in seen:
                    seen.add(identity)
                    results.append({'record_id': rid, 'start_time': first.isoformat(),
                                    'end_time': last.isoformat(), 'size': size, 'playback_uri': uri})
            position += len(items)
            if status != 'MORE':
                return results
            if not items or len(results) == previous_count:
                raise SDSourceError('sd_protocol_error', 'Camera SD pagination did not advance.')
        raise SDSourceError('sd_search_limit', 'SD search exceeded 4000 files; shorten the lookback window.')

    def download(self, recording, destination):
        root = ET.Element('downloadRequest', {'version': '1.0', 'xmlns': NS})
        ET.SubElement(root, 'playbackURI').text = recording['playback_uri']
        total = 0
        with self._request('/ISAPI/ContentMgmt/download', ET.tostring(root), 'GET', timeout=120) as response:
            content_type = response.headers.get('Content-Type', '').lower()
            if 'xml' in content_type or 'html' in content_type or 'json' in content_type:
                raise SDSourceError('sd_download_failed', 'Camera returned a status document rather than recording bytes.')
            length = response.headers.get('Content-Length')
            try:
                expected = int(length) if length is not None else None
                if expected is not None and (expected <= 0 or expected > self.max_bytes):
                    raise ValueError('Invalid download length')
            except ValueError:
                raise SDSourceError('sd_size_limit', 'SD download exceeds the staging limit or has an invalid length.') from None
            try:
                with destination.open('xb') as output:
                    deadline = time.monotonic() + 1800
                    while True:
                        # read1 returns available bytes rather than waiting for
                        # a complete 256 KiB block; a slow continuous stream
                        # cannot prevent our overall time-budget checks.
                        block = response.read1(256 * 1024)
                        if not block:
                            break
                        total += len(block)
                        if total > self.max_bytes or time.monotonic() > deadline:
                            raise SDSourceError('sd_size_limit', 'SD download exceeded its staging limit or time budget.')
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
            except (OSError, TimeoutError):
                raise SDSourceError('sd_download_failed', 'SD recording transfer stopped before completion.') from None
        if not total or expected is not None and total != expected:
            raise SDSourceError('sd_download_incomplete', 'SD recording transfer ended with missing bytes.')
        if recording.get('size', 0) > 0 and total != recording['size']:
            raise SDSourceError('sd_download_incomplete', 'SD file length did not match the searched recording size.')
        return total


# SDK LONG/BOOL and DWORD are 32-bit, including on Linux LP64. Do not use c_long.
# Native natural alignment matches the vendor C definitions for these layouts.
class DVRTime(C.Structure):
    _fields_ = [(name, C.c_uint32) for name in ('year', 'month', 'day', 'hour', 'minute', 'second')]

    @classmethod
    def from_datetime(cls, value):
        return cls(value.year, value.month, value.day, value.hour, value.minute, value.second)

    def as_datetime(self, zone):
        return datetime(self.year, self.month, self.day, self.hour, self.minute, self.second, tzinfo=zone)


class FileCondition(C.Structure):
    _fields_ = [('channel', C.c_int32), ('file_type', C.c_uint32), ('locked', C.c_uint32),
                ('use_card', C.c_uint32), ('card', C.c_ubyte * 32), ('start', DVRTime), ('end', DVRTime)]


class FindData(C.Structure):
    _fields_ = [('filename', C.c_char * 100), ('start', DVRTime), ('end', DVRTime),
                ('size', C.c_uint32), ('card', C.c_char * 32), ('locked', C.c_ubyte),
                ('file_type', C.c_ubyte), ('reserved', C.c_ubyte * 2)]


class DeviceInfo(C.Structure):
    _fields_ = [('serial', C.c_ubyte * 48)] + [(name, C.c_ubyte) for name in (
        'alarm_in', 'alarm_out', 'disks', 'device_type', 'channels', 'start_channel',
        'audio_channels', 'ip_channels', 'zero_channels', 'main_protocol', 'sub_protocol',
        'support', 'support1', 'support2')] + [('type', C.c_uint16)] + [
        (name, C.c_ubyte) for name in ('support3', 'multi_stream', 'start_dchannel',
        'start_talk', 'high_dchannels', 'support4', 'language', 'voice_channels', 'start_voice')
    ] + [('reserved3', C.c_ubyte * 2), ('mirror_channels', C.c_ubyte),
         ('start_mirror', C.c_uint16), ('reserved2', C.c_ubyte * 2)]


class SDKPath(C.Structure):
    _fields_ = [('path', C.c_char * 256), ('reserved', C.c_ubyte * 128)]


def _sdk_library():
    directory = Path(os.environ.get('HCNETSDK_DIR', '/opt/hcnetsdk')).resolve()
    library = directory / 'libhcnetsdk.so'
    if not library.is_file():
        raise SDSourceError('sd_sdk_missing', 'Mount the official Linux HCNetSDK and its HCNetSDKCom directory for this CPU architecture.')
    try:
        with library.open('rb') as handle:
            header = handle.read(20)
        machine = int.from_bytes(header[18:20], 'little' if header[5] == 1 else 'big')
        architecture = platform.machine().lower()
        expected = {'x86_64': (2, 62), 'amd64': (2, 62), 'aarch64': (2, 183),
                    'arm64': (2, 183), 'armv7l': (1, 40), 'armv6l': (1, 40)}.get(architecture)
        if header[:4] != b'\x7fELF' or expected is None or (header[4], machine) != expected:
            raise SDSourceError('sd_sdk_arch_mismatch', 'HCNetSDK library architecture does not match this container.')
        return C.CDLL(str(library)), directory
    except (OSError, IndexError):
        raise SDSourceError('sd_sdk_load_failed', 'HCNetSDK or its shared-library dependencies did not load; check the official SDK package.') from None


class _NativeSession:
    backend = 'hcnetsdk'

    def __init__(self, address, port, username, password, channel, zone, max_bytes, sdk=None):
        self.address, self.port, self.username, self.password = address, port, username, password
        self.channel, self.zone, self.max_bytes = channel, zone, max_bytes
        self.sdk, self.directory = sdk if sdk is not None else _sdk_library()
        self.user = -1
        self.initialized = False
        signatures = {
            'NET_DVR_Init': ([], C.c_int32), 'NET_DVR_Cleanup': ([], C.c_int32),
            'NET_DVR_GetLastError': ([], C.c_uint32),
            'NET_DVR_Login_V30': ([C.c_char_p, C.c_uint16, C.c_char_p, C.c_char_p, C.POINTER(DeviceInfo)], C.c_int32),
            'NET_DVR_Logout': ([C.c_int32], C.c_int32),
            'NET_DVR_FindFile_V30': ([C.c_int32, C.POINTER(FileCondition)], C.c_int32),
            'NET_DVR_FindNextFile_V30': ([C.c_int32, C.POINTER(FindData)], C.c_int32),
            'NET_DVR_FindClose_V30': ([C.c_int32], C.c_int32),
            'NET_DVR_GetFileByName': ([C.c_int32, C.c_char_p, C.c_char_p], C.c_int32),
            'NET_DVR_PlayBackControl_V40': ([C.c_int32, C.c_uint32, C.c_void_p, C.c_uint32, C.c_void_p, C.POINTER(C.c_uint32)], C.c_int32),
            'NET_DVR_GetDownloadPos': ([C.c_int32], C.c_int32),
            'NET_DVR_StopGetFile': ([C.c_int32], C.c_int32),
        }
        try:
            for name, (arguments, result) in signatures.items():
                function = getattr(self.sdk, name)
                function.argtypes, function.restype = arguments, result
        except AttributeError:
            raise SDSourceError('sd_sdk_incompatible', 'HCNetSDK package lacks a required SD search/download function.') from None

    def _error(self, phase):
        code = int(self.sdk.NET_DVR_GetLastError())
        if code in (1, 2, 26):
            return SDSourceError('sd_auth_failed', f'Camera rejected native SD credentials or recording permission (SDK {code}).')
        if code in (4, 6, 23):
            return SDSourceError('sd_native_unsupported', f'Camera rejected native SD protocol/channel (SDK {code}).')
        return SDSourceError('sd_native_error', f'Native SD {phase} failed (SDK {code}); check routing, SDK dependencies and camera recording status.')

    def __enter__(self):
        SDK_LOCK.acquire()
        try:
            if hasattr(self.sdk, 'NET_DVR_SetSDKInitCfg'):
                function = self.sdk.NET_DVR_SetSDKInitCfg
                function.argtypes, function.restype = [C.c_uint32, C.c_void_p], C.c_int32
                path = os.fsencode(str(self.directory) + os.sep)
                if len(path) >= 256:
                    raise SDSourceError('sd_sdk_path_invalid', 'HCNetSDK directory path is too long.')
                config = SDKPath(path=path)
                if not function(2, C.byref(config)):
                    raise self._error('component-path setup')
                for index, filename in ((3, 'libcrypto.so.1.1'), (4, 'libssl.so.1.1')):
                    candidate = self.directory / filename
                    if candidate.is_file():
                        value = C.create_string_buffer(os.fsencode(candidate))
                        if not function(index, value):
                            raise self._error('TLS dependency setup')
            if not self.sdk.NET_DVR_Init():
                raise self._error('initialization')
            self.initialized = True
            if hasattr(self.sdk, 'NET_DVR_SetConnectTime'):
                self.sdk.NET_DVR_SetConnectTime.argtypes = [C.c_uint32, C.c_uint32]
                self.sdk.NET_DVR_SetConnectTime.restype = C.c_int32
                self.sdk.NET_DVR_SetConnectTime(5000, 1)
            info = DeviceInfo()
            self.user = int(self.sdk.NET_DVR_Login_V30(self.address.encode(), self.port,
                            self.username.encode(), self.password.encode(), C.byref(info)))
            if self.user < 0:
                raise self._error('login')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        try:
            if self.user >= 0:
                self.sdk.NET_DVR_Logout(self.user)
                self.user = -1
            if self.initialized:
                self.sdk.NET_DVR_Cleanup()
                self.initialized = False
        finally:
            SDK_LOCK.release()
        return False

    def search(self, start, end):
        condition = FileCondition(channel=self.channel, file_type=0xff, locked=0xff,
                                  start=DVRTime.from_datetime(start.astimezone(self.zone)),
                                  end=DVRTime.from_datetime(end.astimezone(self.zone)))
        handle = int(self.sdk.NET_DVR_FindFile_V30(self.user, C.byref(condition)))
        if handle < 0:
            raise self._error('search')
        results, seen, deadline = [], set(), time.monotonic() + 120
        try:
            while len(results) < MAX_SEARCH_RESULTS:
                data = FindData()
                status = int(self.sdk.NET_DVR_FindNextFile_V30(handle, C.byref(data)))
                if status in (1001, 1003):
                    return results
                if status == 1002:
                    if time.monotonic() > deadline:
                        raise SDSourceError('sd_search_timeout', 'Native camera SD search timed out.')
                    time.sleep(0.1)
                    continue
                if status != 1000:
                    raise self._error('search result')
                try:
                    filename = bytes(data.filename)
                    rid = filename.decode('utf-8')
                    first, last = data.start.as_datetime(self.zone), data.end.as_datetime(self.zone)
                    if not rid or len(filename) >= 100 or last <= first:
                        raise ValueError('Invalid recording metadata')
                except (UnicodeError, ValueError):
                    raise SDSourceError('sd_protocol_error', 'Native camera returned invalid SD recording metadata.') from None
                identity = (rid, first.isoformat())
                if identity in seen:
                    raise SDSourceError('sd_protocol_error', 'Native camera SD search repeated a file without advancing.')
                seen.add(identity)
                results.append({'record_id': rid, 'remote_filename': filename, 'start_time': first.isoformat(),
                                'end_time': last.isoformat(), 'size': int(data.size)})
            raise SDSourceError('sd_search_limit', 'SD search exceeded 4000 files; shorten the lookback window.')
        finally:
            self.sdk.NET_DVR_FindClose_V30(handle)

    def download(self, recording, destination):
        remote = recording.get('remote_filename', recording['record_id'])
        if isinstance(remote, str):
            remote = remote.encode('utf-8')
        handle = int(self.sdk.NET_DVR_GetFileByName(self.user, remote, os.fsencode(destination)))
        if handle < 0:
            raise self._error('download')
        completed = False
        try:
            offset, out_length = C.c_uint32(0), C.c_uint32(0)
            if not self.sdk.NET_DVR_PlayBackControl_V40(handle, 1, C.byref(offset), C.sizeof(offset), None, C.byref(out_length)):
                raise self._error('download start')
            deadline = time.monotonic() + 1800
            while time.monotonic() < deadline:
                progress = int(self.sdk.NET_DVR_GetDownloadPos(handle))
                if progress == 100:
                    completed = True
                    break
                if not 0 <= progress < 100:
                    raise self._error('download transfer')
                if destination.exists() and destination.stat().st_size > self.max_bytes:
                    raise SDSourceError('sd_size_limit', 'Native SD recording exceeded its staging limit.')
                time.sleep(0.25)
            if not completed:
                raise SDSourceError('sd_download_timeout', 'Native camera SD recording download timed out.')
        finally:
            stopped = self.sdk.NET_DVR_StopGetFile(handle)
        if not stopped:
            raise self._error('download finalization')
        if not destination.is_file() or destination.stat().st_size <= 0:
            raise SDSourceError('sd_download_incomplete', 'Native SDK finished without a recording file.')
        size = destination.stat().st_size
        if size > self.max_bytes:
            raise SDSourceError('sd_size_limit', 'Native SD recording exceeded its staging limit.')
        if recording.get('size', 0) > 0 and size != recording['size']:
            raise SDSourceError('sd_download_incomplete', 'Native SD file length did not match the searched recording size.')
        return size


class HCNetSDKSource:
    """Isolate vendor native code from the worker, catalog and Telegram threads.

    Credentials go through an anonymous pipe, never command-line arguments.
    A child crash, malformed output or a bounded timeout closes the session and
    becomes a fixed status; stdout/stderr from native code are never logged.
    """
    backend = 'hcnetsdk'

    def __init__(self, address, port, username, password, channel, zone, max_bytes,
                 worker_command=None, session_timeout=1800, cache_dir=None):
        # Check before spawning so auto can distinguish absent SDK from a
        # broken or wrong-architecture SDK (which should not be concealed).
        self.directory = Path(os.environ.get('HCNETSDK_DIR', '/opt/hcnetsdk')).resolve()
        if worker_command is None and not (self.directory / 'libhcnetsdk.so').is_file():
            raise SDSourceError('sd_sdk_missing', 'Mount the official Linux HCNetSDK and its HCNetSDKCom directory for this CPU architecture.')
        self.config = {'address': address, 'port': port, 'username': username, 'password': password,
                       'channel': channel, 'timezone': getattr(zone, 'key', None) or self._zone_name(zone),
                       'max_bytes': max_bytes}
        self.max_bytes = max_bytes
        self.command = worker_command or [sys.executable, '-m', 'archive_app.sd_source', '--native-worker']
        self.timeout = session_timeout
        self.cache_dir = cache_dir
        self.process = None
        self.inbox = queue.Queue(maxsize=4)

    @staticmethod
    def _zone_name(zone):
        seconds = int(zone.utcoffset(None).total_seconds())
        hours, minutes = divmod(abs(seconds) // 60, 60)
        return f"UTC{'+' if seconds >= 0 else '-'}{hours:02d}:{minutes:02d}"

    def _reader(self):
        process = self.process
        try:
            while True:
                line = process.stdout.readline(2_000_001)
                if not line:
                    self.inbox.put(None, timeout=1)
                    return
                self.inbox.put(line, timeout=1)
                if len(line) > 2_000_000:
                    return
        except (OSError, ValueError, queue.Full):
            pass

    def _exchange(self, payload, timeout):
        if self.process is None:
            raise SDSourceError('sd_native_worker_failed', 'Native SD helper is not running.')
        try:
            self.process.stdin.write((json.dumps(payload) + '\n').encode())
            self.process.stdin.flush()
            remaining = min(timeout, self.deadline - time.monotonic())
            if remaining <= 0:
                raise queue.Empty
            raw = self.inbox.get(timeout=remaining)
            if raw is None:
                raise SDSourceError('sd_native_worker_failed', 'Native SD helper stopped unexpectedly; check the SDK package.')
            response = json.loads(raw)
            if not isinstance(response, dict) or 'ok' not in response:
                raise ValueError('Invalid helper protocol')
            if response['ok'] is not True:
                code = response.get('code', '')
                # Only approved fixed messages are exposed, never arbitrary
                # strings emitted by native code or malformed helper output.
                messages = {
                    'sd_sdk_missing': 'Official Linux HCNetSDK package is missing.',
                    'sd_sdk_arch_mismatch': 'HCNetSDK library architecture does not match this container.',
                    'sd_sdk_load_failed': 'HCNetSDK dependencies did not load; check the official SDK package.',
                    'sd_sdk_incompatible': 'HCNetSDK lacks a required SD search/download function.',
                    'sd_sdk_path_invalid': 'HCNetSDK directory path is invalid.',
                    'sd_auth_failed': 'Camera rejected native SD credentials or recording permission.',
                    'sd_native_unsupported': 'Camera rejected native SD protocol/channel.',
                    'sd_native_error': 'Native SD request failed; check routing, dependencies and camera recording status.',
                    'sd_search_timeout': 'Native camera SD search timed out.',
                    'sd_search_limit': 'SD search exceeded 4000 files; shorten the lookback window.',
                    'sd_protocol_error': 'Camera returned invalid SD recording metadata.',
                    'sd_size_limit': 'Native SD file exceeded its staging limit.',
                    'sd_download_incomplete': 'Native SD recording ended with missing bytes.',
                    'sd_download_timeout': 'Native SD download timed out.',
                    'sd_stage_invalid': 'Native SD destination escaped its isolated staging directory.',
                }
                if code not in messages:
                    code = 'sd_native_error'
                raise SDSourceError(code, messages[code])
            return response.get('result')
        except queue.Empty:
            self.__exit__(None, None, None)
            raise SDSourceError('sd_native_worker_timeout', 'Native SD helper timed out and was stopped; its recording was not uploaded.') from None
        except (OSError, ValueError, TypeError):
            self.__exit__(None, None, None)
            raise SDSourceError('sd_native_worker_failed', 'Native SD helper stopped or returned invalid protocol output.') from None

    def __enter__(self):
        try:
            environment = dict(os.environ)
            environment['MYCAM_NATIVE_PARENT_PID'] = str(os.getpid())
            # Vendor SSL/crypto belongs to the native child only. The parent
            # Telegram client must keep its system SSL and CA trust paths.
            sdk_dir=Path(environment.get('HCNETSDK_DIR','/opt/hcnetsdk')).resolve()
            vendor_paths=[str(sdk_dir),str(sdk_dir/'HCNetSDKCom')]
            inherited=environment.get('LD_LIBRARY_PATH')
            if inherited:vendor_paths.append(inherited)
            environment['LD_LIBRARY_PATH']=os.pathsep.join(vendor_paths)
            for name in list(environment):
                if name.startswith('TELEGRAM_') or name in ('DASHBOARD_PASSWORD', 'DASHBOARD_TOKEN'):
                    environment.pop(name)
            if self.cache_dir is not None:
                environment['CACHE_DIR'] = str(self.cache_dir)
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            bufsize=0, cwd=str(Path(__file__).resolve().parents[1]), env=environment)
        except OSError:
            raise SDSourceError('sd_native_worker_failed', 'Native SD helper did not start.') from None
        self.deadline = time.monotonic() + self.timeout
        self.reader = threading.Thread(target=self._reader, daemon=True)
        self.reader.start()
        try:
            self._exchange({'command': 'init', **self.config}, 30)
            self.config['password'] = ''
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.process is not None:
            try:
                if self.process.poll() is None:
                    try:
                        self.process.stdin.write(b'{"command":"close"}\n')
                        self.process.stdin.flush()
                        self.process.wait(timeout=2)
                    except (OSError, subprocess.TimeoutExpired):
                        self.process.kill()
                self.process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
            finally:
                for pipe in (self.process.stdin, self.process.stdout):
                    if pipe is not None:
                        pipe.close()
                self.process = None
        self.config['password'] = ''
        return False

    def search(self, start, end):
        result = self._exchange({'command': 'search', 'start': start.isoformat(), 'end': end.isoformat()}, 130)
        if not isinstance(result, list) or len(result) > MAX_SEARCH_RESULTS:
            raise SDSourceError('sd_protocol_error', 'Native SD helper returned invalid search results.')
        return result

    def download(self, recording, destination):
        return self._exchange({'command': 'download', 'recording': recording,
                               'destination': str(destination), 'max_bytes': self.max_bytes}, 1805)


def _native_worker():
    """Private child protocol. No SQLite access and no credential output."""
    provider = None
    try:
        if sys.platform.startswith('linux'):
            # Docker worker restart must also stop a hung orphan SDK process.
            # PR_SET_PDEATHSIG affects this helper only, not the parent worker.
            libc = C.CDLL(None, use_errno=True)
            libc.prctl.argtypes = [C.c_int, C.c_ulong, C.c_ulong, C.c_ulong, C.c_ulong]
            libc.prctl.restype = C.c_int
            expected_parent = int(os.environ.get('MYCAM_NATIVE_PARENT_PID', '0'))
            # The archive worker legitimately has PID 1 in a container. Check
            # the exact parent instead of confusing that with an orphan.
            if libc.prctl(1, 9, 0, 0, 0) != 0 or expected_parent <= 0 or os.getppid() != expected_parent:
                return
        while True:
            raw = sys.stdin.buffer.readline(32_001)
            if not raw:
                break
            if len(raw) > 32_000:
                break
            try:
                request = json.loads(raw)
                command = request['command']
                if command == 'close':
                    break
                if command == 'init' and provider is None:
                    provider = _NativeSession(request['address'], request['port'], request['username'],
                                              request['password'], request['channel'],
                                              get_zone(request['timezone']), request['max_bytes'])
                    provider.__enter__()
                    result = None
                elif command == 'search' and provider is not None:
                    result = provider.search(parse_time(request['start']), parse_time(request['end']))
                    for entry in result:
                        entry.pop('remote_filename', None)
                elif command == 'download' and provider is not None:
                    destination = Path(request['destination'])
                    cache = Path(os.environ.get('CACHE_DIR', './cache')).resolve()
                    stage = cache / 'sd-stage'
                    if not destination.is_absolute() or not destination.resolve().is_relative_to(stage) or destination.exists():
                        raise SDSourceError('sd_stage_invalid', 'Native SD destination escaped staging.')
                    provider.max_bytes = request['max_bytes']
                    result = provider.download(request['recording'], destination)
                else:
                    raise SDSourceError('sd_protocol_error', 'Invalid native helper command.')
                response = {'ok': True, 'result': result}
            except SDSourceError as error:
                response = {'ok': False, 'code': error.code}
            except Exception:
                response = {'ok': False, 'code': 'sd_native_error'}
            sys.stdout.write(json.dumps(response) + '\n')
            sys.stdout.flush()
    finally:
        if provider is not None and provider.initialized:
            provider.__exit__(None, None, None)


class SDSource:
    def __init__(self, archive, camera, credentials=None):
        self.archive = archive
        self.camera = camera
        config = credentials if credentials is not None else archive.camera_sd_config(camera)
        self.config = config
        self.camera_id = config.get('id', camera if isinstance(camera, str) else camera.get('id'))
        if not isinstance(self.camera_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', self.camera_id):
            raise SDSourceError('sd_config_invalid', 'Invalid SD camera ID.')

    def _provider(self):
        config = self.config
        password = config.get('sd_password', config.get('password', ''))
        username = config.get('sd_username', config.get('username', 'admin'))
        if not isinstance(password, str) or not password or '\x00' in password or len(password.encode()) > 64:
            raise SDSourceError('sd_credentials_missing', 'Set the camera SD password/verification code in its dashboard settings.')
        if not isinstance(username, str) or not username or '\x00' in username or len(username.encode()) > 64:
            raise SDSourceError('sd_config_invalid', 'Invalid camera SD username.')
        address = _private_address(config.get('host', ''))
        channel = config.get('sd_channel', config.get('channel', 1))
        backend = config.get('sd_backend', config.get('backend', 'auto'))
        if type(channel) is not int or not 1 <= channel <= 9999 or backend not in ('auto', 'isapi', 'hcnetsdk'):
            raise SDSourceError('sd_config_invalid', 'Invalid camera SD backend/channel.')
        zone = get_zone(config.get('sd_timezone', config.get('timezone', self.archive.settings.timezone)))
        max_bytes = min(MAX_NATIVE_FILE, self.archive.settings.cache_max_bytes // 2)
        if max_bytes <= 0:
            raise SDSourceError('sd_cache_budget', 'Cache budget is too small for SD staging.')
        common = (address, username, password, channel, zone, max_bytes)
        if backend in ('auto', 'hcnetsdk'):
            try:
                return HCNetSDKSource(address, config.get('device_port', 8000), *common[1:],
                                     cache_dir=self.archive.settings.cache_dir)
            except SDSourceError as error:
                if backend != 'auto' or error.code != 'sd_sdk_missing':
                    raise
        provider = ISAPISource(address, config.get('http_port', 80), *common[1:])
        provider.native_sdk_missing = backend == 'auto'
        return provider

    def _staging(self):
        cache = self.archive.settings.cache_dir.resolve()
        parent = cache / 'sd-stage'
        if parent.is_symlink() or getattr(parent, 'is_junction', lambda: False)():
            raise SDSourceError('sd_stage_invalid', 'SD staging directory must not be a symlink or junction.')
        parent.mkdir(exist_ok=True)
        root = parent / self.camera_id
        if root.is_symlink() or getattr(root, 'is_junction', lambda: False)():
            raise SDSourceError('sd_stage_invalid', 'Camera SD staging directory must not be a symlink or junction.')
        root.mkdir(exist_ok=True)
        if not root.resolve().is_relative_to(cache):
            raise SDSourceError('sd_stage_invalid', 'SD staging directory escaped the cache directory.')
        # An exclusive archive worker owns staging. Its prior SDK children
        # die with the worker on Linux, so unfinished managed files are not
        # retained forever after a crash. Never follow links or delete unknown
        # files placed by a user in this directory.
        for candidate in root.iterdir():
            if (STAGING_NAME.fullmatch(candidate.name)
                    and not candidate.is_symlink()
                    and not getattr(candidate, 'is_junction', lambda: False)()
                    and candidate.is_file()):
                candidate.unlink()
        return root

    def sync(self, now=None, max_files=100, progress=None):
        if type(max_files) is not int or not 1 <= max_files <= 100:
            raise SDSourceError('sd_config_invalid', 'SD batch must contain 1 to 100 files.')
        if progress is not None and not callable(progress):
            raise SDSourceError('sd_config_invalid', 'SD progress callback must be callable.')
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise SDSourceError('sd_config_invalid', 'SD sync time requires a UTC offset.')
        hours = self.config.get('sd_lookback_hours', self.config.get('lookback_hours', 168))
        if type(hours) is not int or not 1 <= hours <= 720:
            raise SDSourceError('sd_config_invalid', 'SD lookback must be 1 to 720 hours.')
        start, end = now - timedelta(hours=hours), now - timedelta(seconds=SETTLE_SECONDS)
        provider = self._provider()
        statistics = {'backend': provider.backend, 'searched': 0, 'downloaded': 0,
                      'imported': 0, 'already_known': 0, 'deferred': 0, 'backlog': 0}
        def notify(phase):
            if progress is not None:
                progress(dict(statistics, phase=phase))
        with provider:
            notify('sd_search')
            try:
                recordings = sorted(provider.search(start, end), key=lambda row: (parse_time(row['start_time']), row['record_id']))
            except SDSourceError as error:
                if getattr(provider, 'native_sdk_missing', False) and error.code in ('sd_isapi_unsupported', 'sd_network_error'):
                    raise SDSourceError('sd_sdk_missing', 'ISAPI SD history did not respond; mount the architecture-matching official Linux HCNetSDK to use camera port 8000.') from None
                raise
            statistics['searched'] = len(recordings)
            notify('sd_download')
            root = self._staging()
            for recording in recordings:
                enabled = self.archive.conn.execute('SELECT enabled FROM cameras WHERE id=?', (self.camera_id,)).fetchone()
                if enabled is None or not enabled['enabled']:
                    raise SDSourceError('camera_disabled', 'Camera was paused during SD sync; no further recording was downloaded.')
                first, last = parse_time(recording['start_time']), parse_time(recording['end_time'])
                if last > end or last <= first or first >= end or last <= start:
                    statistics['deferred'] += 1
                    notify('sd_download')
                    continue
                channel = self.config.get('sd_channel', self.config.get('channel', 1))
                entry = {'camera': self.camera_id, 'source': 'camera-sd',
                         'record_id': f'ch{channel}:{_utc(first)}', 'start_time': first.isoformat(),
                         'end_time': last.isoformat()}
                key = record_key(entry)
                known = self.archive.conn.execute('SELECT status,deleted_at,start_ms,end_ms FROM recordings WHERE key=?', (key,)).fetchone()
                # Never re-fetch uploaded/uncertain/trashed recordings. Failed
                # media validation is retried on a later explicitly queued scan.
                if known is not None and (known['deleted_at'] is not None or known['status'] not in ('failed', 'ingesting')):
                    if known['end_ms'] != int(last.timestamp() * 1000):
                        raise SDSourceError('sd_record_changed', 'Camera changed a closed recording interval; archived footage needs operator review.')
                    statistics['already_known'] += 1
                    notify('sd_download')
                    continue
                if statistics['downloaded'] >= max_files:
                    statistics['backlog'] += 1
                    notify('sd_download')
                    continue
                used = sum(path.stat().st_size for path in self.archive.settings.cache_dir.rglob('*') if path.is_file())
                free = shutil.disk_usage(root).free
                provider.max_bytes = min(MAX_NATIVE_FILE, (self.archive.settings.cache_max_bytes - used) // 2,
                                         (free - self.archive.settings.min_free_bytes) // 2)
                estimate = max(1, recording.get('size', 0))
                if provider.max_bytes <= 0:
                    raise SDSourceError('sd_cache_budget', 'SD staging cache budget reached; confirmed recordings remain intact.')
                if estimate > provider.max_bytes:
                    raise SDSourceError('sd_size_limit', 'SD file exceeds the bounded native staging size; shorten camera recording segments.')
                needed = estimate * 2  # Staged source and identical-byte cached copy.
                if used + needed > self.archive.settings.cache_max_bytes or free - needed < self.archive.settings.min_free_bytes:
                    raise SDSourceError('sd_cache_budget', 'SD staging cache budget reached; confirmed recordings remain intact.')
                partial = root / f'{key}.{uuid.uuid4().hex}.part'
                complete = root / f'{key}.{uuid.uuid4().hex}.source'
                try:
                    notify('sd_download')
                    provider.download(recording, partial)
                    partial.replace(complete)
                    statistics['downloaded'] += 1
                    entry['path'] = str(complete)
                    result = self.archive.ingest_download(entry, complete)
                    if result.get('status') == 'camera_disabled':
                        statistics['deferred'] += 1
                        raise SDSourceError('camera_disabled', 'Camera was paused during SD sync; the download was not imported.')
                    statistics['imported'] += 1
                    notify('sd_download')
                except SDSourceError:
                    raise
                except Exception:
                    raise SDSourceError('sd_media_validation_failed', 'Downloaded SD recording could not be copied intact into the managed cache; no Telegram upload was attempted.') from None
                finally:
                    # Catalog owns an identical-byte cached original; staging
                    # files are temporary, including after a copy failure.
                    partial.unlink(missing_ok=True)
                    complete.unlink(missing_ok=True)
        notify('sd_complete')
        return statistics


if __name__ == '__main__':
    if sys.argv[1:] == ['--native-worker']:
        _native_worker()
    else:
        raise SystemExit('SD source is an internal worker module.')
