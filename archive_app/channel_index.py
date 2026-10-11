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
    NAVIGATION_VERSION = 4
    PAGE_SIZE = 20
    MAX_ROOT_YEARS = 12
    MAX_ROOT_MONTHS = 12
    MAX_ROOT_DAYS = 7

    def __init__(self, archive, telegram, *, stop_event=None):
        self.archive, self.telegram = archive, telegram
        self.stop_event = stop_event
        self.conn, self.settings = archive.conn, archive.settings
        self.zone = get_zone(self.settings.timezone)
        self.owner = secrets.token_hex(16)
        self._job = None
        self._next_navigation_check = 0
        # Additive tables only; old media references and old channel indexes
        # survive cutover. One active node per camera/target/type/period.
        statements = (
            '''CREATE TABLE IF NOT EXISTS channel_index_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_id TEXT NOT NULL, channel_chat_id INTEGER NOT NULL,
                index_type TEXT NOT NULL CHECK(index_type IN ('root','years','year','month','day','hour')),
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
        from .channel_index_schema import ensure_index_message_schema
        ensure_index_message_schema(self.conn, statements[0])
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
        row = self.conn.execute('SELECT * FROM cameras WHERE id=? AND deleted_at IS NULL', (camera,)).fetchone()
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
        if kind == 'hour':
            value, _ = self._page(period)
            start = datetime.strptime(value, '%Y-%m-%dT%H').replace(tzinfo=self.zone)
            end = start + timedelta(hours=1)
            return int(start.timestamp() * 1000), int(end.timestamp() * 1000)
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

    @staticmethod
    def _page(period):
        value, separator, page = period.partition(':')
        if separator and (not page.isdecimal() or not 2 <= int(page) <= 10000):
            raise ValueError('Invalid navigation page')
        return value, int(page) if separator else 1

    @staticmethod
    def _page_key(value, page):
        return value if page == 1 else f'{value}:{page}'

    def _available_periods(self, camera, channel, kind):
        rows = self.conn.execute('''SELECT period_key FROM channel_index_messages
            WHERE camera_id=? AND channel_chat_id=? AND index_type=? AND state='ready'
            AND tg_message_id BETWEEN 1 AND 2147483647 ORDER BY period_key''',
            (camera, channel, kind))
        return [row[0] for row in rows if self._count(camera, channel, kind, row[0])]

    def _hour_records(self, camera, channel, period):
        value, _ = self._page(period)
        hour = datetime.strptime(value, '%Y-%m-%dT%H').hour
        cache = getattr(self, '_day_records_cache', None)
        if cache is None:
            cache = self._day_records_cache = {}
        cache_key = (camera, channel, value[:10])
        if cache_key not in cache:
            buckets, seen = [[] for _ in range(24)], set()
            for row in self._records(camera, channel, 'day', value[:10]):
                identity = row['storage_message_id']
                if identity not in seen:
                    seen.add(identity)
                    buckets[from_epoch_ms(row['start_ms'], self.zone).hour].append(row)
            cache[cache_key] = buckets
        return cache[cache_key][hour]

    @staticmethod
    def _contiguous(rows):
        # Consecutive message IDs in recording-time order are a conservative
        # proof that scrolling visits every clip. Backfill/gaps use a list.
        return all(right['storage_message_id'] == left['storage_message_id'] + 1
                   for left, right in zip(rows, rows[1:]))

    def _neighbors(self, camera, channel, kind, period):
        periods = self._available_periods(camera, channel, kind)
        before = [value for value in periods if value < period]
        after = [value for value in periods if value > period]
        return (before[-1] if before else None, after[0] if after else None)

    def _neighbor_links(self, camera, channel, kind, period, parent):
        previous, following = self._neighbors(camera, channel, kind, period)
        label = 'Ngày' if kind == 'day' else 'Tháng'
        links = []
        if previous:
            links.append(self._node_link(camera, channel, kind, previous, f'← {label} trước'))
        if parent:
            links.append(parent)
        if following:
            links.append(self._node_link(camera, channel, kind, following, f'{label} sau →'))
        return ' | '.join(link for link in links if link)

    def _bot_link(self):
        username = getattr(self.settings, 'bot_username', '') or self.archive.state('telegram_bot_username') or ''
        if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', username):
            return self._link('🤖 Bot quản lý', f'https://t.me/{username}')
        return None

    def _recent_periods(self, camera, channel, kind, limit):
        """Ready, nonempty periods only; the full history stays in the tree."""
        rows = self.conn.execute('''SELECT period_key FROM channel_index_messages
            WHERE camera_id=? AND channel_chat_id=? AND index_type=? AND state='ready'
            AND tg_message_id BETWEEN 1 AND 2147483647 ORDER BY period_key DESC''',
            (camera, channel, kind))
        result = []
        for row in rows:
            if self._count(camera, channel, kind, row[0]):
                result.append(row[0])
                if len(result) == limit:
                    break
        return result

    def refresh_navigation(self):
        """Queue existing indexes once per upgrade/local day, including paused cameras.

        This does not create empty days, send media, or turn upload on. Persisted
        markers and the same transaction as enqueue prevent duplicate schedulers.
        """
        if not self.enabled:
            return 0
        now = time.time()
        if now < self._next_navigation_check:
            return 0
        today = datetime.fromtimestamp(now, self.zone).date().toordinal()
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            markers = dict(self.conn.execute('''SELECT name,value FROM channel_index_runtime
                WHERE name IN ('pin_navigation_version','pin_today_ordinal')'''))
            queued = 0
            if (markers.get('pin_navigation_version') != self.NAVIGATION_VERSION
                    or markers.get('pin_today_ordinal') != today):
                roots = self.conn.execute('''SELECT root.camera_id,root.channel_chat_id,
                    MAX(day.period_key) AS recorded_date FROM channel_index_messages root
                    JOIN cameras c ON c.id=root.camera_id AND c.channel_chat_id=root.channel_chat_id
                    JOIN channel_index_messages day ON day.camera_id=root.camera_id
                        AND day.channel_chat_id=root.channel_chat_id AND day.index_type='day'
                        AND day.state='ready' AND day.tg_message_id BETWEEN 1 AND 2147483647
                    WHERE root.index_type='root' AND root.state='ready' AND c.channel_enabled=1 AND c.deleted_at IS NULL
                        AND root.tg_message_id BETWEEN 1 AND 2147483647
                    GROUP BY root.camera_id,root.channel_chat_id''').fetchall()
                for row in roots:
                    self._enqueue(row['camera_id'], row['channel_chat_id'], row['recorded_date'], immediate=True)
                    queued += 1
                    if markers.get('pin_navigation_version') != self.NAVIGATION_VERSION:
                        # Upgrade every existing day in-place, not just the
                        # latest pin. No media copies or recording rewrites.
                        self.enqueue_camera(row['camera_id'], commit=False)
                for name, value in (('pin_navigation_version', self.NAVIGATION_VERSION), ('pin_today_ordinal', today)):
                    self.conn.execute('''INSERT INTO channel_index_runtime(name,value) VALUES(?,?)
                        ON CONFLICT(name) DO UPDATE SET value=excluded.value''', (name, value))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        self._next_navigation_check = now + 60
        return queued

    def _render(self, camera, channel, kind, period):
        self._day_records_cache = {}
        info = self._camera(camera)
        name = html.escape(str(info['name'])[:80])
        lines = []
        if kind == 'root':
            today = datetime.fromtimestamp(time.time(), self.zone).date()
            lines = [f'📌 <b>KHO VIDEO – {name}</b>', '']
            latest = self._records(camera, channel, descending=True, limit=1).fetchone()
            if latest:
                lines.append(self._link('🎬 Video mới nhất', channel_message_url(channel, latest['storage_message_id'])))
            else:
                lines.append('🎬 Video mới nhất · Chưa có video')
            if self._count(camera, channel, 'day', today.isoformat()):
                day, label = today.isoformat(), '📅 Hôm nay'
            elif latest:
                parsed = from_epoch_ms(latest['start_ms'], self.zone).date()
                day, label = parsed.isoformat(), f'📅 Ngày gần nhất có video · {parsed:%d/%m/%Y}'
            else:
                day, label = today.isoformat(), '📅 Hôm nay · Chưa có video'
            lines.append((self._node_link(camera, channel, 'day', day, label) or label) if latest else label)
            month = today.strftime('%Y-%m')
            current = self._node_link(camera, channel, 'month', month, '🗓 Tháng hiện tại') if self._count(camera, channel, 'month', month) else None
            lines.append(current or '🗓 Tháng hiện tại · Chưa có video')
            lines.append(self._node_link(camera, channel, 'years', 'years', '📁 Kho lưu trữ theo năm') or '📁 Kho lưu trữ theo năm')
            lines.append(self._bot_link() or '🤖 Bot quản lý')
        elif kind == 'years':
            value, page = self._page(period)
            if value != 'years':
                raise ValueError('Invalid year list period')
            years = list(reversed(self._available_periods(camera, channel, 'year')))
            lines = [f'📁 <b>KHO LƯU TRỮ – {name}</b>', '']
            for year in years[(page-1)*self.PAGE_SIZE:page*self.PAGE_SIZE]:
                link = self._node_link(camera, channel, 'year', year, f'📁 {year}')
                if link:
                    lines.append(link)
            if not years[(page-1)*self.PAGE_SIZE:page*self.PAGE_SIZE]:
                lines.append('Chưa có video')
            links = []
            if page > 1:
                links.append(self._node_link(camera, channel, 'years', self._page_key('years', page-1), '← Trước'))
            if page*self.PAGE_SIZE < len(years):
                links.append(self._node_link(camera, channel, 'years', self._page_key('years', page+1), 'Sau →'))
            links.append(self._node_link(camera, channel, 'root', 'root', '← Trang chủ'))
            lines.extend(('', ' | '.join(link for link in links if link)))
        elif kind == 'year':
            lines = [f'📁 <b>NĂM {period} – {name}</b>', '', 'Quý I | Quý II | Quý III | Quý IV']
            for offset in range(1, 4):
                row = []
                for month in (offset, offset+3, offset+6, offset+9):
                    key = f'{period}-{month:02d}'
                    link = self._node_link(camera, channel, 'month', key, f'{month:02d}') if self._count(camera, channel, 'month', key) else None
                    row.append(link or f'{month:02d} —')
                lines.append(' · '.join(row))
            link = self._node_link(camera, channel, 'years', 'years', '← Danh sách năm')
            home = self._node_link(camera, channel, 'root', 'root', 'Trang chủ')
            lines.extend(('', ' | '.join(value for value in (link, home) if value)))
        elif kind == 'month':
            parsed = date.fromisoformat(period+'-01')
            lines = [f'📅 <b>THÁNG {parsed:%m/%Y} – {name}</b>', '']
            cells, populated, total = [], 0, 0
            for number in range(1, 32):
                try:
                    day = date(parsed.year, parsed.month, number)
                except ValueError:
                    break
                total += 1
                exists = self._count(camera, channel, 'day', day.isoformat())
                populated += bool(exists)
                link = self._node_link(camera, channel, 'day', day.isoformat(), f'{number:02d}') if exists else None
                cells.append(link or f'{number:02d} —')
                if len(cells) == 7:
                    lines.append(' · '.join(cells)); cells = []
            if cells:
                lines.append(' · '.join(cells))
            lines.extend(('', f'Có video: {populated}/{total} ngày', '— Chưa có dữ liệu'))
            parent = self._node_link(camera, channel, 'year', period[:4], f'Năm {period[:4]}')
            lines.append(self._neighbor_links(camera, channel, kind, period, parent))
        elif kind == 'day':
            parsed = date.fromisoformat(period)
            lines = [f'📅 <b>{parsed:%d/%m/%Y} – {name}</b>', '', '🕓 Các giờ có video:']
            cells = []
            for hour in range(24):
                key = f'{period}T{hour:02d}'
                rows = self._hour_records(camera, channel, key)
                if rows:
                    if self._contiguous(rows):
                        link = self._link(f'{hour:02d}:00', channel_message_url(channel, rows[0]['storage_message_id']))
                    else:
                        link = self._node_link(camera, channel, 'hour', key, f'{hour:02d}:00')
                    cells.append(link or f'{hour:02d} —')
                else:
                    cells.append(f'{hour:02d} —')
                if len(cells) == 6:
                    lines.append(' · '.join(cells)); cells = []
            lines.extend(('', '— Chưa có dữ liệu'))
            parent = self._node_link(camera, channel, 'month', period[:7], f'Tháng {parsed:%m/%Y}')
            lines.append(self._neighbor_links(camera, channel, kind, period, parent))
        elif kind == 'hour':
            value, page = self._page(period)
            parsed = datetime.strptime(value, '%Y-%m-%dT%H')
            rows = self._hour_records(camera, channel, value)
            lines = [f'🕓 <b>{parsed:%d/%m/%Y} · {parsed:%H}:00–{parsed:%H}:59 – {name}</b>', '']
            timings = {}
            for row in rows:
                identity = (row['start_ms'], row['end_ms'])
                timings[identity] = timings.get(identity, 0) + 1
            for number, row in enumerate(rows[(page-1)*self.PAGE_SIZE:page*self.PAGE_SIZE], (page-1)*self.PAGE_SIZE+1):
                start, end = (from_epoch_ms(row[field], self.zone) for field in ('start_ms','end_ms'))
                ending = end.strftime('%H:%M:%S') if start.date() == end.date() else end.strftime('%d/%m/%Y %H:%M:%S')
                label = f'{start:%H:%M:%S} – {ending}'
                if timings[(row['start_ms'], row['end_ms'])] > 1:
                    label += f' · Đoạn {number}'
                lines.append(self._link(label, channel_message_url(channel, row['storage_message_id'])))
            if not rows:
                lines.append('Chưa có video')
            links = []
            if page > 1:
                links.append(self._node_link(camera, channel, kind, self._page_key(value, page-1), '← Trước'))
            if page*self.PAGE_SIZE < len(rows):
                links.append(self._node_link(camera, channel, kind, self._page_key(value, page+1), 'Sau →'))
            links.append(self._node_link(camera, channel, 'day', value[:10], '← Về ngày'))
            lines.extend(('', ' | '.join(link for link in links if link)))
        else:
            raise ValueError('Invalid index type')
        result = '\n'.join(lines)
        if len(result.encode('utf-16-le'))//2 > 4096:
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
        if self.stop_event is not None and self.stop_event.is_set():
            raise _Retry(1, 'index_stopping')
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
        child = {'root': 'years', 'years': 'year', 'year': 'month', 'month': 'day', 'day': 'hour'}.get(kind)
        if not child:
            return
        camera, channel = self._job['camera_id'], self._job['channel_chat_id']
        prefix = '%' if kind in ('root', 'years') else period + ('T%' if kind == 'day' else '-%')
        with self.conn:
            self.conn.execute('''UPDATE channel_index_messages SET last_render_hash=NULL
                WHERE camera_id=? AND channel_chat_id=? AND index_type=? AND period_key LIKE ?''',
                (camera, channel, child, prefix))
            days = list(self.conn.execute('''SELECT period_key FROM channel_index_messages
                WHERE camera_id=? AND channel_chat_id=? AND index_type='day' AND period_key LIKE ?''',
                (camera, channel, '%' if kind in ('root', 'years') else period + '%')))
            for row in days:
                if kind != 'day' or row[0] != self._job['recorded_date']:
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
        self.refresh_navigation()
        self.recover()
        job = self._claim()
        if not job:
            return None
        self._job = job
        self._day_records_cache = {}
        state, error, delay = 'done', None, 0
        try:
            day = job['recorded_date']
            self.telegram.verify_camera_channel(self.archive,job['camera_id'],require_index=True)
            existing = self._node(job['camera_id'], job['channel_chat_id'], 'day', day)
            if self._count(job['camera_id'], job['channel_chat_id'], 'day', day) or existing:
                camera, channel = job['camera_id'], job['channel_chat_id']
                years = set(self._available_periods(camera, channel, 'year'))
                if self._count(camera, channel, 'day', day):
                    years.add(day[:4])
                graph = [('root', 'root')]
                graph.extend(('years', self._page_key('years', page))
                             for page in range(1, max(1, math.ceil(len(years)/self.PAGE_SIZE))+1))
                graph.extend(('years', row[0]) for row in self.conn.execute('''SELECT period_key
                    FROM channel_index_messages WHERE camera_id=? AND channel_chat_id=?
                    AND index_type='years' ''', (camera, channel)) if ('years', row[0]) not in graph)
                graph.extend((('year', day[:4]), ('month', day[:7]), ('day', day)))
                for hour in range(24):
                    key = f'{day}T{hour:02d}'
                    rows = self._hour_records(camera, channel, key)
                    if rows and not self._contiguous(rows):
                        graph.extend(('hour', self._page_key(key, page))
                                     for page in range(1, math.ceil(len(rows)/self.PAGE_SIZE)+1))
                # Retire old list pages in place when clips were deleted or
                # the hour becomes contiguous; never leave stale media URLs.
                graph.extend(('hour', row[0]) for row in self.conn.execute('''SELECT period_key
                    FROM channel_index_messages WHERE camera_id=? AND channel_chat_id=?
                    AND index_type='hour' AND period_key LIKE ?''', (camera, channel, day+'T%'))
                    if ('hour', row[0]) not in graph)
                for kind, period in graph:
                    self._ensure(kind, period)
                for kind, period in reversed(graph):
                    self._ensure(kind, period)
                # Adjacent existing pages gained/lost a neighbor link. Edits
                # never create empty periods or duplicate their message IDs.
                for kind, period in (('day', day), ('month', day[:7])):
                    for neighbor in self._neighbors(camera, channel, kind, period):
                        if neighbor:
                            self._ensure(kind, neighbor)
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
                    AND (index_type IN ('root','years') OR period_key LIKE ? OR ? LIKE period_key||'%')''',
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
        if index_type not in ('root', 'years', 'year', 'month', 'day', 'hour'):
            raise ValueError('Invalid index type')
        if index_type == 'root':
            if period != 'root':
                raise ValueError('Invalid root period')
        elif index_type == 'years':
            value, _ = self._page(period)
            if value != 'years':
                raise ValueError('Invalid year list period')
        else:
            expected = {'year': r'\d{4}', 'month': r'\d{4}-\d{2}', 'day': r'\d{4}-\d{2}-\d{2}',
                        'hour': r'\d{4}-\d{2}-\d{2}T\d{2}(?::[2-9]\d*|:1\d+)?'}[index_type]
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
