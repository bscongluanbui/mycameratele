"""Bounded, durable retries for old, definitively unsent camera media.

The normal uploader remains the only Telegram sender. Ambiguous deliveries,
global channel failures and rate limits never enter this retry queue.
"""
from __future__ import annotations

import math
import re
import time


MAX_RETRIES = 3
RETRY_SECONDS = 900


def _result(key, kind, *, ready=False, attempted=False, queued=False):
    return {'key': key, 'ready': bool(ready), 'attempted': bool(attempted),
            'queued': bool(queued), 'kind': kind}


def retry_one(archive, camera, *, after, now=None):
    """Reserve one old error retry, or permanently park an exhausted error.

    ``after`` is the current healthy sync's start, not a fabricated receipt
    time. Each API retry requires a newer confirmed delivery for the current
    camera, bot and channel. Raw retries reuse the retained source with stream
    copy; neither this helper nor its callers may transcode it.

    A returned API ``ready`` row must pass through the normal uploader. The
    retry counter is reserved before that claim, so a process restart cannot
    create an unbounded retry loop. Cache backpressure before remux refunds
    the reservation instead of consuming one of the three retries.
    """
    if not isinstance(camera, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', camera):
        raise ValueError('Invalid error retry camera')
    now = time.time() if now is None else now
    for value in (after, now):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0):
            raise ValueError('Invalid error retry timestamp')

    chosen = None
    archive.conn.execute('BEGIN IMMEDIATE')
    try:
        rows = archive.conn.execute('''SELECT r.* FROM recordings r
            JOIN cameras c ON c.id=r.camera
            WHERE r.camera=? AND r.status='needs_review'
                AND r.last_error IN ('failed_remux','api_rejected')
                AND r.created_at<? AND r.error_started_at<?
                AND r.deleted_at IS NULL AND r.media_expired_at IS NULL
                AND r.error_queue_at IS NULL
                AND (r.error_retry_count>=? OR r.error_retry_at<=?)
                AND c.deleted_at IS NULL AND c.enabled=1 AND c.upload_enabled=1
            ORDER BY r.created_at,r.key''',
            (camera, after, after, MAX_RETRIES, now)).fetchall()
        for row in rows:
            kind = 'raw' if row['last_error'] == 'failed_remux' else 'api'
            if kind == 'raw' and archive.settings.media_mode != 'remux_copy':
                continue
            valid = (archive.retained_remux_failure(row['key']) if kind == 'raw'
                     else archive.retained_api_rejection(row['key'], after=after, camera=camera))
            if not valid:
                continue
            count = row['error_retry_count']
            if count >= MAX_RETRIES:
                archive.conn.execute('''UPDATE recordings SET error_queue_at=?
                    WHERE key=? AND status='needs_review' AND error_queue_at IS NULL
                        AND error_retry_count>=?''', (now, row['key'], MAX_RETRIES))
                archive.conn.commit()
                return _result(row['key'], kind, queued=True)
            status = 'failed' if kind == 'raw' else 'downloaded'
            changed = archive.conn.execute('''UPDATE recordings
                SET status=?,last_error=?,error_retry_count=error_retry_count+1,
                    error_retry_at=?,retry_at=0
                WHERE key=? AND status='needs_review' AND error_retry_count=?
                    AND error_queue_at IS NULL AND deleted_at IS NULL
                    AND media_expired_at IS NULL''',
                (status, 'failed_remux' if kind == 'raw' else None,
                 now + RETRY_SECONDS, row['key'], count)).rowcount
            if changed:
                chosen = (dict(row), kind)
                break
        archive.conn.commit()
    except Exception:
        archive.conn.rollback()
        raise
    if chosen is None:
        return None
    row, kind = chosen
    if kind == 'api':
        return _result(row['key'], kind, ready=True, attempted=True)

    outcome = archive.remux_pending_cache(camera, limit=1, key=row['key'])
    if outcome.get('budget_blocked'):
        # Remux admission declined before FFmpeg or media claim. Restore only
        # our unchanged reservation; never rewrite a concurrent upload receipt.
        with archive.conn:
            archive.conn.execute('''UPDATE recordings
                SET status='needs_review',last_error='failed_remux',
                    error_retry_count=?,error_retry_at=?,retry_at=?
                WHERE key=? AND status='failed' AND last_error='failed_remux'
                    AND error_retry_count=? AND error_queue_at IS NULL''',
                (row['error_retry_count'], row['error_retry_at'], row['retry_at'],
                 row['key'], row['error_retry_count'] + 1))
        return _result(row['key'], kind)

    with archive.conn:
        if row['error_retry_count'] + 1 >= MAX_RETRIES:
            archive.conn.execute('''UPDATE recordings
                SET status='needs_review',error_queue_at=coalesce(error_queue_at,?)
                WHERE key=? AND status IN ('failed','needs_review')
                    AND error_retry_count>=? AND deleted_at IS NULL
                    AND media_expired_at IS NULL
                    AND file_id IS NULL AND message_id IS NULL AND chat_id IS NULL
                    AND uploaded_at IS NULL''', (now, row['key'], MAX_RETRIES))
        current = archive.conn.execute('''SELECT status,error_queue_at FROM recordings
            WHERE key=?''', (row['key'],)).fetchone()
    return _result(row['key'], kind, ready=current['status'] == 'downloaded',
                   attempted=True, queued=current['error_queue_at'] is not None)
