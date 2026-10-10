"""Durable, independent Telegram navigation index for per-camera channels.

The recording database remains authoritative. This worker never changes media
states or sends/copies videos. Known message IDs are edited in place; uncertain
text sends require explicit reconciliation rather than blind duplication.
"""
import hashlib
import html
import math
import re
import secrets
import time
from datetime import date, datetime, timedelta

from .core import from_epoch_ms, get_zone
from .telegram import ApiRejected


def channel_message_url(chat_id, message_id):
    if (type(chat_id) is not int or not re.fullmatch(r'-100[1-9][0-9]*', str(chat_id))
            or type(message_id) is not int or not 0 < message_id <= 2147483647):
        raise ValueError('Invalid channel message reference')
    return f'https://t.me/c/{str(chat_id)[4:]}/{message_id}'


class _Retry(Exception):
    def __init__(self, delay=60, label='temporary_error'):
        self.delay, self.label = delay, label


class _Blocked(Exception):
    pass


class _Unknown(Exception):
    pass


class _Obsolete(Exception):
    pass


class ChannelIndex:
    """ChannelIndex(archive, telegram); all public scheduling calls are offline.

    enqueue(camera, date, commit=False) is suitable inside the SAME transaction
    which confirms upload/delete/restore. Normal callers use commit=True.
    process_one() claims one date, creates/edits its day -> month -> year -> root
    graph and returns a classified state. It contains Telegram errors itself.
    rebuild(..., dry_run=True) is an offline plan; False only enqueues work.
    """
    LEASE_SECONDS = 600
    MAX_ROOT_YEARS = 40

    def __init__(self, archive, telegram):
        self.archive, self.telegram = archive, telegram
        self.conn, self.settings = archive.conn, archive.settings
        self.zone = get_zone(self.settings.timezone)
        self.owner = secrets.token_hex(16)
        self._job = None
        # Additive tables only; old media references and old channel indexes
        # survive cutover. One active node per camera/target/type/period.
        statements = (
            '''CREATE TABLE IF NOT EXISTS channel_index_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_id TEXT NOT NULL, channel_chat_id INTEGER NOT NULL,
                index_type TEXT NOT NULL CHECK(index_type IN ('root','year','month','day')),
                period_key TEXT NOT NULL, tg_message_id INTEGER, tg_message_url TEXT,
                state TEXT NOT NULL DEFAULT 'pending', revision INTEGER NOT NULL DEFAULT 0,
                last_render_hash TEXT, pinned INTEGER NOT NULL DEFAULT 0,
                attempt_id TEXT, last_error TEXT, updated_at REAL NOT NULL,
                UNIQUE(camera_id,channel_chat_id,index_type,period_key))''',
            '''CREATE TABLE IF NOT EXISTS channel_index_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_id TEXT NOT NULL, channel_chat_id INTEGER NOT NULL,
                recorded_date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                generation INTEGER NOT NULL DEFAULT 1, completed_generation INTEGER NOT NULL DEFAULT 0,
                requested_at REAL NOT NULL, due_at REAL NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT, lease_owner TEXT, lease_until REAL NOT NULL DEFAULT 0,
                UNIQUE(camera_id,channel_chat_id,recorded_date))''',
            '''CREATE INDEX IF NOT EXISTS channel_index_jobs_due
                ON channel_index_jobs(status,due_at)''',
            '''CREATE TABLE IF NOT EXISTS channel_index_leases (
                camera_id TEXT NOT NULL, channel_chat_id INTEGER NOT NULL,
                owner TEXT NOT NULL, lease_until REAL NOT NULL,
                PRIMARY KEY(camera_id,channel_chat_id))''',
            '''CREATE TABLE IF NOT EXISTS channel_index_runtime (
                name TEXT PRIMARY KEY, value REAL NOT NULL)''',
            '''CREATE INDEX IF NOT EXISTS recording_channel_index
                ON recordings(camera,storage_chat_id,status,deleted_at,start_ms)''',
        )
        for statement in statements:
            self.conn.execute(statement)
        # CREATE TABLE does not commit a caller's pending writes. Root creates
        # this helper before entering the recording mutation transaction.

    @property
    def enabled(self):
        return (getattr(self.settings, 'multi_channel_routing', False) is True
                and getattr(self.settings, 'channel_index_enabled', True) is True)

    @property
    def bot_id(self):
        token = getattr(self.settings, 'token', '')
        prefix, separator, _ = token.partition(':')
        value = prefix if separator and prefix.isdecimal() else self.archive.state('telegram_bot_id')
        return int(value) if isinstance(value, str) and value.isdecimal() and int(value) > 0 else 0

    def _camera(self, camera):
        if not isinstance(camera, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', camera):
            raise ValueError('Invalid camera ID')
        row = self.conn.execute('SELECT * FROM cameras WHERE id=?', (camera,)).fetchone()
        if row is None:
            raise KeyError('Unknown camera')
        result = dict(row)
        channel = result.get('channel_chat_id')
        if (type(channel) is not int or not re.fullmatch(r'-100[1-9][0-9]*', str(channel))
                or not result.get('channel_enabled', 1)):
            raise ValueError('Camera channel is not configured')
        return result

    @staticmethod
    def _date(value):
        if type(value) is date:
            return value.isoformat()
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('Expected recorded date YYYY-MM-DD')
        return date.fromisoformat(value).isoformat()

    def _enqueue(self, camera, channel, day, immediate=False):
        now = time.time()
        debounce = getattr(self.settings, 'channel_index_debounce_seconds', 60)
        delay = 0 if immediate else max(0, min(int(debounce), 300))
        self.conn.execute('''INSERT INTO channel_index_jobs
            (camera_id,channel_chat_id,recorded_date,requested_at,due_at)
            VALUES(?,?,?,?,?) ON CONFLICT(camera_id,channel_chat_id,recorded_date)
            DO UPDATE SET generation=generation+1,requested_at=excluded.requested_at,
            due_at=CASE WHEN status IN ('pending','running')
                       THEN MIN(due_at,excluded.due_at) ELSE excluded.due_at END,
            status=CASE WHEN status IN ('needs_reconcile','running') THEN status ELSE 'pending' END,
            last_error=CASE WHEN status='needs_reconcile' THEN last_error ELSE NULL END''',
            (camera, channel, day, now, now + delay))

    def enqueue(self, camera, recorded_date, *, commit=True):
        if not self.enabled:
            return False
        day = self._date(recorded_date)
        try:
            target = self._camera(camera)['channel_chat_id']
        except (KeyError, ValueError):
            return False
        self._enqueue(camera, target, day)
        if commit:
            self.conn.commit()
        return True

    def enqueue_recording(self, row_or_key, *, commit=True):
        if isinstance(row_or_key, str):
            row = self.conn.execute('SELECT * FROM recordings WHERE key=?', (row_or_key,)).fetchone()
        else:
            row = row_or_key
        if row is None:
            return False
        row = dict(row)
        # Deleted uploaded rows are intentional: refresh their former day.
        if (row.get('status') != 'uploaded' or row.get('storage_kind') != 'channel'
                or type(row.get('start_ms')) is not int):
            return False
        try:
            target = self._camera(row['camera'])['channel_chat_id']
        except (KeyError, ValueError):
            return False
        if row.get('storage_chat_id') != target or row.get('bot_id') != self.bot_id:
            return False  # Legacy posts stay accessible through the main bot.
        return self.enqueue(row['camera'], from_epoch_ms(row['start_ms'], self.zone).date(), commit=commit)

    def enqueue_camera(self, camera, *, commit=True):
        if not self.enabled:
            return False
        try:
            channel = self._camera(camera)['channel_chat_id']
        except (KeyError, ValueError):
            return False
        days = {row[0] for row in self.conn.execute('''SELECT period_key
            FROM channel_index_messages WHERE camera_id=? AND channel_chat_id=? AND index_type='day' ''',
            (camera, channel))}
        for row in self._records(camera, channel):
            days.add(from_epoch_ms(row['start_ms'], self.zone).date().isoformat())
        for day in sorted(days):
            self._enqueue(camera, channel, day)
        if commit:
            self.conn.commit()
        return bool(days)

    def _range(self, kind, period):
        if kind == 'day':
            start = date.fromisoformat(period)
            end = start + timedelta(days=1)
        elif kind == 'month':
            start = date.fromisoformat(period + '-01')
            end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
        elif kind == 'year':
            start = date(int(period), 1, 1)
            end = date(int(period) + 1, 1, 1)
        else:
            raise ValueError('Invalid index period')
        return (int(datetime.combine(start, datetime.min.time(), self.zone).timestamp() * 1000),
                int(datetime.combine(end, datetime.min.time(), self.zone).timestamp() * 1000))

    def _records(self, camera, channel, kind=None, period=None, *, descending=False, limit=None):
        sql = '''SELECT key,start_ms,end_ms,storage_chat_id,storage_message_id FROM recordings
            WHERE camera=? AND storage_chat_id=? AND storage_kind='channel'
            AND status='uploaded' AND deleted_at IS NULL AND bot_id=?
            AND storage_message_id BETWEEN 1 AND 2147483647'''
        params = [camera, channel, self.bot_id]
        if kind:
            start, end = self._range(kind, period)
            sql += ' AND start_ms>=? AND start_ms<?'
            params.extend((start, end))
        sql += ' ORDER BY start_ms ' + ('DESC' if descending else 'ASC') + ',key'
        if limit is not None:
            sql += ' LIMIT ?'
            params.append(limit)
        return self.conn.execute(sql, params)

    def _count(self, camera, channel, kind, period):
        start, end = self._range(kind, period)
        return self.conn.execute('''SELECT COUNT(*) FROM recordings WHERE camera=?
            AND storage_chat_id=? AND storage_kind='channel' AND status='uploaded'
            AND deleted_at IS NULL AND bot_id=? AND storage_message_id BETWEEN 1 AND 2147483647
            AND start_ms>=? AND start_ms<?''', (camera, channel, self.bot_id, start, end)).fetchone()[0]

    def _node(self, camera, channel, kind, period):
        row = self.conn.execute('''SELECT * FROM channel_index_messages WHERE camera_id=?
            AND channel_chat_id=? AND index_type=? AND period_key=?''', (camera, channel, kind, period)).fetchone()
        return dict(row) if row else None

    def message_url(self, camera, index_type='root', period='root'):
        try:
            channel = self._camera(camera)['channel_chat_id']
        except (KeyError, ValueError):
            return None
        node = self._node(camera, channel, index_type, period)
        if node and node['state'] == 'ready' and node['tg_message_id']:
            return channel_message_url(channel, node['tg_message_id'])
        return None

    @staticmethod
    def _link(label, url):
        return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'

    def _node_link(self, camera, channel, kind, period, label):
        node = self._node(camera, channel, kind, period)
        if node and node['state'] == 'ready' and node['tg_message_id']:
            return self._link(label, channel_message_url(channel, node['tg_message_id']))
        return None

    def _bot_link(self):
        username = getattr(self.settings, 'bot_username', '') or self.archive.state('telegram_bot_username') or ''
        if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', username):
            return self._link('🤖 Bot quản lý', f'https://t.me/{username}')
        return None

    def _render(self, camera, channel, kind, period):
        info = self._camera(camera)
        name = html.escape(str(info['name'])[:80])
        identity = f'{html.escape(camera)} | {name}'
        lines = []
        if kind == 'root':
            lines = [f'📌 KHO VIDEO – {identity}', '', '📚 XEM THEO NĂM']
            years = list(self.conn.execute('''SELECT period_key FROM channel_index_messages
                WHERE camera_id=? AND channel_chat_id=? AND index_type='year' AND state='ready'
                ORDER BY period_key DESC''', (camera, channel)))
            active = [row[0] for row in years if self._count(camera, channel, 'year', row[0])]
            for year in reversed(active[:self.MAX_ROOT_YEARS]):
                link = self._node_link(camera, channel, 'year', year, f'📁 {year}')
                if link:
                    lines.append(link)
            if len(active) > self.MAX_ROOT_YEARS:
                lines.append(f'{len(active) - self.MAX_ROOT_YEARS} năm khác: tìm trong bot')
            latest = self._records(camera, channel, descending=True, limit=1).fetchone()
            if latest:
                day = from_epoch_ms(latest['start_ms'], self.zone).date()
                link = self._node_link(camera, channel, 'day', day.isoformat(), f'📅 {day:%d/%m/%Y}')
                if link:
                    lines.extend(('', link))
                lines.append(self._link('🎬 Video mới nhất', channel_message_url(
                    latest['storage_chat_id'], latest['storage_message_id'])))
            else:
                lines.append('Chưa có video')
            bot = self._bot_link()
            if bot:
                lines.extend(('', bot))
        elif kind == 'year':
            lines = [f'📁 {identity} | NĂM {period}', '']
            for month in range(1, 13):
                key = f'{period}-{month:02d}'
                if self._count(camera, channel, 'month', key):
                    link = self._node_link(camera, channel, 'month', key, f'📆 Tháng {month:02d}')
                    if link:
                        lines.append(link)
            link = self._node_link(camera, channel, 'root', 'root', '⬆️ Mục lục chính')
            if link:
                lines.extend(('', link))
        elif kind == 'month':
            parsed = date.fromisoformat(period + '-01')
            lines = [f'📅 {identity} | THÁNG {parsed:%m/%Y}', '']
            for number in range(1, 32):
                try:
                    day = date(parsed.year, parsed.month, number)
                except ValueError:
                    break
                if self._count(camera, channel, 'day', day.isoformat()):
                    link = self._node_link(camera, channel, 'day', day.isoformat(), f'{day:%d/%m}')
                    if link:
                        lines.append(link)
            lines.extend(('', f'Tổng: {self._count(camera, channel, kind, period)} video'))
            link = self._node_link(camera, channel, 'year', period[:4], f'⬅️ Năm {period[:4]}')
            if link:
                lines.append(link)
        else:
            parsed = date.fromisoformat(period)
            lines = [f'🗓 {identity} | NGÀY {parsed:%d/%m/%Y}', '']
            buckets = [{'count': 0, 'first': None} for _ in range(6)]
            first = last = None
            for row in self._records(camera, channel, 'day', period):
                bucket = buckets[from_epoch_ms(row['start_ms'], self.zone).hour // 4]
                bucket['count'] += 1
                bucket['first'] = bucket['first'] or row
                first, last = first or row, row
            for number, bucket in enumerate(buckets):
                label = f'{number * 4:02d}:00–{number * 4 + 3:02d}:59 · {bucket["count"]} video'
                row = bucket['first']
                lines.append(self._link(label, channel_message_url(row['storage_chat_id'],
                    row['storage_message_id'])) if row else label)
            lines.extend(('', f'Tổng: {sum(bucket["count"] for bucket in buckets)} video'))
            for label, row in (('🔗 Video đầu ngày', first), ('🔗 Video cuối ngày', last)):
                if row:
                    lines.append(self._link(label, channel_message_url(row['storage_chat_id'], row['storage_message_id'])))
            link = self._node_link(camera, channel, 'month', period[:7], f'⬅️ Tháng {parsed:%m/%Y}')
            if link:
                lines.append(link)
        result = '\n'.join(lines)
        # Telegram's limit counts UTF-16 code units. Do not truncate HTML or
        # links: bounded identities, 40 years, 12 months, 31 days, 6 buckets.
        if len(result.encode('utf-16-le')) // 2 > 4096:
            raise _Blocked('render_limit')
        return result

    def recover(self):
        """Reclaim expired jobs, but quarantine any interrupted text send."""
        now = time.time()
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            expired = list(self.conn.execute('''SELECT * FROM channel_index_jobs
                WHERE status='running' AND lease_until<=?''', (now,)))
            for job in expired:
                sending = self.conn.execute('''SELECT 1 FROM channel_index_messages
                    WHERE camera_id=? AND channel_chat_id=? AND state IN ('sending','needs_reconcile') LIMIT 1''',
                    (job['camera_id'], job['channel_chat_id'])).fetchone()
                if sending:
                    self.conn.execute('''UPDATE channel_index_messages SET state='needs_reconcile',
                        last_error='interrupted_send',updated_at=? WHERE camera_id=? AND channel_chat_id=? AND state='sending' ''',
                        (now, job['camera_id'], job['channel_chat_id']))
                self.conn.execute('''UPDATE channel_index_jobs SET status=?,last_error=?,
                    lease_owner=NULL,lease_until=0,due_at=? WHERE id=?''',
                    ('needs_reconcile' if sending else 'pending', 'interrupted_send' if sending else None, now, job['id']))
            self.conn.execute('DELETE FROM channel_index_leases WHERE lease_until<=?', (now,))
            self.conn.commit()
            return len(expired)
        except Exception:
            self.conn.rollback()
            raise

    def _claim(self):
        now = time.time()
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            for row in self.conn.execute('''SELECT * FROM channel_index_jobs
                WHERE status='pending' AND due_at<=? ORDER BY due_at,id LIMIT 32''', (now,)):
                try:
                    info = self._camera(row['camera_id'])
                    if info['channel_chat_id'] != row['channel_chat_id']:
                        raise _Obsolete()
                except (KeyError, ValueError, _Obsolete):
                    self.conn.execute("UPDATE channel_index_jobs SET status='obsolete',last_error='mapping_changed' WHERE id=?", (row['id'],))
                    continue
                if self.conn.execute('''SELECT 1 FROM channel_index_leases
                    WHERE camera_id=? AND channel_chat_id=?''', (row['camera_id'], row['channel_chat_id'])).fetchone():
                    continue
                self.conn.execute('''INSERT INTO channel_index_leases VALUES(?,?,?,?)''',
                    (row['camera_id'], row['channel_chat_id'], self.owner, now + self.LEASE_SECONDS))
                self.conn.execute('''UPDATE channel_index_jobs SET status='running',
                    lease_owner=?,lease_until=?,attempt_count=attempt_count+1 WHERE id=?''',
                    (self.owner, now + self.LEASE_SECONDS, row['id']))
                self.conn.commit()
                return dict(row)
            self.conn.commit()
            return None
        except Exception:
            self.conn.rollback()
            raise

    def _target(self):
        if not self.enabled:
            raise _Obsolete()
        try:
            info = self._camera(self._job['camera_id'])
        except (KeyError, ValueError):
            raise _Obsolete() from None
        if info['channel_chat_id'] != self._job['channel_chat_id']:
            raise _Obsolete()
        lease = self.conn.execute('''SELECT owner FROM channel_index_leases
            WHERE camera_id=? AND channel_chat_id=?''', (info['id'], info['channel_chat_id'])).fetchone()
        if not lease or lease['owner'] != self.owner:
            raise _Unknown('lease_lost')
        until = time.time() + self.LEASE_SECONDS
        with self.conn:
            self.conn.execute('UPDATE channel_index_leases SET lease_until=? WHERE owner=?', (until, self.owner))
            self.conn.execute('UPDATE channel_index_jobs SET lease_until=? WHERE lease_owner=?', (until, self.owner))

    @staticmethod
    def _delay(value):
        return max(1, min(float(value), 86400)) if type(value) in (int, float) and math.isfinite(value) else 60

    def _error(self, exc, *, sending=False):
        if isinstance(exc, ApiRejected):
            if exc.code == 429:
                delay = self._delay(exc.retry_after)
                with self.conn:
                    self.conn.execute('''INSERT INTO channel_index_runtime(name,value)
                        VALUES('retry_at',?) ON CONFLICT(name) DO UPDATE SET value=MAX(value,excluded.value)''', (time.time() + delay,))
                raise _Retry(delay, 'rate_limited') from None
            if 400 <= exc.code < 500:
                raise _Blocked('permission_or_request_rejected') from None
        if sending:
            raise _Unknown('send_outcome_unknown') from None
        raise _Retry(60, 'telegram_unavailable') from None

    def _invalidate_children(self, kind, period):
        child = {'root': 'year', 'year': 'month', 'month': 'day'}.get(kind)
        if not child:
            return
        camera, channel = self._job['camera_id'], self._job['channel_chat_id']
        prefix = '%' if kind == 'root' else period + '-%'
        with self.conn:
            self.conn.execute('''UPDATE channel_index_messages SET last_render_hash=NULL
                WHERE camera_id=? AND channel_chat_id=? AND index_type=? AND period_key LIKE ?''',
                (camera, channel, child, prefix))
            days = list(self.conn.execute('''SELECT period_key FROM channel_index_messages
                WHERE camera_id=? AND channel_chat_id=? AND index_type='day' AND period_key LIKE ?''',
                (camera, channel, '%' if kind == 'root' else period + '%')))
            for row in days:
                self._enqueue(camera, channel, row[0])

    def _ensure(self, kind, period, *, recreated=False):
        self._target()
        camera, channel = self._job['camera_id'], self._job['channel_chat_id']
        text = self._render(camera, channel, kind, period)
        digest = hashlib.sha256(text.encode()).hexdigest()
        node = self._node(camera, channel, kind, period)
        if node and node['state'] in ('sending', 'needs_reconcile'):
            raise _Unknown('index_needs_reconcile')
        fields = {'chat_id': channel, 'text': text, 'parse_mode': 'HTML',
                  'link_preview_options': {'is_disabled': True}}
        if not node or node['tg_message_id'] is None:
            attempt = secrets.token_hex(16)
            with self.conn:
                self.conn.execute('''INSERT INTO channel_index_messages
                    (camera_id,channel_chat_id,index_type,period_key,state,attempt_id,updated_at)
                    VALUES(?,?,?,?,'sending',?,?) ON CONFLICT(camera_id,channel_chat_id,index_type,period_key)
                    DO UPDATE SET state='sending',attempt_id=excluded.attempt_id,last_error=NULL,
                    updated_at=excluded.updated_at''', (camera, channel, kind, period, attempt, time.time()))
            try:
                result = self.telegram.request('sendMessage', {**fields, 'disable_notification': True})
            except Exception as exc:
                known = isinstance(exc, ApiRejected) and 400 <= exc.code < 500
                with self.conn:
                    self.conn.execute('''UPDATE channel_index_messages SET state=?,last_error=?,updated_at=?
                        WHERE camera_id=? AND channel_chat_id=? AND index_type=? AND period_key=? AND attempt_id=?''',
                        ('pending' if known else 'needs_reconcile', 'send_rejected' if known else 'send_outcome_unknown',
                         time.time(), camera, channel, kind, period, attempt))
                self._error(exc, sending=True)
            message = result.get('message_id') if isinstance(result, dict) else None
            actual_chat = result.get('chat', {}).get('id') if isinstance(result, dict) and isinstance(result.get('chat'), dict) else None
            if type(message) is not int or not 0 < message <= 2147483647 or actual_chat != channel:
                with self.conn:
                    self.conn.execute('''UPDATE channel_index_messages SET state='needs_reconcile',
                        last_error='invalid_send_confirmation',updated_at=? WHERE attempt_id=?''', (time.time(), attempt))
                raise _Unknown('invalid_send_confirmation')
            with self.conn:
                self.conn.execute('''UPDATE channel_index_messages SET state='ready',tg_message_id=?,
                    tg_message_url=?,last_render_hash=?,revision=revision+1,last_error=NULL,updated_at=?
                    WHERE camera_id=? AND channel_chat_id=? AND index_type=? AND period_key=? AND attempt_id=?''',
                    (message, channel_message_url(channel, message), digest, time.time(), camera, channel, kind, period, attempt))
            node = self._node(camera, channel, kind, period)
        elif node['last_render_hash'] != digest:
            try:
                result = self.telegram.request('editMessageText', {**fields, 'message_id': node['tg_message_id']})
                if not (result is True or isinstance(result, dict) and result.get('message_id') == node['tg_message_id']):
                    raise RuntimeError('Index edit was not confirmed')
            except ApiRejected as exc:
                if exc.not_modified:
                    pass
                elif getattr(exc, 'index_message_missing', False) or exc.source_missing:
                    if recreated:
                        raise _Blocked('repeated_missing_index') from None
                    with self.conn:
                        self.conn.execute('''UPDATE channel_index_messages SET state='pending',
                            tg_message_id=NULL,tg_message_url=NULL,last_render_hash=NULL,pinned=0,last_error='deleted_index'
                            WHERE id=?''', (node['id'],))
                    self._invalidate_children(kind, period)
                    return self._ensure(kind, period, recreated=True)
                else:
                    self._error(exc)
            except Exception as exc:
                self._error(exc)
            with self.conn:
                self.conn.execute('''UPDATE channel_index_messages SET state='ready',last_render_hash=?,
                    revision=revision+1,last_error=NULL,updated_at=? WHERE id=?''', (digest, time.time(), node['id']))
        if kind == 'root' and not node['pinned']:
            self._target()
            try:
                if self.telegram.request('pinChatMessage', {'chat_id': channel, 'message_id': node['tg_message_id'],
                        'disable_notification': True}) is not True:
                    raise RuntimeError('Pin was not confirmed')
            except Exception as exc:
                self._error(exc)
            with self.conn:
                self.conn.execute('UPDATE channel_index_messages SET pinned=1 WHERE id=?', (node['id'],))

    def process_one(self):
        if not self.enabled or not self.bot_id or not getattr(self.settings, 'token', ''):
            return None
        retry = self.conn.execute("SELECT value FROM channel_index_runtime WHERE name='retry_at'").fetchone()
        if retry and retry[0] > time.time():
            return 'rate_limited'
        self.recover()
        job = self._claim()
        if not job:
            return None
        self._job = job
        state, error, delay = 'done', None, 0
        try:
            day = job['recorded_date']
            self.telegram.verify_camera_channel(self.archive,job['camera_id'],require_index=True)
            existing = self._node(job['camera_id'], job['channel_chat_id'], 'day', day)
            if self._count(job['camera_id'], job['channel_chat_id'], 'day', day) or existing:
                graph = [('root', 'root'), ('year', day[:4]), ('month', day[:7]), ('day', day)]
                for kind, period in graph:
                    self._ensure(kind, period)
                for kind, period in reversed(graph):
                    self._ensure(kind, period)
        except _Retry as exc:
            state, error, delay = 'pending', exc.label, exc.delay
        except _Blocked as exc:
            state, error = 'blocked', str(exc)
        except _Unknown as exc:
            state, error = 'needs_reconcile', str(exc)
        except _Obsolete:
            state, error = 'obsolete', 'mapping_changed'
        except ApiRejected as exc:
            if exc.code==429:
                state,error,delay='pending','rate_limited',self._delay(exc.retry_after)
            else:state,error='blocked','channel_check_failed'
        except ValueError:
            state,error='blocked','channel_check_failed'
        except Exception:
            # Failed DB persistence after an API response leaves the durable
            # pre-send marker intact. Quarantine it before any further sends.
            self.conn.rollback()
            unknown = self.conn.execute('''SELECT 1 FROM channel_index_messages
                WHERE camera_id=? AND channel_chat_id=? AND state IN ('sending','needs_reconcile') LIMIT 1''',
                (job['camera_id'], job['channel_chat_id'])).fetchone()
            if unknown:
                with self.conn:
                    self.conn.execute("UPDATE channel_index_messages SET state='needs_reconcile',last_error='index_internal_unknown' WHERE camera_id=? AND channel_chat_id=? AND state='sending'",(job['camera_id'],job['channel_chat_id']))
            state, error, delay = ('needs_reconcile', 'index_internal_unknown', 0) if unknown else ('pending', 'index_internal_error', 60)
        finally:
            with self.conn:
                current = self.conn.execute('SELECT generation FROM channel_index_jobs WHERE id=?', (job['id'],)).fetchone()
                if state == 'done' and current and current[0] > job['generation']:
                    state = 'pending'
                self.conn.execute('''UPDATE channel_index_jobs SET status=?,last_error=?,lease_owner=NULL,
                    lease_until=0,due_at=MAX(due_at,?),completed_generation=CASE WHEN ? IN ('done','pending')
                    AND ? IS NULL THEN ? ELSE completed_generation END WHERE id=? AND lease_owner=?''',
                    (state, error, time.time() + delay, state, error, job['generation'], job['id'], self.owner))
                self.conn.execute('DELETE FROM channel_index_leases WHERE owner=?', (self.owner,))
            self._job = None
        return 'updated' if state == 'done' else ('retry' if state == 'pending' else state)

    def rebuild(self, camera, period=None, dry_run=True):
        if type(dry_run) is not bool:
            raise ValueError('Invalid dry-run value')
        info = self._camera(camera)
        if period is not None:
            if not isinstance(period, str) or not re.fullmatch(r'\d{4}(?:-\d{2}(?:-\d{2})?)?', period):
                raise ValueError('Expected YYYY, YYYY-MM or YYYY-MM-DD')
            self._range({4: 'year', 7: 'month', 10: 'day'}[len(period)], period)
        channel = info['channel_chat_id']
        rows = self._records(camera, channel,
            {4: 'year', 7: 'month', 10: 'day'}[len(period)] if period else None, period)
        days, recordings = set(), 0
        for row in rows:
            recordings += 1
            days.add(from_epoch_ms(row['start_ms'], self.zone).date().isoformat())
        days.update(row[0] for row in self.conn.execute('''SELECT period_key FROM channel_index_messages
            WHERE camera_id=? AND channel_chat_id=? AND index_type='day' AND period_key LIKE ?''',
            (camera, channel, period + '%' if period else '%')))
        uncertain = self.conn.execute('''SELECT COUNT(*) FROM channel_index_messages
            WHERE camera_id=? AND channel_chat_id=? AND state IN ('sending','needs_reconcile')''', (camera, channel)).fetchone()[0]
        if not dry_run:
            if not self.enabled:
                raise ValueError('Channel indexing is not enabled')
            with self.conn:
                for day in sorted(days):
                    self._enqueue(camera, channel, day, immediate=True)
                # Force a genuine edit check for known IDs, so a manually
                # deleted index is detected. Do not reset uncertain sends.
                self.conn.execute('''UPDATE channel_index_messages SET last_render_hash=NULL
                    WHERE camera_id=? AND channel_chat_id=? AND state='ready'
                    AND (index_type='root' OR period_key LIKE ? OR ? LIKE period_key||'%')''',
                    (camera, channel, period + '%' if period else '%', period or ''))
        return {'camera': camera, 'channel_chat_id': channel, 'period': period,
                'dry_run': dry_run, 'dates': sorted(days), 'recordings': recordings,
                'jobs': len(days), 'needs_reconcile': uncertain}

    def reconcile(self, camera, channel_chat_id, index_type, period, message_id):
        """Adopt an operator-verified message ID; never guess Telegram history.

        This method must be exposed only behind an owner/admin confirmation.
        The caller supplies verified evidence of a text send's actual outcome.
        """
        info = self._camera(camera)
        channel = info['channel_chat_id']
        if type(channel_chat_id) is not int or channel_chat_id != channel:
            raise ValueError('Reconciliation target is not the current camera channel')
        if index_type not in ('root', 'year', 'month', 'day'):
            raise ValueError('Invalid index type')
        if index_type == 'root':
            if period != 'root':
                raise ValueError('Invalid root period')
        else:
            expected = {'year': r'\d{4}', 'month': r'\d{4}-\d{2}', 'day': r'\d{4}-\d{2}-\d{2}'}[index_type]
            if not isinstance(period, str) or not re.fullmatch(expected, period):
                raise ValueError('Invalid index period')
            self._range(index_type, period)
        url = channel_message_url(channel, message_id)
        node = self._node(camera, channel, index_type, period)
        if not node or node['state'] != 'needs_reconcile':
            raise ValueError('Index is not awaiting reconciliation')
        with self.conn:
            self.conn.execute('''UPDATE channel_index_messages SET state='ready',tg_message_id=?,
                tg_message_url=?,last_render_hash=NULL,last_error=NULL,pinned=0,revision=revision+1,updated_at=?
                WHERE id=?''', (message_id, url, time.time(), node['id']))
            self.conn.execute('''UPDATE channel_index_jobs SET status='pending',last_error=NULL,due_at=?
                WHERE camera_id=? AND channel_chat_id=? AND status='needs_reconcile' ''', (time.time(), camera, channel))
        return url
