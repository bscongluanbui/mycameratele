"""Synthetic House01 stream-copy/channel lifecycle; never real Telegram posts."""
import hashlib,json,os,platform,subprocess,tempfile
from pathlib import Path
from unittest.mock import patch
from archive_app.core import Archive,Settings
from archive_app.telegram import Telegram

def run():
    with tempfile.TemporaryDirectory(prefix='house01-smoke-') as tmp:
        root=Path(tmp).resolve();source=root/'opaque-recording-name.ts'
        # Encoding here only creates a synthetic fixture before the app guard.
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y',
            '-f','lavfi','-i','color=c=blue:s=96x64:r=12','-f','lavfi','-i','anullsrc=r=16000:cl=mono',
            '-t','1','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-threads','1',
            '-f','mpegts',str(source)],check=True,timeout=120)
        original=hashlib.sha256(source.read_bytes()).hexdigest();commands=[]
        real_run=subprocess.run
        def copy_only(command,**kwargs):
            commands.append(command)
            assert command[0]=='ffmpeg' and command[command.index('-c')+1]=='copy'
            assert '-c:v' not in command and '-c:a' not in command
            assert 'libx264' not in command and 'libx265' not in command and 'aac' not in command
            assert 'null' not in command and 'ffprobe' not in command[0]
            return real_run(command,**kwargs)
        settings=Settings(root/'state',root/'cache',root/'input','UTC+07:00',
            keep_cache=False,cache_retention_hours=1,min_free_bytes=0,enable_upload=True,
            owner_user_id=42,allowed_users=(42,77),token='synthetic-not-a-real-token',
            media_mode='remux_copy',tenant_id='house01',telegram_destination='channel',storage_channel_id=-100123456)
        settings.input_dir.mkdir();inside=settings.input_dir/source.name;inside.write_bytes(source.read_bytes())
        archive=Archive(settings);posts=[]
        telegram=Telegram(settings)
        def fake_request(method,fields,**kwargs):
            posts.append(method)
            if method=='getMe':return {'id':900,'is_bot':True,'username':'fixture_house01_bot'}
            if method=='getChat':return {'id':-100123456,'type':'channel','title':'Synthetic House01'}
            if method=='getChatMember':return {'user':{'id':900,'is_bot':True},'status':'administrator','can_post_messages':True}
            if method=='sendVideo':
                assert fields['chat_id']==-100123456 and kwargs['file_field']=='video'
                assert Path(kwargs['file_path']).suffix=='.mp4'
                return {'message_id':11,'chat':{'id':-100123456,'type':'channel'},
                        'video':{'file_id':'synthetic-video','file_unique_id':'synthetic-unique'}}
            if method=='copyMessage':
                assert fields['chat_id']==77 and fields['from_chat_id']==-100123456 and fields['message_id']==11
                return {'message_id':22}
            raise AssertionError(method)
        telegram.request=fake_request
        try:
            with patch('archive_app.core.subprocess.run',side_effect=copy_only):
                row=archive.ingest_entry({'camera':'front','record_id':'opaque-recording-name',
                    'path':str(inside),'start_time':'2026-10-04T08:00:00+07:00','end_time':'2026-10-04T08:00:01+07:00'})
            assert len(commands)==1 and row['processing_method']=='remux_copy'
            assert row['media_probe_status']=='disabled' and row['media_extension']=='.mp4'
            cached=Path(row['local_path']);assert cached.read_bytes()[4:8]==b'ftyp'
            assert hashlib.sha256(inside.read_bytes()).hexdigest()==original
            assert telegram.upload_one(archive)=='uploaded'
            saved=dict(archive.conn.execute('SELECT * FROM recordings WHERE key=?',(row['key'],)).fetchone())
            assert saved['storage_kind']=='channel' and saved['storage_chat_id']==-100123456 and saved['storage_message_id']==11
            assert saved['media_type']=='video' and saved['bot_id']==900 and cached.exists()
            assert not archive.cleanup(row['key'])
            with patch('archive_app.core.time.time',return_value=saved['uploaded_at']+3601):assert archive.cleanup(row['key'])
            assert not cached.exists() and archive.browse(status='uploaded')['total']==1
            assert telegram.replay(archive,row['key'][:32],chat_id=77)=='replayed'
            assert archive.soft_delete(row['key'],77)
            assert archive.browse(status='uploaded')['total']==0
            assert archive.restore_recording(row['key'],77)
            assert archive.browse(status='uploaded')['total']==1
            try:telegram.replay(archive,row['key'][:32],chat_id=88)
            except ValueError:pass
            else:raise AssertionError('Allowlist was bypassed')
            assert posts.count('sendVideo')==1 and posts.count('copyMessage')==1 and 'sendDocument' not in posts
            assert hashlib.sha256(inside.read_bytes()).hexdigest()==original
        finally:archive.close()
    print(json.dumps({'result':'OK','machine':platform.machine(),'tenant':'house01',
        'media':'MP4-stream-copy-video+audio','metadata':'SDK/filename-no-ffprobe-or-full-decode',
        'destination':'private-channel-only','retrieval':'copyMessage-no-cache-read',
        'retention':'1h-after-confirmed-upload','index_after_cleanup':'uploaded',
        'allowlist':'enforced','trash':'soft-delete+restore','real_telegram_posts':0}))

run()
