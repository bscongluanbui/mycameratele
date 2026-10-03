import argparse
import json
import os
import platform
from pathlib import Path
import signal
import socket
import threading
from contextlib import contextmanager
import time

from .core import Archive, Settings
from .telegram import Telegram


def emit(event, **data):
    print(json.dumps({'event':event,**data},ensure_ascii=False),flush=True)


@contextmanager
def mutation_lock(settings):
    settings.state_dir.mkdir(parents=True,exist_ok=True)
    with (settings.state_dir/'worker.lock').open('a+b') as lock:
        if os.name=='nt':
            import msvcrt
            lock.seek(0,2)
            if lock.tell()==0:lock.write(b'0');lock.flush()
            lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield


def main():
    parser=argparse.ArgumentParser(description='Portable exported SD clip ingest and Telegram archive; native SD downloader pending.')
    subs=parser.add_subparsers(dest='command',required=True)
    doctor=subs.add_parser('doctor');doctor.add_argument('--network',action='store_true')
    dashboard=subs.add_parser('dashboard');dashboard.add_argument('--host',default=os.environ.get('DASHBOARD_HOST','0.0.0.0'));dashboard.add_argument('--port',type=int,default=int(os.environ.get('DASHBOARD_PORT','8080')))
    ingest=subs.add_parser('ingest');ingest.add_argument('--manifest',required=True);ingest.add_argument('--dry-run',action='store_true')
    subs.add_parser('run');subs.add_parser('health')
    listing=subs.add_parser('list');listing.add_argument('--date',required=True);listing.add_argument('--camera');listing.add_argument('--order',choices=('asc','desc'),default='asc')
    reconcile=subs.add_parser('reconcile');reconcile.add_argument('--key',required=True);reconcile.add_argument('--chat-id',required=True)
    reconcile.add_argument('--message-id',type=int,required=True);reconcile.add_argument('--file-id',required=True)
    args=parser.parse_args()
    settings=Settings.from_env()
    if args.command=='dashboard':
        from .dashboard import serve
        emit('dashboard_started',port=args.port,sd_auto_download='not_implemented')
        serve(settings,args.host,args.port);return 0
    if args.command=='doctor':
        data={'machine':platform.machine(),'sd_adapter':'exported-file-ingest','automatic_sd_download':'not_implemented',
              'input_dir':str(settings.input_dir),'upload_enabled':settings.enable_upload,'api_mode':settings.api_mode}
        if args.network:
            host=os.environ.get('CAMERA_HOST','192.168.1.10');data['tcp']={}
            for port in (int(os.environ.get('CAMERA_DEVICE_PORT','8000')),int(os.environ.get('CAMERA_RTSP_PORT','554')),80):
                try:
                    with socket.create_connection((host,port),timeout=3):data['tcp'][str(port)]='open'
                except OSError:data['tcp'][str(port)]='unconfirmed'
        emit('doctor',**data);return 0
    if args.command=='health':
        heartbeat=settings.state_dir/'heartbeat'
        good=heartbeat.is_file() and time.time()-heartbeat.stat().st_mtime<max(120,settings.interval*4)
        emit('health',healthy=good);return 0 if good else 1
    # All mutation CLI commands share the runner's lock; list remains read-only.
    lock_scope=mutation_lock(settings) if args.command!='list' else None
    if lock_scope is not None:lock_scope.__enter__()
    archive=Archive(settings)
    try:
        if args.command=='ingest':
            rows=archive.ingest_manifest(args.manifest,args.dry_run)
            emit('ingest',dry_run=args.dry_run,recordings=[{'key':r['key'],'status':r['status']} for r in rows]);return 0
        if args.command=='list':
            emit('archive',date=args.date,recordings=archive.list_day(args.date,args.camera,args.order));return 0
        if args.command=='reconcile':
            row=archive.conn.execute('SELECT status FROM recordings WHERE key=?',(args.key,)).fetchone()
            if row is None or row[0]!='upload_unknown':raise ValueError('Reconcile requires upload_unknown recording and confirmed Message metadata')
            archive.mark_uploaded(args.key,args.chat_id,args.message_id,args.file_id)
            archive.cleanup(args.key)
            emit('reconciled',key=args.key);return 0
        running=True
        def stop(*_):
            nonlocal running
            running=False
        signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
        archive.recover_uploads()
        heartbeat_stop=threading.Event()
        def heartbeat():
            while not heartbeat_stop.is_set():
                (settings.state_dir/'heartbeat').write_text(str(time.time()))
                heartbeat_stop.wait(10)
        heartbeat_thread=threading.Thread(target=heartbeat,daemon=True)
        heartbeat_thread.start()
        telegram=Telegram(settings)
        def poll_commands():
            # Each thread owns its SQLite connection; ingestion/upload cannot block the menu.
            poll_archive=Archive(settings)
            try:
                while not heartbeat_stop.is_set():
                    try:telegram.poll(poll_archive)
                    except Exception as exc:emit('telegram_poll_error',error_type=type(exc).__name__)
                    heartbeat_stop.wait(2)
            finally:
                poll_archive.close()
        poll_thread=threading.Thread(target=poll_commands,daemon=True)
        poll_thread.start()
        emit('started',**archive.status())
        while running:
            for manifest in sorted(settings.input_dir.glob('*.json')):
                try:
                    for row in archive.ingest_manifest(manifest,continue_on_error=True):
                        if row['status']=='failed':emit('ingest_error',manifest=manifest.name,error_type=row['error_type'])
                except Exception as exc:emit('ingest_error',manifest=manifest.name,error_type=type(exc).__name__)
            try:
                result=telegram.upload_one(archive)
                if result:emit('upload',status=result)
            except Exception as exc:emit('telegram_error',error_type=type(exc).__name__)
            if not settings.keep_cache:
                for row in archive.conn.execute("SELECT key FROM recordings WHERE status='uploaded'").fetchall():
                    try:archive.cleanup(row[0])
                    except Exception as exc:emit('cleanup_error',error_type=type(exc).__name__)
            for _ in range(settings.interval):
                if not running:break
                time.sleep(1)
        heartbeat_stop.set();heartbeat_thread.join(timeout=11);poll_thread.join(timeout=36)
        emit('stopped');return 0
    finally:
        archive.close()
        if lock_scope is not None:lock_scope.__exit__(None,None,None)


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        emit('error',error_type=type(exc).__name__)
        raise SystemExit(1)
