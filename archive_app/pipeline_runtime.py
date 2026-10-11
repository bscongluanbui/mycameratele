"""Process-local coordination for independent camera download workers.

SQLite still owns durable job/recording claims. These guards only coordinate
resources shared by this one archive process; the runner's file lock prevents
another archive process from competing for the same cache.
"""
from contextlib import contextmanager
from pathlib import Path
import shutil
import stat
import threading


_UPLOAD_LOCK = threading.RLock()
_CACHE_LOCK = threading.RLock()
_CACHE_RESERVATIONS = {}
_THREAD_RESERVATIONS = threading.local()


class SerializedUploads:
    """Serialize media claims through response/cleanup, not bot navigation."""

    def __init__(self, telegram, lock=None, *, stop_event=None):
        self._telegram = telegram
        self._lock = lock if lock is not None else _UPLOAD_LOCK
        self._stop_event = stop_event

    def _stopping(self):
        return self._stop_event is not None and self._stop_event.is_set()

    def upload_one(self, *args, **kwargs):
        if self._stopping():
            return None
        with self._lock:
            # A worker can have waited behind another camera's active upload
            # when shutdown began. Only the already-started upload may drain;
            # a waiter must not claim/send another recording after it acquires.
            if self._stopping():
                return None
            return self._telegram.upload_one(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._telegram, name)


def serialized_uploads(telegram, *, stop_event=None):
    if isinstance(telegram, SerializedUploads) and (
            stop_event is None or telegram._stop_event is stop_event):
        return telegram
    return SerializedUploads(telegram, stop_event=stop_event)


def cache_used_bytes(root):
    """Count regular cache files, tolerating only concurrent removal."""
    root = Path(root)
    total = 0
    for path in root.rglob('*'):
        try:
            info = path.stat(follow_symlinks=False)
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
        except FileNotFoundError:
            # Confirmed uploads may remove their files during this walk.
            continue
    return total


@contextmanager
def reserve_cache(settings, required_bytes, directory=None):
    """Yield admission and retain an aggregate reservation for the context.

    An outer SD reservation includes both raw download and MP4 construction.
    Nested ingest in the same thread reuses it, rather than counting that same
    file twice. Larger nested requirements reserve only the additional bytes.
    Actual cache usage plus reservations is intentionally conservative.
    """
    if type(required_bytes) is not int or required_bytes < 0:
        raise ValueError('Cache reservation must be a non-negative integer')
    root = Path(settings.cache_dir).resolve()
    target = root if directory is None else Path(directory).resolve()
    if not target.is_relative_to(root):
        raise ValueError('Cache reservation directory escaped cache')
    key = str(root)
    held = getattr(_THREAD_RESERVATIONS, 'held', None)
    if held is None:
        held = _THREAD_RESERVATIONS.held = {}
    previous = held.get(key, 0)
    extra = max(0, required_bytes - previous)
    admitted = False
    with _CACHE_LOCK:
        outstanding = _CACHE_RESERVATIONS.get(key, 0)
        if not extra:
            admitted = True
        else:
            used = cache_used_bytes(root)
            free = shutil.disk_usage(target).free
            admitted = (used + outstanding + extra <= settings.cache_max_bytes
                        and free - settings.min_free_bytes - outstanding >= extra)
        if admitted:
            _CACHE_RESERVATIONS[key] = outstanding + extra
            held[key] = previous + extra
    try:
        yield admitted
    finally:
        if admitted:
            with _CACHE_LOCK:
                remaining = _CACHE_RESERVATIONS[key] - extra
                if remaining:
                    _CACHE_RESERVATIONS[key] = remaining
                else:
                    del _CACHE_RESERVATIONS[key]
                if previous:
                    held[key] = previous
                else:
                    held.pop(key, None)


class PipelineRuntime:
    """Bounded camera workers; every worker creates/closes its own SQLite DB."""

    def __init__(self, settings, *, stop_event=None, emit=None,
                 archive_factory=None, queue_factory=None, telegram_factory=None):
        from .core import Archive
        from .sync import SyncQueue
        from .telegram import Telegram
        count = getattr(settings, 'sd_download_workers', 1)
        if type(count) is not int or not 1 <= count <= 5:
            raise ValueError('SD download workers must be between 1 and 5')
        self.settings = settings
        self.count = count
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.emit = emit if emit is not None else lambda *args, **kwargs: None
        self.archive_factory = archive_factory or Archive
        self.queue_factory = queue_factory or SyncQueue
        self.telegram_factory = telegram_factory or Telegram
        self._wake = threading.Event()
        self._threads = []
        self._started = False
        self._failed = threading.Event()
        self._admission = threading.Lock()
        self._active = 0

    def set_limit(self, count):
        """A lower limit drains admitted jobs; it never interrupts a file."""
        if type(count) is not int or not 1 <= count <= 5:
            raise ValueError('SD download workers must be between 1 and 5')
        with self._admission:
            changed = self.count != count
            self.count = count
        if changed:
            self.wake()
        return changed

    def _admit(self, archive):
        with self._admission:
            # Each active worker reads on its own connection before admitting
            # the next job. Dormant workers use the main loop's live snapshot.
            if archive is not None and hasattr(archive, 'pipeline_settings'):
                count = archive.pipeline_settings()['sd_download_workers']
                if type(count) is not int or not 1 <= count <= 5:
                    raise ValueError('Invalid persistent camera worker limit')
                self.count = count
            if self.stop_event.is_set() or self._active >= self.count:
                return False
            self._active += 1
            return True

    def _confirm_admission(self, archive):
        # A newly activated slot did not yet own a connection at _admit().
        # Recheck persistence after opening it, before claiming any job.
        with self._admission:
            if hasattr(archive, 'pipeline_settings'):
                count = archive.pipeline_settings()['sd_download_workers']
                if type(count) is not int or not 1 <= count <= 5:
                    raise ValueError('Invalid persistent camera worker limit')
                self.count = count
            return not self.stop_event.is_set() and self._active <= self.count

    def _worker(self, number):
        archive = None
        initialized = False
        try:
            # sqlite3 connections must not cross thread boundaries. Schema and
            # crash recovery were already completed once by the main runner.
            while not self.stop_event.is_set():
                admitted = False
                try:
                    admitted = self._admit(archive)
                    if admitted:
                        if archive is None:
                            archive = self.archive_factory(self.settings)
                            queue = self.queue_factory(archive, stop_event=self.stop_event)
                            telegram = serialized_uploads(self.telegram_factory(self.settings),
                                                          stop_event=self.stop_event)
                            initialized = True
                        if self._confirm_admission(archive) and queue.run_once(telegram):
                            self.emit('sync_job_finished', worker=number)
                            continue
                except Exception as error:
                    if not initialized:
                        raise
                    self.emit('sync_error', worker=number, error_type=type(error).__name__)
                finally:
                    if admitted:
                        with self._admission:
                            self._active -= 1
                # Bound latency for jobs queued by dashboard/bot as well as the
                # scheduler; do not busy-spin when there is no queued work.
                self._wake.wait(0.5)
                self._wake.clear()
        except Exception as error:
            self.emit('sync_worker_error', worker=number, error_type=type(error).__name__)
            self._failed.set()
            self.stop()
        finally:
            if archive is not None:
                archive.close()

    def start(self):
        if self._started:
            raise RuntimeError('Camera download workers already started')
        self._started = True
        try:
            # Stable maximum-size executor enables 1 -> 5 without a restart.
            # Connections remain lazy: dormant slots own no SQLite resources.
            for number in range(1, 6):
                thread = threading.Thread(target=self._worker, args=(number,),
                                          name='camera-download-' + str(number), daemon=False)
                thread.start()
                self._threads.append(thread)
        except Exception:
            self.stop()
            self.join()
            raise
        return self

    @property
    def failed(self):
        return self._failed.is_set()

    def wake(self):
        self._wake.set()

    def stop(self):
        self.stop_event.set()
        self._wake.set()

    def join(self):
        # The in-flight file may take longer than an HTTP request. Keep the
        # process lock until it completes, commits, and closes its SDK child.
        for thread in self._threads:
            thread.join()
