from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


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

    @classmethod
    def from_env(cls):
        mode = os.environ.get('TELEGRAM_API_MODE', 'cloud')
        if mode not in ('cloud', 'local'):
            raise ValueError('TELEGRAM_API_MODE must be cloud or local')
        allowed = tuple(int(x) for x in re.split(r'[,\s]+', os.environ.get('TELEGRAM_ALLOWED_USER_IDS', '').strip()) if x)
        result = cls(
            Path(os.environ.get('STATE_DIR', './data')).resolve(),
            Path(os.environ.get('CACHE_DIR', './cache')).resolve(),
            Path(os.environ.get('INPUT_DIR', './input')).resolve(),
            os.environ.get('DISPLAY_TIMEZONE', 'Asia/Ho_Chi_Minh'),
            os.environ.get('KEEP_CACHE', 'true').lower() == 'true',
            os.environ.get('ENABLE_UPLOAD', 'false').lower() == 'true',
            secret('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID', '').strip(),
            os.environ.get('TELEGRAM_API_BASE', os.environ.get('TELEGRAM_API_BASE_URL', 'https://api.telegram.org')).rstrip('/'),
            mode, allowed, os.environ.get('FFMPEG_BIN', 'ffmpeg'), os.environ.get('FFPROBE_BIN', 'ffprobe'),
            int(os.environ.get('TELEGRAM_MAX_BYTES', '2000000000' if mode == 'local' else '50000000')),
            int(float(os.environ.get('CACHE_MAX_GB', '100')) * 1e9),
            int(float(os.environ.get('CACHE_MIN_FREE_GB', '5')) * 1e9),
            max(1, int(os.environ.get('SCAN_INTERVAL_SECONDS', '15'))),
        )
        get_zone(result.timezone)
        limit = 2000000000 if mode == 'local' else 50000000
        if result.max_bytes <= 0 or result.max_bytes > limit:
            raise ValueError('TELEGRAM_MAX_BYTES exceeds configured API mode limit')
        if result.cache_max_bytes <= 0 or result.min_free_bytes < 0:
            raise ValueError('Invalid cache budget')
        if result.enable_upload and (not result.token or not result.chat_id):
            raise ValueError('ENABLE_UPLOAD needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID')
        return result


def normalize(source, dest, settings):
    dest = Path(dest)
    partial = dest.with_suffix('.part.mp4')
    command = [settings.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-i', str(source),
               '-map', '0:v:0', '-map', '0:a?', '-c', 'copy', '-movflags', '+faststart', str(partial)]
    try:
        result = subprocess.run(command, capture_output=True, timeout=1800)
        if result.returncode or result.stderr:
            raise ValueError('Remux returned an error; source remains intact')
        probe = subprocess.run([settings.ffprobe, '-v', 'error', '-show_format', '-show_streams',
                                '-of', 'json', str(partial)], capture_output=True, timeout=120)
        if probe.returncode or probe.stderr:
            raise ValueError('Output probe failed')
        data = json.loads(probe.stdout)
        if 'mp4' not in data['format']['format_name'].split(','):
            raise ValueError('Output is not ISO BMFF MP4')
        decode = subprocess.run([settings.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-i', str(partial),
                                 '-map', '0:v:0', '-map', '0:a?', '-fps_mode', 'passthrough',
                                 '-enc_time_base:v', '1:90000', '-f', 'null', '-'],
                                capture_output=True, timeout=1800)
        if decode.returncode or decode.stderr:
            raise ValueError('Decode validation failed')
        video = next(s for s in data['streams'] if s['codec_type'] == 'video')
        audio = next((s for s in data['streams'] if s['codec_type'] == 'audio'), {})
        partial.replace(dest)
        return {'duration': float(data['format']['duration']), 'codec_video': video['codec_name'],
                'codec_audio': audio.get('codec_name'), 'bytes': dest.stat().st_size}
    finally:
        if partial.exists():
            partial.unlink()


class Archive:
    def __init__(self, settings):
        self.settings = settings
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        settings.cache_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(settings.state_dir / 'archive.db', timeout=30)
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
        self.conn.execute('''INSERT OR IGNORE INTO cameras(id,name,created_at)
            SELECT DISTINCT camera,camera,? FROM recordings''',(time.time(),))
        self.conn.commit()

    def close(self):
        self.conn.close()

    @staticmethod
    def _camera_fields(data, partial=False):
        allowed={'id','name','model','host','device_port','rtsp_port','http_port','enabled'}
        if not isinstance(data,dict) or set(data)-allowed:
            raise ValueError('Unknown camera fields')
        values=dict(data)
        if not partial:
            values={'name':data.get('id',''),'model':'','host':'','device_port':8000,
                    'rtsp_port':554,'http_port':80,'enabled':True,**values}
        if 'id' in values and (not isinstance(values['id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',values['id'])):
            raise ValueError('Camera ID must be a stable ASCII slug')
        for field,maximum in (('name',100),('model',100),('host',253)):
            if field not in values:continue
            value=values[field]
            if not isinstance(value,str) or len(value)>maximum or any(ord(c)<32 for c in value):
                raise ValueError('Invalid camera '+field)
            values[field]=value.strip()
        if 'name' in values and not values['name']:raise ValueError('Camera name is required')
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
        if 'enabled' in values:
            if type(values['enabled']) is not bool:raise ValueError('enabled must be boolean')
            values['enabled']=int(values['enabled'])
        return values

    def cameras(self):
        rows=self.conn.execute('''SELECT c.*,COUNT(r.key) record_count,
            COALESCE(SUM(r.status='uploaded'),0) uploaded_count FROM cameras c
            LEFT JOIN recordings r ON r.camera=c.id GROUP BY c.id ORDER BY c.name COLLATE NOCASE,c.id''')
        results=[]
        for row in rows:
            item=dict(row);item['enabled']=bool(item['enabled'])
            item['probe']=json.loads(item.pop('probe_json')) if item['probe_json'] else None
            item.pop('probe_json',None)
            results.append(item)
        return results

    def camera_name(self,slug):
        row=self.conn.execute('SELECT name FROM cameras WHERE id=?',(slug,)).fetchone()
        return row[0] if row else slug

    def add_camera(self,data):
        values=self._camera_fields(data)
        if not values.get('id'):raise ValueError('Camera ID is required')
        values['created_at']=time.time()
        try:
            with self.conn:
                self.conn.execute('INSERT INTO cameras('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',tuple(values.values()))
        except sqlite3.IntegrityError:
            raise ValueError('Camera ID already exists') from None
        return next(c for c in self.cameras() if c['id']==values['id'])

    def update_camera(self,slug,data):
        if not isinstance(data,dict) or 'id' in data:raise ValueError('Camera ID is immutable')
        values=self._camera_fields(data,partial=True)
        if not values:raise ValueError('No changes provided')
        if set(values)&{'host','device_port','rtsp_port','http_port'}:values['probe_json']=None
        with self.conn:
            changed=self.conn.execute('UPDATE cameras SET '+','.join(k+'=?' for k in values)+' WHERE id=?',(*values.values(),slug)).rowcount
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

    @staticmethod
    def telegram_url(row):
        try:
            chat=int(row['chat_id']);message=int(row['message_id'])
            return f'https://t.me/c/{-chat-1000000000000}/{message}' if chat < -1000000000000 and message>0 else None
        except (ValueError,TypeError):return None

    def calendar(self,camera):
        zone=get_zone(self.settings.timezone);tree={}
        for start,end in self.conn.execute("SELECT start_ms,end_ms FROM recordings WHERE camera=? AND status='uploaded'",(camera,)):
            first=datetime.fromtimestamp(start/1000,zone).date()
            last=datetime.fromtimestamp((end-1)/1000,zone).date()
            while first<=last:
                tree.setdefault(first.year,{}).setdefault(first.month,set()).add(first.day)
                first+=timedelta(days=1)
        return {'years':[{'year':y,'months':[{'month':m,'days':sorted(days)} for m,days in sorted(months.items())]} for y,months in sorted(tree.items())]}

    def browse(self,camera=None,year=None,month=None,day=None,order='asc',status='uploaded',offset=0,limit=25):
        if order not in ('asc','desc') or status not in ('uploaded','all'):raise ValueError('Invalid archive filter')
        if not 0<=offset<=1000000 or not 1<=limit<=100:raise ValueError('Invalid pagination')
        clauses=[];params=[]
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
        recordings=[{**{k:row[k] for k in public},'telegram_url':self.telegram_url(row)} for row in rows]
        return {'recordings':recordings,'total':total,'offset':offset,'limit':limit}

    def ingest_entry(self, entry, dry_run=False):
        key = record_key(entry)
        start = parse_time(entry['start_time'])
        end = parse_time(entry['end_time'])
        if end <= start:
            raise ValueError('end_time must be greater than start_time')
        if end-start>timedelta(days=7):raise ValueError('Recording interval exceeds seven days')
        source = resolve_input(entry['path'], self.settings.input_dir)
        start_ms, end_ms = int(start.timestamp()*1000), int(end.timestamp()*1000)
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
            if old['status'] in ('downloaded', 'uploading', 'upload_unknown', 'uploaded', 'needs_review'):
                # Closed clips are immutable after ingest; avoid changing archived time retroactively.
                if old['end_ms'] != end_ms:
                    raise ValueError('Closed recording end_time changed; operator review required')
                return dict(old, record_key=key)
        in_cache = sum(p.stat().st_size for p in self.settings.cache_dir.rglob('*') if p.is_file())
        needed = source.stat().st_size * 2
        free = shutil.disk_usage(self.settings.cache_dir).free
        if in_cache + needed > self.settings.cache_max_bytes or free - needed < self.settings.min_free_bytes:
            raise ValueError('Cache budget reached; source was retained')
        dest = self.settings.cache_dir / (key + '.mp4')
        with self.conn:
            self.conn.execute('''INSERT INTO recordings(key,camera,record_id,start_ms,end_ms,source_path,status,created_at)
                VALUES(?,?,?,?,?,?,'ingesting',?) ON CONFLICT(key) DO UPDATE SET status='ingesting',end_ms=excluded.end_ms''',
                (key, entry['camera'], str(entry['record_id']), start_ms, end_ms, str(source), time.time()))
        try:
            info = normalize(source, dest, self.settings)
            with dest.open('rb') as handle:
                digest = hashlib.file_digest(handle, 'sha256').hexdigest()
            with self.conn:
                self.conn.execute('''UPDATE recordings SET local_path=?,status='downloaded',duration=?,codec_video=?,
                    codec_audio=?,file_size=?,sha256=?,last_error=NULL WHERE key=?''',
                    (str(dest), info['duration'], info['codec_video'], info['codec_audio'], info['bytes'], digest, key))
        except Exception as exc:
            with self.conn:
                self.conn.execute("UPDATE recordings SET status='failed',last_error=? WHERE key=?", (type(exc).__name__, key))
            raise
        return dict(self.conn.execute('SELECT * FROM recordings WHERE key=?', (key,)).fetchone(), record_key=key)

    def ingest_manifest(self, path, dry_run=False, continue_on_error=False):
        path = resolve_input(path, self.settings.input_dir)
        if path.stat().st_size > 1000000:
            raise ValueError('Manifest too large')
        document = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(document.get('recordings'), list) or len(document['recordings']) > 1000:
            raise ValueError('Manifest requires recordings list (max 1000 entries)')
        results=[]
        for entry in document['recordings']:
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
        sql = "SELECT * FROM recordings WHERE status='uploaded' AND start_ms<? AND end_ms>?"
        values = [int(finish.timestamp()*1000), int(begin.timestamp()*1000)]
        if camera is not None:
            sql += ' AND camera=?'
            values.append(camera)
        direction='ASC' if order=='asc' else 'DESC'
        return [dict(row) for row in self.conn.execute(sql + ' ORDER BY start_ms '+direction+',key '+direction, values)]

    def claim_upload(self):
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            row = self.conn.execute("SELECT r.* FROM recordings r JOIN cameras c ON c.id=r.camera WHERE r.status='downloaded' AND c.enabled=1 AND r.retry_at<=? ORDER BY r.start_ms,r.key LIMIT 1", (time.time(),)).fetchone()
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

    def mark_uploaded(self, key, chat_id, message_id, file_id):
        if not chat_id or int(message_id) <= 0 or not file_id:
            raise ValueError('Confirmed Telegram metadata required')
        with self.conn:
            changed = self.conn.execute("UPDATE recordings SET status='uploaded',chat_id=?,message_id=?,file_id=?,last_error=NULL WHERE key=?",
                                       (str(chat_id),int(message_id),str(file_id),key)).rowcount
        if not changed:
            raise ValueError('Unknown record key')

    def recover_uploads(self):
        with self.conn:
            return self.conn.execute("UPDATE recordings SET status='upload_unknown',last_error='process_restart_after_claim' WHERE status='uploading'").rowcount

    def cleanup(self, key):
        row = self.conn.execute('SELECT * FROM recordings WHERE key=?', (key,)).fetchone()
        if self.settings.keep_cache or row is None or row['status'] != 'uploaded' or not all(row[n] for n in ('chat_id','message_id','file_id')):
            return False
        if not row['local_path']:
            return True
        path = Path(row['local_path']).resolve()
        if not path.is_relative_to(self.settings.cache_dir.resolve()):
            raise ValueError('Cleanup path outside cache')
        if path.exists():
            path.unlink()
        return True

    def state(self, name, value=None):
        if value is None:
            row = self.conn.execute('SELECT value FROM state WHERE name=?', (name,)).fetchone()
            return row[0] if row else None
        with self.conn:
            self.conn.execute('INSERT INTO state(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value', (name,str(value)))

    def status(self):
        return {'queue':dict(self.conn.execute('SELECT status,count(*) FROM recordings GROUP BY status').fetchall()),
                'sd_adapter':'exported-file-ingest', 'sd_auto_download':'not_implemented',
                'upload_enabled':self.settings.enable_upload, 'timezone':self.settings.timezone,
                'version':'2.0','counts':{'cameras':self.conn.execute('SELECT COUNT(*) FROM cameras').fetchone()[0],
                'recordings':self.conn.execute('SELECT COUNT(*) FROM recordings').fetchone()[0]}}
