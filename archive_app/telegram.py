import json
import hashlib
import math
import re
import secrets
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from .core import get_zone,from_epoch_ms
from .telegram_menu import TimeMenus
from .upload_spool import check_upload_spool, SpoolBudgetError
from .local_upload import local_mp4_uri, LocalUploadError


class ApiRejected(Exception):
    def __init__(self, code, retry_after=0, description=''):
        self.code, self.retry_after = code, retry_after
        # Keep only a classification, never arbitrary API text in logs/state.
        self.source_missing = code == 400 and isinstance(description, str) and description.lower() in (
            'bad request: message to copy not found', 'bad request: message not found',
            'bad request: message_id_invalid')
        normalized=description.lower() if isinstance(description,str) else ''
        self.index_message_missing=code==400 and normalized in (
            'bad request: message to edit not found','bad request: message not found','bad request: message_id_invalid')
        self.not_modified=code==400 and normalized.startswith('bad request: message is not modified')
        self.menu_uneditable=code==400 and normalized in (
            'bad request: message to edit not found', "bad request: message can't be edited",
            'bad request: message_id_invalid')
        super().__init__('Telegram API rejected request')


class Telegram:
    def __init__(self, settings):
        self.settings = settings
        self._menu_retry_at = 0
        self._storage_verified = None
        self._channel_verified = {}

    def register_commands(self, archive):
        """Configure the Telegram Menu once per token/schema, with bounded retries."""
        if not self.settings.token:
            return False
        schema=json.dumps(TimeMenus.commands(),ensure_ascii=False,sort_keys=True,separators=(',',':'))
        state_key='telegram_commands_v5:'+hashlib.sha256((self.settings.token+'\0'+schema).encode()).hexdigest()[:16]
        if archive.state(state_key)=='1':return True
        if time.time()<self._menu_retry_at:return False
        self._menu_retry_at=time.time()+60
        if self.request('setMyCommands',{'commands':TimeMenus.commands(),'scope':{'type':'all_private_chats'}}) is not True:
            raise ValueError('Telegram command menu was not confirmed')
        if self.request('setChatMenuButton',{'menu_button':{'type':'commands'}}) is not True:
            raise ValueError('Telegram menu button was not confirmed')
        archive.state(state_key,'1')
        return True

    @staticmethod
    def reply_keyboard():
        return {'keyboard':[[{'text':'🏠 Start'},{'text':'▶ Start sync'}],
                            [{'text':'📅 Hôm nay'},{'text':'📆 Hôm qua'},{'text':'🕕 6 giờ trước'}],
                            [{'text':'📅 Tuần này'},{'text':'📆 Tuần trước'}],
                            [{'text':'🗓 Tùy chọn thời gian'}],
                            [{'text':'📷 Camera'},{'text':'🕐 Video gần đây'},{'text':'🗑 Thùng rác'}],
                            [{'text':'⚙ Trạng thái'}]],'resize_keyboard':True,'is_persistent':True}

    def _keyboard_state_key(self,actor):
        return f'telegram_keyboard_removed_v1:{actor}:'+hashlib.sha256(self.settings.token.encode()).hexdigest()[:12]

    def ensure_keyboard(self, archive, actor, *, force=False):
        """Retire the old persistent mother keyboard once per viewer/token."""
        archive._archive_actor(actor)
        name=self._keyboard_state_key(actor)
        if archive.state(name) != '1':
            sent=self.request('sendMessage',{'chat_id':actor,'text':'🏠 Menu',
                                            'reply_markup':{'remove_keyboard':True},'disable_notification':True})
            if not (isinstance(sent,dict) and type(sent.get('message_id')) is int and sent['message_id']>0):
                raise RuntimeError('Keyboard removal was not confirmed')
            archive.state(name,'1')

    def _menu_state_key(self,actor):
        return f'telegram_active_menu_v1:{actor}:'+hashlib.sha256(self.settings.token.encode()).hexdigest()[:12]

    def _active_menu(self,archive,actor):
        raw=archive.state(self._menu_state_key(actor))
        return int(raw) if isinstance(raw,str) and len(raw)<=10 and raw.isdecimal() and 0<int(raw)<=2147483647 else None

    def _route_state_key(self,actor):
        return self._menu_state_key(actor)+':route'

    def _menu_route(self,archive,actor):
        route=archive.state(self._route_state_key(actor))
        return route if isinstance(route,str) and 1<=len(route.encode())<=64 else 'home'

    def navigation_buttons(self,archive,actor,route,buttons):
        """Return from action panels to their list, not the camera root."""
        actions=('x:','xc:','u:','bw:','bwq:','bd:','bulk-','sync:','up:','ss:')
        if isinstance(route,str) and route.startswith(actions):
            buttons=[[dict(button) for button in line] for line in buttons]
            for line in buttons:
                for button in line:
                    if (button.get('callback_data') in ('home','root') or
                            'Quay lại' in button.get('text','') or button.get('text')=='↩ Camera'):
                        button.update(text='↩ Quay lại',callback_data='nav-return')
            if not any(b.get('callback_data')=='nav-return' for line in buttons for b in line):
                buttons.append([{'text':'↩ Quay lại','callback_data':'nav-return'}])
        return buttons

    def remember_menu_route(self,archive,actor,route):
        actions=('x:','xc:','u:','bw:','bwq:','bd:','bulk-','sync:','up:','ss:')
        if (isinstance(route,str) and 1<=len(route.encode())<=64 and
                not route.startswith(actions)):
            archive.state(self._route_state_key(actor),route)

    def present_menu(self,archive,chat_id,actor,text,buttons,*,message_id=None):
        """Replace a screen in-place; retire only the confirmed prior keyboard."""
        try:archive._archive_actor(actor)
        except (PermissionError,ValueError,TypeError):raise ValueError('Viewer is not authorized') from None
        if type(chat_id) is not int or chat_id!=actor:raise ValueError('Invalid private menu destination')
        prior=self._active_menu(archive,actor)
        target=message_id if type(message_id) is int and 0<message_id<=2147483647 else None
        fields={'chat_id':chat_id,'text':text,'reply_markup':{'inline_keyboard':buttons}}
        if target is not None:
            try:
                edited=self.request('editMessageText',{**fields,'message_id':target})
                if not (edited is True or isinstance(edited,dict) and
                        type(edited.get('message_id')) is int and edited['message_id']==target):
                    raise RuntimeError('Menu edit was not confirmed')
            except ApiRejected as exc:
                if exc.not_modified:pass
                elif exc.menu_uneditable:target=None
                else:raise
        if target is None:
            sent=self.request('sendMessage',fields)
            target=sent.get('message_id') if isinstance(sent,dict) else None
            if type(target) is not int or not 0<target<=2147483647:
                raise RuntimeError('Menu delivery was not confirmed')
        archive.state(self._menu_state_key(actor),str(target))
        if prior and prior!=target:
            try:self.request('editMessageReplyMarkup',{'chat_id':chat_id,'message_id':prior,
                                                     'reply_markup':{'inline_keyboard':[]}})
            except Exception:pass # Retiring an old keyboard must not resend the current screen.
        return target

    @staticmethod
    def recording_buttons(row, label):
        prefix=row['key'][:32]
        return [{'text':label,'callback_data':'v:'+prefix},
                {'text':'⬇ Tải','callback_data':'f:'+prefix},
                {'text':'🗑 Xóa','callback_data':'x:'+prefix}]

    def native_video_link(self, archive, row, actor):
        """Open the original channel post; the Telegram client fetches media."""
        try:archive._archive_actor(actor)
        except (PermissionError,ValueError,TypeError):raise ValueError('Viewer is not authorized') from None
        multi=(bool(getattr(self.settings,'multi_channel_routing',False))
               or archive.state('multi_channel_history')=='1')
        channel=row.get('storage_chat_id') if multi else self.settings.storage_channel_id
        message=row.get('storage_message_id')
        prefix,separator,_=self.settings.token.partition(':')
        bot=prefix if separator and prefix.isdecimal() else archive.state('telegram_bot_id')
        if (not self.channel_mode or type(channel) is not int or
                not re.fullmatch(r'-100[1-9][0-9]*',str(channel)) or
                row.get('status')!='uploaded' or row.get('deleted_at') is not None or
                row.get('storage_kind')!='channel' or
                type(row.get('storage_chat_id')) is not int or row['storage_chat_id']!=channel or
                type(message) is not int or message<=0 or
                not isinstance(bot,str) or not bot.isdecimal() or int(bot)<=0 or
                type(row.get('bot_id')) is not int or row['bot_id']!=int(bot) or
                row.get('media_type') not in ('video','document')):
            raise ValueError('Unknown channel video selection')
        # Telegram iOS discards a zero timecode; a positive timestamp opens
        # the native media viewer. Other clients may only focus the post.
        query='?single&t=1' if row['media_type']=='video' else '?single'
        return f'https://t.me/c/{str(channel)[4:]}/{message}'+query

    def player_buttons(self, archive, buttons, actor):
        """Channel views stay in Telegram; private-mode web play is optional."""
        if not self.channel_mode and not self.settings.player_public_url:
            return buttons
        result=[]
        for line in buttons:
            new_line=[]
            for button in line:
                callback=button.get('callback_data','')
                if callback.startswith('v:'):
                    row=archive.find_recording(callback[2:])
                    if not row:raise ValueError('Unknown player selection')
                    if self.channel_mode:
                        url=self.native_video_link(archive,row,actor)
                    else:
                        from .telegram_player import TelegramPlayer
                        url=TelegramPlayer(self.settings).issue(archive,row,actor)
                    new_line.append({'text':button['text'],'url':url})
                else:new_line.append(dict(button))
            result.append(new_line)
        return result

    @property
    def owner(self):
        owner = self.settings.effective_owner
        return owner if isinstance(owner, int) and not isinstance(owner, bool) and owner > 0 else 0

    @property
    def viewers(self):
        return {value for value in (*self.settings.allowed_users,self.owner)
                if type(value) is int and value > 0}

    @staticmethod
    def play_callback(row):
        return 'v:'+row['key'][:32]

    def caption(self, archive, row):
        zone = get_zone(self.settings.timezone)
        start = from_epoch_ms(row['start_ms'],zone)
        end = from_epoch_ms(row['end_ms'],zone)
        if self.settings.multi_channel_routing:
            seconds=max(1,(row['end_ms']-row['start_ms'])//1000)
            duration=f'{seconds//60:02d}:{seconds%60:02d}'
            end_label=end.strftime('%H:%M:%S') if start.date()==end.date() else end.strftime('%d/%m/%Y %H:%M:%S')
            camera=row['camera'];tag=re.sub(r'[^A-Za-z0-9_]', '_', camera)
            size=(row.get('file_size') or 0)/1e6
            return (f'🎥 {camera} | {archive.camera_name(camera)}\n'
                    f'📅 {start:%d/%m/%Y}\n🕒 {start:%H:%M:%S} → {end_label}\n'
                    f'⏱ {duration} | 📦 {size:.1f} MB\n'
                    f'#{tag} #Y{start:%Y} #M{start:%Y%m} #D{start:%Y%m%d}')
        return f"{archive.camera_name(row['camera'])} | {start.isoformat()} → {end.isoformat()}\nArchive: {row['key']}"

    @property
    def channel_mode(self):
        return (getattr(self.settings, 'telegram_destination', 'owner_private') == 'channel'
                or self.settings.multi_channel_routing
                or getattr(self.settings, 'storage_channel_id', 0) != 0)

    def verify_storage(self, archive):
        """Validate the exact tenant channel before claiming any upload."""
        channel = getattr(self.settings, 'storage_channel_id', 0)
        if type(channel) is not int or channel >= 0:
            raise ValueError('A negative storage channel ID is required')
        identity = (getattr(self.settings, 'tenant_id', 'house01'), channel,
                    hashlib.sha256(self.settings.token.encode()).hexdigest())
        if self._storage_verified and self._storage_verified[0] == identity and self._storage_verified[1] > time.time():
            return channel, self._storage_verified[2]
        me = self.request('getMe', {})
        if not isinstance(me, dict) or type(me.get('id')) is not int or me['id'] <= 0 or me.get('is_bot') is not True:
            raise ValueError('Bot identity was not confirmed')
        chat = self.request('getChat', {'chat_id':channel})
        if (not isinstance(chat, dict) or type(chat.get('id')) is not int
                or chat['id'] != channel or chat.get('type') != 'channel'
                or chat.get('username') or chat.get('active_usernames')):
            raise ValueError('Storage channel identity was not confirmed')
        membership = self.request('getChatMember', {'chat_id':channel, 'user_id':me['id']})
        member_user = membership.get('user', {}) if isinstance(membership, dict) else {}
        if (not isinstance(membership, dict) or not isinstance(member_user, dict)
                or type(member_user.get('id')) is not int or member_user['id'] != me['id']
                or member_user.get('is_bot') is not True
                or membership.get('status') != 'administrator' or membership.get('can_post_messages') is not True):
            raise ValueError('Bot channel posting rights were not confirmed')
        archive.state('telegram_bot_id', me['id'])
        self._storage_verified = (identity, time.time()+60, me['id'])
        return channel, me['id']

    def verify_channel(self, archive, channel, *, require_index=True):
        """Validate a candidate private channel without binding a camera."""
        if type(channel) is not int or not re.fullmatch(r'-100[1-9][0-9]*',str(channel)):
            raise ValueError('A private channel ID (-100...) is required')
        identity=(self.settings.tenant_id,channel,hashlib.sha256(self.settings.token.encode()).hexdigest(),require_index)
        cached=self._channel_verified.get(identity)
        if cached and cached[0]>time.time():return channel,cached[1]
        me=self.request('getMe',{})
        if not isinstance(me,dict) or type(me.get('id')) is not int or me['id']<=0 or me.get('is_bot') is not True:
            raise ValueError('Bot identity was not confirmed')
        chat=self.request('getChat',{'chat_id':channel})
        if (not isinstance(chat,dict) or type(chat.get('id')) is not int or chat['id']!=channel or chat.get('type')!='channel'
                or chat.get('username') or chat.get('active_usernames')):
            raise ValueError('Private channel identity was not confirmed')
        member=self.request('getChatMember',{'chat_id':channel,'user_id':me['id']})
        user=member.get('user',{}) if isinstance(member,dict) else {}
        if (not isinstance(member,dict) or not isinstance(user,dict) or user.get('is_bot') is not True
                or type(user.get('id')) is not int or user['id']!=me['id'] or member.get('status')!='administrator'
                or member.get('can_post_messages') is not True or (require_index and member.get('can_edit_messages') is not True)):
            raise ValueError('Channel post/edit/pin rights were not confirmed')
        archive.state('telegram_bot_id',me['id'])
        from .channel_directory import ChannelDirectory
        ChannelDirectory(archive).remember(chat,status='ready')
        # Bound cache to this bot, tenant, permissions and ID; names are not routing keys.
        if len(self._channel_verified)>100:self._channel_verified.clear()
        self._channel_verified[identity]=(time.time()+60,me['id'])
        return channel,me['id']

    def verify_camera_channel(self, archive, camera, *, require_index=True):
        channel=None
        previous=archive.conn.execute('SELECT channel_status FROM cameras WHERE id=?',(camera,)).fetchone()
        try:
            channel=archive.resolve_camera_channel(camera)
            result=self.verify_channel(archive,channel,require_index=require_index)
        except Exception as error:
            code='channel_rate_limited' if isinstance(error,ApiRejected) and error.code==429 else 'channel_check_failed'
            archive.set_channel_status(camera,'error',code,expected_channel=channel)
            raise
        if not archive.set_channel_status(camera,'ready',expected_channel=channel):
            raise ValueError('Camera channel changed during verification')
        if previous and previous[0]!='ready' and archive.channel_index:
            archive.channel_index.enqueue_camera(camera)
        return result

    def _upload_backoff_key(self):
        token_prefix, separator, _ = self.settings.token.partition(':')
        bot = ('id:'+(token_prefix.lstrip('0') or '0') if separator and token_prefix.isdecimal()
               else 'token:'+hashlib.sha256(self.settings.token.encode()).hexdigest())
        destination = 'camera_channels' if self.settings.multi_channel_routing else (getattr(self.settings, 'storage_channel_id', 0) if self.channel_mode else self.owner)
        scope = [getattr(self.settings, 'tenant_id', 'house01'), bot,
                 'channel' if self.channel_mode else 'owner_private', destination]
        return 'telegram_upload_retry_until:'+hashlib.sha256(json.dumps(scope,separators=(',',':')).encode()).hexdigest()

    def upload_retry_until(self, archive):
        """Persistent upload-only pause, shared by all cameras and workers."""
        try:
            value = float(archive.state(self._upload_backoff_key()) or 0)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        return value if math.isfinite(value) and value > 0 else 0.0

    def _pause_uploads(self, archive, retry_after, record_key=None):
        delay = 1.0
        try:
            if type(retry_after) in (int,float):
                value=float(retry_after)
                if math.isfinite(value):delay=max(1.0,value)
        except (ValueError,OverflowError):
            pass
        key = self._upload_backoff_key()
        archive.conn.execute('BEGIN IMMEDIATE')
        try:
            until = max(self.upload_retry_until(archive), time.time()+delay)
            archive.conn.execute('INSERT INTO state(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value',
                                 (key,str(until)))
            if record_key is not None:
                archive.conn.execute("UPDATE recordings SET status='downloaded',retry_at=?,last_error='rate_limited' WHERE key=? AND status='uploading'",
                                     (until,record_key))
            archive.conn.commit()
            return until
        except Exception:
            archive.conn.rollback()
            raise

    def validate_media_message(self, message, field, chat_id=None, *, chat_type='private'):
        expected_chat = self.owner if chat_id is None else chat_id
        if not isinstance(message, dict):
            raise ValueError('Invalid Telegram media response')
        chat = message.get('chat', {})
        message_id = message.get('message_id')
        media = message.get(field, {})
        if (chat_type not in ('private', 'channel') or not isinstance(chat, dict) or chat.get('type') != chat_type or
            type(chat.get('id')) is not int or chat['id'] != expected_chat or
            (expected_chat <= 0 if chat_type == 'private' else expected_chat >= 0) or
            type(message_id) is not int or message_id <= 0 or not isinstance(media, dict) or
            not isinstance(media.get('file_id'), str) or not media['file_id'].strip() or
            not isinstance(media.get('file_unique_id'), str) or not media['file_unique_id'].strip() or
            ('document' if field == 'video' else 'video') in message):
            raise ValueError('Unconfirmed owner/private/media identity')
        return media

    def request(self, method, fields, *, file_path=None, file_field=None, local_file=False):
        if not self.settings.token:
            raise ValueError('Bot token is not configured')
        url = self.settings.api_base + '/bot' + self.settings.token + '/' + method
        headers = {'Content-Type':'application/json'}
        if local_file:
            if method != 'sendVideo' or file_field != 'video' or file_path is None:
                raise LocalUploadError('Local upload requires a managed MP4 video')
            # Keep file_path for the original 1800s upload timeout, but do not
            # open/read its bytes or construct a multipart request body.
            body = json.dumps({**fields, 'video': local_mp4_uri(self.settings, file_path)}).encode()
        elif file_path is None:
            body = json.dumps(fields).encode()
        else:
            boundary = 'ezviz-archive-'+secrets.token_hex(16)
            chunks = []
            for name, value in fields.items():
                value = value if isinstance(value,str) else json.dumps(value)
                chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode())
            filename=re.sub(r'[^A-Za-z0-9._-]', '_', Path(file_path).name)[:180] or 'camera-recording.bin'
            mime='application/octet-stream' if file_field=='document' else 'video/mp4'
            chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n').encode())
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
            raise ApiRejected(result.get('error_code',0),result.get('parameters',{}).get('retry_after',0),result.get('description',''))
        return result['result']

    def upload_one(self, archive, camera=None):
        if not self.settings.enable_upload or not self.settings.token:
            return None
        if self.upload_retry_until(archive) > time.time():
            return 'rate_limited'
        destination, bot_id = self.owner, None
        row=None
        if self.settings.multi_channel_routing:
            # A blocked camera must not starve any other camera or claim its file.
            pending=archive.pending_upload_cameras(camera)
            for slug in pending:
                try:
                    destination,bot_id=self.verify_camera_channel(archive,slug,require_index=self.settings.channel_index_enabled)
                except ApiRejected as error:
                    if error.code==429:
                        self._pause_uploads(archive,error.retry_after)
                        return 'rate_limited'
                    continue
                except Exception:continue
                row=archive.claim_upload(slug,channel_chat_id=destination)
                if row is not None:break
            if row is None:return 'storage_blocked' if pending else None
        elif self.channel_mode:
            try:
                destination, bot_id = self.verify_storage(archive)
            except ApiRejected as error:
                if error.code == 429:
                    self._pause_uploads(archive,error.retry_after)
                    return 'rate_limited'
                return 'storage_blocked'
            except Exception:
                return 'storage_blocked'
        elif not self.owner or archive.state(f'telegram_owner_started:{self.owner}') != '1':
            return None
        if not self.settings.multi_channel_routing:
            row = archive.claim_upload() if camera is None else archive.claim_upload(camera)
        if row is None:
            return None
        path = Path(row['local_path'])
        if not path.is_file() or path.stat().st_size > self.settings.max_bytes:
            with archive.conn:
                archive.conn.execute("UPDATE recordings SET status='needs_review',last_error='missing_or_oversize_file' WHERE key=?", (row['key'],))
            return 'needs_review'
        if (getattr(self.settings, 'media_mode', 'raw') == 'remux_copy'
                and (row.get('processing_method') != 'remux_copy' or path.suffix.lower() != '.mp4')):
            with archive.conn:
                archive.conn.execute("UPDATE recordings SET status='needs_review',last_error='mp4_remux_required' WHERE key=?", (row['key'],))
            return 'needs_review'
        video = row.get('processing_method') == 'remux_copy' and path.suffix.lower() == '.mp4'
        direct = self.settings.api_mode == 'local' and self.settings.upload_transport == 'local_file' and video
        if direct:
            try:
                local_mp4_uri(self.settings, path, expected_key=row['key'], cache_root=archive._cache_root)
            except LocalUploadError:
                with archive.conn:
                    archive.conn.execute("UPDATE recordings SET status='needs_review',last_error='local_upload_path' WHERE key=?", (row['key'],))
                return 'needs_review'
        try:
            check_upload_spool(self.settings, path.stat().st_size, local_file=direct)
        except (SpoolBudgetError, OSError):
            # Capacity was rejected before POST: this is a known unsent upload,
            # not an ambiguous Telegram delivery. Retain source and retry later.
            with archive.conn:
                archive.conn.execute("UPDATE recordings SET status='downloaded',retry_at=?,last_error='upload_spool_budget' WHERE key=? AND status='uploading'",
                                     (time.time()+60, row['key']))
            return 'upload_spool_budget'
        caption = self.caption(archive, row)
        # Preserve raw bytes: do not request Telegram's video processing path.
        method,field=('sendVideo','video') if video else ('sendDocument','document')
        fields={'chat_id':destination,'caption':caption,'disable_notification':True}
        if video:fields['supports_streaming']=True
        else:fields['disable_content_type_detection']=True
        try:
            kwargs = {'file_path': path, 'file_field': field}
            if direct:kwargs['local_file'] = True
            message = self.request(method,fields,**kwargs)
            media = self.validate_media_message(message, field, destination,
                                                chat_type='channel' if self.channel_mode else 'private')
            bot_value = archive.state('telegram_bot_id')
            if bot_id is None:bot_id = int(bot_value) if bot_value and bot_value.isdecimal() and int(bot_value)>0 else None
            placement = ({'storage_kind':'channel','storage_chat_id':destination,
                          'storage_message_id':message['message_id']} if self.channel_mode else {})
            archive.mark_uploaded(row['key'],message['chat']['id'],message['message_id'],media['file_id'],
                                  file_unique_id=media['file_unique_id'],media_type=field,bot_id=bot_id,**placement)
        except LocalUploadError:
            # request() rechecks the path before constructing/sending JSON.
            # A changed path at this point is still a definitive unsent error.
            with archive.conn:
                archive.conn.execute("UPDATE recordings SET status='needs_review',last_error='local_upload_path' WHERE key=?", (row['key'],))
            return 'needs_review'
        except ApiRejected as exc:
            if exc.code == 429:
                self._pause_uploads(archive,exc.retry_after,row['key'])
                return 'api_rejected'
            with archive.conn:
                if 400 <= exc.code < 500:
                    archive.conn.execute("UPDATE recordings SET status='needs_review',last_error='api_rejected' WHERE key=?", (row['key'],))
                else:
                    archive.conn.execute("UPDATE recordings SET status='upload_unknown',last_error='server_error_after_send' WHERE key=?", (row['key'],))
            return 'api_rejected'
        except Exception as error:
            # Keep a sanitized type for diagnosis, never the token-bearing URL.
            error_type=re.sub(r'[^A-Za-z0-9_]', '', type(error).__name__)[:48]
            with archive.conn:
                archive.conn.execute("UPDATE recordings SET status='upload_unknown',last_error=? WHERE key=?", ('ambiguous_'+error_type,row['key']))
            return 'upload_unknown'
        archive.cleanup(row['key'])
        return 'uploaded'

    @staticmethod
    def _replay_attempt(archive):
        value=archive.state('telegram_replay_attempt')
        if not value:
            return {}
        try:
            attempt=json.loads(value) if len(value)<=512 else {}
            return attempt if isinstance(attempt,dict) else {}
        except (ValueError,TypeError):
            return {}

    @staticmethod
    def _save_replay_attempt(archive, update_id, phase, *, advance=False, retry_at=0, details=None):
        # Two fixed-size state rows, not one ever-growing row per button click.
        context = details or {}
        if not details:
            previous=Telegram._replay_attempt(archive)
            if previous.get('update_id')==update_id:
                context={key:value for key,value in previous.items() if key not in ('update_id','phase')}
        attempt=json.dumps({**context,'update_id':update_id,'phase':phase}) if phase else '{}'
        with archive.conn:
            for name,value in (('telegram_replay_attempt',attempt),('telegram_replay_retry_at',str(retry_at))):
                archive.conn.execute('INSERT INTO state(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value',
                                     (name,value))
            if advance:
                archive.conn.execute("INSERT INTO state(name,value) VALUES('telegram_offset',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                                     (str(update_id+1),))

    @staticmethod
    def _guarded_attempt(archive,update_id):
        current=Telegram._replay_attempt(archive)
        if current.get('update_id')==update_id and current.get('phase') in ('pending','unknown','done','rejected'):
            return current
        try:
            epoch=json.loads(archive.state('telegram_poll_epoch') or '{}')
            raw=epoch.get('replay_attempt')
            old=json.loads(raw) if isinstance(raw,str) and len(raw)<=4096 else {}
            if isinstance(old,dict) and old.get('update_id')==update_id and old.get('phase') in ('pending','unknown','done','rejected'):
                return old
        except (ValueError,TypeError,AttributeError):
            pass
        return {}

    def replay(self, archive, prefix, chat_id=None, *, update_id=None, purpose='view', consume_rejection=False):
        """Resend Telegram's stored file ID; never read/delete/reupload local bytes."""
        recipient = self.owner if chat_id is None else chat_id
        if type(recipient) is not int or recipient not in self.viewers or not re.fullmatch(r'[a-f0-9]{32}', prefix) or purpose not in ('view','download'):
            raise ValueError('Invalid archive play selection')
        rows = archive.conn.execute("SELECT * FROM recordings WHERE status='uploaded' AND deleted_at IS NULL AND substr(key,1,32)=?",
                                    (prefix,)).fetchall()
        if len(rows) != 1:
            raise ValueError('Unknown or ambiguous archive selection')
        row = dict(rows[0])
        file_id = row.get('file_id')
        if not isinstance(file_id, str) or not file_id.strip():
            raise ValueError('Archive media identity is missing')
        token_prefix,separator,_=self.settings.token.partition(':')
        active_bot = token_prefix if separator and token_prefix.isdecimal() else archive.state('telegram_bot_id')
        if row.get('bot_id') is not None and active_bot and active_bot.isdecimal() and row['bot_id']!=int(active_bot):
            raise ValueError('Archived file belongs to a different bot')
        field = row.get('media_type')
        if field is None:
            # Pre-private rows have no media_type column value. The upload path
            # originally selected its method from these exact codec fields.
            field = 'video' if row['codec_video']=='h264' and row['codec_audio'] in (None,'aac') else 'document'
        if field not in ('video','document'):
            raise ValueError('Unknown archived media type')
        fields = {'chat_id':recipient,field:file_id,'caption':self.caption(archive,row),'disable_notification':True}
        fields['reply_markup']={'inline_keyboard':[self.recording_buttons(row,'▶ Xem lại')]}
        if purpose=='download':
            fields['caption']+='\n⬇ Tải bản gốc: dùng nút tải hoặc menu Telegram → Lưu video / Save to Downloads.'
        if field == 'video':
            fields['supports_streaming'] = True
        storage_chat = row.get('storage_chat_id') or row.get('chat_id')
        storage_message = row.get('storage_message_id') or row.get('message_id')
        # Old private-mode installations may contain historical channel file IDs.
        # Their established file-ID replay remains valid without enabling a new
        # channel placement or automatically uploading anything to owner chat.
        is_channel = self.channel_mode and (row.get('storage_kind') == 'channel' or str(storage_chat).startswith('-'))
        if is_channel:
            if (not re.fullmatch(r'-[1-9][0-9]*',str(storage_chat))
                    or type(storage_message) is not int or storage_message <= 0):
                raise ValueError('Channel placement is invalid')
            # A channel change affects new uploads, not immutable placements in
            # this tenant-bound archive. The same-bot check above still applies.
        method = 'copyMessage' if is_channel else ('sendVideo' if field == 'video' else 'sendDocument')
        details=None
        if is_channel:
            details={'tenant_id':getattr(self.settings,'tenant_id','house01'),'record_key':row['key'],
                     'recipient':recipient,'method':method,'source_chat_id':int(storage_chat),
                     'source_message_id':storage_message}
        if update_id is not None:
            previous=self._guarded_attempt(archive,update_id)
            if previous.get('update_id')==update_id and previous.get('phase') in ('pending','unknown','done','rejected'):
                self._save_replay_attempt(archive,update_id,'unknown' if previous['phase']=='pending' else previous['phase'],advance=True)
                return 'consumed_without_retry'
            self._save_replay_attempt(archive,update_id,'pending',details=details)
        try:
            if is_channel:
                copy_fields={key:value for key,value in fields.items() if key not in (field,'supports_streaming')}
                copy_fields.update(from_chat_id=int(storage_chat),message_id=storage_message)
                try:
                    message=self.request('copyMessage',copy_fields)
                except ApiRejected as error:
                    if not error.source_missing:raise
                    if self.archive_visibility(archive,row['key']) is False:
                        raise ValueError('Recording was removed during retrieval')
                    details['method']='sendVideo' if field == 'video' else 'sendDocument'
                    if update_id is not None:self._save_replay_attempt(archive,update_id,'pending',details=details)
                    message=self.request(details['method'],fields)
                    self.validate_media_message(message,field,recipient)
                else:
                    if not isinstance(message,dict) or set(message)!= {'message_id'} or type(message['message_id']) is not int or message['message_id'] <= 0:
                        raise ValueError('Copy result MessageId was not confirmed')
            else:
                message = self.request(method, fields)
                self.validate_media_message(message,field,recipient)
        except ApiRejected as exc:
            if update_id is None:
                raise
            if exc.code==429:
                deadline=time.time()+self._retry_delay(exc.retry_after)
                if consume_rejection:
                    # Poll consumes a known rejection. Only a new manual click
                    # after the deadline can retry; menus keep receiving updates.
                    self._save_replay_attempt(archive,update_id,'rejected',advance=True,retry_at=deadline)
                    return 'rate_limited'
                # Preserve the direct-call contract for callers outside poll().
                self._save_replay_attempt(archive,update_id,None,retry_at=deadline)
                raise
            phase='rejected' if 400<=exc.code<500 else 'unknown'
            self._save_replay_attempt(archive,update_id,phase,advance=True)
            return phase
        except Exception:
            if update_id is None:
                raise
            # A timeout, 5xx or invalid response can follow a successful POST.
            # Consume this event; a new manual click gets a new update ID.
            self._save_replay_attempt(archive,update_id,'unknown',advance=True)
            return 'unknown'
        if update_id is not None:
            self._save_replay_attempt(archive,update_id,'done',advance=True)
        return 'replayed'

    @staticmethod
    def archive_visibility(archive,key):
        return archive.conn.execute("SELECT 1 FROM recordings WHERE key=? AND status='uploaded' AND deleted_at IS NULL",(key,)).fetchone() is not None

    def start_viewer(self, archive, actor):
        if actor == self.owner:
            archive.state(f'telegram_owner_started:{self.owner}', '1')
        # getMe is informative only: a transient failure must not undo /start.
        try:
            me = self.request('getMe', {})
            username = me.get('username') if isinstance(me,dict) else None
            if isinstance(username,str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}',username):
                archive.state('telegram_bot_username', username)
            if isinstance(me,dict) and type(me.get('id')) is int and me['id']>0:
                archive.state('telegram_bot_id',me['id'])
        except Exception:
            pass

    def channel_setup(self, actor, message=None):
        """Owner-only discovery helper; it never changes destination/config."""
        if type(actor) is not int or actor <= 0 or actor != self.owner:
            raise ValueError('Channel setup requires the owner')
        channel = None
        message = message if isinstance(message, dict) else {}
        origin = message.get('forward_origin')
        if origin is not None:
            chat = origin.get('chat') if isinstance(origin, dict) and origin.get('type') == 'channel' else None
        else:
            chat = message.get('forward_from_chat')
        if (isinstance(chat, dict) and chat.get('type') == 'channel'
                and type(chat.get('id')) is int and chat['id'] < 0):
            channel = chat['id']
        multi=bool(getattr(self.settings,'multi_channel_routing',False))
        if channel is not None and multi:
            text=(f'Channel ID: {channel}\n'
                  'Dashboard → Camera → Chỉnh sửa → Channel ID. '
                  'Mỗi camera dùng một private channel riêng; cùng bot làm admin có quyền đăng, sửa và ghim. '
                  'Bot chỉ hiển thị ID; chưa đổi mapping hoặc bật upload.')
        elif multi:
            text=('Mỗi camera một private channel:\n'
                  '1. Tạo channel Private cho camera.\n'
                  '2. Thêm bot này làm admin có quyền đăng, sửa và ghim.\n'
                  '3. Forward một tin từ channel vào đây để lấy ID.\n'
                  '4. Điền Channel ID cho camera trên dashboard.\n'
                  'Community do bạn gom channel trong Telegram; bot không tự tạo channel.')
        elif channel is not None:
            text=(f'Channel ID: {channel}\n\nĐiền TELEGRAM_STORAGE_CHANNEL_ID={channel} '
                  'và TELEGRAM_DESTINATION=channel trong cấu hình, sau đó khởi động lại worker. '
                  'Bot chỉ hiển thị ID; cấu hình và upload chưa được thay đổi.')
        else:
            text=('Thiết lập kho channel riêng tư:\n1. Tạo một channel Private.\n'
                  '2. Thêm bot làm administrator và bật quyền Post Messages.\n'
                  '3. Gửi một tin nhắn văn bản trong channel rồi Forward tin đó vào chat riêng với bot này.\n'
                  'Bot sẽ hiển thị Channel ID để bạn điền TELEGRAM_STORAGE_CHANNEL_ID. '
                  'Bot không tự chọn channel hoặc tự bật upload.')
        return text, [[{'text':'↩ Camera','callback_data':'root'}]]

    def rebuild_index(self, archive, actor, words):
        """Owner-only explicit queue administration; the default is read-only."""
        if type(actor) is not int or actor != self.owner or actor <= 0:
            raise ValueError('Index administration requires the owner')
        if not getattr(self.settings,'multi_channel_routing',False):
            raise ValueError('Camera channel routing is not enabled')
        if not isinstance(words,list) or not 3<=len(words)<=4:
            raise ValueError('Expected camera and index period')
        camera,period=words[1:3]
        if (not isinstance(camera,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',camera)
                or not any(c['id']==camera for c in archive.cameras())
                or not isinstance(period,str)
                or not re.fullmatch(r'[0-9]{4}(?:-[0-9]{2})?(?:-[0-9]{2})?',period)):
            raise ValueError('Invalid camera or index period')
        # Parse the actual calendar instead of accepting impossible dates.
        datetime.fromisoformat(period+'-01-01' if len(period)==4 else
                               period+'-01' if len(period)==7 else period)
        flag=words[3] if len(words)==4 else '--dry-run'
        if flag not in ('--dry-run','--apply'):
            raise ValueError('Expected --dry-run or --apply')
        from .channel_index import ChannelIndex
        result=ChannelIndex(archive,self).rebuild(camera,period,dry_run=flag!='--apply')
        mode='Xem trước' if result['dry_run'] else 'Đã xếp hàng'
        text=(f"{mode} · {archive.camera_name(camera)} · {period}\n"
              f"Video: {result['recordings']} · Ngày: {len(result['dates'])} · Job: {result['jobs']}")
        return text,[[{'text':'↩ Camera','callback_data':'root'}]]

    def recent_menu(self, archive, page=0):
        if page < 0 or page > 100000:
            raise ValueError('Invalid recent page')
        result = archive.browse(order='desc',status='uploaded',offset=page*10,limit=10)
        if page and not result['recordings']:
            raise ValueError('Invalid recent page')
        zone = get_zone(self.settings.timezone)
        buttons = []
        for row in result['recordings']:
            stamp = from_epoch_ms(row['start_ms'],zone).strftime('%d/%m %H:%M:%S')
            buttons.append(self.recording_buttons(row,archive.camera_name(row['camera'])+' | '+stamp+' ▶'))
        nav = []
        if page:
            nav.append({'text':'←','callback_data':f'recent:{page-1}'})
        if (page+1)*10 < result['total']:
            nav.append({'text':'→','callback_data':f'recent:{page+1}'})
        if nav:
            buttons.append(nav)
        buttons.append([{'text':'↩ Quay lại','callback_data':'home'}])
        return f'Video gần đây | trang {page+1}',buttons

    def menu(self, archive, data='root', actor=None):
        # Camera IDs stay immutable; friendly names never enter callback_data.
        # Keep callback payloads compact even for a 64-character camera ID.
        if data == 'status':
            return self.status_text(archive),[[{'text':'↩ Quay lại','callback_data':'home'}]]
        if data == 'home':
            buttons=TimeMenus.shortcuts()
            buttons.extend([[{'text':'📷 Camera / Kho video','callback_data':'root'}],
                            [{'text':'▶ Start sync tất cả','callback_data':'sync:all'}],
                            [{'text':'📊 Tiến trình sync','callback_data':'ss:all'}],
                            [{'text':'🕐 Video gần đây','callback_data':'recent:0'}],
                            [{'text':'🗑 Thùng rác','callback_data':'trash:0'}],
                            [{'text':'⚙ Trạng thái','callback_data':'status'}]])
            for line in buttons:
                for button in line:
                    button['style'] = 'success'
            return '📹 Camera · Menu',buttons
        if data in ('today','yesterday','last6h','thisweek','lastweek','custom-time') or data.startswith(('w:','wc:','wq:','wqc:')):
            return TimeMenus(self).menu(archive,data,actor=actor)
        if data.startswith('trash:'):
            return self.trash_menu(archive,int(data.split(':')[1]))
        if data.startswith('recent:'):
            return self.recent_menu(archive,int(data.split(':')[1]))
        if data.startswith('ss:'):
            target=data[3:]
            camera=None if target=='all' else self._camera(archive,target)['id']
            return self.sync_menu(archive,camera)
        if data == 'root' or data.startswith('r:'):
            page = 0 if data == 'root' else int(data.split(':')[1])
            cameras = sorted(archive.cameras(), key=lambda c: (c['name'].casefold(), c['id']))
            if page < 0 or page > max(0, (len(cameras)-1)//10):
                raise ValueError('Invalid camera page')
            buttons = [[{'text':c['name'], 'callback_data':f"c:{self.camera_token(c['id'])}:asc", 'style':'success'}]
                       for c in cameras[page*10:page*10+10]]
            nav = []
            if page:
                nav.append({'text':'←', 'callback_data':f'r:{page-1}'})
            if (page+1)*10 < len(cameras):
                nav.append({'text':'→', 'callback_data':f'r:{page+1}'})
            if nav:
                buttons.append(nav)
            buttons.append([{'text':'▶ Start sync tất cả','callback_data':'sync:all'}])
            buttons.append([{'text':'📊 Tiến trình sync','callback_data':'ss:all'}])
            buttons.append([{'text':'↩ Quay lại','callback_data':'home'}])
            return 'Chọn Camera' + ('' if cameras else ' (chưa có camera)'), buttons
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
            if getattr(self.settings,'multi_channel_routing',False):
                url=camera.get('channel_index_url')
                if isinstance(url,str) and re.fullmatch(r'https://t\.me/c/[1-9][0-9]*/[1-9][0-9]*',url):
                    buttons.append([{'text':'📌 Mục lục channel','url':url,'style':'success'}])
            buttons.extend(self.sync_camera_buttons(camera))
            self._controls(buttons, f'c:{token}:{{order}}', 'root', order)
            upload='ON' if camera.get('upload_enabled',True) else 'OFF'
            return f'{name} · Upload {upload}\nChọn Năm', buttons
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
            stamp = from_epoch_ms(row['start_ms'],zone).strftime('%H:%M:%S')
            buttons.append(self.recording_buttons(row,stamp+' ▶'))
        nav = []
        if page:
            nav.append({'text':'←', 'callback_data':f'p:{token}:{day}:{order}:{page-1}'})
        if (page+1)*10 < len(rows):
            nav.append({'text':'→', 'callback_data':f'p:{token}:{day}:{order}:{page+1}'})
        if nav:
            buttons.append(nav)
        self._controls(buttons, f'p:{token}:{day}:{{order}}:0', f'm:{token}:{day[:7]}:{order}', order)
        if rows:
            buttons.insert(len(buttons)-1,[{'text':f'⬇ Tải toàn bộ ({len(rows)})',
                                          'callback_data':f'bd:{token}:{day}:'+('a' if order=='asc' else 'd')}])
        return f'{name} | {day} | trang {page+1} | '+('Cũ → mới' if order == 'asc' else 'Mới → cũ'), buttons

    @staticmethod
    def camera_token(camera_id):
        return hashlib.sha256(camera_id.encode()).hexdigest()[:12]

    def _camera(self, archive, token):
        matches = [c for c in archive.cameras() if self.camera_token(c['id']) == token]
        if len(matches) != 1:
            raise ValueError('Invalid camera selection')
        return matches[0]

    def sync_camera_buttons(self,camera):
        token=self.camera_token(camera['id'])
        upload=camera.get('upload_enabled',True)
        return [[{'text':'▶ Start sync','callback_data':'sync:'+token},
                 {'text':'Tiến trình','callback_data':'ss:'+token}],
                [{'text':'Upload ON → OFF' if upload else 'Upload OFF → ON',
                  'callback_data':f'up:{token}:{0 if upload else 1}'}]]

    def sync_menu(self,archive,camera=None,result=None):
        """Public queue diagnostics only; work runs in the worker, never poll()."""
        from .sync import SyncQueue
        result=result if result is not None else SyncQueue(archive).status(camera=camera,limit=20)
        states={'queued':'Đang chờ','running':'Đang chạy','completed':'Hoàn tất',
                'blocked':'Cần xử lý','failed':'Lỗi'}
        phases={'queued':'Hàng đợi','sd_download':'Tải SD','download':'Tải SD',
                'ingest':'Lập chỉ mục','normalize':'Chuẩn hóa','upload':'Upload Telegram',
                'completed':'Kết thúc','preflight':'Kiểm tra nguồn SD'}
        lines=['Sync '+(archive.camera_name(camera) if camera else 'tất cả camera'),
               'Worker: '+('đang hoạt động' if result.get('worker_alive') else 'chưa thấy heartbeat mới')]
        jobs=result.get('jobs',[])
        latest=[];seen=set()
        for job in jobs:
            camera_id=job.get('camera_id')
            if camera_id in seen:continue
            seen.add(camera_id);latest.append(job)
            if len(latest)==5:break
        for job in latest:
            name=job.get('camera_name') or archive.camera_name(job.get('camera_id',''))
            state=states.get(job.get('state'),str(job.get('state') or 'Chưa rõ'))
            phase=phases.get(job.get('phase'),str(job.get('phase') or '—'))
            lines.append(f'{name}: {state} | {phase}')
            if job.get('code'):lines.append('Mã: '+str(job['code'])[:100])
            if job.get('code')=='sd_sdk_missing':lines.append('SDK chưa sẵn sàng')
            statistics=job.get('statistics')
            if isinstance(statistics,dict):
                numbers=[f'{label}: {statistics[key]}' for key,label in
                         (('sd_searched','SD'),('sd_downloaded','Tải'),('uploaded','Upload'),('pending','Chờ'))
                         if type(statistics.get(key)) in (int,float)]
                if numbers:lines.append(' · '.join(numbers))
        if not jobs:lines.append('Chưa có công việc sync.')
        token=self.camera_token(camera) if camera else 'all'
        buttons=[[{'text':'▶ Start sync','callback_data':'sync:'+token},
                  {'text':'↻ Tiến trình','callback_data':'ss:'+token}]]
        if camera:
            configured=self._camera(archive,token)
            buttons.extend(self.sync_camera_buttons(configured)[1:])
        buttons.append([{'text':'↩ Camera','callback_data':f'c:{token}:asc' if camera else 'root'}])
        return '\n'.join(lines)[:3900],buttons

    def sync_action(self,archive,data,actor):
        """Mutations are reached only after the private-chat allowlist gate."""
        if type(actor) is not int or actor<=0 or actor not in self.viewers:
            raise ValueError('Invalid sync actor')
        from .sync import SyncQueue
        if data.startswith('sync:'):
            target=data[5:]
            if target=='all':camera=None
            else:
                configured=self._camera(archive,target)
                if configured.get('enabled') is False:raise ValueError('Camera is disabled')
                camera=configured['id']
            result=SyncQueue(archive).enqueue(camera=camera,source='telegram',actor=actor)
            text,buttons=self.sync_menu(archive,camera)
            count=len(result.get('jobs',[]))
            return f'Đã tiếp nhận sync: {count} camera.\n'+text,buttons
        selection=re.fullmatch(r'up:([a-f0-9]{12}):([01])',data)
        if selection:
            configured=self._camera(archive,selection[1])
            archive.update_camera(configured['id'],{'upload_enabled':selection[2]=='1'})
            return self.menu(archive,f"c:{selection[1]}:asc")
        raise ValueError('Invalid sync action')

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
            return pieces[1]+' — chọn camera', [[{'text':archive.camera_name(camera),'callback_data':f'p:{pieces[1]}:{hashlib.sha256(camera.encode()).hexdigest()[:12]}:0','style':'success'}] for camera in cameras[:100]]
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
                stamp=from_epoch_ms(row['start_ms'],zone).strftime('%H:%M:%S')
                buttons.append(self.recording_buttons(row,stamp+' ▶'))
            nav=[]
            if page:
                nav.append({'text':'←','callback_data':f'p:{day}:{camera_token}:{page-1}'})
            if (page+1)*10<len(rows):
                nav.append({'text':'→','callback_data':f'p:{day}:{camera_token}:{page+1}'})
            if nav:
                buttons.append(nav)
            return f'{day} | {archive.camera_name(camera)} | trang {page+1}',buttons
        raise ValueError('Unknown menu selection')

    def trash_menu(self, archive, page=0):
        if type(page) is not int or not 0<=page<=100000:raise ValueError('Invalid trash page')
        result=archive.trash(offset=page*10,limit=10)
        if page and not result['recordings']:raise ValueError('Invalid trash page')
        buttons=[];zone=get_zone(self.settings.timezone)
        for row in result['recordings']:
            stamp=from_epoch_ms(row['start_ms'],zone).strftime('%d/%m %H:%M:%S')
            buttons.append([{'text':f"↩ Khôi phục {archive.camera_name(row['camera'])} | {stamp}",
                             'callback_data':'u:'+row['key'][:32]}])
        nav=[]
        if page:nav.append({'text':'←','callback_data':f'trash:{page-1}'})
        if (page+1)*10<result['total']:nav.append({'text':'→','callback_data':f'trash:{page+1}'})
        if nav:buttons.append(nav)
        buttons.append([{'text':'↩ Quay lại','callback_data':'home'}])
        return f"Thùng rác kho chung | {result['total']} video | trang {page+1}",buttons

    def deletion_menu(self, archive, prefix, actor):
        if actor not in self.viewers:raise ValueError('Viewer is not authorized')
        row=archive.find_recording(prefix)
        if not row:raise ValueError('Recording is no longer available')
        nonce=secrets.token_hex(6)
        archive.state(f'telegram_delete_confirm:{actor}',json.dumps({'key':row['key'],'nonce':nonce,'expires':time.time()+300}))
        text=(self.caption(archive,row)+'\n\nXóa khỏi kho chung? Tất cả người được phép sẽ không thấy video. '
              'Khôi phục được trong Thùng rác; bản đã gửi/tải vẫn còn.')
        return text,[[{'text':'🗑 Xác nhận xóa khỏi kho','callback_data':f'xc:{prefix}:{nonce}'},
                      {'text':'Hủy','callback_data':'cancel-delete'}]]

    def confirm_deletion(self, archive, data, actor):
        if actor not in self.viewers:raise ValueError('Viewer is not authorized')
        pieces=data.split(':')
        if len(pieces)!=3 or not re.fullmatch(r'[a-f0-9]{32}',pieces[1]) or not re.fullmatch(r'[a-f0-9]{12}',pieces[2]):
            raise ValueError('Invalid delete confirmation')
        pending=json.loads(archive.state(f'telegram_delete_confirm:{actor}') or '{}')
        if (not isinstance(pending,dict) or not isinstance(pending.get('key'),str) or
            pending['key'][:32]!=pieces[1] or pending.get('nonce')!=pieces[2] or
            not isinstance(pending.get('expires'),(int,float)) or time.time()>pending['expires']):
            raise ValueError('Delete confirmation expired or belongs to another viewer')
        archive.soft_delete(pending['key'],actor)
        archive.state(f'telegram_delete_confirm:{actor}','{}')
        return 'Đã chuyển vào Thùng rác.',[[{'text':'↩ Khôi phục','callback_data':'u:'+pieces[1]},
                                                            {'text':'🗑 Thùng rác','callback_data':'trash:0'},
                                                            {'text':'📷 Camera','callback_data':'root'}]]

    def status_text(self,archive):
        status=archive.status();counts=status.get('counts',{});queue=status.get('queue',{})
        labels={'downloaded':'Chờ upload','uploading':'Đang upload','upload_unknown':'Cần kiểm tra',
                'needs_review':'Cần xử lý','failed':'Lỗi','ingesting':'Đang tải'}
        pending=[f'{labels.get(key,key)}: {value}' for key,value in sorted(queue.items())
                 if key!='uploaded' and value]
        return (f"Camera: {counts.get('cameras',0)} · Đã lưu: {counts.get('uploaded',0)} · Thùng rác: {counts.get('deleted',0)}\n"
                f"Upload: {'ON' if status.get('upload_enabled') else 'OFF'} · API: {status.get('api_mode','cloud')}\n"
                'Hàng đợi · '+(' | '.join(pending) if pending else 'Không có video chờ'))

    @staticmethod
    def _retry_delay(value):
        try:
            delay=float(value)
            return max(1.0,delay) if math.isfinite(delay) else 1.0
        except (TypeError,ValueError,OverflowError):
            return 1.0

    def _poll_backend(self,archive):
        prefix,separator,_=self.settings.token.partition(':')
        bot=('id:'+(prefix.lstrip('0') or '0') if separator and prefix.isdecimal()
             else 'id:'+archive.state('telegram_bot_id') if (archive.state('telegram_bot_id') or '').isdecimal()
             else 'token:'+hashlib.sha256(self.settings.token.encode()).hexdigest())
        scope=[self.settings.api_mode,self.settings.api_base.rstrip('/'),bot]
        return hashlib.sha256(json.dumps(scope,separators=(',',':')).encode()).hexdigest()

    @staticmethod
    def _valid_update(update):
        if not isinstance(update,dict) or type(update.get('update_id')) is not int or update['update_id']<0:
            return False
        callback=update.get('callback_query')
        if callback is not None:
            return (isinstance(callback,dict) and isinstance(callback.get('id'),str)
                    and isinstance(callback.get('data'),str) and isinstance(callback.get('from'),dict)
                    and (callback.get('message') is None or isinstance(callback.get('message'),dict)))
        member=update.get('my_chat_member')
        post=update.get('channel_post')
        if member is not None:
            return (isinstance(member,dict) and isinstance(member.get('chat'),dict)
                    and isinstance(member.get('new_chat_member'),dict))
        if post is not None:
            return isinstance(post,dict) and isinstance(post.get('chat'),dict)
        return isinstance(update.get('message'),dict)

    def _adopt_poll_cursor(self,archive,updates,backend,offset):
        """Recover only from an actual wholly lower batch, never an empty reset.

        Telegram update IDs can restart after a backend migration or a week of
        inactivity. Preserve the exact old cursor/journal in one bounded epoch;
        overlapping guarded IDs keep their journal because their POST is uncertain.
        """
        if not isinstance(updates,list) or not updates or not all(self._valid_update(u) for u in updates):
            return
        ids=[u['update_id'] for u in updates]
        old_backend=archive.state('telegram_poll_backend')
        try:last=float(archive.state('telegram_poll_last_processed_at') or 0)
        except (TypeError,ValueError,OverflowError):last=0
        idle=math.isfinite(last) and last>0 and time.time()-last>=7*86400
        if max(ids)>=offset or not (old_backend!=backend or idle):return
        journal=archive.state('telegram_replay_attempt')
        previous=self._replay_attempt(archive)
        overlapping=(previous.get('update_id') in ids
                     and previous.get('phase') in ('pending','unknown','done','rejected'))
        # Oversized/corrupt state is retained rather than truncating an exact
        # rollback record. Normal journals are less than 512 bytes.
        if journal is not None and len(journal)>4096:return
        try:epoch=json.loads(archive.state('telegram_poll_epoch') or '{}').get('epoch',0)
        except (ValueError,TypeError,AttributeError):epoch=0
        epoch=epoch if type(epoch) is int and epoch>=0 else 0
        diagnostic={'epoch':epoch+1,'at':time.time(),'reason':'backend' if old_backend!=backend else 'idle',
                    'old_backend':old_backend,'new_backend':backend,
                    'old_cursor':archive.state('telegram_offset'),'new_cursor':min(ids),
                    'replay_attempt':journal,'replay_retry_at':archive.state('telegram_replay_retry_at')}
        with archive.conn:
            changes=[('telegram_poll_epoch',json.dumps(diagnostic,separators=(',',':'))),
                     ('telegram_offset',str(min(ids))),('telegram_poll_backend',backend)]
            if not overlapping:
                changes.extend((('telegram_replay_attempt','{}'),('telegram_replay_retry_at','0')))
            for name,value in changes:
                archive.conn.execute('INSERT INTO state(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value',(name,value))

    @staticmethod
    def _advance_poll(archive,update_id,backend):
        with archive.conn:
            for name,value in (('telegram_offset',str(update_id+1)),('telegram_poll_backend',backend),
                               ('telegram_poll_last_processed_at',str(time.time()))):
                archive.conn.execute('INSERT INTO state(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value',(name,value))

    def _poll_replay(self,archive,prefix,chat_id,update_id,*,purpose='view'):
        try:deadline=float(archive.state('telegram_replay_retry_at') or 0)
        except (TypeError,ValueError,OverflowError):deadline=0
        if math.isfinite(deadline) and deadline>time.time():
            self._save_replay_attempt(archive,update_id,'rejected',advance=True,retry_at=deadline)
            result='rate_limited'
        else:
            result=self.replay(archive,prefix,chat_id,update_id=update_id,purpose=purpose,consume_rejection=True)
        if result=='rate_limited':
            try:
                deadline=float(archive.state('telegram_replay_retry_at') or 0)
                seconds=max(1,math.ceil(deadline-time.time()))
                self.request('sendMessage',{'chat_id':chat_id,'text':f'Telegram đang giới hạn. Bấm Xem/Tải lại sau {seconds}s.'})
            except Exception:
                pass # A rejected notification must not block other menus.
        return result

    def poll(self,archive):
        if not self.settings.token or not self.viewers:
            return
        offset=int(archive.state('telegram_offset') or 0)
        backend=self._poll_backend(archive)
        updates=self.request('getUpdates',{'offset':offset,'timeout':0,
                                         'allowed_updates':['message','callback_query','my_chat_member','channel_post']})
        if not isinstance(updates,list):raise ValueError('Invalid Telegram update batch')
        self._adopt_poll_cursor(archive,updates,backend,offset)
        for update in updates:
            if (isinstance(update,dict) and type(update.get('update_id')) is int
                    and update['update_id']>=int(archive.state('telegram_offset') or 0)
                    and ('my_chat_member' in update or 'channel_post' in update)):
                from .channel_directory import ChannelDirectory
                ChannelDirectory(archive).observe(update)
                self._advance_poll(archive,update['update_id'],backend)
                continue
            if not self._valid_update(update):continue
            if update['update_id'] < int(archive.state('telegram_offset') or 0):
                continue
            callback=update.get('callback_query')
            message=((callback or {}).get('message') or {}) if callback else update.get('message',{})
            sender=(callback or message).get('from')
            actor=sender.get('id') if isinstance(sender,dict) else None
            chat=message.get('chat')
            chat=chat if isinstance(chat,dict) else {}
            if (type(actor) is not int or actor not in self.viewers or chat.get('type') != 'private' or
                type(chat.get('id')) is not int or chat.get('id') != actor):
                self._advance_poll(archive,update['update_id'],backend)
                continue
            chat_id=message['chat']['id']
            refresh_keyboard=False
            menu_message_id=message.get('message_id') if callback else None
            menu_route=None
            previous=self._guarded_attempt(archive,update['update_id'])
            if previous.get('update_id')==update['update_id'] and previous.get('phase') in ('pending','unknown','done','rejected'):
                # Crash after the POST but before its durable cursor commit.
                # Do not invoke getMe, answerCallbackQuery or the media POST.
                deadline=archive.state('telegram_replay_retry_at') or 0
                if self._replay_attempt(archive).get('update_id')!=update['update_id']:
                    try:deadline=json.loads(archive.state('telegram_poll_epoch') or '{}').get('replay_retry_at') or 0
                    except (ValueError,TypeError,AttributeError):pass
                self._save_replay_attempt(archive,update['update_id'],
                                          'unknown' if previous['phase']=='pending' else previous['phase'],advance=True,
                                          retry_at=deadline,details={k:v for k,v in previous.items() if k not in ('update_id','phase')})
                self._advance_poll(archive,update['update_id'],backend)
                continue
            try:
                try:
                    if callback:
                        menu_route=callback['data']
                        try:self.request('answerCallbackQuery',{'callback_query_id':callback['id']})
                        except Exception:pass # ACK expiry/rate limiting does not cancel the action.
                        if callback['data'] != 'custom-time':
                            TimeMenus(self).dismiss_input(archive,actor)
                        if callback['data'].startswith(('v:','f:')):
                            self._poll_replay(archive,callback['data'][2:],chat_id,update['update_id'],
                                        purpose='download' if callback['data'].startswith('f:') else 'view')
                            self._advance_poll(archive,update['update_id'],backend)
                            continue
                        if callback['data'].startswith(('bw:','bwq:','bd:')):
                            from .telegram_bulk import TelegramBulk
                            bulk=TelegramBulk(self)
                            selection=TimeMenus(self).download_window(archive,callback['data'],actor)
                            job=bulk.enqueue(archive,actor,selection,update_id=update['update_id'])
                            if job['id'] is None:
                                text,buttons='Chưa có video.',[[{'text':'🏠 Menu','callback_data':'home'}]]
                            else:text,buttons=bulk.menu(archive,actor,job['id'])
                        elif callback['data'].startswith(('bulk-status:','bulk-cancel:')):
                            from .telegram_bulk import TelegramBulk
                            bulk=TelegramBulk(self)
                            job_id=callback['data'].split(':',1)[1]
                            if callback['data'].startswith('bulk-cancel:'):bulk.cancel(archive,actor,job_id)
                            text,buttons=bulk.menu(archive,actor,job_id)
                        elif callback['data'].startswith('x:'):
                            text,buttons=self.deletion_menu(archive,callback['data'][2:],actor)
                        elif callback['data'].startswith('xc:'):
                            text,buttons=self.confirm_deletion(archive,callback['data'],actor)
                        elif callback['data'].startswith('u:'):
                            row=archive.find_recording(callback['data'][2:],include_deleted=True)
                            if not row:raise ValueError('Unknown recording to restore')
                            archive.restore_recording(row['key'],actor)
                            text,buttons=self.trash_menu(archive)
                            text='Đã khôi phục video.\n'+text
                        elif callback['data'] in ('cancel-delete','nav-return'):
                            archive.state(f'telegram_delete_confirm:{actor}','{}')
                            menu_route=self._menu_route(archive,actor)
                            text,buttons=self.menu(archive,menu_route,actor=actor)
                        elif callback['data']=='cancel-time':
                            TimeMenus(self).cancel(archive,actor)
                            text,buttons=self.menu(archive,'home')
                            menu_route='home'
                        elif callback['data'].startswith(('sync:','up:')):
                            text,buttons=self.sync_action(archive,callback['data'],actor)
                        else:text,buttons=self.menu(archive,callback['data'],actor=actor)
                    else:
                        words=message.get('text','').split()
                        command=words[0].split('@')[0] if words else ''
                        raw_text=message.get('text','').strip()
                        is_start=command == '/start' or raw_text in ('🏠 Start','Start','🏠 Menu')
                        mapping={'/today':'today','/yesterday':'yesterday','/last6h':'last6h','/thisweek':'thisweek','/lastweek':'lastweek','/recent':'recent:0','/trash':'trash:0',
                                 '/time':'custom-time','🗓 Tùy chọn thời gian':'custom-time','Tùy chọn thời gian':'custom-time',
                                 '📅 Hôm nay':'today','Hôm nay':'today','📆 Hôm qua':'yesterday','Hôm qua':'yesterday',
                                 '🕕 6 giờ trước':'last6h','6 giờ trước':'last6h','6 giờ gần nhất':'last6h',
                                 '📅 Tuần này':'thisweek','Tuần này':'thisweek','📆 Tuần trước':'lastweek','Tuần trước':'lastweek',
                                 '🗓 Tuần này':'thisweek','🗓 Tuần trước':'lastweek',
                                 '📷 Camera':'root','🕐 Video gần đây':'recent:0','🗑 Thùng rác':'trash:0','⚙ Trạng thái':'status'}
                        # Commands, aliases, and visible actions exit an unfinished date prompt.
                        actions={button['text'] for row in self.reply_keyboard()['keyboard'] for button in row}
                        date_input=None
                        if (command.startswith('/') or raw_text in actions or raw_text in mapping or
                                is_start or raw_text in ('Start sync','Hủy')):
                            TimeMenus(self).dismiss_input(archive,actor)
                        else:
                            date_input=TimeMenus(self).accept(archive,actor,raw_text)
                        if command == '/rebuild_index':
                            text,buttons=self.rebuild_index(archive,actor,words)
                        elif actor == self.owner and (command == '/channel' or
                                isinstance(message.get('forward_origin'), dict) and message['forward_origin'].get('type') == 'channel' or
                                isinstance(message.get('forward_from_chat'), dict) and message['forward_from_chat'].get('type') == 'channel'):
                            origin=message.get('forward_origin')
                            forwarded=(origin.get('chat') if isinstance(origin,dict) and origin.get('type')=='channel'
                                       else message.get('forward_from_chat') if origin is None else None)
                            if isinstance(forwarded,dict):
                                from .channel_directory import ChannelDirectory
                                ChannelDirectory(archive).remember(forwarded)
                            text,buttons=self.channel_setup(actor,message)
                        elif is_start:
                            self.start_viewer(archive,actor)
                            if command == '/start' and len(words)>1:
                                if not words[1].startswith('play_'):
                                    raise ValueError('Unknown start payload')
                                self._poll_replay(archive,words[1][5:],chat_id,update['update_id'])
                                self._advance_poll(archive,update['update_id'],backend)
                                continue
                            text,buttons=self.menu(archive,'home')
                            menu_route='home'
                            refresh_keyboard=True
                        elif command in ('/cancel','/huy') or raw_text == 'Hủy':
                            TimeMenus(self).cancel(archive,actor)
                            text,buttons=self.menu(archive,'home')
                            menu_route='home'
                        elif date_input is not None:
                            text,buttons=date_input
                            menu_message_id=self._active_menu(archive,actor)
                            session=TimeMenus(self)._session(archive,actor)
                            menu_route=('wq:'+session['token']+':a:0') if session.get('token') else 'custom-time'
                        elif command == '/status':
                            text=self.status_text(archive)
                            buttons=[[{'text':'↩ Quay lại','callback_data':'home'}]]
                            menu_route='status'
                        elif command == '/sync' or message.get('text','').strip() in ('▶ Start sync','Start sync'):
                            text,buttons=self.sync_action(archive,'sync:all',actor)
                            menu_route='sync:all'
                        else:
                            data=mapping.get(message.get('text','').strip(),mapping.get(command,'root'))
                            text,buttons=self.menu(archive,data,actor=actor)
                            menu_route=data
                    buttons=self.navigation_buttons(archive,actor,menu_route,buttons)
                    buttons=self.player_buttons(archive,buttons,actor)
                    # Inline-only navigation prevents mother actions remaining
                    # visible on every child screen. The old reply keyboard is
                    # removed with one migration message, never reinstalled.
                    self.present_menu(archive,chat_id,actor,text,buttons,message_id=menu_message_id)
                    self.remember_menu_route(archive,actor,menu_route)
                    try:self.ensure_keyboard(archive,actor,force=refresh_keyboard)
                    except Exception:pass
                except (ValueError,KeyError,IndexError):
                    self.present_menu(archive,chat_id,actor,'Mục này đã thay đổi.',
                                      [[{'text':'↩ Quay lại','callback_data':'home'}]],message_id=menu_message_id)
                    self.remember_menu_route(archive,actor,'home')
            except ApiRejected as exc:
                if not (400 <= exc.code < 500) or exc.code==429:
                    raise
                # Expired callback/blocked user must not pin the durable update cursor.
            self._advance_poll(archive,update['update_id'],backend)
