"""Bot-visible channel catalog. Membership updates, never an account scraper.

Bot API has no list-all-my-channels method. Persist the bot's channel events
and seed existing configuration; refresh titles and exact admin rights by ID.
"""
import hashlib
import re
import time


def valid_channel(value):
    return type(value) is int and bool(re.fullmatch(r'-100[1-9][0-9]{0,12}',str(value))) and abs(value)<=2**52


class ChannelDirectory:
    def __init__(self, archive):
        self.archive=archive;self.conn=archive.conn
        prefix,sep,_=archive.settings.token.partition(':')
        self.bot_id=int(prefix) if sep and prefix.isdecimal() and int(prefix)>0 else None
        self.bot_key=str(self.bot_id) if self.bot_id else hashlib.sha256(archive.settings.token.encode()).hexdigest()
        with self.conn:
            self.conn.execute('''CREATE TABLE IF NOT EXISTS telegram_channels (
                bot_key TEXT NOT NULL,chat_id INTEGER NOT NULL,title TEXT NOT NULL,
                private INTEGER NOT NULL DEFAULT 1,status TEXT NOT NULL DEFAULT 'known',
                error TEXT,updated_at REAL NOT NULL,checked_at REAL,
                PRIMARY KEY(bot_key,chat_id))''')
        # IDs survive restarts; real titles are populated by events/getChat.
        ids={row[0] for row in self.conn.execute('SELECT channel_chat_id FROM cameras WHERE channel_chat_id IS NOT NULL')}
        ids.add(archive.settings.storage_channel_id)
        with self.conn:
            for channel in ids:
                if valid_channel(channel):
                    self.conn.execute("INSERT OR IGNORE INTO telegram_channels(bot_key,chat_id,title,updated_at) VALUES(?,?,?,?)",(self.bot_key,channel,str(channel),time.time()))

    def remember(self, chat, *, status='known', error=None):
        if not isinstance(chat,dict) or not valid_channel(chat.get('id')) or chat.get('type')!='channel':return False
        if status not in ('known','ready','blocked'):return False
        title=chat.get('title')
        if not isinstance(title,str):title=str(chat['id'])
        title=''.join(c for c in title if ord(c)>=32 and ord(c)!=127)[:128].strip() or str(chat['id'])
        private=not bool(chat.get('username') or chat.get('active_usernames'))
        checked=time.time() if status in ('ready','blocked') else None
        with self.conn:
            self.conn.execute('''INSERT INTO telegram_channels(bot_key,chat_id,title,private,status,error,updated_at,checked_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(bot_key,chat_id) DO UPDATE SET
                title=excluded.title,private=excluded.private,updated_at=excluded.updated_at,
                status=CASE WHEN excluded.status='known' THEN telegram_channels.status ELSE excluded.status END,
                error=CASE WHEN excluded.status='known' THEN telegram_channels.error ELSE excluded.error END,
                checked_at=COALESCE(excluded.checked_at,telegram_channels.checked_at)''',
                (self.bot_key,chat['id'],title,int(private),status,error,time.time(),checked))
        return True

    def observe(self, update):
        if not isinstance(update,dict):return False
        membership=update.get('my_chat_member')
        if isinstance(membership,dict):
            member=membership.get('new_chat_member',{});user=member.get('user',{}) if isinstance(member,dict) else {}
            expected=self.bot_id
            if expected is None:
                value=self.archive.state('telegram_bot_id')
                expected=int(value) if isinstance(value,str) and value.isdecimal() else None
            if expected is None or not isinstance(user,dict) or type(user.get('id')) is not int or user['id']!=expected or user.get('is_bot') is not True:return False
            ready=(member.get('status')=='administrator' and member.get('can_post_messages') is True and member.get('can_edit_messages') is True)
            return self.remember(membership.get('chat'),status='known' if ready else 'blocked',error=None if ready else 'channel_admin_required')
        post=update.get('channel_post')
        if isinstance(post,dict):return self.remember(post.get('chat'))
        return False

    def list(self):
        bound={r['channel_chat_id']:r for r in self.conn.execute('SELECT id,channel_chat_id,channel_name FROM cameras WHERE channel_chat_id IS NOT NULL')}
        channels=[]
        for r in self.conn.execute('SELECT * FROM telegram_channels WHERE bot_key=?',(self.bot_key,)):
            camera=bound.get(r['chat_id'])
            alias=camera['channel_name'] if camera is not None else ''
            channels.append(dict(chat_id=r['chat_id'],name=alias or r['title'],title=r['title'],channel_name=alias,private=bool(r['private']),
                     ready=r['status']=='ready',status=r['status'],error=r['error'],
                     bound_camera_id=camera['id'] if camera is not None else None,checked_at=r['checked_at']))
        return sorted(channels,key=lambda channel:(channel['name'].casefold(),channel['chat_id']))[:500]

    def refresh(self, telegram):
        from .telegram import ApiRejected
        for row in self.list():
            try:
                telegram.verify_channel(self.archive,row['chat_id'],require_index=True)
            except ApiRejected as error:
                code='channel_rate_limited' if error.code==429 else 'channel_access_denied'
                with self.conn:self.conn.execute("UPDATE telegram_channels SET status='blocked',error=?,checked_at=? WHERE bot_key=? AND chat_id=?",(code,time.time(),self.bot_key,row['chat_id']))
                if error.code==429:break
            except Exception:
                with self.conn:self.conn.execute("UPDATE telegram_channels SET status='blocked',error='channel_check_failed',checked_at=? WHERE bot_key=? AND chat_id=?",(time.time(),self.bot_key,row['chat_id']))
        return self.list()
