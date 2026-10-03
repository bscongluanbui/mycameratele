"""Stateless time shortcuts for the private Telegram archive.

A callback contains the time when its shortcut was selected, not a moving
relative range. Paging, sorting and returning to cameras therefore retain the
same window even after midnight. Camera names never become callback identity.
"""
from datetime import datetime, timedelta
import re
import time

from .core import get_zone


class TimeMenus:
    PAGE_SIZE = 10
    _KINDS = {'today': 't', 'yesterday': 'y', 'last6h': 'h'}
    _LABELS = {'t': 'Hôm nay', 'y': 'Hôm qua', 'h': '6 giờ trước'}
    _CAMERAS = re.compile(r'w:([tyh]):([1-9][0-9]{0,10}):([ad]):(0|[1-9][0-9]{0,5})')
    _CLIPS = re.compile(r'wc:([tyh]):([1-9][0-9]{0,10}):([a-f0-9]{12}):([ad]):(0|[1-9][0-9]{0,5}):(0|[1-9][0-9]{0,5})')

    def __init__(self, telegram):
        self.telegram = telegram

    @staticmethod
    def shortcuts():
        """Fresh inline-keyboard rows, suitable for /start and archive menus."""
        return [
            [{'text': '📅 Hôm nay', 'callback_data': 'today'},
             {'text': '📆 Hôm qua', 'callback_data': 'yesterday'}],
            [{'text': '🕕 6 giờ trước', 'callback_data': 'last6h'}],
        ]

    @staticmethod
    def commands():
        """Commands for Telegram's persistent Menu button (setMyCommands)."""
        return [
            {'command': 'sync', 'description': 'Start sync tất cả camera đang bật'},
            {'command': 'today', 'description': 'Hôm nay → chọn Camera → video'},
            {'command': 'yesterday', 'description': 'Hôm qua → chọn Camera → video'},
            {'command': 'last6h', 'description': '6 giờ trước → chọn Camera → video'},
            {'command': 'archive', 'description': 'Kho video theo Camera / năm / tháng / ngày'},
            {'command': 'recent', 'description': 'Video gần đây'},
            {'command': 'trash', 'description': 'Video đã xóa và khôi phục'},
            {'command': 'status', 'description': 'Trạng thái hệ thống'},
        ]

    @staticmethod
    def _button(text, callback):
        # Telegram accepts 1–64 UTF-8 bytes, not 64 Unicode characters.
        if not 1 <= len(callback.encode('utf-8')) <= 64:
            raise ValueError('Time-menu callback is too long')
        return {'text': text, 'callback_data': callback}

    @staticmethod
    def _camera_callback(kind, anchor, order, page):
        return f'w:{kind}:{anchor}:{order}:{page}'

    @staticmethod
    def _clip_callback(kind, anchor, token, order, page, camera_page):
        return f'wc:{kind}:{anchor}:{token}:{order}:{page}:{camera_page}'

    def _window(self, kind, anchor):
        if kind not in self._LABELS or type(anchor) is not int or anchor <= 0 or anchor > int(time.time()) + 300:
            raise ValueError('Invalid time-menu window')
        zone = get_zone(self.telegram.settings.timezone)
        selected = datetime.fromtimestamp(anchor, zone)
        if kind == 'h':
            start_ms, end_ms = (anchor - 6 * 3600) * 1000, anchor * 1000
            if start_ms < 0:
                raise ValueError('Invalid time-menu window')
            start = datetime.fromtimestamp(start_ms / 1000, zone)
            end = selected
        else:
            # Construct local calendar midnights rather than assume all days
            # have 86,400 elapsed seconds in zones with daylight saving time.
            day = selected.date() - (timedelta(days=1) if kind == 'y' else timedelta())
            start = datetime.combine(day, datetime.min.time(), zone)
            end = datetime.combine(day + timedelta(days=1), datetime.min.time(), zone)
            start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
            if start_ms < 0:
                raise ValueError('Invalid time-menu window')
        label = self._LABELS[kind]
        summary = (f'{label} | {start:%d/%m/%Y %H:%M} → {end:%d/%m/%Y %H:%M}'
                   f' ({self.telegram.settings.timezone})')
        return start_ms, end_ms, summary

    @staticmethod
    def _valid_page(result, page, key):
        if page and not result[key]:
            raise ValueError('Invalid time-menu page')

    def menu(self, archive, data):
        """Return (text, keyboard) for a shortcut or its compact callback."""
        if not isinstance(data, str) or len(data.encode('utf-8')) > 64:
            raise ValueError('Invalid time-menu selection')
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

    def _cameras(self, archive, kind, anchor, order, page):
        start_ms, end_ms, summary = self._window(kind, anchor)
        result = archive.window_cameras(start_ms, end_ms, offset=page * self.PAGE_SIZE, limit=self.PAGE_SIZE)
        self._valid_page(result, page, 'cameras')
        buttons = []
        for camera in result['cameras']:
            token = self.telegram.camera_token(camera['id'])
            buttons.append([self._button(
                f"{camera['name']} ({camera['count']} video)",
                self._clip_callback(kind, anchor, token, order, 0, page))])
        nav = []
        if page:
            nav.append(self._button('← Camera', self._camera_callback(kind, anchor, order, page - 1)))
        if (page + 1) * self.PAGE_SIZE < result['total']:
            nav.append(self._button('Camera →', self._camera_callback(kind, anchor, order, page + 1)))
        if nav:
            buttons.append(nav)
        buttons.extend(self.shortcuts())
        buttons.append([self._button('↩ Tất cả Camera', 'root')])
        detail = 'Chọn Camera' if result['cameras'] else 'Chưa có video trong khoảng thời gian này.'
        return f'{summary}\n{detail} | trang {page + 1}', buttons

    def _clips(self, archive, kind, anchor, token, order, page, camera_page):
        start_ms, end_ms, summary = self._window(kind, anchor)
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
            start = datetime.fromtimestamp(recording['start_ms'] / 1000, zone)
            end = datetime.fromtimestamp(recording['end_ms'] / 1000, zone)
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
        buttons.append([
            self._button('Đổi: Mới → cũ' if order == 'a' else 'Đổi: Cũ → mới',
                         self._clip_callback(kind, anchor, token, 'd' if order == 'a' else 'a', 0, camera_page)),
            self._button('↩ Camera', self._camera_callback(kind, anchor, order, camera_page)),
        ])
        if not lines:
            lines.append('Chưa có video trong khoảng thời gian này.')
        sorting = 'Cũ → mới' if order == 'a' else 'Mới → cũ'
        title = f"{camera['name']}\n{summary}\n{sorting} | trang {page + 1} | {result['total']} video"
        return title + '\n' + '\n'.join(lines), buttons
