"""Stateless time shortcuts for the private Telegram archive.

A callback contains the time when its shortcut was selected, not a moving
relative range. Paging, sorting and returning to cameras therefore retain the
same window even after midnight. Camera names never become callback identity.
"""
from datetime import datetime, timedelta
import json
import math
import re
import secrets
import time

from .core import from_epoch_ms, get_zone


class TimeMenus:
    PAGE_SIZE = 10
    INPUT_TTL = 10 * 60
    RANGE_TTL = 24 * 3600
    MAX_RANGE_MS = 31 * 86400000
    _KINDS = {'today': 't', 'yesterday': 'y', 'last6h': 'h', 'thisweek': 's', 'lastweek': 'p'}
    _LABELS = {'t': 'Hôm nay', 'y': 'Hôm qua', 'h': '6 giờ trước', 's': 'Tuần này', 'p': 'Tuần trước'}
    _CAMERAS = re.compile(r'w:([tyhsp]):([1-9][0-9]{0,10}):([ad]):(0|[1-9][0-9]{0,5})')
    _CLIPS = re.compile(r'wc:([tyhsp]):([1-9][0-9]{0,10}):([a-f0-9]{12}):([ad]):(0|[1-9][0-9]{0,5}):(0|[1-9][0-9]{0,5})')
    _CUSTOM_CAMERAS = re.compile(r'wq:([a-f0-9]{12}):([ad]):(0|[1-9][0-9]{0,5})')
    _CUSTOM_CLIPS = re.compile(r'wqc:([a-f0-9]{12}):([a-f0-9]{12}):([ad]):(0|[1-9][0-9]{0,5}):(0|[1-9][0-9]{0,5})')
    _DOWNLOAD = re.compile(r'bw:([tyhsp]):([1-9][0-9]{0,10}):([a-f0-9]{12}|all):([ad])')
    _CUSTOM_DOWNLOAD = re.compile(r'bwq:([a-f0-9]{12}):([a-f0-9]{12}|all):([ad])')

    def __init__(self, telegram):
        self.telegram = telegram

    @staticmethod
    def shortcuts():
        """Fresh inline-keyboard rows, suitable for /start and archive menus."""
        return [
            [TimeMenus._button('📅 Hôm nay', 'today', green=True)],
            [TimeMenus._button('📆 Hôm qua', 'yesterday', green=True)],
            [TimeMenus._button('🕕 6 giờ trước', 'last6h', green=True)],
            [TimeMenus._button('🗓 Tuần này', 'thisweek', green=True)],
            [TimeMenus._button('📆 Tuần trước', 'lastweek', green=True)],
            [TimeMenus._button('🗓 Tùy chọn thời gian', 'custom-time', green=True)],
        ]

    @staticmethod
    def commands():
        """Commands for Telegram's persistent Menu button (setMyCommands)."""
        return [
            {'command': 'start', 'description': 'Start / Menu'},
            {'command': 'sync', 'description': 'Start sync'},
            {'command': 'today', 'description': 'Hôm nay'},
            {'command': 'yesterday', 'description': 'Hôm qua'},
            {'command': 'last6h', 'description': '6 giờ trước'},
            {'command': 'thisweek', 'description': 'Tuần này'},
            {'command': 'lastweek', 'description': 'Tuần trước'},
            {'command': 'time', 'description': 'Tùy chọn thời gian'},
            {'command': 'archive', 'description': 'Kho video'},
            {'command': 'recent', 'description': 'Video gần đây'},
            {'command': 'trash', 'description': 'Thùng rác'},
            {'command': 'status', 'description': 'Trạng thái'},
        ]

    @staticmethod
    def _button(text, callback, *, green=False):
        # Telegram accepts 1–64 UTF-8 bytes, not 64 Unicode characters.
        if not 1 <= len(callback.encode('utf-8')) <= 64:
            raise ValueError('Time-menu callback is too long')
        button = {'text': text, 'callback_data': callback}
        # Native Bot API style: Telegram renders the green background itself.
        if green:
            button['style'] = 'success'
        return button

    @staticmethod
    def _camera_callback(kind, anchor, order, page):
        if kind == 'q':
            return f'wq:{anchor}:{order}:{page}'
        return f'w:{kind}:{anchor}:{order}:{page}'

    @staticmethod
    def _clip_callback(kind, anchor, token, order, page, camera_page):
        if kind == 'q':
            return f'wqc:{anchor}:{token}:{order}:{page}:{camera_page}'
        return f'wc:{kind}:{anchor}:{token}:{order}:{page}:{camera_page}'

    @staticmethod
    def _download_callback(kind, anchor, token, order):
        if kind == 'q':
            return f'bwq:{anchor}:{token}:{order}'
        return f'bw:{kind}:{anchor}:{token}:{order}'

    def download_window(self, archive, data, actor):
        """Resolve an allowlisted viewer's frozen bulk window; never a page slice."""
        self._state_key(actor)
        if not isinstance(data, str) or len(data.encode('utf-8')) > 64:
            raise ValueError('Invalid bulk-download selection')
        calendar = re.fullmatch(r'bd:([a-f0-9]{12}):([0-9]{4}-[0-9]{2}-[0-9]{2}):([ad])', data)
        if calendar:
            token, day, order = calendar.groups()
            begin = datetime.strptime(day, '%Y-%m-%d')
            start = self._local_timestamp(begin)
            end = self._local_timestamp(begin + timedelta(days=1))
            camera = self.telegram._camera(archive, token)['id']
            return {'start_ms': start, 'end_ms': end, 'camera': camera,
                    'order': 'asc' if order == 'a' else 'desc', 'label': begin.strftime('%d/%m/%y')}
        custom = self._CUSTOM_DOWNLOAD.fullmatch(data)
        if custom:
            anchor, token, order = custom.groups()
            start, end, label = self._custom_window(archive, actor, anchor)
        else:
            selected = self._DOWNLOAD.fullmatch(data)
            if selected is None:
                raise ValueError('Invalid bulk-download selection')
            kind, anchor, token, order = selected.groups()
            start, end, label = self._window(kind, int(anchor))
        camera = None if token == 'all' else self.telegram._camera(archive, token)['id']
        return {'start_ms': start, 'end_ms': end, 'camera': camera,
                'order': 'asc' if order == 'a' else 'desc', 'label': label}

    def _state_key(self, actor):
        if type(actor) is not int or actor not in self.telegram.viewers:
            raise ValueError('Invalid time-menu actor')
        return f'telegram_time_selection:{actor}'

    def _session(self, archive, actor):
        key = self._state_key(actor)
        try:
            value = json.loads(archive.state(key) or '{}')
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _save_session(self, archive, actor, value):
        archive.state(self._state_key(actor), json.dumps(value, separators=(',', ':')))

    @staticmethod
    def _cancel_buttons():
        return [[{'text': '↩ Quay lại', 'callback_data': 'cancel-time'}]]

    def begin(self, archive, actor):
        self._save_session(archive, actor, {'step': 'start', 'expires': time.time() + self.INPUT_TTL})
        return '🗓 Từ ngày nào?\n'+self._format_hint(), self._cancel_buttons()

    def _format_hint(self):
        configured=self.telegram.settings.timezone
        zone='giờ VN' if configured in ('UTC+07:00','Asia/Ho_Chi_Minh','Asia/Bangkok','Etc/GMT-7') else configured
        return 'DD/MM/YY · '+zone

    def cancel(self, archive, actor):
        self._save_session(archive, actor, {})

    def dismiss_input(self, archive, actor):
        if self._session(archive, actor).get('step') in ('start', 'end'):
            self.cancel(archive, actor)

    def _local_timestamp(self, naive):
        zone = get_zone(self.telegram.settings.timezone)
        selected = naive.replace(tzinfo=zone)
        # Never silently choose a repeated clock hour or normalize a DST gap.
        if selected.utcoffset() != selected.replace(fold=1).utcoffset():
            raise ValueError('Ambiguous selected local time')
        result = int(selected.timestamp() * 1000)
        if (0 <= result < 4133980800000 and
                from_epoch_ms(result, zone).replace(tzinfo=None) == naive):
            return result
        raise ValueError('Invalid selected time')

    def _parse_selection(self, text):
        if not isinstance(text, str) or len(text) > 32:
            raise ValueError('Invalid selected time')
        text = text.strip()
        date_only = re.fullmatch(r'([0-9]{2})/([0-9]{2})/([0-9]{2}|[0-9]{4})', text)
        if date_only:
            day, month, year = date_only.groups()
            # Two-digit years always mean 2000–2099, not strptime's 1969 pivot.
            year = int(year) + (2000 if len(year) == 2 else 0)
            return self._local_timestamp(datetime(year, int(month), int(day))), True
        formats = ((r'[0-9]{2}/[0-9]{2}/[0-9]{4} [0-9]{2}:[0-9]{2}', '%d/%m/%Y %H:%M'),
                   (r'[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}', '%Y-%m-%d %H:%M'))
        for pattern, format_ in formats:
            if re.fullmatch(pattern, text):
                return self._local_timestamp(datetime.strptime(text, format_)), False
        raise ValueError('Invalid selected time')

    def parse_timestamp(self, text):
        """Date-only values start at local midnight; legacy datetimes still work."""
        return self._parse_selection(text)[0]

    def _range_allowed(self, start, end, date_only=False):
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= 4133980800000:
            return False
        if not date_only:
            return end - start <= self.MAX_RANGE_MS
        zone = get_zone(self.telegram.settings.timezone)
        begin, finish = (from_epoch_ms(value, zone) for value in (start, end))
        return (begin.time() == datetime.min.time() and finish.time() == datetime.min.time()
                and 1 <= (finish.date() - begin.date()).days <= 31)

    def accept(self, archive, actor, text):
        """Consume date input only for this allowlisted user's pending session."""
        session = self._session(archive, actor)
        if session.get('step') not in ('start', 'end'):
            return None
        if (type(session.get('expires')) not in (int, float) or not math.isfinite(session['expires']) or
                session['expires'] <= time.time()):
            self.cancel(archive, actor)
            return '⌛ Hết thời gian chọn.', [[self._button('🗓 Chọn lại', 'custom-time'), self._button('🏠 Menu', 'home')]]
        try:
            selected, date_only = self._parse_selection(text)
            if session['step'] == 'end' and date_only:
                zone = get_zone(self.telegram.settings.timezone)
                # Inclusive selected end date; construct tomorrow's midnight
                # in the configured zone, not an elapsed 86,400-second offset.
                end_date = from_epoch_ms(selected, zone).date() + timedelta(days=1)
                selected = self._local_timestamp(datetime.combine(end_date, datetime.min.time()))
        except (ValueError, OverflowError, OSError):
            return 'Ngày giờ chưa đúng.\n'+self._format_hint(), self._cancel_buttons()
        if session['step'] == 'start':
            session.update(step='end', start_ms=selected, start_date_only=date_only)
            self._save_session(archive, actor, session)
            return '🗓 Đến ngày nào?\n'+self._format_hint(), self._cancel_buttons()
        start = session.get('start_ms')
        if type(start) is not int or selected <= start:
            return 'Giờ kết thúc phải sau giờ bắt đầu.', self._cancel_buttons()
        range_date_only = session.get('start_date_only') is True and date_only
        if not self._range_allowed(start, selected, range_date_only):
            return 'Chọn tối đa 31 ngày.', self._cancel_buttons()
        session = {'token': secrets.token_hex(6), 'start_ms': start, 'end_ms': selected,
                   'date_only': range_date_only, 'expires': time.time() + self.RANGE_TTL}
        self._save_session(archive, actor, session)
        return self.menu(archive, self._camera_callback('q', session['token'], 'a', 0), actor=actor)

    def _custom_window(self, archive, actor, token):
        session = self._session(archive, actor)
        start, end, expires = (session.get(key) for key in ('start_ms', 'end_ms', 'expires'))
        if (session.get('token') != token or type(start) is not int or type(end) is not int or
                not self._range_allowed(start, end, session.get('date_only') is True) or
                type(expires) not in (int, float) or not math.isfinite(expires) or expires <= time.time()):
            raise ValueError('Expired custom time selection')
        zone = get_zone(self.telegram.settings.timezone)
        if session.get('date_only') is True:
            finish = from_epoch_ms(end, zone).date() - timedelta(days=1)
            label = f'{from_epoch_ms(start, zone):%d/%m/%y} → {finish:%d/%m/%y}'
        else:
            label = f'{from_epoch_ms(start, zone):%d/%m/%Y %H:%M} → {from_epoch_ms(end, zone):%d/%m/%Y %H:%M}'
        return start, end, label

    def _window(self, kind, anchor):
        if kind not in self._LABELS or type(anchor) is not int or anchor <= 0 or anchor > int(time.time()) + 300:
            raise ValueError('Invalid time-menu window')
        zone = get_zone(self.telegram.settings.timezone)
        selected = from_epoch_ms(anchor * 1000, zone)
        if kind == 'h':
            start_ms, end_ms = (anchor - 6 * 3600) * 1000, anchor * 1000
            if start_ms < 0:
                raise ValueError('Invalid time-menu window')
            start = from_epoch_ms(start_ms, zone)
            end = selected
        else:
            # Construct local calendar midnights rather than assume all days
            # have 86,400 elapsed seconds in zones with daylight saving time.
            day = selected.date() - (timedelta(days=1) if kind == 'y' else timedelta())
            span = 1
            if kind in ('s', 'p'):
                day = selected.date() - timedelta(days=selected.weekday() + (7 if kind == 'p' else 0))
                span = 7
            start = datetime.combine(day, datetime.min.time(), zone)
            end = datetime.combine(day + timedelta(days=span), datetime.min.time(), zone)
            start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
            if start_ms < 0:
                raise ValueError('Invalid time-menu window')
        return start_ms, end_ms, self._LABELS[kind]

    @staticmethod
    def _valid_page(result, page, key):
        if page and not result[key]:
            raise ValueError('Invalid time-menu page')

    def menu(self, archive, data, actor=None):
        """Return (text, keyboard) for a shortcut or its compact callback."""
        if not isinstance(data, str) or len(data.encode('utf-8')) > 64:
            raise ValueError('Invalid time-menu selection')
        if data == 'custom-time':
            return self.begin(archive, actor)
        custom_camera = self._CUSTOM_CAMERAS.fullmatch(data)
        if custom_camera:
            token, order, page = custom_camera.groups()
            return self._cameras(archive, 'q', token, order, int(page),
                                 custom=self._custom_window(archive, actor, token))
        custom_clip = self._CUSTOM_CLIPS.fullmatch(data)
        if custom_clip:
            token, camera, order, page, camera_page = custom_clip.groups()
            return self._clips(archive, 'q', token, camera, order, int(page), int(camera_page),
                               custom=self._custom_window(archive, actor, token))
        if data in self._KINDS:
            kind, anchor, order, page = self._KINDS[data], int(time.time()), 'a', 0
            return self._cameras(archive, kind, anchor, order, page)
        camera_selection = self._CAMERAS.fullmatch(data)
        if camera_selection:
            kind, anchor, order, page = camera_selection.groups()
            return self._cameras(archive, kind, int(anchor), order, int(page))
        clip_selection = self._CLIPS.fullmatch(data)
        if clip_selection:
            kind, anchor, token, order, page, camera_page = clip_selection.groups()
            return self._clips(archive, kind, int(anchor), token, order, int(page), int(camera_page))
        raise ValueError('Invalid time-menu selection')

    def _cameras(self, archive, kind, anchor, order, page, custom=None):
        start_ms, end_ms, summary = custom if custom is not None else self._window(kind, anchor)
        result = archive.window_cameras(start_ms, end_ms, offset=page * self.PAGE_SIZE, limit=self.PAGE_SIZE)
        self._valid_page(result, page, 'cameras')
        buttons = []
        for camera in result['cameras']:
            token = self.telegram.camera_token(camera['id'])
            buttons.append([self._button(
                f"{camera['name']} ({camera['count']} video)",
                self._clip_callback(kind, anchor, token, order, 0, page), green=True)])
        nav = []
        if page:
            nav.append(self._button('← Camera', self._camera_callback(kind, anchor, order, page - 1)))
        if (page + 1) * self.PAGE_SIZE < result['total']:
            nav.append(self._button('Camera →', self._camera_callback(kind, anchor, order, page + 1)))
        if nav:
            buttons.append(nav)
        if result['cameras']:
            total = archive.list_window(start_ms, end_ms, limit=1)['total']
            buttons.append([self._button(f'⬇ Tải toàn bộ ({total})',
                                         self._download_callback(kind, anchor, 'all', order))])
        buttons.append([self._button('↩ Quay lại', 'home')])
        title = f'{summary} · Camera · trang {page + 1}'
        if not result['cameras']:
            title += '\nChưa có video.'
        return title, buttons

    def _clips(self, archive, kind, anchor, token, order, page, camera_page, custom=None):
        start_ms, end_ms, summary = custom if custom is not None else self._window(kind, anchor)
        camera = self.telegram._camera(archive, token)
        result = archive.list_window(start_ms, end_ms, camera=camera['id'],
                                     order='asc' if order == 'a' else 'desc',
                                     offset=page * self.PAGE_SIZE, limit=self.PAGE_SIZE)
        self._valid_page(result, page, 'recordings')
        zone = get_zone(self.telegram.settings.timezone)
        buttons = []
        lines = []
        for index, recording in enumerate(result['recordings'], page * self.PAGE_SIZE + 1):
            key = recording['key']
            if not isinstance(key, str) or not re.fullmatch(r'[a-f0-9]{64}', key):
                raise ValueError('Invalid recording identity')
            start = from_epoch_ms(recording['start_ms'], zone)
            end = from_epoch_ms(recording['end_ms'], zone)
            lines.append(f'{index}. {start:%d/%m %H:%M:%S} → {end:%d/%m %H:%M:%S}')
            prefix = key[:32]
            buttons.append([
                self._button(f'{index}. ▶ Xem', 'v:' + prefix),
                self._button(f'{index}. ⬇ Tải', 'f:' + prefix),
                self._button(f'{index}. 🗑 Xóa', 'x:' + prefix),
            ])
        nav = []
        if page:
            nav.append(self._button('← Video', self._clip_callback(kind, anchor, token, order, page - 1, camera_page)))
        if (page + 1) * self.PAGE_SIZE < result['total']:
            nav.append(self._button('Video →', self._clip_callback(kind, anchor, token, order, page + 1, camera_page)))
        if nav:
            buttons.append(nav)
        if result['total']:
            buttons.append([self._button(f"⬇ Tải toàn bộ ({result['total']})",
                                         self._download_callback(kind, anchor, token, order))])
        buttons.append([
            self._button('Mới → cũ' if order == 'a' else 'Cũ → mới',
                         self._clip_callback(kind, anchor, token, 'd' if order == 'a' else 'a', 0, camera_page)),
            self._button('↩ Quay lại', self._camera_callback(kind, anchor, order, camera_page)),
        ])
        if not lines:
            lines.append('Chưa có video.')
        title = f"{camera['name']} · {summary} · {result['total']} video · trang {page + 1}"
        return title + '\n' + '\n'.join(lines), buttons
