"""Durable, camera-scoped SD download and exported MP4/manifest jobs.

LAN checks report reachability only; SD providers must confirm search/download.
The queue never rewrites recording identities or trash state.
"""
import json
import re
import time
import uuid


MAX_ACTIVE_JOBS = 200
MAX_HISTORY = 1000
MAX_UPLOADS_PER_JOB = 100
MAX_MANIFESTS_PER_JOB = 200
TERMINAL_STATES = ('completed', 'blocked', 'failed')


def _statistics():
    return {'manifests': 0, 'matched': 0, 'imported': 0, 'already_known': 0,
            'uploaded': 0, 'failed': 0, 'ready': 0, 'pending': 0, 'remuxed': 0, 'remux_failed': 0,
            'remux_budget_blocked': 0,
            'needs_review': 0, 'upload_unknown': 0, 'deleted': 0,
            'failed_records': 0, 'expired': 0, 'spool_blocked': 0, 'probe_tcp_open': 0, 'probe_error_type': None,
            'sd_backend': None, 'sd_searched': 0, 'sd_downloaded': 0, 'sd_deferred': 0,
            'sd_backlog': 0, 'sd_error_code': None,
            'sd_connect_seconds': 0.0, 'sd_search_seconds': 0.0,
            'sd_download_seconds': 0.0, 'sd_ingest_seconds': 0.0,
            'sd_download_bytes': 0, 'upload_seconds': 0.0}


class SyncQueue:
    def __init__(self, archive):
        self.archive = archive
        self.conn = archive.conn
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            self.conn.execute('''CREATE TABLE IF NOT EXISTS sync_jobs (
                id TEXT PRIMARY KEY, camera_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('queued','running','completed','blocked','failed')),
                phase TEXT NOT NULL, code TEXT NOT NULL, message TEXT NOT NULL,
                statistics_json TEXT NOT NULL, source TEXT NOT NULL, actor TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                started_at REAL, finished_at REAL
            )''')
            self.conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS one_active_camera_sync
                ON sync_jobs(camera_id) WHERE state IN ('queued','running')''')
            self.conn.execute('CREATE INDEX IF NOT EXISTS by_sync_created ON sync_jobs(created_at,id)')
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    @staticmethod
    def _camera_id(camera):
        if camera is not None and (not isinstance(camera, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,64}', camera) is None):
            raise ValueError('Invalid camera ID')

    @staticmethod
    def _origin(source, actor):
        if not isinstance(source, str) or re.fullmatch(r'[A-Za-z0-9_.-]{1,32}', source) is None:
            raise ValueError('Invalid sync source')
        if type(actor) is int and actor > 0:
            actor = str(actor)
        if actor is not None and (not isinstance(actor, str) or re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', actor) is None):
            raise ValueError('Invalid sync actor')
        return source, actor

    @staticmethod
    def _error_type(error):
        name = type(error).__name__
        return name if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', name) else 'Error'

    def _worker_alive(self):
        heartbeat = self.archive.settings.state_dir / 'heartbeat'
        try:
            return heartbeat.is_file() and time.time() - heartbeat.stat().st_mtime < max(120, self.archive.settings.interval * 4)
        except OSError:
            return False

    def _public(self, row):
        fields = ('id', 'camera_id', 'state', 'phase', 'code', 'message', 'source', 'actor',
                  'created_at', 'updated_at', 'started_at', 'finished_at')
        result = {field: row[field] for field in fields}
        result['camera_name'] = self.archive.camera_name(row['camera_id'])
        result['statistics'] = json.loads(row['statistics_json'])
        return result

    def _prune(self):
        # Active jobs are never removed by history pruning. The partial unique
        # index bounds active work for each camera and guards all processes.
        self.conn.execute('''DELETE FROM sync_jobs WHERE state IN ('completed','blocked','failed')
            AND id NOT IN (SELECT id FROM sync_jobs WHERE state IN ('completed','blocked','failed')
            ORDER BY created_at DESC,id DESC LIMIT ?)''', (MAX_HISTORY,))

    def enqueue(self, camera=None, source='dashboard', actor=None):
        self._camera_id(camera)
        source, actor = self._origin(source, actor)
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            if camera is None:
                cameras = self.conn.execute('SELECT id FROM cameras WHERE enabled=1 ORDER BY name COLLATE NOCASE,id').fetchall()
            else:
                selected = self.conn.execute('SELECT id,enabled FROM cameras WHERE id=?', (camera,)).fetchone()
                if selected is None:
                    raise KeyError('Unknown camera')
                if not selected['enabled']:
                    raise ValueError('Camera is disabled')
                cameras = [selected]
            self._prune()
            active = {row['camera_id']: row for row in self.conn.execute("SELECT * FROM sync_jobs WHERE state IN ('queued','running')")}
            additional = sum(row['id'] not in active for row in cameras)
            if len(active) + additional > MAX_ACTIVE_JOBS:
                raise ValueError('Sync queue is full')
            result = []
            now = time.time()
            for selected in cameras:
                slug = selected['id']
                row = active.get(slug)
                if row is None:
                    job_id = uuid.uuid4().hex
                    self.conn.execute('''INSERT INTO sync_jobs
                        (id,camera_id,state,phase,code,message,statistics_json,source,actor,created_at,updated_at)
                        VALUES (?,?,'queued','queued','','Đang chờ worker xử lý',?,?,?,?,?)''',
                        (job_id, slug, json.dumps(_statistics()), source, actor, now, now))
                    row = self.conn.execute('SELECT * FROM sync_jobs WHERE id=?', (job_id,)).fetchone()
                result.append(row)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {'jobs': [self._public(row) for row in result], 'worker_alive': self._worker_alive()}

    def status(self, camera=None, limit=20):
        self._camera_id(camera)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid sync history limit')
        where = ' WHERE camera_id=?' if camera is not None else ''
        parameters = (camera,) if camera is not None else ()
        rows = self.conn.execute('SELECT * FROM sync_jobs' + where + ' ORDER BY created_at DESC,id DESC LIMIT ?', (*parameters, limit)).fetchall()
        latest_rows = self.conn.execute('''SELECT j.* FROM sync_jobs j
            WHERE NOT EXISTS (SELECT 1 FROM sync_jobs newer WHERE newer.camera_id=j.camera_id
                AND (newer.created_at>j.created_at OR (newer.created_at=j.created_at AND newer.id>j.id)))''' +
            (' AND j.camera_id=?' if camera is not None else ''), parameters).fetchall()
        return {'jobs': [self._public(row) for row in rows],
                'latest': {row['camera_id']: self._public(row) for row in latest_rows},
                'worker_alive': self._worker_alive()}

    def recover(self):
        """Call only when the exclusive worker starts; do not reset recording states."""
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            count = self.conn.execute('''UPDATE sync_jobs SET state='queued',phase='queued',code='worker_restarted',
                message='Worker khởi động lại; công việc được xếp hàng để kiểm tra tiếp',
                statistics_json=?,updated_at=?,started_at=NULL,finished_at=NULL WHERE state='running' ''',
                (json.dumps(_statistics()), time.time())).rowcount
            self.conn.commit()
            return count
        except Exception:
            self.conn.rollback()
            raise

    def _claim(self):
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            row = self.conn.execute("SELECT * FROM sync_jobs WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                self.conn.commit()
                return None
            now = time.time()
            self.conn.execute('''UPDATE sync_jobs SET state='running',phase='probing',code='',
                message='Đang kiểm tra LAN camera',updated_at=?,started_at=?,statistics_json=? WHERE id=?''',
                (now, now, json.dumps(_statistics()), row['id']))
            self.conn.commit()
            return dict(row)
        except Exception:
            self.conn.rollback()
            raise

    def _camera(self, slug):
        return self.conn.execute('SELECT id,enabled,upload_enabled FROM cameras WHERE id=?', (slug,)).fetchone()

    def _counts(self, slug, statistics):
        now = time.time()
        row = self.conn.execute('''SELECT count(*) AS total,
            count(CASE WHEN deleted_at IS NULL AND media_expired_at IS NULL AND status='downloaded' AND coalesce(retry_at,0)<=? THEN 1 END) AS ready,
            count(CASE WHEN deleted_at IS NULL AND media_expired_at IS NULL AND status!='uploaded' THEN 1 END) AS pending,
            count(CASE WHEN deleted_at IS NULL AND media_expired_at IS NULL AND status='needs_review' THEN 1 END) AS needs_review,
            count(CASE WHEN deleted_at IS NULL AND media_expired_at IS NULL AND status IN ('upload_unknown','uploading') THEN 1 END) AS upload_unknown,
            count(CASE WHEN deleted_at IS NOT NULL THEN 1 END) AS deleted,
            count(CASE WHEN deleted_at IS NULL AND media_expired_at IS NULL AND status IN ('failed','ingesting') THEN 1 END) AS failed_records,
            count(CASE WHEN media_expired_at IS NOT NULL AND status!='uploaded' THEN 1 END) AS expired,
            count(CASE WHEN deleted_at IS NULL AND media_expired_at IS NULL AND status='downloaded' AND coalesce(retry_at,0)>? THEN 1 END) AS delayed
            FROM recordings WHERE camera=?''', (now,now,slug)).fetchone()
        for field in ('ready','pending','needs_review','upload_unknown','deleted','failed_records','expired'):
            statistics[field] = row[field]
        return row['total'], row['delayed']

    def _progress(self, job, statistics, phase, message):
        self._counts(job['camera_id'], statistics)
        with self.conn:
            self.conn.execute("UPDATE sync_jobs SET phase=?,message=?,statistics_json=?,updated_at=? WHERE id=? AND state='running'",
                              (phase, message, json.dumps(statistics), time.time(), job['id']))

    def _finish(self, job, statistics, state, code, message):
        self._counts(job['camera_id'], statistics)
        if statistics['probe_error_type']:
            message += ' Probe LAN chưa được xác nhận (' + statistics['probe_error_type'] + '); kết quả SD được ghi riêng.'
        now = time.time()
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            self.conn.execute('''UPDATE sync_jobs SET state=?,phase='finished',code=?,message=?,statistics_json=?,
                updated_at=?,finished_at=? WHERE id=? AND state='running' ''',
                (state, code, message, json.dumps(statistics), now, now, job['id']))
            self._prune()
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _gate(self, slug):
        camera = self._camera(slug)
        if camera is None or not camera['enabled']:
            return 'camera_disabled', 'Camera đã tắt; công việc dừng, video/cache vẫn được giữ'
        if not camera['upload_enabled']:
            return 'camera_upload_disabled', 'Upload Telegram của camera này đang tắt; video đã nhập được giữ trong cache'
        settings = self.archive.settings
        if not settings.enable_upload:
            return 'upload_disabled', 'Upload toàn cục đang tắt; video đã nhập được giữ trong cache'
        channel_mode = settings.telegram_destination=='channel' or bool(settings.storage_channel_id)
        if not settings.token:
            return 'telegram_not_configured', 'Chưa cấu hình bot token'
        if channel_mode:
            if type(settings.storage_channel_id) is not int or settings.storage_channel_id>=0:
                return 'channel_not_configured', 'House01 cần private storage channel có bot admin quyền đăng bài'
            return None
        if not settings.effective_owner:
            return 'telegram_not_configured', 'Chưa cấu hình bot token và ID owner hợp lệ'
        if self.archive.state(f'telegram_owner_started:{settings.effective_owner}') != '1':
            return 'owner_not_started', 'Owner cần mở bot và gửi /start trước khi upload'
        return None

    def run_once(self, telegram):
        job = self._claim()
        if job is None:
            return False
        statistics = _statistics()
        try:
            self._run(job, statistics, telegram)
        except Exception as error:
            self._finish(job, statistics, 'failed', 'sync_failed',
                         'Công việc gặp lỗi xử lý (' + self._error_type(error) + '); không tự gửi lại video mơ hồ')
        return True

    def _run(self, job, statistics, telegram):
        slug = job['camera_id']
        camera = self._camera(slug)
        if camera is None or not camera['enabled']:
            self._finish(job, statistics, 'blocked', 'camera_disabled', 'Camera đã tắt; không nhập hoặc upload video')
            return
        # probe_camera always performs new bounded LAN checks; no cached result
        # is treated as SD access. Offline LAN does not invalidate exported MP4s.
        try:
            probe = self.archive.probe_camera(slug)
            statistics['probe_tcp_open'] = sum(value == 'open' for value in probe.get('tcp', {}).values())
            if not statistics['probe_tcp_open']:
                statistics['probe_error_type'] = 'Unconfirmed'
        except Exception as error:
            statistics['probe_error_type'] = self._error_type(error)
        sd_error=None
        upload_paused=False
        upload_error_type=None
        def upload_ready():
            nonlocal upload_paused,upload_error_type
            if upload_paused or statistics['uploaded']>=MAX_UPLOADS_PER_JOB or self._gate(slug):
                return None
            try:
                started=time.monotonic()
                result=telegram.upload_one(self.archive,camera=slug)
            except Exception as error:
                upload_paused=True
                upload_error_type=self._error_type(error)
                return None
            finally:
                statistics['upload_seconds']+=round(time.monotonic()-started,6)
            if result=='uploaded':statistics['uploaded']+=1
            elif result=='upload_spool_budget':
                statistics['spool_blocked']=1
                upload_paused=True
            elif result is not None:upload_paused=True
            return result
        if self.archive.settings.media_mode=='remux_copy':
            # Migrate only confirmed unposted raw cache, one file at a time.
            # Heartbeat and Telegram polling already run in their own threads.
            for _ in range(MAX_UPLOADS_PER_JOB):
                current=self._camera(slug)
                if current is None or not current['enabled']:break
                self._progress(job,statistics,'remuxing','Đang chuyển container sang MP4 bằng stream-copy; không transcode')
                converted=self.archive.remux_pending_cache(camera=slug,limit=1)
                statistics['remuxed']+=converted['converted']
                statistics['remux_failed']+=converted['failed']
                statistics['remux_budget_blocked']+=converted.get('budget_blocked',0)
                if not (converted['converted'] or converted['failed']):break
                if converted['converted'] and not upload_paused and statistics['uploaded']<MAX_UPLOADS_PER_JOB and not self._gate(slug):
                    self._progress(job,statistics,'uploading','Đang upload MP4 vào storage channel')
                    upload_ready()
            if statistics['remux_budget_blocked']:
                self._finish(job,statistics,'blocked','remux_cache_budget',
                             'Cache cần thêm dung lượng để chuyển container MP4; file nguồn vẫn được giữ')
                return
        self._progress(job, statistics, 'sd_search', 'Đang tìm và tải recording SD của camera')
        try:
            from .sd_source import SDSource, SDSourceError
            last_imported=0
            def sd_progress(snapshot):
                nonlocal last_imported
                statistics['sd_backend']=snapshot.get('backend')
                for output,source in (('sd_searched','searched'),('sd_downloaded','downloaded'),
                                      ('sd_imported','imported'),('sd_deferred','deferred'),('sd_backlog','backlog')):
                    statistics[output]=snapshot.get(source,0)
                # Snapshots are cumulative, so do not add the same file twice.
                statistics['imported']=snapshot.get('imported',0)
                statistics['already_known']=snapshot.get('already_known',0)
                for field in ('connect_seconds','search_seconds','download_seconds','ingest_seconds','download_bytes'):
                    statistics['sd_'+field]=snapshot.get(field,0)
                phase=snapshot.get('phase','sd_download')
                if phase not in ('sd_search','sd_download','sd_complete'):phase='sd_download'
                self._progress(job,statistics,phase,'Đang xử lý recording SD; số liệu cập nhật sau từng file')
                # Upload each ready file without waiting for the whole SD batch.
                # A failed attempt pauses uploads across ALL phases of this job.
                imported=snapshot.get('imported',0)
                is_new=imported>last_imported
                last_imported=max(last_imported,imported)
                if is_new and not upload_paused and statistics['uploaded']<MAX_UPLOADS_PER_JOB and not self._gate(slug):
                    self._progress(job,statistics,'uploading','Đang upload video vừa tải từ SD')
                    upload_ready()
                    self._progress(job,statistics,phase,'Đã xử lý video; tiếp tục tải recording SD')
            sd_result=SDSource(self.archive,slug).sync(progress=sd_progress)
            sd_progress(sd_result)
        except Exception as error:
            # Provider messages are fixed and sanitized; unknown exceptions do
            # not leak credentials/paths through job status. Cache upload may
            # still proceed, but SD failure never becomes a successful sync.
            code=getattr(error,'code',None)
            if isinstance(code,str) and re.fullmatch(r'sd_[a-z0-9_]{1,48}',code):
                sd_error=(code, 'Nguồn SD chưa hoàn tất ('+code+'); kiểm tra cấu hình camera, SDK và tuyến mạng')
            else:
                sd_error=('sd_source_failed', 'Nguồn SD gặp lỗi ('+self._error_type(error)+'); chưa có xác nhận tải recording')
            statistics['sd_error_code']=sd_error[0]
        camera = self._camera(slug)
        if camera is None or not camera['enabled']:
            self._finish(job, statistics, 'blocked', 'camera_disabled', 'Camera đã tắt; dừng tải SD và giữ cache đã xác nhận')
            return
        self._progress(job, statistics, 'scanning', 'Đang kiểm tra MP4/manifest có sẵn của camera')
        existing = {row[0] for row in self.conn.execute('SELECT key FROM recordings WHERE camera=?', (slug,))}
        manifests = sorted(self.archive.settings.input_dir.glob('*.json'))
        scan_errors = []
        for path in manifests[:MAX_MANIFESTS_PER_JOB]:
            camera = self._camera(slug)
            if camera is None or not camera['enabled']:
                self._finish(job, statistics, 'blocked', 'camera_disabled', 'Camera đã tắt trong khi xử lý; nguồn/cache vẫn được giữ')
                return
            statistics['manifests'] += 1
            try:
                results = self.archive.ingest_manifest(path, continue_on_error=True, camera=slug)
            except Exception as error:
                statistics['failed'] += 1
                scan_errors.append(self._error_type(error))
                continue
            statistics['matched'] += len(results)
            for result in results:
                if result.get('media_expired_at') is not None:
                    statistics['already_known'] += 1
                elif result['status'] == 'failed':
                    statistics['failed'] += 1
                    scan_errors.append(result.get('error_type', 'Error'))
                elif result.get('key') in existing:
                    statistics['already_known'] += 1
                elif result['status'] == 'downloaded':
                    statistics['imported'] += 1
                    existing.add(result['key'])
            self._progress(job, statistics, 'scanning', 'Đang nhập nguồn MP4/manifest của camera')
        total, delayed = self._counts(slug, statistics)
        if statistics['failed']:
            types = ', '.join(sorted(set(scan_errors))[:3])
            self._finish(job, statistics, 'failed', 'scan_failed', 'Nguồn manifest/MP4 gặp lỗi (' + types + '); video hợp lệ đã nhập được giữ')
            return
        if len(manifests) > MAX_MANIFESTS_PER_JOB:
            self._finish(job, statistics, 'blocked', 'scan_batch_limit', 'Đã chạm giới hạn manifest mỗi lần; cần xử lý bớt nguồn trước khi đồng bộ tiếp')
            return
        if not total:
            if sd_error:self._finish(job,statistics,'blocked',*sd_error)
            else:self._finish(job,statistics,'completed','no_new_recordings','Camera không trả recording SD mới trong khoảng tìm kiếm')
            return
        if not statistics['pending']:
            if sd_error:self._finish(job,statistics,'blocked',*sd_error)
            elif statistics['sd_backlog']:self._finish(job,statistics,'blocked','sd_batch_limit','Nguồn SD còn recording; worker sẽ xử lý trong lần đồng bộ tiếp')
            elif statistics['uploaded']:self._finish(job,statistics,'completed','completed','Đã tải và upload file gốc của camera')
            else:self._finish(job, statistics, 'completed', 'no_new_recordings', 'Không có video mới cần upload; các bản ghi đã lưu giữ nguyên')
            return
        self._progress(job, statistics, 'uploading', 'Đang xử lý hàng đợi upload của camera')
        for _ in range(max(0,MAX_UPLOADS_PER_JOB-statistics['uploaded'])):
            if upload_paused:break
            gate = self._gate(slug)
            if gate:
                self._finish(job, statistics, 'blocked', *gate)
                return
            _, delayed = self._counts(slug, statistics)
            if not statistics['ready']:
                break
            result=upload_ready()
            if result == 'uploaded':
                self._progress(job, statistics, 'uploading', 'Đang upload các video sẵn sàng của camera')
            else:
                break
        _, delayed = self._counts(slug, statistics)
        gate = self._gate(slug) if statistics['pending'] else None
        if upload_error_type:
            self._finish(job, statistics, 'failed', 'upload_failed',
                         'Upload gặp lỗi (' + upload_error_type + '); cần kiểm tra trạng thái trước khi thử lại')
        elif gate:
            self._finish(job, statistics, 'blocked', *gate)
        elif statistics['upload_unknown']:
            self._finish(job, statistics, 'blocked', 'upload_unknown', 'Có video upload chưa được xác nhận; cần đối soát, không tự gửi lại')
        elif statistics['needs_review']:
            self._finish(job, statistics, 'blocked', 'needs_review', 'Có video cần kiểm tra nguồn hoặc trạng thái Telegram trước khi xử lý tiếp')
        elif statistics['remux_budget_blocked']:
            self._finish(job, statistics, 'blocked', 'remux_cache_budget', 'Cache cần thêm dung lượng để chuyển container MP4; file nguồn vẫn được giữ')
        elif statistics['failed_records']:
            self._finish(job, statistics, 'blocked', 'needs_review', 'Có video cần kiểm tra nguồn hoặc trạng thái Telegram trước khi xử lý tiếp')
        elif statistics['spool_blocked'] or self.conn.execute("SELECT 1 FROM recordings WHERE camera=? AND status='downloaded' AND deleted_at IS NULL AND media_expired_at IS NULL AND last_error='upload_spool_budget' AND retry_at>? LIMIT 1",(slug,time.time())).fetchone():
            self._finish(job, statistics, 'blocked', 'upload_spool_budget', 'Vùng tạm upload cần dung lượng; giữ video và thử lại sau')
        elif delayed:
            self._finish(job, statistics, 'blocked', 'rate_limited', 'Telegram đang giới hạn tốc độ; video được giữ để thử sau thời gian retry')
        elif statistics['ready'] and statistics['uploaded'] >= MAX_UPLOADS_PER_JOB:
            self._finish(job, statistics, 'blocked', 'upload_batch_limit', 'Đã upload tối đa 100 video cho công việc; đồng bộ lại để xử lý phần còn lại')
        elif statistics['pending']:
            self._finish(job, statistics, 'blocked', 'upload_not_ready', 'Video còn chờ xử lý; chưa có xác nhận upload hoàn tất')
        elif sd_error:
            self._finish(job,statistics,'blocked',*sd_error)
        elif statistics['sd_backlog']:
            self._finish(job,statistics,'blocked','sd_batch_limit','Đã xử lý batch SD; nguồn còn recording cho lần đồng bộ tiếp')
        else:
            self._finish(job, statistics, 'completed', 'completed', 'Đã xử lý nguồn SD/MP4/manifest và upload video của camera')
