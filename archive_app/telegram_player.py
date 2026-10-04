"""Short-lived, actor-issued player links; media bytes never enter archive cache.

The URL is a bearer capability, not a Telegram login assertion. Its actor is the
allowlisted private-chat user to whom the bot issued it. Every HTTP request checks
that actor again and rechecks the recording, bot, tenant and channel placement.
Local Bot API files are read only through its explicitly mounted state directory;
cloud download URLs and bot credentials never leave this server.
"""
import hashlib
import hmac
import html
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time
import urllib.parse
import urllib.request


class PlayerDenied(ValueError):
    """Unusable or revoked capability; deliberately contains no identity data."""


class PlayerUnavailable(Exception):
    """Unavailable media; deliberately contains no upstream error or file path."""


class RangeRejected(Exception):
    pass


def byte_range(value, size):
    """Return inclusive bounds for exactly one RFC 9110 byte range."""
    if type(size) is not int or size <= 0:
        raise PlayerUnavailable()
    if value is None:
        return 0, size - 1, False
    if not isinstance(value, str) or len(value) > 128:
        raise RangeRejected()
    match = re.fullmatch(r'bytes=([0-9]*)-([0-9]*)', value.strip())
    if not match or not (match[1] or match[2]):
        raise RangeRejected()
    if match[1]:
        start = int(match[1])
        end = int(match[2]) if match[2] else size - 1
        if start >= size or end < start:
            raise RangeRejected()
        end = min(end, size - 1)
    else:
        suffix = int(match[2])
        if suffix <= 0:
            raise RangeRejected()
        start, end = max(0, size - suffix), size - 1
    return start, end, True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        # Never forward a credential-bearing Bot API download URL elsewhere.
        return None


class PlayerMedia:
    """A sized, seekable local source or an exact-range upstream proxy."""
    def __init__(self, size, *, local=None, url=None, opener=None):
        self.size, self.local, self.url = size, local, url
        self.opener = opener or urllib.request.build_opener(_NoRedirect()).open
        self.handle = None

    def open(self, start, end, partial=False):
        if self.local is not None:
            self.handle = self.local
            self.handle.seek(start)
            return self
        headers = {'Accept-Encoding': 'identity'}
        if partial:
            headers['Range'] = f'bytes={start}-{end}'
        try:
            response = self.opener(urllib.request.Request(self.url, headers=headers), timeout=35)
            status = getattr(response, 'status', response.getcode())
            if (status != (206 if partial else 200) or
                    response.headers.get('Content-Encoding', 'identity') not in ('', 'identity') or
                    response.headers.get('Content-Length') != str(end-start+1) or
                    partial and response.headers.get('Content-Range') != f'bytes {start}-{end}/{self.size}'):
                response.close()
                raise PlayerUnavailable()
            self.handle = response
            return self
        except PlayerUnavailable:
            raise
        except Exception:
            raise PlayerUnavailable() from None

    def chunks(self, length):
        while length:
            data = self.handle.read(min(65536, length))
            if not data:
                raise PlayerUnavailable()
            length -= len(data)
            yield data

    def close(self):
        if self.handle is not None:
            self.handle.close()
        elif self.local is not None:
            self.local.close()


class TelegramPlayer:
    PREFIX = 'telegram_player_cap:'
    CAP_RE = re.compile(r'[A-Za-z0-9_-]{43}\.[a-f0-9]{32}')

    def __init__(self, settings, *, telegram=None, opener=None):
        self.settings, self.telegram, self.opener = settings, telegram, opener

    def public_url(self):
        raw = getattr(self.settings, 'player_public_url', '')
        if not isinstance(raw, str) or any(character.isspace() or ord(character) < 32 for character in raw):
            raise ValueError('Invalid player public URL')
        value = raw.rstrip('/')
        parsed = urllib.parse.urlsplit(value)
        try:
            port = parsed.port
        except ValueError:
            raise ValueError('Invalid player public URL') from None
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or
                parsed.query or parsed.fragment or parsed.path not in ('', '/') or
                any(character.isspace() or ord(character) < 32 for character in value) or
                port is not None and not 1 <= port <= 65535):
            raise ValueError('Invalid player public URL')
        return value

    def _bot(self, archive):
        prefix, separator, _ = self.settings.token.partition(':')
        candidate = prefix if separator and prefix.isdecimal() else archive.state('telegram_bot_id')
        if not isinstance(candidate, str) or not candidate.isdecimal() or int(candidate) <= 0:
            raise PlayerDenied()
        return int(candidate)

    def _scope(self, archive):
        if not self.settings.token:
            raise PlayerDenied()
        return [getattr(self.settings, 'tenant_id', 'house01'), self._bot(archive),
                hashlib.sha256(self.settings.token.encode()).hexdigest(),
                getattr(self.settings, 'storage_channel_id', 0), self.settings.effective_owner]

    def _signature(self, nonce):
        return hmac.new(hashlib.sha256(self.settings.token.encode()).digest(),
                        ('ezviz-player-v1\0'+nonce).encode(), hashlib.sha256).hexdigest()[:32]

    @staticmethod
    def _actor(archive, actor):
        if type(actor) is not int or actor <= 0:
            raise PlayerDenied()
        try:
            archive._archive_actor(actor)
        except (PermissionError, ValueError, TypeError):
            raise PlayerDenied() from None

    def _row(self, archive, key):
        row = archive.find_recording(key)
        if not row or row.get('bot_id') != self._bot(archive):
            raise PlayerDenied()
        if not isinstance(row.get('file_id'), str) or not row['file_id'].strip():
            raise PlayerDenied()
        kind = row.get('media_type')
        if kind not in ('video', 'document'):
            raise PlayerDenied()
        channel = getattr(self.settings, 'storage_channel_id', 0)
        chat = row.get('storage_chat_id') or row.get('chat_id')
        message = row.get('storage_message_id') or row.get('message_id')
        if type(message) is not int or message <= 0:
            raise PlayerDenied()
        if channel:
            if (row.get('storage_kind') != 'channel' or type(chat) is not int or chat != channel or channel >= 0):
                raise PlayerDenied()
        elif row.get('storage_kind') != 'owner_private' or str(chat) != str(self.settings.effective_owner):
            raise PlayerDenied()
        # The archive catalog is authoritative for display names, including
        # cameras renamed after the player capability was issued.
        row['camera_name'] = archive.camera_name(row['camera'])
        return row

    @staticmethod
    def _placement(row):
        return [row['key'], row['file_id'], row.get('file_unique_id'), row.get('bot_id'),
                row.get('storage_kind'), row.get('storage_chat_id'), row.get('storage_message_id'),
                row.get('chat_id'), row.get('message_id'), row.get('media_type')]

    def _prune(self, archive, now):
        try:
            last = float(archive.state('telegram_player_last_prune') or 0)
        except (ValueError, TypeError):
            last = 0
        if math.isfinite(last) and now-last < 60:
            return
        entries = archive.conn.execute('SELECT name,value FROM state WHERE name LIKE ?', (self.PREFIX+'%',)).fetchall()
        stale, alive = [], []
        for entry in entries:
            try:
                expiry = json.loads(entry['value']).get('expires')
                if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= now:
                    stale.append(entry['name'])
                else:
                    alive.append((expiry, entry['name']))
            except (ValueError, TypeError, AttributeError):
                stale.append(entry['name'])
        # Bound retained capabilities even if a client repeatedly changes pages.
        stale.extend(name for _, name in sorted(alive)[:max(0, len(alive)-9990)])
        with archive.conn:
            archive.conn.executemany('DELETE FROM state WHERE name=?', [(name,) for name in stale])
        archive.state('telegram_player_last_prune', now)

    def issue(self, archive, row_or_key, actor, ttl=900):
        base = self.public_url()
        self._actor(archive, actor)
        if type(ttl) is not int or not 1 <= ttl <= 900:
            raise ValueError('Player expiry must be between 1 and 900 seconds')
        key = row_or_key.get('key') if isinstance(row_or_key, dict) else row_or_key
        row = self._row(archive, key)
        now = time.time()
        self._prune(archive, now)
        nonce = secrets.token_urlsafe(32)
        cap = nonce+'.'+self._signature(nonce)
        value = {'actor': actor, 'key': row['key'], 'expires': now+ttl,
                 'scope': self._scope(archive), 'placement': self._placement(row)}
        archive.state(self.PREFIX+hashlib.sha256(cap.encode()).hexdigest(),
                      json.dumps(value, separators=(',', ':')))
        return base+'/player/'+cap

    def resolve(self, archive, cap):
        # Removing the web-player setting disables previously issued links too.
        if not self.settings.player_public_url:
            raise PlayerDenied()
        if not isinstance(cap, str) or not self.CAP_RE.fullmatch(cap):
            raise PlayerDenied()
        nonce, signature = cap.split('.')
        if not hmac.compare_digest(signature, self._signature(nonce)):
            raise PlayerDenied()
        raw = archive.state(self.PREFIX+hashlib.sha256(cap.encode()).hexdigest())
        try:
            saved = json.loads(raw or '{}')
            expires = saved.get('expires')
            if (type(expires) not in (int, float) or not math.isfinite(expires) or
                    expires <= time.time() or saved.get('scope') != self._scope(archive)):
                raise PlayerDenied()
            self._actor(archive, saved.get('actor'))
            row = self._row(archive, saved.get('key'))
            if saved.get('placement') != self._placement(row):
                raise PlayerDenied()
            return row
        except (ValueError, TypeError, AttributeError):
            raise PlayerDenied() from None

    @staticmethod
    def playable(row):
        # A document can still be an MP4; never invoke ffprobe/FFmpeg here.
        return (row.get('media_type') == 'video' or
                row.get('media_container') in ('mp4', 'mov,mp4,m4a,3gp,3g2,mj2') or
                str(row.get('media_extension', '')).lower() in ('.mp4', 'mp4'))

    def open_media(self, archive, row):
        if self.telegram is None:
            from .telegram import Telegram
            telegram = Telegram(self.settings)
        else:
            telegram = self.telegram
        try:
            result = telegram.request('getFile', {'file_id': row['file_id']})
            if (not isinstance(result, dict) or not isinstance(result.get('file_id'), str) or
                    not result['file_id'].strip() or
                    row.get('file_unique_id') and result.get('file_unique_id') != row['file_unique_id']):
                raise PlayerUnavailable()
            path = result.get('file_path')
            if not isinstance(path, str) or not path or len(path) > 4096 or '\0' in path:
                raise PlayerUnavailable()
            if self.settings.api_mode == 'local':
                root = Path(getattr(self.settings, 'bot_api_file_root', '/var/lib/telegram-bot-api')).resolve(strict=True)
                original = Path(path)
                if not original.is_absolute():
                    raise PlayerUnavailable()
                resolved = original.resolve(strict=True)
                relative = resolved.relative_to(root)
                if not relative.parts or relative.parts[0] != self.settings.token:
                    raise PlayerUnavailable()
                # Reject pipes/devices before opening them. NONBLOCK also
                # prevents a FIFO swapped into place from blocking this worker;
                # NOFOLLOW rejects a final-component symlink introduced after
                # the containment check. fstat remains the final authority.
                expected = resolved.stat()
                if not stat.S_ISREG(expected.st_mode):
                    raise PlayerUnavailable()
                flags = (os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) |
                         getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
                descriptor = os.open(resolved, flags)
                try:
                    handle = os.fdopen(descriptor, 'rb')
                except Exception:
                    os.close(descriptor)
                    raise
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or
                        (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino)):
                    handle.close()
                    raise PlayerUnavailable()
                declared = result.get('file_size')
                if declared is not None and (type(declared) is not int or declared != info.st_size):
                    handle.close()
                    raise PlayerUnavailable()
                return PlayerMedia(info.st_size, local=handle)
            size = result.get('file_size')
            if (type(size) is not int or size <= 0 or
                    not re.fullmatch(r'[A-Za-z0-9_./-]+', path) or path.startswith('/') or
                    any(piece in ('', '.', '..') for piece in path.split('/'))):
                raise PlayerUnavailable()
            url = self.settings.api_base.rstrip('/')+'/file/bot'+self.settings.token+'/'+urllib.parse.quote(path, safe='/')
            return PlayerMedia(size, url=url, opener=self.opener)
        except PlayerUnavailable:
            raise
        except Exception:
            raise PlayerUnavailable() from None

    @staticmethod
    def _headers(handler, status, length, mime, *, extra=None):
        handler.send_response(status)
        for name, value in [('Content-Type', mime), ('Content-Length', str(length)),
                            ('Cache-Control', 'no-store, private'), ('Pragma', 'no-cache'),
                            ('Referrer-Policy', 'no-referrer'), ('X-Content-Type-Options', 'nosniff'),
                            ('X-Frame-Options', 'DENY'),
                            ('Content-Security-Policy', "default-src 'none'; media-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")]:
            handler.send_header(name, value)
        for name, value in (extra or {}).items():
            handler.send_header(name, value)
        handler.end_headers()

    @classmethod
    def _error(cls, handler, status, *, head=False, size=None):
        body = ('Video chưa sẵn sàng.' if status == 502 else
                'Liên kết đã hết hạn hoặc video đã bị xóa. Mở lại danh sách trên bot.').encode('utf-8')
        extra = {'Content-Range': f'bytes */{size}'} if status == 416 and size else None
        cls._headers(handler, status, len(body), 'text/plain; charset=utf-8', extra=extra)
        if not head:
            handler.wfile.write(body)

    def handle(self, handler, archive, path, *, head=False):
        if not path.startswith('/player/'):
            return False
        match = re.fullmatch(r'/player/([A-Za-z0-9_-]{43}\.[a-f0-9]{32})(?:/(video|download))?', path)
        if not match:
            self._error(handler, 404, head=head)
            return True
        cap, action = match.groups()
        media = None
        sent = False
        try:
            row = self.resolve(archive, cap)
            if action is None:
                title = html.escape(str(row.get('camera_name') or row['camera']), quote=True)
                if self.playable(row):
                    content = ('<video controls autoplay muted playsinline preload="metadata" '
                               f'src="/player/{cap}/video"></video>')
                else:
                    content = '<p>Định dạng này dùng nút Tải để xem bản gốc.</p>'
                body = ('<!doctype html><html lang="vi"><meta charset="utf-8">'
                        '<meta name="viewport" content="width=device-width,initial-scale=1">'
                        f'<title>{title}</title><style>body{{margin:0;background:#0c111b;color:#eef3fa;'
                        'font:16px system-ui;padding:20px}main{max-width:1080px;margin:auto}'
                        'video{width:100%;max-height:84vh;background:#000;border-radius:12px}'
                        'h1{font-size:20px}a{display:inline-block;color:#8ac7ff;margin-top:16px}</style>'
                        f'<main><h1>{title}</h1>{content}<a href="/player/{cap}/download">⬇ Tải bản gốc</a>'
                        '</main></html>').encode('utf-8')
                self._headers(handler, 200, len(body), 'text/html; charset=utf-8')
                if not head:
                    handler.wfile.write(body)
                return True
            if action == 'video' and not self.playable(row):
                raise PlayerUnavailable()
            media = self.open_media(archive, row)
            start, end, partial = byte_range(handler.headers.get('Range'), media.size)
            # getFile can take time: a deletion/revocation while fetching must win.
            self.resolve(archive, cap)
            if not head:
                media.open(start, end, partial)
            extra = {'Accept-Ranges': 'bytes'}
            if partial:
                extra['Content-Range'] = f'bytes {start}-{end}/{media.size}'
            if action == 'download':
                extra['Content-Disposition'] = 'attachment; filename="camera-'+row['key'][:16]+('.mp4"' if self.playable(row) else '.bin"')
            else:
                extra['Content-Disposition'] = 'inline'
            self._headers(handler, 206 if partial else 200, end-start+1,
                          'video/mp4' if self.playable(row) else 'application/octet-stream', extra=extra)
            sent = True
            if not head:
                for data in media.chunks(end-start+1):
                    handler.wfile.write(data)
            return True
        except PlayerDenied:
            if not sent:
                self._error(handler, 403, head=head)
        except RangeRejected:
            if not sent:
                self._error(handler, 416, head=head, size=media.size if media else None)
        except PlayerUnavailable:
            if not sent:
                self._error(handler, 502, head=head)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            if media is not None:
                media.close()
        return True
