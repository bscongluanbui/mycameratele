from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import socket
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def from_epoch_ms(milliseconds, zone):
    """Portable UTC milliseconds -> local datetime, including 32-bit ARM >2038."""
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) +
            timedelta(milliseconds=milliseconds)).astimezone(zone)


def get_zone(name):
    match = re.fullmatch(r'UTC([+-])(\d{2}):(\d{2})', name)
    if match:
        hours, minutes = int(match[2]), int(match[3])
        if hours > 23 or minutes > 59:
            raise ValueError('Invalid UTC offset')
        delta = timedelta(hours=hours, minutes=minutes)
        return timezone(delta if match[1] == '+' else -delta)
    return ZoneInfo(name)


def parse_time(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('Recording timestamp must include a UTC offset')
    return parsed


def record_key(entry):
    camera = entry.get('camera', '')
    rid = str(entry.get('record_id', ''))
    source = str(entry.get('source', 'studio-export'))
    if not isinstance(camera,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', camera) or not rid or len(rid) > 200:
        raise ValueError('camera slug and stable record_id are required')
    return hashlib.sha256(json.dumps([camera, source, rid], ensure_ascii=False).encode()).hexdigest()


def resolve_input(path, root):
    root = Path(root).resolve()
    path = Path(path)
    resolved = (path if path.is_absolute() else root / path).resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError('Source must be a regular file inside INPUT_DIR')
    return resolved


def secret(name):
    filename = os.environ.get(name + '_FILE', '')
    return Path(filename).read_text().strip() if filename else os.environ.get(name, '').strip()


@dataclass
class Settings:
    state_dir: Path
    cache_dir: Path
    input_dir: Path
    timezone: str
    keep_cache: bool = True
    enable_upload: bool = False
    token: str = ''
    chat_id: str = ''
    api_base: str = 'https://api.telegram.org'
    api_mode: str = 'cloud'
    allowed_users: tuple = ()
    ffmpeg: str = 'ffmpeg'
    ffprobe: str = 'ffprobe'
    max_bytes: int = 50000000
    cache_max_bytes: int = 100000000000
    min_free_bytes: int = 5000000000
    interval: int = 15
    owner_user_id: int = 0
    bot_username: str = ''
    # Confirmed uploads default to immediate cleanup in all deployments.
    cache_retention_hours: float = 0.0
    # Raw-mode metadata probing is optional and disabled by default. MP4
    # stream-copy mode never invokes a probe or decode-validation process.
    passthrough_probe: bool = False
    tenant_id: str = 'house01'
    telegram_destination: str = 'owner_private'
    storage_channel_id: int = 0
    # Library/test constructors remain byte-preserving. A deployment may
    # explicitly select MP4 container remuxing with video/audio stream copy.
    media_mode: str = 'raw'
    player_public_url: str = ''
    bot_api_file_root: Path = Path('/var/lib/telegram-bot-api')
    bot_api_spool_root: Path | None = None
    bot_api_spool_max_bytes: int = 5000000000
    error_retention_hours: float = 72.0
    upload_transport: str = 'multipart'
    local_upload_root: str = ''

    @property
    def effective_owner(self):
        """Only a positive private-user ID can be an upload destination."""
        value = self.owner_user_id if self.owner_user_id else self.chat_id
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return value if value > 0 else 0
        if isinstance(value, str) and re.fullmatch(r'[0-9]+', value.strip()):
            owner = int(value)
            return owner if owner > 0 else 0
        return 0

    @classmethod
    def from_env(cls):
        mode = os.environ.get('TELEGRAM_API_MODE', 'cloud')
        if mode not in ('cloud', 'local'):
            raise ValueError('TELEGRAM_API_MODE must be cloud or local')
        configured_owner = os.environ.get('TELEGRAM_OWNER_USER_ID', '').strip()
        legacy_chat = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
        owner_value = configured_owner or legacy_chat
        if owner_value and (not re.fullmatch(r'[0-9]+', owner_value) or int(owner_value) <= 0):
            raise ValueError('TELEGRAM_OWNER_USER_ID must be a positive private-user ID; legacy TELEGRAM_CHAT_ID accepts positive IDs only')
        owner = int(owner_value) if owner_value else 0
        allowed_values = [x for x in re.split(r'[,\s]+', os.environ.get('TELEGRAM_ALLOWED_USER_IDS', '').strip()) if x]
        if any(not re.fullmatch(r'[0-9]+', x) or int(x) <= 0 for x in allowed_values):
            raise ValueError('TELEGRAM_ALLOWED_USER_IDS must contain positive numeric user IDs')
        allowed = tuple(dict.fromkeys(([owner] if owner else []) + [int(x) for x in allowed_values]))
        username = os.environ.get('TELEGRAM_BOT_USERNAME', '').strip().lstrip('@')
        if username and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', username):
            raise ValueError('Invalid TELEGRAM_BOT_USERNAME')
        tenant = os.environ.get('TENANT_ID', 'house01').strip()
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', tenant):
            raise ValueError('TENANT_ID must be a stable lowercase tenant slug')
        destination = os.environ.get('TELEGRAM_DESTINATION', 'owner_private').strip()
        if destination not in ('owner_private', 'channel'):
            raise ValueError('TELEGRAM_DESTINATION must be owner_private or channel')
        channel_value = os.environ.get('TELEGRAM_STORAGE_CHANNEL_ID', '').strip()
        if channel_value and (not re.fullmatch(r'-[0-9]+', channel_value) or int(channel_value)>=0):
            raise ValueError('TELEGRAM_STORAGE_CHANNEL_ID must be a negative numeric channel ID')
        channel = int(channel_value) if channel_value else 0
        if channel:destination='channel'
        retention = float(os.environ.get('CACHE_RETENTION_HOURS', '0'))
        if not math.isfinite(retention) or retention < 0:
            raise ValueError('CACHE_RETENTION_HOURS must be a nonnegative finite number')
        error_retention = float(os.environ.get('ERROR_RETENTION_HOURS', '72'))
        if not math.isfinite(error_retention) or error_retention <= 0:
            raise ValueError('ERROR_RETENTION_HOURS must be a positive finite number')
        spool_gb = float(os.environ.get('BOT_API_SPOOL_MAX_GB', '5'))
        if not math.isfinite(spool_gb) or spool_gb <= 0:
            raise ValueError('BOT_API_SPOOL_MAX_GB must be a positive finite number')
        spool_path = os.environ.get('BOT_API_SPOOL_DIR', '').strip()
        upload_transport = os.environ.get('TELEGRAM_UPLOAD_TRANSPORT', '').strip() or 'multipart'
        local_upload_root = os.environ.get('TELEGRAM_LOCAL_UPLOAD_ROOT', '').strip()
        if upload_transport not in ('multipart', 'local_file'):
            raise ValueError('TELEGRAM_UPLOAD_TRANSPORT must be multipart or local_file')
        if upload_transport == 'local_file':
            from .local_upload import validate_upload_root
            if mode != 'local':
                raise ValueError('Local file uploads require Local Bot API')
            validate_upload_root(local_upload_root)
        media_mode = os.environ.get('MEDIA_MODE', 'raw').strip().lower()
        if media_mode not in ('raw', 'remux_copy'):
            raise ValueError('MEDIA_MODE must be raw or remux_copy')
        result = cls(
            Path(os.environ.get('STATE_DIR', './data')).resolve(),
            Path(os.environ.get('CACHE_DIR', './cache')).resolve(),
            Path(os.environ.get('INPUT_DIR', './input')).resolve(),
            os.environ.get('DISPLAY_TIMEZONE', 'Asia/Ho_Chi_Minh'),
            os.environ.get('KEEP_CACHE', 'true').lower() == 'true',
            os.environ.get('ENABLE_UPLOAD', 'false').lower() == 'true',
            secret('TELEGRAM_BOT_TOKEN'), str(owner) if owner else '',
            os.environ.get('TELEGRAM_API_BASE', os.environ.get('TELEGRAM_API_BASE_URL', 'https://api.telegram.org')).rstrip('/'),
            mode, allowed, os.environ.get('FFMPEG_BIN', 'ffmpeg'), os.environ.get('FFPROBE_BIN', 'ffprobe'),
            int(os.environ.get('TELEGRAM_MAX_BYTES', '2000000000' if mode == 'local' else '50000000')),
            int(float(os.environ.get('CACHE_MAX_GB', '100')) * 1e9),
            int(float(os.environ.get('CACHE_MIN_FREE_GB', '5')) * 1e9),
            max(1, int(os.environ.get('SCAN_INTERVAL_SECONDS', '15'))),
            owner, username, retention,
            error_retention_hours=error_retention,
            passthrough_probe=os.environ.get('PASSTHROUGH_PROBE_METADATA', 'false').lower() == 'true',
            tenant_id=tenant, telegram_destination=destination, storage_channel_id=channel,
            media_mode=media_mode,
            player_public_url=os.environ.get('TELEGRAM_PLAYER_PUBLIC_URL', '').strip().rstrip('/'),
            bot_api_file_root=Path(os.environ.get('TELEGRAM_BOT_API_FILE_ROOT', '/var/lib/telegram-bot-api')),
            bot_api_spool_root=Path(spool_path) if spool_path else None,
            bot_api_spool_max_bytes=int(spool_gb * 1e9),
            upload_transport=upload_transport,
            local_upload_root=local_upload_root,
        )
        get_zone(result.timezone)
        limit = 2000000000 if mode == 'local' else 50000000
        if result.max_bytes <= 0 or result.max_bytes > limit:
            raise ValueError('TELEGRAM_MAX_BYTES exceeds configured API mode limit')
        if result.cache_max_bytes <= 0 or result.min_free_bytes < 0:
            raise ValueError('Invalid cache budget')
        if result.enable_upload and (not result.token or (result.telegram_destination=='owner_private' and not result.effective_owner)):
            raise ValueError('ENABLE_UPLOAD needs bot credentials and a configured destination')
        return result


def normalize(source, dest, settings):
    """Select original-byte transfer or explicitly approved MP4 stream copy."""
    if settings.media_mode == 'remux_copy':
        return _remux_copy(source, dest, settings)
    if settings.media_mode != 'raw':
        raise ValueError('MEDIA_MODE must be raw or remux_copy')
    return _copy_original(source, dest, settings)


def _copy_original(source, dest, settings, *, force_extension=None, probe_metadata=None):
    """Compatibility name for a byte-for-byte copy, never media conversion.

    A camera's SDK can return a proprietary container despite its filename.
    Inspect at most 4 KiB for naming, retain unknown formats as .bin, and make
    any optional metadata probe best effort. Upload must not depend on being
    able to decode camera audio/video.
    """
    source, dest = Path(source), Path(dest)
    before = source.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
        raise ValueError('Source must be a nonempty regular file')
    with source.open('rb') as handle:
        header = handle.read(4096)
    container, extension = _media_header(header)
    probe_status, duration, video, audio = 'disabled', None, None, None
    if settings.passthrough_probe if probe_metadata is None else probe_metadata:
        probe_status = 'unavailable'
        try:
            probe = subprocess.run([
                settings.ffprobe, '-v', 'error', '-probesize', '262144',
                '-analyzeduration', '500000', '-show_entries',
                'format=format_name,duration:stream=codec_type,codec_name',
                '-of', 'json', str(source),
            ], capture_output=True, timeout=5)
            if not probe.returncode and len(probe.stdout) <= 1000000:
                data = json.loads(probe.stdout)
                if isinstance(data, dict):
                    media_format = data.get('format', {})
                    streams = data.get('streams', [])
                    if isinstance(media_format, dict) and isinstance(streams, list):
                        name = media_format.get('format_name')
                        if isinstance(name, str) and len(name) <= 100:
                            container = name
                            extension = _media_extension(name, extension)
                        try:
                            value = float(media_format.get('duration'))
                            if math.isfinite(value) and value > 0:
                                duration = value
                        except (TypeError, ValueError, OverflowError):
                            pass
                        for stream in streams:
                            if not isinstance(stream, dict):
                                continue
                            codec = stream.get('codec_name')
                            if not isinstance(codec, str) or len(codec) > 100:
                                continue
                            if stream.get('codec_type') == 'video' and video is None:
                                video = codec
                            elif stream.get('codec_type') == 'audio' and audio is None:
                                audio = codec
                        probe_status = 'metadata'
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            # Original bytes remain useful even when a proprietary container
            # is not supported by FFprobe or FFprobe is absent.
            pass
    suffix = source.suffix.lower()
    if re.fullmatch(r'\.[a-z0-9]{1,10}', suffix) and suffix not in ('.source', '.part', '.partial'):
        extension = suffix
    if force_extension is not None:
        extension = force_extension
    dest = dest.with_suffix(extension)
    partial = dest.with_suffix('.part' + extension)
    _confined_path(dest, dest.parent.resolve(), allow_missing=True)
    _confined_path(partial, dest.parent.resolve(), allow_missing=True)
    source_digest = hashlib.sha256()
    created = False
    try:
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        created = True
        copied = 0
        with os.fdopen(descriptor, 'wb') as output_handle, source.open('rb') as input_handle:
            for block in iter(lambda: input_handle.read(1024 * 1024), b''):
                source_digest.update(block)
                output_handle.write(block)
                copied += len(block)
            output_handle.flush()
            os.fsync(output_handle.fileno())
            after = os.fstat(input_handle.fileno())
        current = source.stat()
        if (copied != before.st_size or after.st_size != before.st_size
                or current.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns
                or current.st_mtime_ns != before.st_mtime_ns
                or (after.st_ino, after.st_dev) != (before.st_ino, before.st_dev)
                or (current.st_ino, current.st_dev) != (before.st_ino, before.st_dev)):
            raise ValueError('Source changed during pass-through copy; source was retained')
        with partial.open('rb') as handle:
            cached_digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        if source_digest.hexdigest() != cached_digest or partial.stat().st_size != copied:
            raise ValueError('Pass-through copy integrity mismatch; source was retained')
        _confined_path(dest, dest.parent.resolve(), allow_missing=True)
        partial.replace(dest)
        return {'duration': duration, 'codec_video': video, 'codec_audio': audio,
                'bytes': copied, 'sha256': cached_digest, 'path': str(dest),
                'container': container, 'probe_status': probe_status,
                'file_extension': extension, 'processing_method': 'passthrough'}
    finally:
        if created and partial.exists():
            _confined_path(partial, dest.parent.resolve()).unlink()


def _remux_copy(source, dest, settings):
    """MP4 output without any encoder, audio conversion or decode validation.

    Existing MP4 bytes are copied intact. Camera PS/TS packets are remuxed
    with FFmpeg's stream-copy path; unsupported codecs fail rather than being
    re-encoded. The source remains read-only and no FFprobe process is started.
    """
    source, dest = Path(source), Path(dest).with_suffix('.mp4')
    before = source.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
        raise ValueError('Source must be a nonempty regular file')
    with source.open('rb') as handle:
        container, extension = _media_header(handle.read(4096))
    if extension == '.mp4':
        info = _copy_original(source, dest, settings, force_extension='.mp4', probe_metadata=False)
        return dict(info, processing_method='remux_copy', container='mov,mp4', probe_status='disabled')
    if container not in ('mpeg', 'mpegts'):
        raise ValueError('Stream-copy MP4 input must be an existing MP4 or camera PS/TS file; source was retained')
    partial = dest.with_suffix('.part.mp4')
    root = dest.parent.resolve()
    _confined_path(dest, root, allow_missing=True)
    _confined_path(partial, root, allow_missing=True)
    created = False
    try:
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(descriptor)
        created = True
        environment = dict(os.environ)
        # Vendor HCNetSDK OpenSSL libraries must not replace FFmpeg's system
        # libraries. The SDK runs separately and keeps its own environment.
        environment.pop('LD_LIBRARY_PATH', None)
        command = [settings.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                   '-probesize', '1048576', '-analyzeduration', '1000000',
                   '-i', str(source), '-map', '0:v:0', '-map', '0:a?', '-c', 'copy',
                   '-movflags', '+faststart', str(partial)]
        try:
            result = subprocess.run(command, capture_output=True, timeout=300, env=environment)
        except (OSError, subprocess.SubprocessError):
            raise ValueError('Stream-copy MP4 remux did not complete; source was retained') from None
        if result.returncode:
            raise ValueError('Stream-copy MP4 remux failed; source was retained')
        current = source.stat()
        if (current.st_size != before.st_size or current.st_mtime_ns != before.st_mtime_ns
                or (current.st_ino, current.st_dev) != (before.st_ino, before.st_dev)):
            raise ValueError('Source changed during stream-copy remux; source was retained')
        output = _confined_path(partial, root)
        output_size = output.stat().st_size
        with output.open('rb') as handle:
            _, output_extension = _media_header(handle.read(4096))
            handle.seek(0)
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        if output_size <= 0 or output_extension != '.mp4' or output_size > before.st_size * 2:
            raise ValueError('Stream-copy remux did not produce an MP4 container; source was retained')
        _confined_path(dest, root, allow_missing=True)
        output.replace(dest)
        return {'duration': None, 'codec_video': None, 'codec_audio': None,
                'bytes': output_size, 'sha256': digest, 'path': str(dest),
                'container': 'mov,mp4', 'probe_status': 'disabled',
                'file_extension': '.mp4', 'processing_method': 'remux_copy'}
    finally:
        if created and partial.exists():
            _confined_path(partial, root).unlink()


def _media_header(header):
    """Bounded naming hint; it neither changes bytes nor certifies decodability."""
    if len(header) >= 12 and header[4:8] == b'ftyp':
        return 'mov,mp4', '.mp4'
    if header.startswith(b'\x00\x00\x01\xba'):
        return 'mpeg', '.ps'
    if len(header) >= 377 and all(header[offset] == 0x47 for offset in (0, 188, 376)):
        return 'mpegts', '.ts'
    if header.startswith(b'\x1aE\xdf\xa3'):
        return 'matroska', '.mkv'
    if header.startswith(b'RIFF') and header[8:12] == b'AVI ':
        return 'avi', '.avi'
    if header.startswith(b'FLV'):
        return 'flv', '.flv'
    return None, '.bin'


def _media_extension(container, fallback='.bin'):
    names = set(container.split(','))
    for name, extension in (('mp4', '.mp4'), ('mov', '.mov'), ('mpeg', '.ps'),
                            ('mpegts', '.ts'), ('matroska', '.mkv'), ('webm', '.webm'),
                            ('avi', '.avi'), ('flv', '.flv'), ('h264', '.h264'), ('hevc', '.h265')):
        if name in names:
            return extension
    return fallback


def _is_link(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _confined_path(path, root, *, allow_missing=False):
    """Validate without converting a symlink into its deletion target."""
    root = Path(root)
    path = Path(os.path.abspath(path))
    if root.resolve() != root or _is_link(root) or not path.is_relative_to(root):
        raise ValueError('Managed path outside its original root')
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if _is_link(current):
            raise ValueError('Managed paths must not contain symlinks or junctions')
    if path.exists():
        if not path.is_file():
            raise ValueError('Managed path must be a regular file')
    elif not allow_missing:
        raise FileNotFoundError(path)
    return path


class Archive:
    def __init__(self, settings):
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', settings.tenant_id):
            raise ValueError('Invalid archive tenant identity')
        self.settings = settings
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        settings.cache_dir.mkdir(parents=True, exist_ok=True)
        self._state_root = settings.state_dir.resolve()
        self._cache_root = settings.cache_dir.resolve()
        self._database_path = _confined_path(self._state_root / 'archive.db', self._state_root, allow_missing=True)
        self.conn = sqlite3.connect(self._database_path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS recordings (
                key TEXT PRIMARY KEY, camera TEXT NOT NULL, record_id TEXT NOT NULL,
                start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL CHECK(end_ms>start_ms),
                source_path TEXT NOT NULL, local_path TEXT, status TEXT NOT NULL,
                duration REAL, codec_video TEXT, codec_audio TEXT, file_size INTEGER,
                sha256 TEXT, chat_id TEXT, message_id INTEGER, file_id TEXT,
                last_error TEXT, attempt_id TEXT, retry_at REAL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS by_recording_date ON recordings(start_ms,end_ms);
            CREATE TABLE IF NOT EXISTS state (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cameras (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
                host TEXT NOT NULL DEFAULT '', device_port INTEGER NOT NULL DEFAULT 8000,
                rtsp_port INTEGER NOT NULL DEFAULT 554, http_port INTEGER NOT NULL DEFAULT 80,
                enabled INTEGER NOT NULL DEFAULT 1, probe_json TEXT, created_at REAL NOT NULL
            );
        ''')
        # Add columns in place under a SQLite writer lock. Camera IDs, stable
        # recording keys and all existing Telegram references stay untouched.
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            columns = {row[1] for row in self.conn.execute('PRAGMA table_info(recordings)')}
            for name, kind in (('file_unique_id', 'TEXT'), ('media_type', 'TEXT'),
                               ('bot_id', 'INTEGER'), ('uploaded_at', 'REAL'), ('cleaned_at', 'REAL'),
                               ('error_started_at', 'REAL'), ('media_expired_at', 'REAL'),
                               ('cleanup_revision', 'INTEGER NOT NULL DEFAULT 0'),
                               ('deleted_at', 'REAL'), ('deleted_by', 'INTEGER'),
                               ('processing_method', "TEXT NOT NULL DEFAULT 'legacy'"),
                               ('media_container', 'TEXT'), ('media_probe_status', 'TEXT'),
                               ('media_extension', 'TEXT'),
                               ('storage_kind', "TEXT NOT NULL DEFAULT 'owner_private'"),
                               ('storage_chat_id', 'INTEGER'), ('storage_message_id', 'INTEGER')):
                if name not in columns:
                    self.conn.execute(f'ALTER TABLE recordings ADD COLUMN {name} {kind}')
            camera_columns = {row[1] for row in self.conn.execute('PRAGMA table_info(cameras)')}
            for name, kind in (('upload_enabled','INTEGER NOT NULL DEFAULT 1'),
                               ('sd_backend',"TEXT NOT NULL DEFAULT 'auto'"),
                               ('sd_username',"TEXT NOT NULL DEFAULT 'admin'"),
                               ('sd_channel','INTEGER NOT NULL DEFAULT 1'),
                               ('sd_timezone',"TEXT NOT NULL DEFAULT 'Asia/Ho_Chi_Minh'"),
                               ('sd_lookback_hours','INTEGER NOT NULL DEFAULT 168')):
                if name not in camera_columns:
                    self.conn.execute(f'ALTER TABLE cameras ADD COLUMN {name} {kind}')
            self.conn.execute('''CREATE TABLE IF NOT EXISTS recording_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, recording_key TEXT NOT NULL,
                actor INTEGER NOT NULL, action TEXT NOT NULL CHECK(action IN ('delete','restore')),
                created_at REAL NOT NULL)''')
            self.conn.execute('''CREATE INDEX IF NOT EXISTS by_active_window
                ON recordings(status,deleted_at,start_ms,end_ms,camera)''')
            self.conn.execute('''CREATE INDEX IF NOT EXISTS by_camera_pipeline
                ON recordings(camera,status,deleted_at,retry_at)''')
            self.conn.execute('''CREATE INDEX IF NOT EXISTS by_camera_pipeline_v2
                ON recordings(camera,status,deleted_at,retry_at,media_expired_at)''')
            self.conn.execute('''CREATE INDEX IF NOT EXISTS by_error_expiry
                ON recordings(status,media_expired_at,error_started_at)''')
            # Historical errors have no reliable first-failure timestamp.
            # Give them the full retention window from this upgrade, once.
            self.conn.execute("""UPDATE recordings SET error_started_at=?
                WHERE status IN ('failed','needs_review','upload_unknown')
                AND error_started_at IS NULL""", (time.time(),))
            self.conn.execute('''CREATE TRIGGER IF NOT EXISTS record_first_error_insert
                AFTER INSERT ON recordings
                WHEN NEW.status IN ('failed','needs_review','upload_unknown') AND NEW.error_started_at IS NULL
                BEGIN UPDATE recordings SET error_started_at=CAST(strftime('%s','now') AS REAL) WHERE key=NEW.key; END''')
            self.conn.execute('''CREATE TRIGGER IF NOT EXISTS record_first_error_update
                AFTER UPDATE OF status ON recordings
                WHEN NEW.status IN ('failed','needs_review','upload_unknown') AND NEW.error_started_at IS NULL
                BEGIN UPDATE recordings SET error_started_at=CAST(strftime('%s','now') AS REAL) WHERE key=NEW.key; END''')
            # Old rows have no known upload timestamp. Start their retention
            # clock at the first migration rather than deleting them early.
            self.conn.execute("INSERT OR IGNORE INTO state(name,value) VALUES('cleanup_legacy_hold_since',?)", (str(time.time()),))
            self.conn.execute("INSERT OR IGNORE INTO state(name,value) VALUES('archive_tenant_id',?)", (settings.tenant_id,))
            if self.conn.execute("SELECT value FROM state WHERE name='archive_tenant_id'").fetchone()[0] != settings.tenant_id:
                raise ValueError('Archive database belongs to a different tenant')
            self.conn.execute('''INSERT OR IGNORE INTO cameras(id,name,created_at)
                SELECT DISTINCT camera,camera,? FROM recordings''',(time.time(),))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            self.conn.close()
            raise

    def close(self):
        self.conn.close()

    @staticmethod
    def _camera_fields(data, partial=False):
        allowed={'id','name','model','host','device_port','rtsp_port','http_port','enabled','upload_enabled',
                 'sd_backend','sd_username','sd_channel','sd_timezone','sd_lookback_hours','sd_password','sd_password_clear'}
        if not isinstance(data,dict) or set(data)-allowed:
            raise ValueError('Unknown camera fields')
        values=dict(data)
        if not partial:
            values={'name':data.get('id',''),'model':'','host':'','device_port':8000,
                    'rtsp_port':554,'http_port':80,'enabled':True,'upload_enabled':True,
                    'sd_backend':'auto','sd_username':'admin','sd_channel':1,
                    'sd_timezone':'Asia/Ho_Chi_Minh','sd_lookback_hours':168,**values}
        if 'id' in values and (not isinstance(values['id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',values['id'])):
            raise ValueError('Camera ID must be a stable ASCII slug')
        for field,maximum in (('name',100),('model',100),('host',253),('sd_username',64)):
            if field not in values:continue
            value=values[field]
            if not isinstance(value,str) or len(value)>maximum or any(ord(c)<32 for c in value):
                raise ValueError('Invalid camera '+field)
            values[field]=value.strip()
        if 'name' in values and not values['name']:raise ValueError('Camera name is required')
        if 'sd_username' in values and not values['sd_username']:raise ValueError('SD username is required')
        if 'sd_username' in values and len(values['sd_username'].encode('utf-8'))>64:
            raise ValueError('SD username exceeds 64 UTF-8 bytes')
        if 'sd_backend' in values and values['sd_backend'] not in ('auto','isapi','hcnetsdk'):
            raise ValueError('Invalid SD backend')
        for field,maximum in (('sd_channel',256),('sd_lookback_hours',720)):
            if field in values and (type(values[field]) is not int or not 1<=values[field]<=maximum):
                raise ValueError('Invalid '+field)
        if 'sd_timezone' in values:
            if not isinstance(values['sd_timezone'],str) or len(values['sd_timezone'])>100:
                raise ValueError('Invalid SD timezone')
            try:get_zone(values['sd_timezone'])
            except (ValueError,KeyError):raise ValueError('Invalid SD timezone') from None
        if 'sd_password' in values:
            password=values['sd_password']
            if not isinstance(password,str) or '\0' in password:
                raise ValueError('Invalid SD password')
            if len(password.encode('utf-8'))>64:raise ValueError('SD password exceeds 64 UTF-8 bytes')
        if 'sd_password_clear' in values and type(values['sd_password_clear']) is not bool:
            raise ValueError('sd_password_clear must be boolean')
        if values.get('sd_password') and values.get('sd_password_clear'):
            raise ValueError('Choose set or clear SD password')
        if values.get('host'):
            host=values['host']
            try:
                ip=ipaddress.ip_address(host)
                if not ip.is_private or ip.is_loopback or ip.is_unspecified or ip.is_multicast:
                    raise ValueError('Use a private LAN camera address')
            except ValueError:
                if not re.fullmatch(r'(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?',host) or re.fullmatch(r'[0-9.]+',host):
                    raise ValueError('Invalid LAN camera host')
        for field in ('device_port','rtsp_port','http_port'):
            if field in values:
                port=values[field]
                if type(port) is not int or not 1<=port<=65535:raise ValueError('Invalid camera port')
        for field in ('enabled','upload_enabled'):
            if field in values:
                if type(values[field]) is not bool:raise ValueError(field+' must be boolean')
                values[field]=int(values[field])
        return values

    def cameras(self):
        rows=self.conn.execute('''SELECT c.*,COUNT(r.key) record_count,
            COALESCE(SUM(r.status='uploaded'),0) uploaded_count FROM cameras c
            LEFT JOIN recordings r ON r.camera=c.id AND r.deleted_at IS NULL
            GROUP BY c.id ORDER BY c.name COLLATE NOCASE,c.id''')
        results=[]
        for row in rows:
            item=dict(row);item['enabled']=bool(item['enabled']);item['upload_enabled']=bool(item['upload_enabled'])
            item['probe']=json.loads(item.pop('probe_json')) if item['probe_json'] else None
            item.pop('probe_json',None)
            path=self._camera_password_path(item['id'])
            item['sd_password_configured']=path.is_file() and path.stat().st_size>0
            results.append(item)
        return results

    def _camera_password_path(self,slug,create=False):
        if not isinstance(slug,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',slug):
            raise ValueError('Invalid camera ID')
        if self.settings.state_dir.resolve()!=self._state_root:
            raise ValueError('State directory changed')
        root=self._state_root/'camera-secrets'
        if _is_link(root):raise ValueError('Camera secrets directory must not be a link')
        if create:
            root.mkdir(exist_ok=True)
            os.chmod(root,0o700)
        return _confined_path(root/(slug+'.password'),self._state_root,allow_missing=True)

    def _write_camera_password(self,slug,password=None,clear=False):
        if not password and not clear:return
        destination=self._camera_password_path(slug,create=True)
        if clear:
            if destination.exists():destination.unlink()
            return
        partial=self._camera_password_path(slug).parent/('.'+slug+'.'+uuid.uuid4().hex+'.partial')
        descriptor=os.open(partial,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        try:
            with os.fdopen(descriptor,'wb') as handle:
                handle.write(password.encode('utf-8'));handle.flush();os.fsync(handle.fileno())
            _confined_path(destination,self._state_root,allow_missing=True)
            os.replace(partial,destination)
        finally:
            if partial.exists():_confined_path(partial,self._state_root).unlink()

    def camera_sd_config(self,slug):
        """Worker-only credentials; never return this dictionary through an API."""
        camera=next((item for item in self.cameras() if item['id']==slug),None)
        if camera is None:raise KeyError('Unknown camera')
        path=self._camera_password_path(slug)
        if path.exists() and path.stat().st_size>4096:raise ValueError('Invalid camera secret')
        camera['sd_password']=path.read_text(encoding='utf-8') if path.is_file() else ''
        return camera

    def camera_name(self,slug):
        row=self.conn.execute('SELECT name FROM cameras WHERE id=?',(slug,)).fetchone()
        return row[0] if row else slug

    def add_camera(self,data):
        values=self._camera_fields(data)
        if not values.get('id'):raise ValueError('Camera ID is required')
        values['created_at']=time.time()
        password=values.pop('sd_password',None);clear=values.pop('sd_password_clear',False)
        try:
            with self.conn:
                self.conn.execute('INSERT INTO cameras('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',tuple(values.values()))
                self._write_camera_password(values['id'],password,clear)
        except sqlite3.IntegrityError:
            raise ValueError('Camera ID already exists') from None
        return next(c for c in self.cameras() if c['id']==values['id'])

    def update_camera(self,slug,data):
        if not isinstance(data,dict) or 'id' in data:raise ValueError('Camera ID is immutable')
        values=self._camera_fields(data,partial=True)
        if not values:raise ValueError('No changes provided')
        password=values.pop('sd_password',None);clear=values.pop('sd_password_clear',False)
        if set(values)&{'host','device_port','rtsp_port','http_port'}:values['probe_json']=None
        with self.conn:
            if values:
                changed=self.conn.execute('UPDATE cameras SET '+','.join(k+'=?' for k in values)+' WHERE id=?',(*values.values(),slug)).rowcount
            else:
                self.conn.execute('BEGIN IMMEDIATE')
                changed=self.conn.execute('SELECT 1 FROM cameras WHERE id=?',(slug,)).fetchone() is not None
            if not changed:raise KeyError('Unknown camera')
            self._write_camera_password(slug,password,clear)
        if not changed:raise KeyError('Unknown camera')
        return next(c for c in self.cameras() if c['id']==slug)

    def probe_camera(self,slug):
        camera=next((c for c in self.cameras() if c['id']==slug),None)
        if camera is None:raise KeyError('Unknown camera')
        if not camera['host']:raise ValueError('Camera host is not configured')
        addresses=[]
        for _,_,_,_,addr in socket.getaddrinfo(camera['host'],None,type=socket.SOCK_STREAM):
            ip=ipaddress.ip_address(addr[0])
            if not ip.is_private or ip.is_loopback or ip.is_unspecified or ip.is_multicast:
                raise ValueError('Probe requires a private LAN camera address')
            if addr[0] not in addresses:addresses.append(addr[0])
        if not addresses:raise ValueError('Camera address unresolved')
        result={'host':camera['host'],'checked_at':time.time(),'tcp':{},
                'sd_download':'unverified','model_identity':'user_entered_not_verified'}
        for port in dict.fromkeys(camera[p] for p in ('device_port','http_port','rtsp_port')):
            try:
                with socket.create_connection((addresses[0],port),timeout=3):result['tcp'][str(port)]='open'
            except OSError:result['tcp'][str(port)]='unconfirmed'
        with self.conn:self.conn.execute('UPDATE cameras SET probe_json=? WHERE id=?',(json.dumps(result),slug))
        return result

    def telegram_url(self, row):
        username = self.settings.bot_username or self.state('telegram_bot_username') or ''
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', username):
            return None
        if row['status'] != 'uploaded' or not row['file_id'] or not re.fullmatch(r'[a-f0-9]{64}', row['key']):
            return None
        # Check the durable row too: a stale menu/row must not produce a replay
        # link after another allowed viewer has moved this clip to trash.
        current = self.conn.execute('SELECT deleted_at FROM recordings WHERE key=?', (row['key'],)).fetchone()
        if current is not None and current['deleted_at'] is not None:
            return None
        return f"https://t.me/{username}?start=play_{row['key'][:32]}"

    def calendar(self,camera):
        zone=get_zone(self.settings.timezone);tree={}
        for start,end in self.conn.execute("SELECT start_ms,end_ms FROM recordings WHERE camera=? AND status='uploaded' AND deleted_at IS NULL",(camera,)):
            first=from_epoch_ms(start,zone).date()
            last=from_epoch_ms(end-1,zone).date()
            while first<=last:
                tree.setdefault(first.year,{}).setdefault(first.month,set()).add(first.day)
                first+=timedelta(days=1)
        return {'years':[{'year':y,'months':[{'month':m,'days':sorted(days)} for m,days in sorted(months.items())]} for y,months in sorted(tree.items())]}

    def browse(self,camera=None,year=None,month=None,day=None,order='asc',status='uploaded',offset=0,limit=25):
        if order not in ('asc','desc') or status not in ('uploaded','all'):raise ValueError('Invalid archive filter')
        self._pagination(offset, limit)
        clauses=['r.deleted_at IS NULL'];params=[]
        if status!='all':clauses.append('r.status=?');params.append(status)
        if camera:clauses.append('r.camera=?');params.append(camera)
        if day is not None and month is None or month is not None and year is None:raise ValueError('Date filters require year/month')
        if year is not None:
            year=int(year);month=int(month) if month is not None else None;day=int(day) if day is not None else None
            if not 1<=year<=9998 or (month is not None and not 1<=month<=12) or (day is not None and not 1<=day<=31):raise ValueError('Invalid calendar date')
            start=datetime(year,month or 1,day or 1,tzinfo=get_zone(self.settings.timezone))
            if day is not None:finish=start+timedelta(days=1)
            elif month is not None:finish=datetime(year+(month==12),1 if month==12 else month+1,1,tzinfo=start.tzinfo)
            else:finish=datetime(year+1,1,1,tzinfo=start.tzinfo)
            clauses.extend(('r.start_ms<?','r.end_ms>?'));params.extend((int(finish.timestamp()*1000),int(start.timestamp()*1000)))
        where=' WHERE '+' AND '.join(clauses) if clauses else ''
        total=self.conn.execute('SELECT COUNT(*) FROM recordings r'+where,params).fetchone()[0]
        direction='ASC' if order=='asc' else 'DESC'
        rows=self.conn.execute('SELECT r.*,COALESCE(c.name,r.camera) camera_name FROM recordings r LEFT JOIN cameras c ON c.id=r.camera'+where+' ORDER BY r.start_ms '+direction+',r.key '+direction+' LIMIT ? OFFSET ?',(*params,limit,offset))
        # API intentionally excludes host paths, errors, attempt IDs and Telegram file IDs.
        public=('key','camera','camera_name','start_ms','end_ms','status','duration','file_size')
        recordings=[{**{k:row[k] for k in public},'telegram_url':self.telegram_url(row),
                     'telegram_available':row['status']=='uploaded' and bool(row['file_id'])} for row in rows]
        return {'recordings':recordings,'total':total,'offset':offset,'limit':limit}

    @staticmethod
    def _pagination(offset, limit):
        if type(offset) is not int or type(limit) is not int or not 0 <= offset <= 1000000 or not 1 <= limit <= 100:
            raise ValueError('Invalid pagination')

    @staticmethod
    def _window(start_ms, end_ms):
        # UTC milliseconds remain independent of the display timezone. Limit
        # menu windows so malformed callbacks cannot run unbounded catalogs.
        if type(start_ms) is not int or type(end_ms) is not int:
            raise ValueError('Window bounds must be integer UTC milliseconds')
        maximum = 4133980800000  # 2101-01-01T00:00:00Z; includes all of 2100.
        # A 31-local-calendar-day selection can include a DST fall-back hour.
        # TimeMenus enforces the calendar limit; the catalog keeps a hard ceiling.
        if not 0 <= start_ms < end_ms <= maximum or end_ms - start_ms > 32 * 86400000:
            raise ValueError('Invalid archive window')

    def list_window(self, start_ms, end_ms, camera=None, order='asc', offset=0, limit=10):
        """Return bot-internal metadata for uploaded clips overlapping [start,end)."""
        self._window(start_ms, end_ms)
        self._pagination(offset, limit)
        if order not in ('asc', 'desc'):
            raise ValueError('Invalid archive sort order')
        if camera is not None and (not isinstance(camera, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', camera)):
            raise ValueError('Invalid camera ID')
        clauses = ["r.status='uploaded'", 'r.deleted_at IS NULL', 'r.start_ms<?', 'r.end_ms>?']
        values = [end_ms, start_ms]
        if camera is not None:
            clauses.append('r.camera=?')
            values.append(camera)
        where = ' WHERE ' + ' AND '.join(clauses)
        total = self.conn.execute('SELECT COUNT(*) FROM recordings r' + where, values).fetchone()[0]
        direction = 'ASC' if order == 'asc' else 'DESC'
        rows = self.conn.execute('''SELECT r.*,COALESCE(c.name,r.camera) camera_name
            FROM recordings r LEFT JOIN cameras c ON c.id=r.camera''' + where +
            ' ORDER BY r.start_ms ' + direction + ',r.key ' + direction + ' LIMIT ? OFFSET ?',
            (*values, limit, offset))
        return {'recordings': [dict(row) for row in rows], 'total': total, 'offset': offset, 'limit': limit}

    def window_cameras(self, start_ms, end_ms, offset=0, limit=10):
        self._window(start_ms, end_ms)
        self._pagination(offset, limit)
        rows = self.conn.execute('''SELECT r.camera id,COALESCE(c.name,r.camera) name,COUNT(*) count
            FROM recordings r LEFT JOIN cameras c ON c.id=r.camera
            WHERE r.status='uploaded' AND r.deleted_at IS NULL AND r.start_ms<? AND r.end_ms>?
            GROUP BY r.camera''', (end_ms, start_ms))
        # SQLite NOCASE handles ASCII only; casefold also orders Vietnamese
        # names deterministically without changing stable camera IDs.
        cameras = sorted((dict(row) for row in rows), key=lambda item: (item['name'].casefold(), item['id']))
        return {'cameras': cameras[offset:offset+limit], 'total': len(cameras), 'offset': offset, 'limit': limit}

    def find_recording(self, prefix, include_deleted=False):
        if not isinstance(prefix, str) or not re.fullmatch(r'[a-f0-9]{32}(?:[a-f0-9]{32})?', prefix):
            return None
        if type(include_deleted) is not bool:
            raise ValueError('Invalid deleted filter')
        sql = "SELECT r.*,COALESCE(c.name,r.camera) camera_name FROM recordings r LEFT JOIN cameras c ON c.id=r.camera WHERE r.status='uploaded' AND r.key LIKE ?"
        if not include_deleted:
            sql += ' AND r.deleted_at IS NULL'
        rows = self.conn.execute(sql + ' LIMIT 2', (prefix+'%',)).fetchall()
        return dict(rows[0]) if len(rows) == 1 else None

    def _archive_actor(self, actor):
        allowed = {value for value in self.settings.allowed_users if type(value) is int and value > 0}
        if self.settings.effective_owner:
            allowed.add(self.settings.effective_owner)
        if type(actor) is not int or actor <= 0 or actor not in allowed:
            raise PermissionError('Archive access requires an allowed private user ID')

    def _recording_mutation(self, key, actor, restore):
        self._archive_actor(actor)
        if not isinstance(key, str) or not re.fullmatch(r'[a-f0-9]{64}', key):
            raise ValueError('A full stable recording key is required')
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            row = self.conn.execute("SELECT deleted_at FROM recordings WHERE key=? AND status='uploaded'", (key,)).fetchone()
            changed = row is not None and (row['deleted_at'] is not None if restore else row['deleted_at'] is None)
            if changed:
                now = time.time()
                if restore:
                    self.conn.execute('UPDATE recordings SET deleted_at=NULL,deleted_by=NULL WHERE key=?', (key,))
                else:
                    self.conn.execute('UPDATE recordings SET deleted_at=?,deleted_by=? WHERE key=?', (now, actor, key))
                self.conn.execute('INSERT INTO recording_audit(recording_key,actor,action,created_at) VALUES(?,?,?,?)',
                                  (key, actor, 'restore' if restore else 'delete', now))
            self.conn.commit()
            return changed
        except Exception:
            self.conn.rollback()
            raise

    def soft_delete(self, key, actor):
        """Hide one shared clip atomically, retaining Telegram IDs for undo."""
        return self._recording_mutation(key, actor, False)

    def restore_recording(self, key, actor):
        return self._recording_mutation(key, actor, True)

    def trash(self, offset=0, limit=10, order='desc'):
        """Bot-internal trash metadata; the caller authenticates its private chat."""
        self._pagination(offset, limit)
        if order not in ('asc', 'desc'):
            raise ValueError('Invalid archive sort order')
        where = " WHERE r.status='uploaded' AND r.deleted_at IS NOT NULL"
        total = self.conn.execute('SELECT COUNT(*) FROM recordings r'+where).fetchone()[0]
        direction = 'ASC' if order == 'asc' else 'DESC'
        rows = self.conn.execute('''SELECT r.*,COALESCE(c.name,r.camera) camera_name FROM recordings r
            LEFT JOIN cameras c ON c.id=r.camera''' + where + ' ORDER BY r.deleted_at '+direction+',r.key '+direction+' LIMIT ? OFFSET ?',
            (limit, offset))
        return {'recordings': [dict(row) for row in rows], 'total': total, 'offset': offset, 'limit': limit}

    def ingest_entry(self, entry, dry_run=False):
        return self._ingest_entry(entry,dry_run)

    def ingest_download(self,entry,path,dry_run=False):
        """Accept only an adapter-owned, camera-scoped file inside cache/sd-stage."""
        record_key(entry)
        source=_confined_path(path,self._cache_root)
        root=self._cache_root/'sd-stage'/entry['camera']
        if not source.is_relative_to(root):raise ValueError('Download source outside camera staging directory')
        return self._ingest_entry(dict(entry,path=str(source)),dry_run,source)

    def _ingest_entry(self, entry, dry_run=False, downloaded_source=None):
        key = record_key(entry)
        start = parse_time(entry['start_time'])
        end = parse_time(entry['end_time'])
        if end <= start:
            raise ValueError('end_time must be greater than start_time')
        if end-start>timedelta(days=7):raise ValueError('Recording interval exceeds seven days')
        start_ms, end_ms = int(start.timestamp()*1000), int(end.timestamp()*1000)
        expired = self.conn.execute('SELECT * FROM recordings WHERE key=? AND media_expired_at IS NOT NULL', (key,)).fetchone()
        if expired is not None and not dry_run:
            if (expired['start_ms'] != start_ms or expired['end_ms'] != end_ms
                    or expired['camera'] != entry['camera']):
                raise ValueError('Expired recording identity changed; operator review required')
            return dict(expired, record_key=key)
        source = resolve_input(entry['path'], self.settings.input_dir) if downloaded_source is None else downloaded_source
        if dry_run:
            return {'key': key, 'record_key': key, 'camera': entry['camera'], 'status': 'validated', 'source_bytes': source.stat().st_size}
        with self.conn:
            self.conn.execute('INSERT OR IGNORE INTO cameras(id,name,created_at) VALUES(?,?,?)',(entry['camera'],entry['camera'],time.time()))
        camera=self.conn.execute('SELECT enabled FROM cameras WHERE id=?',(entry['camera'],)).fetchone()
        if camera and not camera[0]:return {'key':key,'record_key':key,'status':'camera_disabled'}
        old = self.conn.execute('SELECT * FROM recordings WHERE key=?', (key,)).fetchone()
        if old:
            if old['start_ms'] != start_ms or old['camera'] != entry['camera']:
                raise ValueError('Stable record_id collision; do not merge different recording')
            if old['media_expired_at'] is not None or old['status'] in ('downloaded', 'uploading', 'upload_unknown', 'uploaded', 'needs_review'):
                # Closed clips are immutable after ingest; avoid changing archived time retroactively.
                if old['end_ms'] != end_ms:
                    raise ValueError('Closed recording end_time changed; operator review required')
                return dict(old, record_key=key)
        in_cache = sum(p.stat().st_size for p in self.settings.cache_dir.rglob('*') if p.is_file())
        needed = source.stat().st_size * (2 if self.settings.media_mode == 'remux_copy' else 1)
        free = shutil.disk_usage(self.settings.cache_dir).free
        if in_cache + needed > self.settings.cache_max_bytes or free - needed < self.settings.min_free_bytes:
            raise ValueError('Cache budget reached; source was retained')
        dest = _confined_path(self._cache_root / (key + '.mp4'), self._cache_root, allow_missing=True)
        with self.conn:
            self.conn.execute('''INSERT INTO recordings(key,camera,record_id,start_ms,end_ms,source_path,status,created_at)
                VALUES(?,?,?,?,?,?,'ingesting',?) ON CONFLICT(key) DO UPDATE SET status='ingesting',end_ms=excluded.end_ms''',
                (key, entry['camera'], str(entry['record_id']), start_ms, end_ms, str(source), time.time()))
        try:
            info = normalize(source, dest, self.settings)
            dest = self._store_media(key, info, dest, (end-start).total_seconds())
            # Remove only this row's prior managed converted copy, after the
            # original-byte copy and its durable catalog reference exist.
            if (old and old['local_path'] and old['local_path'] != str(dest)
                    and re.fullmatch(re.escape(key) + r'\.[a-z0-9]{1,10}', Path(old['local_path']).name)):
                prior = _confined_path(old['local_path'], self._cache_root, allow_missing=True)
                if prior.exists():
                    prior.unlink()
        except Exception as exc:
            if self.settings.media_mode == 'remux_copy':
                try:
                    self._retain_raw_remux_failure(key, source, (end-start).total_seconds())
                except Exception:
                    with self.conn:
                        self.conn.execute("UPDATE recordings SET status='needs_review',last_error='failed_remux' WHERE key=?", (key,))
                raise
            with self.conn:
                self.conn.execute("UPDATE recordings SET status='failed',last_error=? WHERE key=?", (type(exc).__name__, key))
            raise
        return dict(self.conn.execute('SELECT * FROM recordings WHERE key=?', (key,)).fetchone(), record_key=key)

    def _store_media(self, key, info, fallback_path, fallback_duration, *, status='downloaded', error=None):
        """Persist one confirmed local artifact without changing stable identity."""
        if status not in ('downloaded', 'needs_review'):
            raise ValueError('Invalid ingested media status')
        processing = info.get('processing_method', 'passthrough')
        if processing not in ('passthrough', 'remux_copy'):
            raise ValueError('Invalid media processing method')
        path = _confined_path(info.get('path', fallback_path), self._cache_root)
        digest = info.get('sha256')
        if not digest:
            with path.open('rb') as handle:
                digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        duration = info['duration'] if info.get('duration') is not None else fallback_duration
        with self.conn:
            self.conn.execute('''UPDATE recordings SET local_path=?,status=?,duration=?,codec_video=?,
                codec_audio=?,file_size=?,sha256=?,last_error=?,processing_method=?,
                media_container=?,media_probe_status=?,media_extension=? WHERE key=?''',
                (str(path), status, duration, info.get('codec_video'), info.get('codec_audio'), info['bytes'], digest,
                 error, processing, info.get('container'), info.get('probe_status', 'disabled'),
                 info.get('file_extension', path.suffix), key))
        return path

    def _retain_raw_remux_failure(self, key, source, duration):
        """Keep an original cached copy when MP4 cannot retain its codecs."""
        dest = _confined_path(self._cache_root/(key+'.mp4'), self._cache_root, allow_missing=True)
        info = _copy_original(source, dest, self.settings, probe_metadata=False)
        return self._store_media(key, info, dest, duration, status='needs_review', error='failed_remux')

    def ingest_manifest(self, path, dry_run=False, continue_on_error=False, camera=None):
        if camera is not None and (not isinstance(camera,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',camera)):
            raise ValueError('Invalid camera filter')
        path = resolve_input(path, self.settings.input_dir)
        if path.stat().st_size > 1000000:
            raise ValueError('Manifest too large')
        document = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(document,dict) or not isinstance(document.get('recordings'), list) or len(document['recordings']) > 1000:
            raise ValueError('Manifest requires recordings list (max 1000 entries)')
        results=[]
        for entry in document['recordings']:
            if camera is not None and (not isinstance(entry,dict) or entry.get('camera')!=camera):
                continue
            try:
                results.append(self.ingest_entry(entry, dry_run))
            except Exception as exc:
                if not continue_on_error:raise
                results.append({'status':'failed','error_type':type(exc).__name__})
        return results

    def list_day(self, day, camera=None, order='asc'):
        if order not in ('asc','desc'):raise ValueError('Invalid archive sort order')
        local = datetime.fromisoformat(day)
        if local.time() != datetime.min.time() or local.tzinfo is not None or len(day) != 10:
            raise ValueError('Expected YYYY-MM-DD')
        begin = local.replace(tzinfo=get_zone(self.settings.timezone))
        finish = begin + timedelta(days=1)
        sql = "SELECT * FROM recordings WHERE status='uploaded' AND deleted_at IS NULL AND start_ms<? AND end_ms>?"
        values = [int(finish.timestamp()*1000), int(begin.timestamp()*1000)]
        if camera is not None:
            sql += ' AND camera=?'
            values.append(camera)
        direction='ASC' if order=='asc' else 'DESC'
        return [dict(row) for row in self.conn.execute(sql + ' ORDER BY start_ms '+direction+',key '+direction, values)]

    def claim_upload(self,camera=None):
        if camera is not None and (not isinstance(camera,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',camera)):
            raise ValueError('Invalid camera filter')
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            sql="SELECT r.* FROM recordings r JOIN cameras c ON c.id=r.camera WHERE r.status='downloaded' AND r.media_expired_at IS NULL AND r.deleted_at IS NULL AND c.enabled=1 AND c.upload_enabled=1 AND r.retry_at<=?"
            params=[time.time()]
            if camera is not None:sql+=' AND r.camera=?';params.append(camera)
            row = self.conn.execute(sql+' ORDER BY r.start_ms,r.key LIMIT 1',params).fetchone()
            if row is None:
                self.conn.commit()
                return None
            attempt = hashlib.sha256(os.urandom(32)).hexdigest()
            self.conn.execute("UPDATE recordings SET status='uploading',attempt_id=? WHERE key=?", (attempt,row['key']))
            self.conn.commit()
            return dict(self.conn.execute('SELECT * FROM recordings WHERE key=?', (row['key'],)).fetchone())
        except Exception:
            self.conn.rollback()
            raise

    def mark_uploaded(self, key, chat_id, message_id, file_id, file_unique_id=None, media_type=None, bot_id=None,
                      *, storage_kind=None, storage_chat_id=None, storage_message_id=None):
        if not chat_id or int(message_id) <= 0 or not isinstance(file_id, str) or not file_id.strip():
            raise ValueError('Confirmed Telegram metadata required')
        if file_unique_id is not None and (not isinstance(file_unique_id, str) or not file_unique_id.strip()):
            raise ValueError('Invalid Telegram file_unique_id')
        if media_type not in (None, 'video', 'document'):
            raise ValueError('Telegram media_type must be video or document')
        if bot_id is not None and (type(bot_id) is not int or bot_id <= 0):
            raise ValueError('Invalid Telegram bot identity')
        placement = storage_kind or ('channel' if int(chat_id)<0 else 'owner_private')
        if placement not in ('channel', 'owner_private'):
            raise ValueError('Invalid Telegram storage kind')
        if placement == 'channel':
            source_chat = int(chat_id) if storage_chat_id is None else storage_chat_id
            source_message = int(message_id) if storage_message_id is None else storage_message_id
            if (type(source_chat) is not int or source_chat>=0 or source_chat!=int(chat_id)
                    or type(source_message) is not int or source_message<=0 or source_message!=int(message_id)):
                raise ValueError('Confirmed channel placement is inconsistent')
        else:
            if int(chat_id)<=0 or storage_chat_id is not None or storage_message_id is not None:
                raise ValueError('Private placement is inconsistent')
            source_chat=source_message=None
        with self.conn:
            changed = self.conn.execute("""UPDATE recordings SET status='uploaded',chat_id=?,message_id=?,file_id=?,
                file_unique_id=COALESCE(?,file_unique_id),media_type=COALESCE(?,media_type),bot_id=COALESCE(?,bot_id),
                uploaded_at=COALESCE(uploaded_at,?),last_error=NULL,
                storage_kind=?,storage_chat_id=?,storage_message_id=? WHERE key=?""",
                (str(chat_id),int(message_id),file_id,file_unique_id,media_type,bot_id,time.time(),
                 placement,source_chat,source_message,key)).rowcount
        if not changed:
            raise ValueError('Unknown record key')

    def recover_uploads(self):
        with self.conn:
            return self.conn.execute("UPDATE recordings SET status='upload_unknown',last_error='process_restart_after_claim' WHERE status='uploading'").rowcount

    def recover_ingests(self):
        """Exclusive worker startup only; no Telegram attempt exists yet."""
        rows=self.conn.execute("SELECT key FROM recordings WHERE status='ingesting' AND deleted_at IS NULL AND file_id IS NULL AND message_id IS NULL AND chat_id IS NULL").fetchall()
        for row in rows:
            if not re.fullmatch(r'[0-9a-f]{64}',row['key']):raise ValueError('Invalid managed recording key')
            # The old remux partial (.part.mp4) and new original-format copy
            # partials share a generated stable key; never delete unknown files.
            for candidate in self._cache_root.glob(row['key'] + '.part.*'):
                if not re.fullmatch(re.escape(row['key']) + r'\.part\.[a-z0-9]{1,10}', candidate.name):
                    continue
                partial=_confined_path(candidate,self._cache_root)
                partial.unlink()
        with self.conn:
            return self.conn.execute("UPDATE recordings SET status='failed',last_error='worker_restarted_during_ingest' WHERE status='ingesting' AND deleted_at IS NULL AND file_id IS NULL AND message_id IS NULL AND chat_id IS NULL").rowcount

    def invalidate_legacy_cache(self, camera=None):
        """Request raw re-ingest only for unposted, unambiguous old media.

        An operator can call this once when migrating a deployed remux cache.
        Existing posted, in-flight, uncertain and deleted clips remain intact;
        no Telegram retry or source deletion occurs here.
        """
        if camera is not None and (not isinstance(camera, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', camera)):
            raise ValueError('Invalid camera filter')
        current_modes = ('remux_copy',) if self.settings.media_mode == 'remux_copy' else ('passthrough', 'remux_copy')
        placeholders = ','.join('?' for _ in current_modes)
        sql = f"""UPDATE recordings SET status='failed',last_error='raw_reingest_required'
            WHERE processing_method NOT IN ({placeholders}) AND status IN ('downloaded','failed') AND media_expired_at IS NULL
            AND deleted_at IS NULL AND file_id IS NULL AND message_id IS NULL AND chat_id IS NULL
            AND COALESCE(last_error,'')!='raw_reingest_required'"""
        values = list(current_modes)
        if camera is not None:
            sql += ' AND camera=?'
            values.append(camera)
        with self.conn:
            return self.conn.execute(sql, values).rowcount

    def remux_pending_cache(self, camera=None, limit=5):
        """Exclusive-worker migration: reuse unposted raw cache, not the SD card.

        Posted/in-flight/uncertain/deleted rows never enter this operation. A
        row is atomically made non-uploadable before starting FFmpeg; failure
        retains its original bytes and goes to explicit operator review.
        """
        result = {'converted': 0, 'failed': 0}
        if camera is not None and (not isinstance(camera, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', camera)):
            raise ValueError('Invalid camera filter')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid pending remux batch size')
        if self.settings.media_mode != 'remux_copy':
            return result
        where = """processing_method='passthrough' AND status IN ('downloaded','failed') AND media_expired_at IS NULL
            AND deleted_at IS NULL AND file_id IS NULL AND message_id IS NULL AND chat_id IS NULL
            AND local_path IS NOT NULL AND local_path!=''"""
        values = []
        if camera is not None:
            where += ' AND camera=?'
            values.append(camera)
        rows = self.conn.execute('SELECT * FROM recordings WHERE '+where+' ORDER BY start_ms,key LIMIT ?', [*values, limit]).fetchall()
        for row in rows:
            if not re.fullmatch(r'[0-9a-f]{64}', row['key']):
                raise ValueError('Invalid managed recording key')
            # Reserve worst-case MP4 working space before taking the row away
            # from its current pending state. A full disk is backpressure, not
            # an unsupported-codec failure, and must not strand valid raw data
            # in needs_review or repeatedly claim it in a limit=1 caller loop.
            try:
                existing_source = _confined_path(row['local_path'], self._cache_root)
                source_size = existing_source.stat().st_size
            except (OSError, TypeError, ValueError):
                # The existing claim/error path below diagnoses missing or
                # invalid paths atomically and preserves uncertain row guards.
                pass
            else:
                used = sum(path.stat().st_size for path in self._cache_root.rglob('*') if path.is_file())
                reserved = source_size * 2
                free = shutil.disk_usage(self._cache_root).free
                if used + reserved > self.settings.cache_max_bytes or free - reserved < self.settings.min_free_bytes:
                    return dict(result, budget_blocked=1)
            with self.conn:
                claimed = self.conn.execute("UPDATE recordings SET status='ingesting' WHERE key=? AND "+where,
                                            [row['key'], *values]).rowcount
            if not claimed:
                continue
            try:
                source = _confined_path(row['local_path'], self._cache_root)
                dest = _confined_path(self._cache_root/(row['key']+'.mp4'), self._cache_root, allow_missing=True)
                info = normalize(source, dest, self.settings)
                saved = self._store_media(row['key'], info, dest, (row['end_ms']-row['start_ms'])/1000)
                if source != saved and re.fullmatch(re.escape(row['key'])+r'\.[a-z0-9]{1,10}', source.name):
                    _confined_path(source, self._cache_root).unlink()
                result['converted'] += 1
            except FileNotFoundError:
                with self.conn:
                    self.conn.execute("UPDATE recordings SET status='failed',last_error='raw_reingest_required',local_path=NULL WHERE key=?", (row['key'],))
                result['failed'] += 1
            except Exception:
                # The previous raw local_path and its SHA remain referenced;
                # never retry it as a document or alter Telegram references.
                with self.conn:
                    self.conn.execute("UPDATE recordings SET status='needs_review',last_error='failed_remux' WHERE key=?", (row['key'],))
                result['failed'] += 1
        return result

    def _managed_media_paths(self, row):
        """One recording's generated files only; never input originals or SD."""
        key = row['key']
        if not re.fullmatch(r'[a-f0-9]{64}', key):
            raise ValueError('Invalid managed recording key')
        if self.settings.cache_dir.resolve() != self._cache_root:
            raise ValueError('Cache directory changed')
        paths = []
        if row['local_path']:
            paths.append(_confined_path(row['local_path'], self._cache_root, allow_missing=True))
        generated = re.compile(re.escape(key) + r'\.(?:part\.)?[a-z0-9]{1,10}')
        for path in self._cache_root.glob(key + '.*'):
            if generated.fullmatch(path.name):
                paths.append(_confined_path(path, self._cache_root, allow_missing=True))
        camera = row['camera']
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', camera):
            raise ValueError('Invalid managed camera ID')
        stage = self._cache_root / 'sd-stage' / camera
        for directory in (stage.parent, stage):
            if _is_link(directory) or (directory.exists() and not directory.is_dir()):
                raise ValueError('Invalid managed staging directory')
        if stage.exists():
            staged = re.compile(re.escape(key) + r'\.[a-f0-9]{32}\.(?:part|source)')
            for path in stage.glob(key + '.*'):
                if staged.fullmatch(path.name):
                    paths.append(_confined_path(path, self._cache_root, allow_missing=True))
        # Validate every candidate before the first unlink. Repeated paths are
        # harmless, but a stale cleaned_at must not hide PS/TS/staging siblings.
        return list(dict.fromkeys(paths))

    def cleanup(self, key):
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            row = self.conn.execute('SELECT * FROM recordings WHERE key=?', (key,)).fetchone()
            if (self.settings.keep_cache or row is None or row['status'] != 'uploaded'
                    or not all(row[n] for n in ('chat_id','message_id','file_id'))):
                self.conn.commit()
                return False
            retention = self.settings.cache_retention_hours
            if not math.isfinite(retention) or retention < 0:
                raise ValueError('Invalid cache retention')
            now = time.time()
            if retention:
                start = row['uploaded_at'] or self.state('cleanup_legacy_hold_since')
                try:
                    start = float(start)
                except (ValueError, TypeError):
                    self.conn.commit()
                    return False
                if not math.isfinite(start) or now-start < retention*3600:
                    self.conn.commit()
                    return False
            paths = self._managed_media_paths(row)
            existing = [path for path in paths if path.exists()]
            for path in existing:
                path.unlink(missing_ok=True)
            changed = bool(existing) or row['cleaned_at'] is None
            self.conn.execute('UPDATE recordings SET cleaned_at=COALESCE(cleaned_at,?),cleanup_revision=1 WHERE key=?', (now,key))
            self.conn.commit()
            return changed
        except Exception:
            self.conn.rollback()
            raise

    def expire_error_media(self, now=None, limit=100):
        """Expire terminal-error media, retaining identity/status/Telegram IDs."""
        now = time.time() if now is None else now
        hours = self.settings.error_retention_hours
        if (not math.isfinite(now) or not math.isfinite(hours) or hours <= 0
                or type(limit) is not int or not 1 <= limit <= 1000):
            raise ValueError('Invalid error media retention')
        cutoff = now-hours*3600
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            rows = self.conn.execute("""SELECT * FROM recordings
                WHERE status IN ('failed','needs_review','upload_unknown')
                AND ((media_expired_at IS NULL AND error_started_at<=?)
                    OR (media_expired_at IS NOT NULL AND cleanup_revision<1))
                ORDER BY error_started_at,key LIMIT ?""", (cutoff,limit)).fetchall()
            # No active upload/ingest enters this transaction; retaining the
            # tombstone prevents SD/manifest scans from recreating expired data.
            plans = [(row,self._managed_media_paths(row)) for row in rows]
            for row,_ in plans:
                # Commit expiry BEFORE filesystem mutation. A crash or unlink
                # error can never roll back the tombstone and cause re-download.
                self.conn.execute('UPDATE recordings SET media_expired_at=COALESCE(media_expired_at,?) WHERE key=?', (now,row['key']))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        for row,paths in plans:
            for path in paths:
                path.unlink(missing_ok=True)
            with self.conn:
                self.conn.execute('''UPDATE recordings SET cleaned_at=COALESCE(cleaned_at,?),
                    cleanup_revision=1 WHERE key=? AND media_expired_at IS NOT NULL''', (now,row['key']))
        return len(rows)

    @contextmanager
    def _backup_lock(self, root):
        path = _confined_path(root / '.backup.lock', root, allow_missing=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, 'r+b') as lock:
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                raise ValueError('Backup lock must be a regular file')
            acquired = False
            try:
                if os.name == 'nt':
                    import msvcrt
                    lock.seek(0, 2)
                    if lock.tell() == 0:
                        lock.write(b'0')
                        lock.flush()
                    lock.seek(0)
                    try:
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                        acquired = True
                    except OSError:
                        pass
                else:
                    import fcntl
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                    except BlockingIOError:
                        pass
                yield acquired
            finally:
                if acquired:
                    if os.name == 'nt':
                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(lock, fcntl.LOCK_UN)

    def backup_daily(self, retention_days=7):
        """Atomically snapshot the committed SQLite catalog; never cache media."""
        if type(retention_days) is not int or retention_days < 7:
            raise ValueError('Keep at least seven daily database backups')
        if self.settings.state_dir.resolve() != self._state_root:
            raise ValueError('State directory changed')
        root = self._state_root / 'backups'
        if _is_link(root):
            raise ValueError('Backup directory must not be a symlink or junction')
        root.mkdir(exist_ok=True)
        if root.resolve() != root:
            raise ValueError('Backup directory outside state')
        today = from_epoch_ms(time.time()*1000, get_zone(self.settings.timezone)).date()
        destination = _confined_path(root / f'archive-{today.isoformat()}.db', root, allow_missing=True)
        with self._backup_lock(root) as acquired:
            if not acquired or destination.exists():
                return None
            source = _confined_path(self._database_path, self._state_root)
            partial = _confined_path(root / f'.archive-{today.isoformat()}.{uuid.uuid4().hex}.partial', root, allow_missing=True)
            descriptor = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            os.close(descriptor)
            try:
                # Read through a separate connection so an in-flight writer's
                # uncommitted changes cannot leak into the snapshot.
                with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=30)) as original:
                    with closing(sqlite3.connect(partial, timeout=30)) as snapshot:
                        original.backup(snapshot)
                        if snapshot.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                            raise ValueError('SQLite backup verification failed')
                with partial.open('r+b') as handle:
                    os.fsync(handle.fileno())
                _confined_path(destination, root, allow_missing=True)
                os.replace(partial, destination)
                if os.name != 'nt':
                    directory = os.open(root, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                cutoff = today - timedelta(days=retention_days - 1)
                managed = []
                for candidate in root.iterdir():
                    match = re.fullmatch(r'archive-(\d{4}-\d{2}-\d{2})\.db', candidate.name)
                    if match and not _is_link(candidate) and candidate.is_file():
                        try:
                            date = datetime.strptime(match[1], '%Y-%m-%d').date()
                        except ValueError:
                            continue
                        managed.append((date, candidate))
                managed.sort(reverse=True)
                # Also retain the seven most recent existing snapshots if
                # outages left gaps between calendar days.
                for date, candidate in managed[7:]:
                    if date < cutoff:
                        _confined_path(candidate, root).unlink()
                return destination
            finally:
                if partial.exists():
                    _confined_path(partial, root).unlink()

    def state(self, name, value=None):
        if value is None:
            row = self.conn.execute('SELECT value FROM state WHERE name=?', (name,)).fetchone()
            return row[0] if row else None
        with self.conn:
            self.conn.execute('INSERT INTO state(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value', (name,str(value)))

    def status(self):
        camera_sources=[{'camera_id':camera['id'],'enabled':camera['enabled'],
                         'backend':camera['sd_backend'],'configured':bool(camera['host'] and camera['sd_password_configured']),
                         'password_configured':camera['sd_password_configured']} for camera in self.cameras()]
        return {'queue':dict(self.conn.execute('SELECT status,count(*) FROM recordings GROUP BY status').fetchall()),
                'sd_adapter':'hcnetsdk-or-isapi+exported-file-ingest', 'sd_auto_download':'per_camera_configured',
                'sd_sources':camera_sources,
                'upload_enabled':self.settings.enable_upload, 'timezone':self.settings.timezone,
                'tenant_id':self.settings.tenant_id,
                'telegram_destination':'channel' if self.settings.telegram_destination=='channel' or self.settings.storage_channel_id else 'owner_private_chat',
                'storage_channel_configured':bool(self.settings.storage_channel_id),
                'media_mode':self.settings.media_mode,
                'owner_configured':bool(self.settings.effective_owner),
                'owner_started':bool(self.settings.effective_owner) and self.state(f'telegram_owner_started:{self.settings.effective_owner}')=='1',
                'allowed_users_count':len(set(self.settings.allowed_users) | ({self.settings.effective_owner} if self.settings.effective_owner else set())),
                  'cache_retention_hours':self.settings.cache_retention_hours,
                  'error_retention_hours':self.settings.error_retention_hours,
                  'bot_api_spool_max_bytes':self.settings.bot_api_spool_max_bytes,'api_mode':self.settings.api_mode,
                'version':'2.4','counts':{'cameras':self.conn.execute('SELECT COUNT(*) FROM cameras').fetchone()[0],
                'recordings':self.conn.execute('SELECT COUNT(*) FROM recordings WHERE deleted_at IS NULL').fetchone()[0],
                'uploaded':self.conn.execute("SELECT COUNT(*) FROM recordings WHERE status='uploaded' AND deleted_at IS NULL").fetchone()[0],
                'deleted':self.conn.execute('SELECT COUNT(*) FROM recordings WHERE deleted_at IS NOT NULL').fetchone()[0]}}
