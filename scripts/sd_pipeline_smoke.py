"""Real FFmpeg + SQLite, synthetic SD source and fake Telegram; no device login."""
import json,os,platform,shutil,subprocess,tempfile
from pathlib import Path
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from archive_app.core import Archive,Settings
from archive_app.sd_source import SDSource
from archive_app.sync import SyncQueue
from archive_app.telegram import Telegram

def run():
    parent=Path(__file__).resolve().parents[1]/'tests' if os.name=='nt' else Path(tempfile.gettempdir())
    with tempfile.TemporaryDirectory(prefix='.tmp-sd-smoke-',dir=parent) as tmp:
        root=Path(tmp);source=root/'synthetic.mp4'
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-f','lavfi','-i','color=c=blue:s=96x64:r=12',
            '-f','lavfi','-i','anullsrc=r=16000:cl=mono','-t','1','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-threads','1',str(source)],check=True,timeout=120)
        settings=Settings(root/'state',root/'cache',root/'input','UTC+07:00',min_free_bytes=0,enable_upload=True,
                          owner_user_id=42,allowed_users=(42,77),token='synthetic-not-a-real-token')
        settings.input_dir.mkdir()
        a=Archive(settings);now=datetime.now(timezone.utc);start=now-timedelta(minutes=5);end=start+timedelta(seconds=1)
        downloads=[];posts=[]
        class Provider:
            backend='isapi';max_bytes=10000000
            def __enter__(self):return self
            def __exit__(self,*_):return False
            def search(self,*_):return [{'record_id':'synthetic-remote-file','start_time':start.isoformat(),'end_time':end.isoformat(),'size':source.stat().st_size}]
            def download(self,recording,destination):
                shutil.copyfile(source,destination);downloads.append(recording['record_id']);return destination.stat().st_size
        t=Telegram(settings)
        def request(method,fields,**kwargs):
            posts.append(method);assert method in ('sendVideo','sendDocument')
            return {'message_id':len(posts),'chat':{'id':42,'type':'private'},
                    'video' if method=='sendVideo' else 'document':{'file_id':'synthetic-file-id','file_unique_id':'synthetic-unique'}}
        t.request=request
        try:
            a.add_camera({'id':'fixture','host':'192.168.31.166','sd_password':'synthetic-device','upload_enabled':False})
            a.state('telegram_owner_started:42','1');q=SyncQueue(a)
            with patch.object(SDSource,'_provider',return_value=Provider()),patch.object(Archive,'probe_camera',return_value={'tcp':{'8000':'unconfirmed'}}):
                q.enqueue('fixture',source='synthetic-smoke');assert q.run_once(t)
                job=q.status('fixture')['latest']['fixture'];assert job['code']=='camera_upload_disabled'
                row=dict(a.conn.execute('SELECT * FROM recordings').fetchone())
                assert row['status']=='downloaded' and Path(row['local_path']).is_file() and not posts
                assert not list(settings.input_dir.iterdir())
                a.update_camera('fixture',{'upload_enabled':True});q.enqueue('fixture',source='synthetic-smoke');q.run_once(t)
                assert q.status('fixture')['latest']['fixture']['state']=='completed'
                assert len(downloads)==1 and len(posts)==1
                row=dict(a.conn.execute('SELECT * FROM recordings').fetchone());assert row['status']=='uploaded'
                q.enqueue('fixture',source='synthetic-smoke');q.run_once(t);assert len(posts)==1 and len(downloads)==1
                a.soft_delete(row['key'],77);q.enqueue('fixture',source='synthetic-smoke');q.run_once(t)
                assert a.browse(status='all')['total']==0 and len(posts)==1 and len(downloads)==1
                assert a.restore_recording(row['key'],77);assert a.browse(status='all')['total']==1
        finally:a.close()
    print(json.dumps({'result':'OK','machine':platform.machine(),'source':'synthetic-SD-provider-not-real-camera',
                      'ffmpeg':'real-remux+decode','upload_toggle':'off-download-cache/on-single-post','rescan':'idempotent',
                      'trash':'not-resurrected+restore','input_writes':0,'real_telegram_posts':0},sort_keys=True))

if __name__=='__main__':run()
