import json
import hashlib
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from .core import get_zone


class ApiRejected(Exception):
    def __init__(self, code, retry_after=0):
        self.code, self.retry_after = code, retry_after
        super().__init__('Telegram API rejected request')


class Telegram:
    def __init__(self, settings):
        self.settings = settings

    def request(self, method, fields, *, file_path=None, file_field=None):
        if not self.settings.token:
            raise ValueError('Bot token is not configured')
        url = self.settings.api_base + '/bot' + self.settings.token + '/' + method
        headers = {'Content-Type':'application/json'}
        if file_path is None or self.settings.api_mode == 'local':
            if file_path is not None:
                fields = dict(fields, **{file_field:Path(file_path).resolve().as_uri()})
            body = json.dumps(fields).encode()
        else:
            boundary = 'ezviz-archive-stdlib-boundary'
            chunks = []
            for name, value in fields.items():
                value = value if isinstance(value,str) else json.dumps(value)
                chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode())
            chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="clip.mp4"\r\nContent-Type: video/mp4\r\n\r\n').encode())
            head = b''.join(chunks)
            tail = f'\r\n--{boundary}--\r\n'.encode()
            def stream():
                yield head
                with open(file_path, 'rb') as handle:
                    for data in iter(lambda:handle.read(1024*1024), b''):
                        yield data
                yield tail
            body = stream()
            headers = {'Content-Type':f'multipart/form-data; boundary={boundary}',
                       'Content-Length':str(len(head)+Path(file_path).stat().st_size+len(tail))}
        try:
            with urllib.request.urlopen(urllib.request.Request(url,data=body,headers=headers),timeout=1800 if file_path else 35) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                result = json.loads(exc.read())
            except Exception:
                raise ApiRejected(exc.code) from None
        if not result.get('ok'):
            raise ApiRejected(result.get('error_code',0),result.get('parameters',{}).get('retry_after',0))
        return result['result']

    def upload_one(self, archive):
        if not self.settings.enable_upload:
            return None
        row = archive.claim_upload()
        if row is None:
            return None
        path = Path(row['local_path'])
        if not path.is_file() or path.stat().st_size > self.settings.max_bytes:
            with archive.conn:
                archive.conn.execute("UPDATE recordings SET status='needs_review',last_error='missing_or_oversize_file' WHERE key=?", (row['key'],))
            return 'needs_review'
        start = datetime.fromtimestamp(row['start_ms']/1000,get_zone(self.settings.timezone))
        end = datetime.fromtimestamp(row['end_ms']/1000,get_zone(self.settings.timezone))
        caption = f"{archive.camera_name(row['camera'])} | {start.isoformat()} → {end.isoformat()}\nArchive: {row['key']}"
        video = row['codec_video']=='h264' and row['codec_audio'] in (None,'aac')
        method, field = ('sendVideo','video') if video else ('sendDocument','document')
        fields = {'chat_id':self.settings.chat_id,'caption':caption,'disable_notification':True}
        if video:
            fields['supports_streaming']=True
        try:
            message = self.request(method,fields,file_path=path,file_field=field)
            archive.mark_uploaded(row['key'],message['chat']['id'],message['message_id'],message[field]['file_id'])
        except ApiRejected as exc:
            with archive.conn:
                if exc.code == 429:
                    archive.conn.execute("UPDATE recordings SET status='downloaded',retry_at=?,last_error='rate_limited' WHERE key=?",
                                         (time.time()+max(1,exc.retry_after),row['key']))
                elif 400 <= exc.code < 500:
                    archive.conn.execute("UPDATE recordings SET status='needs_review',last_error='api_rejected' WHERE key=?", (row['key'],))
                else:
                    archive.conn.execute("UPDATE recordings SET status='upload_unknown',last_error='server_error_after_send' WHERE key=?", (row['key'],))
            return 'api_rejected'
        except Exception:
            with archive.conn:
                archive.conn.execute("UPDATE recordings SET status='upload_unknown',last_error='ambiguous_post_or_commit' WHERE key=?", (row['key'],))
            return 'upload_unknown'
        archive.cleanup(row['key'])
        return 'uploaded'

    def menu(self, archive, data='root'):
        # Camera IDs stay immutable; friendly names never enter callback_data.
        # Keep callback payloads compact even for a 64-character camera ID.
        if data == 'root' or data.startswith('r:'):
            page = 0 if data == 'root' else int(data.split(':')[1])
            cameras = sorted(archive.cameras(), key=lambda c: (c['name'].casefold(), c['id']))
            if page < 0 or page > max(0, (len(cameras)-1)//10):
                raise ValueError('Invalid camera page')
            buttons = [[{'text':c['name'], 'callback_data':f"c:{self.camera_token(c['id'])}:asc"}]
                       for c in cameras[page*10:page*10+10]]
            nav = []
            if page:
                nav.append({'text':'←', 'callback_data':f'r:{page-1}'})
            if (page+1)*10 < len(cameras):
                nav.append({'text':'→', 'callback_data':f'r:{page+1}'})
            if nav:
                buttons.append(nav)
            return 'Archive — chọn Camera' + ('' if cameras else ' (chưa có camera)'), buttons
        pieces = data.split(':')
        kind = pieces[0]
        expected = {'c':3, 'y':4, 'm':4, 'd':4, 'p':5}
        if kind not in expected or len(pieces) != expected[kind]:
            return self._legacy_menu(archive, data)
        camera = self._camera(archive, pieces[1])
        order = pieces[-2] if kind == 'p' else pieces[-1]
        if order not in ('asc', 'desc'):
            raise ValueError('Invalid archive sort order')
        token, name = pieces[1], camera['name']
        calendar = archive.calendar(camera['id'])
        years = calendar['years']
        backwards = order == 'desc'
        if kind == 'c':
            buttons = [[{'text':str(y['year']), 'callback_data':f"y:{token}:{y['year']}:{order}"}]
                       for y in sorted(years, key=lambda y:y['year'], reverse=backwards)]
            self._controls(buttons, f'c:{token}:{{order}}', 'root', order)
            return f'{name} — chọn Năm', buttons
        if kind == 'y':
            year = int(pieces[2])
            if not 1 <= year <= 9999:
                raise ValueError('Invalid archive year')
            selected = next((y for y in years if y['year'] == year), {'months':[]})
            buttons = [[{'text':f"{m['month']:02}", 'callback_data':f"m:{token}:{year:04}-{m['month']:02}:{order}"}]
                       for m in sorted(selected['months'], key=lambda m:m['month'], reverse=backwards)]
            self._controls(buttons, f'y:{token}:{year}:{{order}}', f'c:{token}:{order}', order)
            return f'{name} | {year} — chọn Tháng', buttons
        if kind == 'm':
            month_text = pieces[2]
            if len(month_text) != 7:
                raise ValueError('Invalid archive month')
            month_start = datetime.fromisoformat(month_text+'-01')
            selected_year = next((y for y in years if y['year'] == month_start.year), {'months':[]})
            selected_month = next((m for m in selected_year['months'] if m['month'] == month_start.month), {'days':[]})
            buttons = [[{'text':f'{day:02}', 'callback_data':f'd:{token}:{month_text}-{day:02}:{order}'}]
                       for day in sorted(selected_month['days'], reverse=backwards)]
            self._controls(buttons, f'm:{token}:{month_text}:{{order}}', f'y:{token}:{month_start.year}:{order}', order)
            return f'{name} | {month_text} — chọn Ngày', buttons
        day, page = pieces[2], (0 if kind == 'd' else int(pieces[4]))
        rows = archive.list_day(day, camera=camera['id'], order=order)
        if page < 0 or page > max(0, (len(rows)-1)//10):
            raise ValueError('Invalid archive page')
        zone = get_zone(self.settings.timezone)
        buttons = []
        for row in rows[page*10:page*10+10]:
            chat_id = int(row['chat_id'])
            if chat_id > -1000000000000:
                continue
            stamp = datetime.fromtimestamp(row['start_ms']/1000, zone).strftime('%H:%M:%S')
            buttons.append([{'text':stamp+' ▶', 'url':f"https://t.me/c/{-chat_id-1000000000000}/{row['message_id']}"}])
        nav = []
        if page:
            nav.append({'text':'←', 'callback_data':f'p:{token}:{day}:{order}:{page-1}'})
        if (page+1)*10 < len(rows):
            nav.append({'text':'→', 'callback_data':f'p:{token}:{day}:{order}:{page+1}'})
        if nav:
            buttons.append(nav)
        self._controls(buttons, f'p:{token}:{day}:{{order}}:0', f'm:{token}:{day[:7]}:{order}', order)
        return f'{name} | {day} | trang {page+1} | '+('Cũ → mới' if order == 'asc' else 'Mới → cũ'), buttons

    @staticmethod
    def camera_token(camera_id):
        return hashlib.sha256(camera_id.encode()).hexdigest()[:12]

    def _camera(self, archive, token):
        matches = [c for c in archive.cameras() if self.camera_token(c['id']) == token]
        if len(matches) != 1:
            raise ValueError('Invalid camera selection')
        return matches[0]

    @staticmethod
    def _controls(buttons, sort_callback, back_callback, order):
        buttons.append([
            {'text':'Đổi: Mới → cũ' if order == 'asc' else 'Đổi: Cũ → mới',
             'callback_data':sort_callback.format(order='desc' if order == 'asc' else 'asc')},
            {'text':'↩ Quay lại', 'callback_data':back_callback},
        ])

    def _legacy_menu(self, archive, data):
        # Existing messages/bookmarks retain their previous calendar callbacks.
        zone = get_zone(self.settings.timezone)
        pieces = data.split(':')
        if pieces[0]=='y' and len(pieces)==2:
            year = int(pieces[1])
            return str(year)+' — chọn tháng', [[{'text':f'{month:02}','callback_data':f'm:{year}-{month:02}'}] for month in range(1,13)]
        if pieces[0]=='m' and len(pieces)==2:
            import calendar
            year,month = map(int,pieces[1].split('-'))
            days = calendar.monthrange(year,month)[1]
            return pieces[1]+' — chọn ngày', [[{'text':f'{day:02}','callback_data':f'd:{year}-{month:02}-{day:02}'}] for day in range(1,days+1)]
        if pieces[0]=='d' and len(pieces)==2:
            rows = archive.list_day(pieces[1])
            cameras = sorted({r['camera'] for r in rows})
            # Stable compact slug digest avoids callbacks changing after a new camera arrives.
            return pieces[1]+' — chọn camera', [[{'text':archive.camera_name(camera),'callback_data':f'p:{pieces[1]}:{hashlib.sha256(camera.encode()).hexdigest()[:12]}:0'}] for camera in cameras[:100]]
        if pieces[0]=='p' and len(pieces)==4:
            day, camera_token, page = pieces[1],pieces[2],int(pieces[3])
            rows = archive.list_day(day)
            cameras = sorted({r['camera'] for r in rows})
            matching=[c for c in cameras if hashlib.sha256(c.encode()).hexdigest()[:12]==camera_token]
            if len(matching)!=1 or page < 0:
                raise ValueError('Invalid archive page')
            camera=matching[0]
            rows = [r for r in rows if r['camera']==camera]
            buttons=[]
            for row in rows[page*10:page*10+10]:
                chat_id=int(row['chat_id'])
                if chat_id > -1000000000000:
                    continue
                cid=-chat_id-1000000000000
                stamp=datetime.fromtimestamp(row['start_ms']/1000,zone).strftime('%H:%M:%S')
                buttons.append([{'text':stamp+' ▶','url':f"https://t.me/c/{cid}/{row['message_id']}"}])
            nav=[]
            if page:
                nav.append({'text':'←','callback_data':f'p:{day}:{camera_token}:{page-1}'})
            if (page+1)*10<len(rows):
                nav.append({'text':'→','callback_data':f'p:{day}:{camera_token}:{page+1}'})
            if nav:
                buttons.append(nav)
            return f'{day} | {archive.camera_name(camera)} | trang {page+1}',buttons
        raise ValueError('Unknown menu selection')

    def poll(self,archive):
        if not self.settings.token or not self.settings.allowed_users:
            return
        offset=int(archive.state('telegram_offset') or 0)
        updates=self.request('getUpdates',{'offset':offset,'timeout':0,'allowed_updates':['message','callback_query']})
        for update in updates:
            callback=update.get('callback_query')
            message=update.get('message') or (callback or {}).get('message',{})
            actor=(callback or message).get('from',{}).get('id')
            if actor not in self.settings.allowed_users or message.get('chat',{}).get('type') != 'private':
                archive.state('telegram_offset',update['update_id']+1)
                continue
            chat_id=message['chat']['id']
            try:
                try:
                    if callback:
                        self.request('answerCallbackQuery',{'callback_query_id':callback['id']})
                        text,buttons=self.menu(archive,callback['data'])
                    else:
                        command=message.get('text','').split()[0].split('@')[0] if message.get('text','').strip() else ''
                        if command in ('/status','/start'):
                            text=json.dumps(archive.status(),ensure_ascii=False)
                            buttons=[[{'text':'Archive','callback_data':'root'}]]
                        else:
                            if command=='/today':
                                data='d:'+datetime.now(get_zone(self.settings.timezone)).strftime('%Y-%m-%d')
                            elif command=='/yesterday':
                                from datetime import timedelta
                                data='d:'+(datetime.now(get_zone(self.settings.timezone))-timedelta(days=1)).strftime('%Y-%m-%d')
                            else:
                                data='root'
                            text,buttons=self.menu(archive,data)
                    self.request('sendMessage',{'chat_id':chat_id,'text':text,'reply_markup':{'inline_keyboard':buttons}})
                except (ValueError,KeyError,IndexError):
                    self.request('sendMessage',{'chat_id':chat_id,'text':'Mục lựa chọn đã thay đổi. Dùng /archive để chọn lại.'})
            except ApiRejected as exc:
                if not (400 <= exc.code < 500) or exc.code==429:
                    raise
                # Expired callback/blocked user must not pin the durable update cursor.
            archive.state('telegram_offset',update['update_id']+1)
