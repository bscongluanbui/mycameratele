"""Byte-preserving SQLite smoke executed in each published Linux image by CI.

FFmpeg only generates color/silence input before the pipeline guard. The archive
pipeline uses no subprocess and preserves source/cache/upload SHA-256. No camera
footage or network credentials are supplied.
Run with writable /input, /data, /cache tmpfs and a read-only root filesystem.
"""
import hashlib
import http.client
import json
import os
import platform
import subprocess
import time
import threading
from datetime import timedelta
from pathlib import Path

from archive_app.core import Archive, Settings, get_zone, parse_time
from archive_app.telegram import Telegram
from archive_app.telegram_menu import TimeMenus
from archive_app.dashboard import DashboardServer

assert os.getuid() == 10001, "Image must run as its non-root application user"
settings = Settings.from_env()
assert not settings.enable_upload
settings.min_free_bytes = 0  # Synthetic tmpfs smoke must not require 5 GB free RAM.
source = settings.input_dir / "synthetic-smoke.mp4"
subprocess.run([
    "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
    "-f", "lavfi", "-i", "color=c=blue:s=96x64:r=12",
    "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "1",
    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
    "-threads", "1", str(source),
], check=True, timeout=120)
original = hashlib.sha256(source.read_bytes()).hexdigest()
entry = {
    "camera": "smoke_camera", "source": "synthetic-ci", "record_id": "smoke-001",
    "path": source.name, "start_time": "2026-10-03T10:00:00+07:00",
    "end_time": "2026-10-03T10:00:01+07:00",
}
assert settings.passthrough_probe is False
original_subprocess_run = subprocess.run
pipeline_subprocess_calls = []
def reject_pipeline_subprocess(*args, **kwargs):
    pipeline_subprocess_calls.append((args, kwargs))
    raise AssertionError('Archive pipeline invoked a subprocess')
subprocess.run = reject_pipeline_subprocess
archive = Archive(settings)
try:
    row = archive.ingest_entry(entry)
    assert row["status"] == "downloaded"
    assert row["processing_method"] == "passthrough"
    assert row["sha256"] == original
    assert hashlib.sha256(Path(row["local_path"]).read_bytes()).hexdigest() == original
    assert archive.ingest_entry(entry)["key"] == row["key"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    assert Path(row["local_path"]).is_file()
    assert Telegram(settings).upload_one(archive) is None
    assert not archive.list_day("2026-10-03", "smoke_camera")
    assert parse_time(entry["start_time"]).astimezone(get_zone(settings.timezone)).utcoffset() == timedelta(hours=7)
    reopened = Archive(settings)
    try:
        assert reopened.browse(camera="smoke_camera", status="all")["total"] == 1
    finally:
        reopened.close()
    # Private-bot behavior uses a fake API transport; no credential or network.
    settings.owner_user_id = 42
    settings.allowed_users = (42, 77, 88)
    settings.bot_username = 'fixture_archive_bot'
    settings.token = 'synthetic-ci-not-a-token'
    settings.enable_upload = True
    settings.keep_cache = False
    settings.cache_retention_hours = 24
    telegram = Telegram(settings)
    assert telegram.upload_one(archive) is None  # Owner has not started the bot.
    calls = []
    def fake_request(method, fields, **kwargs):
        calls.append((method, fields, kwargs))
        assert method == 'sendDocument'
        if kwargs.get('file_path'):
            assert kwargs['file_field'] == 'document'
            assert fields['disable_content_type_detection'] is True
            assert hashlib.sha256(Path(kwargs['file_path']).read_bytes()).hexdigest() == original
        field = 'document'
        return {'message_id': len(calls), 'chat': {'id': fields['chat_id'], 'type': 'private'},
                field: {'file_id': 'fixture-file-id', 'file_unique_id': 'fixture-unique-id'}}
    telegram.request = fake_request
    archive.state('telegram_owner_started:42', '1')
    assert telegram.upload_one(archive) == 'uploaded'
    assert calls[0][1]['chat_id'] == 42
    assert Path(row['local_path']).is_file()  # Cache holds for a full 24 hours.
    saved = dict(archive.conn.execute('SELECT * FROM recordings WHERE key=?', (row['key'],)).fetchone())
    assert saved['file_unique_id'] == 'fixture-unique-id' and saved['media_type'] == 'document'
    archive.conn.execute('UPDATE recordings SET uploaded_at=? WHERE key=?', (time.time()-86401, row['key']))
    archive.conn.commit()
    assert archive.cleanup(row['key']) and not Path(row['local_path']).exists()
    before = dict(archive.conn.execute('SELECT * FROM recordings WHERE key=?', (row['key'],)).fetchone())
    for viewer in (77, 88):
        assert telegram.replay(archive, row['key'][:32], viewer) == 'replayed'
        assert calls[-1][1]['chat_id'] == viewer and calls[-1][1]['document'] == 'fixture-file-id'
        assert not calls[-1][2]  # No binary reads/re-upload kwargs after cleanup.
    try: telegram.replay(archive, row['key'][:32], 999)
    except ValueError: pass
    else: raise AssertionError('Non-allowlisted viewer accepted')
    after = dict(archive.conn.execute('SELECT * FROM recordings WHERE key=?', (row['key'],)).fetchone())
    assert before == after  # Replay does not replace owner archive metadata.
    assert archive.telegram_url(after).endswith('start=play_'+row['key'][:32])
    # Shared trash hides old selections for everyone, then restores the same ID.
    assert archive.soft_delete(row['key'],77)
    assert archive.browse(status='all')['total']==0
    assert archive.find_recording(row['key'][:32]) is None
    assert archive.telegram_url(archive.trash()['recordings'][0]) is None
    try: telegram.replay(archive,row['key'][:32],88,purpose='download')
    except ValueError: pass
    else: raise AssertionError('Deleted clip remained downloadable through old bot link')
    assert archive.restore_recording(row['key'],88)
    assert telegram.replay(archive,row['key'][:32],77,purpose='download')=='replayed'
    assert calls[-1][1]['document']=='fixture-file-id' and not calls[-1][2]
    # Frozen shortcut -> camera -> filtered clip includes view/download/delete.
    anchor=int(parse_time('2026-10-03T12:00:00+07:00').timestamp())
    _,cameras=TimeMenus(telegram).menu(archive,f'w:h:{anchor}:a:0')
    callback=next(b['callback_data'] for buttons in cameras for b in buttons if b['callback_data'].startswith('wc:'))
    _,videos=TimeMenus(telegram).menu(archive,callback)
    actions=[b['callback_data'] for buttons in videos for b in buttons]
    assert all(prefix+row['key'][:32] in actions for prefix in ('v:','f:','x:'))
    assert archive.backup_daily().is_file()
    assert archive.backup_daily() is None
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    assert not pipeline_subprocess_calls
finally:
    archive.close()
    subprocess.run = original_subprocess_run

# Exercise the actual HTTP dashboard inside every target image. The fixture
# credentials are local CI data only; no VPS/account secrets are supplied.
dashboard=DashboardServer(('0.0.0.0',0),settings)
thread=threading.Thread(target=dashboard.serve_forever,daemon=True);thread.start()
cookie='';csrf=''
def dashboard_request(method,path,body=None):
    connection=http.client.HTTPConnection('127.0.0.1',dashboard.server_port,timeout=60)
    headers={'Cookie':cookie,'X-CSRF-Token':csrf}
    if body is not None:
        headers['Content-Type']='application/json';body=json.dumps(body).encode()
    connection.request(method,path,body=body,headers=headers)
    response=connection.getresponse();data=json.loads(response.read())
    result=(response.status,data,response.getheader('Set-Cookie',''))
    connection.close();return result
try:
    assert dashboard.server_address[0]=='0.0.0.0'
    assert dashboard_request('GET','/api/cameras')[0]==401
    code,data,header=dashboard_request('POST','/api/login',{'username':'admin','password':'admin'})
    assert code==200 and data['account']['password_change_required']
    cookie=header.split(';')[0];csrf=data['csrf_token']
    assert dashboard_request('GET','/api/cameras')[0]==409
    code,data,_=dashboard_request('POST','/api/account',{'current_password':'admin','username':'smoke_admin','new_password':'synthetic-smoke-password'})
    assert code==200 and data['credentials_updated']
    assert dashboard_request('GET','/api/account')[0]==401
    assert dashboard_request('POST','/api/login',{'username':'admin','password':'admin'})[0]==401
    code,data,header=dashboard_request('POST','/api/login',{'username':'smoke_admin','password':'synthetic-smoke-password'})
    assert code==200 and not data['account']['password_change_required']
    cookie=header.split(';')[0];csrf=data['csrf_token']
    assert dashboard_request('GET','/api/cameras')[0]==200
    assert dashboard.auth.path.stat().st_mode & 0o777==0o600
finally:
    dashboard.shutdown();dashboard.server_close();thread.join(2)
print(json.dumps({"result": "OK", "machine": platform.machine(), "uid": os.getuid(),
                  "pipeline": "byte-preserving-no-subprocess", "sha256": "source=cache=upload",
                  "upload_method": "sendDocument", "fixture": "ffmpeg-generated-color-silence-only",
                  "sqlite": "durable-idempotent",
                  "source": "unchanged", "timezone": "+07:00", "telegram_posts": 0,
                  "private_bot": "owner+2-viewers-file-id-replay", "cache": "24h-cleanup",
                  "bot_controls": "download+shared-trash-restore+time-camera-video",
                  "dashboard_login": "admin-once+change+relogin+ip-bind",
                  "daily_backup": "atomic-sqlite"}, sort_keys=True))
