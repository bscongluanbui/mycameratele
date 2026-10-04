"""Durable, bounded original-file deliveries, independent of command polling.

Albums reuse Telegram file IDs. Nothing reads the SD card, local media, FFmpeg
or an HTTP download URL. A claimed POST is never blindly retried after a crash
or an ambiguous transport failure; only an explicit rejection (429) is resumed.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time


class TelegramBulk:
    MAX_ITEMS = 5000
    MAX_ACTIVE = 32
    MAX_HISTORY = 128
    MAX_REQUESTS_PER_JOB = 128
    BATCH_SIZE = 10
    # Albums count as individual messages against Telegram's per-chat quota.
    # Two groups of ten per minute stay below the common twenty/minute limit.
    CHAT_INTERVAL = 31
    ACTIVE = ('queued', 'running')

    def __init__(self, telegram):
        self.telegram = telegram

    def _scope(self):
        settings = self.telegram.settings
        values = [settings.tenant_id, settings.token,
                  getattr(settings, 'telegram_destination', 'owner_private'),
                  getattr(settings, 'storage_channel_id', 0), self.telegram.owner]
        return hashlib.sha256(json.dumps(values, separators=(',', ':')).encode()).hexdigest()

    def _request_scope(self, archive):
        # Telegram may restart update IDs after a backend migration or a week
        # of inactivity. Queue identity stays stable; request identity includes
        # only the durable epoch counter, not timestamps/cursors that change
        # during an ordinary retry of the same command.
        try:
            value = json.loads(archive.state('telegram_poll_epoch') or '{}')
            epoch = value.get('epoch', 0) if isinstance(value, dict) else 0
        except (TypeError, ValueError):
            epoch = 0
        epoch = epoch if type(epoch) is int and epoch >= 0 else 0
        values = [self._scope(), self.telegram._poll_backend(archive), epoch]
        return hashlib.sha256(json.dumps(values, separators=(',', ':')).encode()).hexdigest()

    @staticmethod
    def ensure(archive):
        names = {row[0] for row in archive.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('telegram_bulk_jobs','telegram_bulk_items','telegram_bulk_requests')")}
        if len(names) == 3 and 'placement' in {row[1] for row in archive.conn.execute('PRAGMA table_info(telegram_bulk_items)')}:
            return
        try:
            archive.conn.executescript('''
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS telegram_bulk_jobs (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, actor INTEGER NOT NULL,
                selection TEXT NOT NULL, selection_hash TEXT NOT NULL,
                state TEXT NOT NULL, total INTEGER NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                retry_at REAL NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
                error_code TEXT, notified INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS telegram_bulk_ready
                ON telegram_bulk_jobs(scope,state,retry_at,created_at);
            CREATE TABLE IF NOT EXISTS telegram_bulk_items (
                job_id TEXT NOT NULL, position INTEGER NOT NULL, recording_key TEXT NOT NULL,
                file_id TEXT, file_unique_id TEXT, media_type TEXT, bot_id INTEGER, placement TEXT,
                state TEXT NOT NULL DEFAULT 'queued', message_id INTEGER,
                PRIMARY KEY(job_id,position)
            );
            CREATE TABLE IF NOT EXISTS telegram_bulk_requests (
                scope TEXT NOT NULL, update_id INTEGER NOT NULL, job_id TEXT NOT NULL,
                PRIMARY KEY(scope,update_id)
            );
            ''')
            columns = {row[1] for row in archive.conn.execute('PRAGMA table_info(telegram_bulk_items)')}
            if 'placement' not in columns:
                # A partially applied older migration has no trustworthy
                # placement snapshot; those rows skip rather than replay.
                archive.conn.execute('ALTER TABLE telegram_bulk_items ADD COLUMN placement TEXT')
            archive.conn.commit()
        except Exception:
            archive.conn.rollback()
            raise

    def _actor(self, actor):
        # This module never accepts a destination override or a group chat.
        if type(actor) is not int or actor not in self.telegram.viewers:
            raise ValueError('Invalid bulk actor')

    @staticmethod
    def _selection(archive, selection):
        if not isinstance(selection, dict):
            raise ValueError('Invalid bulk selection')
        start, end = selection.get('start_ms'), selection.get('end_ms')
        archive._window(start, end)
        camera, order = selection.get('camera'), selection.get('order', 'asc')
        if camera is not None and (not isinstance(camera, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', camera)):
            raise ValueError('Invalid bulk camera')
        if order not in ('asc', 'desc'):
            raise ValueError('Invalid bulk order')
        label = selection.get('label', 'Video')
        if not isinstance(label, str) or len(label) > 200:
            raise ValueError('Invalid bulk label')
        return {'start_ms': start, 'end_ms': end, 'camera': camera, 'order': order, 'label': label}

    @staticmethod
    def _media_type(row):
        field = row.get('media_type')
        if field is None:
            field = 'video' if row.get('codec_video') == 'h264' and row.get('codec_audio') in (None, 'aac') else 'document'
        return field if field in ('video', 'document') else None

    @staticmethod
    def _placement(row):
        return json.dumps([row.get('storage_kind'), row.get('storage_chat_id'), row.get('storage_message_id'),
                           row.get('chat_id'), row.get('message_id')], separators=(',', ':'))

    def enqueue(self, archive, actor, selection, *, update_id):
        """Snapshot every matching clip, not just the visible list page."""
        self._actor(actor)
        if type(update_id) is not int or not 0 <= update_id <= 9007199254740991:
            raise ValueError('Invalid bulk update identity')
        selection = self._selection(archive, selection)
        self.ensure(archive)
        scope, request_scope, now = self._scope(), self._request_scope(archive), time.time()
        identity = json.dumps({key: selection[key] for key in ('start_ms', 'end_ms', 'camera', 'order')}, separators=(',', ':'))
        selection_hash = hashlib.sha256(identity.encode()).hexdigest()
        archive.conn.execute('BEGIN IMMEDIATE')
        try:
            previous = archive.conn.execute('SELECT job_id FROM telegram_bulk_requests WHERE scope=? AND update_id=?',
                                            (request_scope, update_id)).fetchone()
            if previous:
                result_id = previous['job_id']
            else:
                active = archive.conn.execute("SELECT * FROM telegram_bulk_jobs WHERE scope=? AND actor=? AND state IN ('queued','running') ORDER BY created_at LIMIT 1",
                                              (scope, actor)).fetchone()
                if active:
                    # A user has one active delivery. A repeated click attaches
                    # to the same job even if the user has selected a new range.
                    result_id = active['id']
                    if archive.conn.execute('SELECT COUNT(*) FROM telegram_bulk_requests WHERE job_id=?', (result_id,)).fetchone()[0] >= self.MAX_REQUESTS_PER_JOB:
                        raise ValueError('Bulk action request limit reached')
                else:
                    active_count = archive.conn.execute("SELECT COUNT(*) FROM telegram_bulk_jobs WHERE state IN ('queued','running')").fetchone()[0]
                    if active_count >= self.MAX_ACTIVE:
                        raise ValueError('Bulk delivery queue is full')
                    where = "status='uploaded' AND deleted_at IS NULL AND start_ms<? AND end_ms>?"
                    values = [selection['end_ms'], selection['start_ms']]
                    if selection['camera'] is not None:
                        where += ' AND camera=?'
                        values.append(selection['camera'])
                    direction = 'ASC' if selection['order'] == 'asc' else 'DESC'
                    rows = archive.conn.execute('SELECT * FROM recordings WHERE ' + where +
                                                ' ORDER BY start_ms ' + direction + ',key ' + direction + ' LIMIT ?',
                                                (*values, self.MAX_ITEMS + 1)).fetchall()
                    if not rows:
                        archive.conn.commit()
                        return {'id': None, 'state': 'empty', 'total': 0, 'sent': 0, 'skipped': 0, 'unknown': 0}
                    if len(rows) > self.MAX_ITEMS:
                        raise ValueError('Bulk selection exceeds 5000 videos; choose a shorter range')
                    # Terminal jobs are retained only as small bounded metadata;
                    # deleting a queue item never alters the original archive.
                    old = archive.conn.execute("SELECT id FROM telegram_bulk_jobs WHERE state NOT IN ('queued','running') ORDER BY updated_at DESC LIMIT -1 OFFSET ?",
                                               (self.MAX_HISTORY - 1,)).fetchall()
                    for item in old:
                        for table in ('telegram_bulk_items', 'telegram_bulk_requests'):
                            archive.conn.execute(f'DELETE FROM {table} WHERE job_id=?', (item['id'],))
                        archive.conn.execute('DELETE FROM telegram_bulk_jobs WHERE id=?', (item['id'],))
                    result_id = secrets.token_hex(6)
                    archive.conn.execute('''INSERT INTO telegram_bulk_jobs
                        (id,scope,actor,selection,selection_hash,state,total,created_at,updated_at)
                        VALUES(?,?,?,?,?,'queued',?,?,?)''',
                                         (result_id, scope, actor, json.dumps(selection, separators=(',', ':')),
                                          selection_hash, len(rows), now, now))
                    archive.conn.executemany('''INSERT INTO telegram_bulk_items
                        (job_id,position,recording_key,file_id,file_unique_id,media_type,bot_id,placement)
                        VALUES(?,?,?,?,?,?,?,?)''',
                                             ((result_id, position, row['key'], row['file_id'], row['file_unique_id'],
                                               self._media_type(dict(row)), row['bot_id'], self._placement(dict(row))) for position, row in enumerate(rows)))
                archive.conn.execute('INSERT INTO telegram_bulk_requests(scope,update_id,job_id) VALUES(?,?,?)',
                                     (request_scope, update_id, result_id))
            archive.conn.commit()
        except Exception:
            archive.conn.rollback()
            raise
        return self.status(archive, actor, result_id)

    def status(self, archive, actor, job_id):
        self._actor(actor)
        if not isinstance(job_id, str) or not re.fullmatch(r'[a-f0-9]{12}', job_id):
            raise ValueError('Invalid bulk job')
        self.ensure(archive)
        row = archive.conn.execute('SELECT * FROM telegram_bulk_jobs WHERE id=? AND scope=? AND actor=?',
                                   (job_id, self._scope(), actor)).fetchone()
        if row is None:
            raise ValueError('Unknown bulk job')
        counts = dict(archive.conn.execute('SELECT state,COUNT(*) FROM telegram_bulk_items WHERE job_id=? GROUP BY state', (job_id,)))
        return {'id': row['id'], 'state': row['state'], 'total': row['total'],
                'sent': counts.get('sent', 0), 'skipped': counts.get('skipped', 0),
                'unknown': counts.get('unknown', 0), 'retry_at': row['retry_at'],
                'label': json.loads(row['selection'])['label']}

    def menu(self, archive, actor, job_id):
        value = self.status(archive, actor, job_id)
        labels = {'queued': 'Đang chờ', 'running': 'Đang tải', 'done': 'Đã tải',
                  'cancelled': 'Đã hủy', 'unknown': 'Cần kiểm tra', 'failed': 'Lỗi'}
        text = f"⬇ {value['label']} · {value['sent']}/{value['total']} · {labels.get(value['state'], 'Đang chờ')}"
        if value['skipped']:
            text += f" · Bỏ qua {value['skipped']}"
        if value['unknown']:
            text += f" · Chưa xác nhận {value['unknown']}"
        buttons = [[{'text': '🔄 Tiến trình', 'callback_data': 'bulk-status:' + job_id}]]
        if value['state'] in self.ACTIVE:
            buttons.append([{'text': '✖ Hủy tải', 'callback_data': 'bulk-cancel:' + job_id}])
        buttons.append([{'text': '🏠 Menu', 'callback_data': 'home'}])
        return text, buttons

    def cancel(self, archive, actor, job_id):
        self.status(archive, actor, job_id)  # Validates the actor/scope/id first.
        with archive.conn:
            archive.conn.execute("UPDATE telegram_bulk_jobs SET state='cancelled',cancel_requested=1,updated_at=? WHERE id=? AND state IN ('queued','running')",
                                 (time.time(), job_id))
            archive.conn.execute("UPDATE telegram_bulk_items SET state='cancelled' WHERE job_id=? AND state='queued'", (job_id,))
        return self.status(archive, actor, job_id)

    def recover(self, archive):
        """Call once after acquiring the process's exclusive worker lock."""
        self.ensure(archive)
        with archive.conn:
            pending = archive.conn.execute("SELECT DISTINCT job_id FROM telegram_bulk_items WHERE state='sending'").fetchall()
            for row in pending:
                archive.conn.execute("UPDATE telegram_bulk_items SET state='unknown' WHERE job_id=? AND state='sending'", (row['job_id'],))
                archive.conn.execute("UPDATE telegram_bulk_jobs SET state='unknown',error_code='restart_after_claim',updated_at=? WHERE id=? AND state IN ('queued','running')",
                                     (time.time(), row['job_id']))
            # A destination/bot/tenant change never continues an old queue.
            stale = archive.conn.execute("SELECT id FROM telegram_bulk_jobs WHERE scope<>? AND state IN ('queued','running')", (self._scope(),)).fetchall()
            for row in stale:
                archive.conn.execute("UPDATE telegram_bulk_jobs SET state='cancelled',cancel_requested=1,error_code='scope_changed',updated_at=? WHERE id=?", (time.time(), row['id']))
                archive.conn.execute("UPDATE telegram_bulk_items SET state='cancelled' WHERE job_id=? AND state='queued'", (row['id'],))
        return len(pending)

    def _bot_id(self, archive):
        prefix, separator, _ = self.telegram.settings.token.partition(':')
        value = prefix if separator and prefix.isdecimal() else archive.state('telegram_bot_id')
        return int(value) if isinstance(value, str) and value.isdecimal() and int(value) > 0 else None

    def _valid_row(self, archive, item):
        row = archive.conn.execute("SELECT * FROM recordings WHERE key=? AND status='uploaded' AND deleted_at IS NULL", (item['recording_key'],)).fetchone()
        if row is None:
            return None
        row = dict(row)
        file_id = row.get('file_id')
        if (not isinstance(file_id, str) or not file_id.strip() or len(file_id) > 4096 or
                file_id != item['file_id'] or row.get('file_unique_id') != item['file_unique_id'] or
                self._media_type(row) != item['media_type'] or row.get('bot_id') != item['bot_id'] or
                self._placement(row) != item['placement']):
            return None
        if row.get('bot_id') is not None and row['bot_id'] != self._bot_id(archive):
            return None
        if item['media_type'] not in ('video', 'document'):
            return None
        chat = row.get('storage_chat_id') or row.get('chat_id')
        message = row.get('storage_message_id') or row.get('message_id')
        if type(message) is not int or message <= 0:
            return None
        if self.telegram.channel_mode:
            if (row.get('storage_kind') != 'channel' or type(row.get('storage_chat_id')) is not int or
                    row['storage_chat_id'] != self.telegram.settings.storage_channel_id or
                    type(row.get('storage_message_id')) is not int or row['storage_message_id'] <= 0):
                return None
        elif row.get('storage_kind') != 'owner_private' or str(chat) != str(self.telegram.owner):
            return None
        return row

    def _finish(self, archive, job_id, batch, phase, *, retry_at=0, error=None, messages=None):
        now = time.time()
        with archive.conn:
            for position, item in enumerate(batch):
                message_id = messages[position]['message_id'] if messages else None
                archive.conn.execute("UPDATE telegram_bulk_items SET state=?,message_id=? WHERE job_id=? AND position=? AND state='sending'",
                                     (phase, message_id, job_id, item['position']))
            row = archive.conn.execute('SELECT state,cancel_requested FROM telegram_bulk_jobs WHERE id=?', (job_id,)).fetchone()
            if row['cancel_requested']:
                state = 'cancelled'
                archive.conn.execute("UPDATE telegram_bulk_items SET state='cancelled' WHERE job_id=? AND state='queued'", (job_id,))
            elif phase in ('unknown', 'failed', 'cancelled'):
                state = phase
                if phase == 'cancelled':
                    archive.conn.execute("UPDATE telegram_bulk_items SET state='cancelled' WHERE job_id=? AND state='queued'", (job_id,))
            else:
                remaining = archive.conn.execute("SELECT 1 FROM telegram_bulk_items WHERE job_id=? AND state='queued' LIMIT 1", (job_id,)).fetchone()
                state = 'running' if remaining else 'done'
            archive.conn.execute('UPDATE telegram_bulk_jobs SET state=?,updated_at=?,retry_at=?,error_code=? WHERE id=?',
                                 (state, now, retry_at, error, job_id))

    def process_one(self, archive):
        """At most one original-media POST per tick; polling stays independent."""
        # Import lazily so Telegram can import this module without a cycle.
        from .telegram import ApiRejected
        self.ensure(archive)
        now, scope = time.time(), self._scope()
        archive.conn.execute('BEGIN IMMEDIATE')
        try:
            job = archive.conn.execute("SELECT * FROM telegram_bulk_jobs WHERE scope=? AND state IN ('queued','running') AND retry_at<=? ORDER BY updated_at,id LIMIT 1",
                                       (scope, now)).fetchone()
            if job is None:
                archive.conn.commit()
                return None
            job_id, actor = job['id'], job['actor']
            if actor not in self.telegram.viewers or job['cancel_requested']:
                archive.conn.execute("UPDATE telegram_bulk_jobs SET state='cancelled',cancel_requested=1,error_code='access_removed',updated_at=? WHERE id=?", (now, job_id))
                archive.conn.execute("UPDATE telegram_bulk_items SET state='cancelled' WHERE job_id=? AND state='queued'", (job_id,))
                archive.conn.commit()
                return {'id': job_id, 'state': 'cancelled'}
            # A persisted per-chat deadline survives cancellation/restarts and
            # prevents a new job from bypassing the previous album's pacing.
            pause_key = f'telegram_bulk_pause:{scope}:{actor}'
            try:
                pause = float(archive.state(pause_key) or 0)
            except (ValueError, TypeError, OverflowError):
                pause = 0
            if math.isfinite(pause) and pause > now:
                archive.conn.execute('UPDATE telegram_bulk_jobs SET retry_at=? WHERE id=?', (pause, job_id))
                archive.conn.commit()
                return None
            # Never claim another batch while a former call is still pending.
            if archive.conn.execute("SELECT 1 FROM telegram_bulk_items WHERE job_id=? AND state='sending'", (job_id,)).fetchone():
                archive.conn.commit()
                return None
            candidates = archive.conn.execute("SELECT * FROM telegram_bulk_items WHERE job_id=? AND state='queued' ORDER BY position LIMIT ?", (job_id, self.BATCH_SIZE)).fetchall()
            batch, rows, field = [], [], None
            for item in candidates:
                row = self._valid_row(archive, item)
                if row is None:
                    archive.conn.execute("UPDATE telegram_bulk_items SET state='skipped' WHERE job_id=? AND position=?", (job_id, item['position']))
                    continue
                if field is not None and item['media_type'] != field:
                    break  # Homogeneous album, preserving selection order.
                field = item['media_type']
                batch.append(dict(item)); rows.append(row)
            if not batch:
                remaining = archive.conn.execute("SELECT 1 FROM telegram_bulk_items WHERE job_id=? AND state='queued' LIMIT 1", (job_id,)).fetchone()
                state = 'running' if remaining else 'done'
                archive.conn.execute('UPDATE telegram_bulk_jobs SET state=?,updated_at=? WHERE id=?', (state, now, job_id))
                archive.conn.commit()
                return {'id': job_id, 'state': state}
            for item in batch:
                archive.conn.execute("UPDATE telegram_bulk_items SET state='sending' WHERE job_id=? AND position=? AND state='queued'", (job_id, item['position']))
            archive.conn.execute("UPDATE telegram_bulk_jobs SET state='running',updated_at=? WHERE id=?", (now, job_id))
            archive.conn.execute('INSERT INTO state(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value',
                                 (pause_key, str(now + self.CHAT_INTERVAL)))
            archive.conn.commit()
        except Exception:
            archive.conn.rollback()
            raise
        # A delete/allowlist removal/cancel between claim and POST must not send
        # anything. A cancellation during an in-flight POST stops later batches.
        try:
            current = archive.conn.execute('SELECT cancel_requested FROM telegram_bulk_jobs WHERE id=?', (job_id,)).fetchone()
            if actor not in self.telegram.viewers or current['cancel_requested']:
                self._finish(archive, job_id, batch, 'cancelled')
                return {'id': job_id, 'state': 'cancelled'}
            if any(self._valid_row(archive, item) is None for item in batch):
                self._finish(archive, job_id, batch, 'queued', retry_at=time.time() + 1)
                return {'id': job_id, 'state': 'changed'}
            media = []
            for item, row in zip(batch, rows):
                value = {'type': field, 'media': item['file_id'], 'caption': self.telegram.caption(archive, row)[:1024]}
                if field == 'video':
                    value['supports_streaming'] = True
                else:
                    value['disable_content_type_detection'] = True
                media.append(value)
            fields = {'chat_id': actor, 'disable_notification': True, 'protect_content': False}
            if len(batch) == 1:
                method = 'sendVideo' if field == 'video' else 'sendDocument'
                fields.update({field: media[0]['media'], 'caption': media[0]['caption']})
                fields['supports_streaming' if field == 'video' else 'disable_content_type_detection'] = True
            else:
                method = 'sendMediaGroup'
                fields['media'] = media
        except Exception:
            # No POST has happened in this phase, so an ordinary retry is known
            # not to duplicate any message. Do not strand a sending batch.
            self._finish(archive, job_id, batch, 'queued', retry_at=time.time() + self.CHAT_INTERVAL, error='preflight_failed')
            return {'id': job_id, 'state': 'preflight_retry'}
        try:
            response = self.telegram.request(method, fields)
            messages = [response] if len(batch) == 1 else response
            if not isinstance(messages, list) or len(messages) != len(batch):
                raise ValueError('Unconfirmed Telegram album length')
            ids, groups = set(), set()
            for message, item in zip(messages, batch):
                returned = self.telegram.validate_media_message(message, field, actor)
                if item['file_unique_id'] and returned['file_unique_id'] != item['file_unique_id']:
                    raise ValueError('Unconfirmed Telegram media identity')
                ids.add(message['message_id'])
                if len(batch) > 1:
                    group = message.get('media_group_id')
                    if not isinstance(group, str) or not group:
                        raise ValueError('Unconfirmed Telegram album identity')
                    groups.add(group)
            if len(ids) != len(batch) or len(groups) > 1:
                raise ValueError('Unconfirmed Telegram album ordering')
        except ApiRejected as exc:
            if exc.code == 429:
                delay = self.telegram._retry_delay(exc.retry_after)
                deadline = time.time() + delay
                with archive.conn:
                    archive.conn.execute('INSERT INTO state(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value',
                                         (pause_key, str(max(now + self.CHAT_INTERVAL, deadline))))
                self._finish(archive, job_id, batch, 'queued', retry_at=deadline, error='rate_limited')
                return {'id': job_id, 'state': 'rate_limited'}
            phase = 'failed' if type(exc.code) is int and 400 <= exc.code < 500 else 'unknown'
            self._finish(archive, job_id, batch, phase, error='api_rejected' if phase == 'failed' else 'unconfirmed_post')
            return {'id': job_id, 'state': phase}
        except Exception:
            self._finish(archive, job_id, batch, 'unknown', error='unconfirmed_post')
            return {'id': job_id, 'state': 'unknown'}
        self._finish(archive, job_id, batch, 'sent', messages=messages)
        return self.status(archive, actor, job_id)

    def notify_one(self, archive):
        """One best-effort terminal status notice; media are never retried here."""
        self.ensure(archive)
        job = archive.conn.execute("SELECT id,actor FROM telegram_bulk_jobs WHERE scope=? AND state IN ('done','unknown','failed') AND notified=0 ORDER BY updated_at LIMIT 1", (self._scope(),)).fetchone()
        if job is None:
            return None
        with archive.conn:
            archive.conn.execute('UPDATE telegram_bulk_jobs SET notified=1 WHERE id=?', (job['id'],))
        if job['actor'] not in self.telegram.viewers:
            return None
        text, buttons = self.menu(archive, job['actor'], job['id'])
        self.telegram.request('sendMessage', {'chat_id': job['actor'], 'text': text,
                                           'reply_markup': {'inline_keyboard': buttons}, 'disable_notification': True})
        return job['id']
