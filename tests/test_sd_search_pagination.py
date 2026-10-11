"""Silent device caps must not hide newer SD footage or busy-camera history."""
import ctypes as C
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import uuid

from archive_app.core import Archive, Settings, get_zone, parse_time, record_key
from archive_app.sd_source import (DVRTime, FileCondition, FindData, MAX_SEARCH_RESULTS,
                                  SDSource, SDSourceError, _NativeSession)


NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


def files(count, interval=60):
    beginning = NOW-timedelta(days=5)
    return [{'record_id': f'file-{number:05d}', 'start_time': (beginning+timedelta(seconds=number*interval)).isoformat(),
             'end_time': (beginning+timedelta(seconds=number*interval+1)).isoformat(), 'size': 8}
            for number in range(count)]


class Function:
    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


class CappedSDK:
    """Models a camera that says NOMORE after its first 500 matches."""
    def __init__(self, records, newest=False, ignore_window=False):
        self.records, self.newest, self.ignore_window = records, newest, ignore_window
        self.queries, self.closed = [], []
        self.pages, self.next_handle = {}, 1
        for name in ('NET_DVR_Init', 'NET_DVR_Cleanup', 'NET_DVR_Logout',
                     'NET_DVR_GetFileByName', 'NET_DVR_PlayBackControl_V40',
                     'NET_DVR_GetDownloadPos', 'NET_DVR_StopGetFile'):
            setattr(self, name, Function(lambda *args: 1))
        self.NET_DVR_GetLastError = Function(lambda: 0)
        self.NET_DVR_Login_V30 = Function(lambda *args: 7)
        self.NET_DVR_FindFile_V30 = Function(self.find)
        self.NET_DVR_FindNextFile_V30 = Function(self.next_file)
        self.NET_DVR_FindClose_V30 = Function(lambda handle: self.closed.append(handle) or 1)

    def find(self, user, pointer):
        condition = FileCondition.from_buffer_copy(C.string_at(pointer, C.sizeof(FileCondition)))
        low, high = condition.start.as_datetime(timezone.utc), condition.end.as_datetime(timezone.utc)
        self.queries.append((low, high))
        matched = [item for item in self.records if self.ignore_window or
                   parse_time(item['start_time']) <= high and parse_time(item['end_time']) >= low]
        matched.sort(key=lambda item: (item['start_time'], item['record_id']), reverse=self.newest)
        handle, self.next_handle = self.next_handle, self.next_handle+1
        self.pages[handle] = iter(matched[:500])
        return handle

    def next_file(self, handle, pointer):
        try:
            item = next(self.pages[handle])
        except StopIteration:
            return 1003
        value = C.cast(pointer, C.POINTER(FindData)).contents
        value.filename = item['record_id'].encode()
        value.start = DVRTime.from_datetime(parse_time(item['start_time']))
        value.end = DVRTime.from_datetime(parse_time(item['end_time']))
        value.size = item['size']
        return 1000


class NativeSearchPaginationTests(unittest.TestCase):
    def run_search(self, records, **options):
        sdk = CappedSDK(records, **options)
        provider = _NativeSession('192.168.2.7', 8000, 'admin', 'fixture', 1,
                                  get_zone('UTC'), 1024, sdk=(sdk, Path('.')))
        with provider:
            result = provider.search(NOW-timedelta(days=7), NOW-timedelta(seconds=120))
        self.assertEqual(len(sdk.queries), len(sdk.closed))
        return result, sdk

    def test_silent_500_cap_lists_newer_records(self):
        records = files(1300)
        result, sdk = self.run_search(records)
        self.assertEqual([item['record_id'] for item in result], [item['record_id'] for item in records])
        self.assertGreater(len(sdk.queries), 1)

    def test_newest_first_device_still_returns_oldest_prefix(self):
        records = files(4300)
        result, _ = self.run_search(records, newest=True)
        self.assertEqual(len(result), MAX_SEARCH_RESULTS)
        self.assertEqual([item['record_id'] for item in result], [item['record_id'] for item in records[:MAX_SEARCH_RESULTS]])

    def test_overlapping_window_boundary_record_is_not_duplicated(self):
        records = files(700)
        records[350]['end_time'] = (parse_time(records[350]['start_time'])+timedelta(hours=1)).isoformat()
        result, _ = self.run_search(records)
        self.assertEqual(len(result), len(records))
        self.assertEqual(len({(item['record_id'], item['start_time']) for item in result}), len(records))

    def test_exact_500_complete_results_are_not_lost(self):
        records = files(500)
        result, _ = self.run_search(records)
        self.assertEqual(len(result), 500)

    def test_device_ignoring_time_window_fails_instead_of_false_complete(self):
        with self.assertRaises(SDSourceError) as caught:
            self.run_search(files(500), ignore_window=True)
        self.assertEqual(caught.exception.code, 'sd_search_limit')

    def test_query_budget_is_bounded_and_closes_handles(self):
        with patch('archive_app.sd_source.MAX_NATIVE_SEARCH_QUERIES', 1), self.assertRaises(SDSourceError) as caught:
            self.run_search(files(700))
        self.assertEqual(caught.exception.code, 'sd_search_limit')

    def test_total_deadline_applies_even_with_successful_find_results(self):
        sdk = CappedSDK(files(700))
        provider = _NativeSession('192.168.2.7', 8000, 'admin', 'fixture', 1,
                                  get_zone('UTC'), 1024, sdk=(sdk, Path('.')))
        with provider, patch('archive_app.sd_source.time.monotonic', side_effect=[0, 60, 121]), self.assertRaises(SDSourceError) as caught:
            provider.search(NOW-timedelta(days=7), NOW)
        self.assertEqual(caught.exception.code, 'sd_search_timeout')
        self.assertEqual(len(sdk.queries), len(sdk.closed))

    def test_changed_metadata_across_overlapping_windows_is_rejected(self):
        records = files(700)
        records[0]['end_time'] = (NOW-timedelta(days=3)).isoformat()
        sdk = CappedSDK(records)
        original, occurrences = sdk.next_file, []
        def next_file(handle, pointer):
            status = original(handle, pointer)
            value = C.cast(pointer, C.POINTER(FindData)).contents
            if status == 1000 and bytes(value.filename) == b'file-00000':
                occurrences.append(handle)
                value.size = 8+len(occurrences)
            return status
        sdk.NET_DVR_FindNextFile_V30 = Function(next_file)
        provider = _NativeSession('192.168.2.7', 8000, 'admin', 'fixture', 1,
                                  get_zone('UTC'), 1024, sdk=(sdk, Path('.')))
        with provider, self.assertRaises(SDSourceError) as caught:
            provider.search(NOW-timedelta(days=7), NOW)
        self.assertEqual(caught.exception.code, 'sd_protocol_error')
        self.assertEqual(len(sdk.queries), len(sdk.closed))


class PrefixProvider:
    backend = 'hcnetsdk-collector'

    def __init__(self, records, cap):
        self.records, self.cap = records, cap
        self.queries, self.downloads, self.canonical = [], [], set()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def search(self, low, high):
        self.queries.append((low, high))
        records = [item for item in self.records if parse_time(item['start_time']) >= low and parse_time(item['start_time']) <= high][:self.cap]
        self.canonical = {item['record_id'] for item in records}
        return records

    def download(self, item, path):
        if item['record_id'] not in self.canonical:
            raise AssertionError('Download lost its final-search canonical metadata')
        self.downloads.append(item['record_id'])
        path.write_bytes(b'fixture8')


class KnownPrefixContinuationTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).parent if os.name == 'nt' else Path(tempfile.gettempdir())
        self.root = parent.resolve()/('.tmp-sd-search-'+uuid.uuid4().hex)
        self.root.mkdir()
        self.archive = Archive(Settings(self.root/'state', self.root/'cache', self.root/'input', 'UTC',
                                        min_free_bytes=0, cache_max_bytes=1024*1024))
        self.archive.add_camera({'id': 'middle', 'name': 'Middle', 'host': '192.168.2.7'})
        self.config = {'id': 'middle', 'host': '192.168.2.7', 'sd_backend': 'hcnetsdk',
                       'sd_username': 'admin', 'sd_password': 'fixture', 'sd_timezone': 'UTC'}

    def tearDown(self):
        self.archive.close()
        shutil.rmtree(self.root)

    def seed(self, records, status='uploaded'):
        with self.archive.conn:
            for item in records:
                first, last = parse_time(item['start_time']), parse_time(item['end_time'])
                rid = 'ch1:'+first.isoformat(timespec='seconds').replace('+00:00', 'Z')
                key = record_key({'camera': 'middle', 'source': 'camera-sd', 'record_id': rid})
                self.archive.conn.execute('''INSERT INTO recordings
                    (key,camera,record_id,start_ms,end_ms,source_path,status,created_at)
                    VALUES(?,?,?,?,?,?,?,?)''', (key, 'middle', rid, int(first.timestamp()*1000),
                                               int(last.timestamp()*1000), '', status, 1))

    def normalize(self, source, destination, settings):
        destination.write_bytes(Path(source).read_bytes())
        return {'duration': 1, 'bytes': 8, 'codec_video': 'h264', 'codec_audio': None}

    def sync(self, provider, threads=1, **kwargs):
        config = dict(self.config, sd_download_threads=threads)
        with patch.object(SDSource, '_provider', return_value=provider), patch('archive_app.core.normalize', side_effect=self.normalize):
            return SDSource(self.archive, 'middle', config).sync(now=NOW, **kwargs)

    def test_more_than_4000_known_records_do_not_starve_newer_history(self):
        records = files(4003)
        self.seed(records[:4000])
        provider = PrefixProvider(records, 4000)
        result = self.sync(provider)
        self.assertEqual(result['downloaded'], 3)
        self.assertEqual(result['already_known'], 4000)
        self.assertEqual(result['searched'], 4003)
        self.assertEqual(provider.downloads, [item['record_id'] for item in records[4000:]])
        self.assertEqual(len(provider.queries), 2)

    def test_full_unknown_prefix_keeps_backlog_continuation_sentinel(self):
        records = files(5)
        self.seed(records[:3])
        provider = PrefixProvider(records, 4)
        with patch('archive_app.sd_source.MAX_SEARCH_RESULTS', 4):
            first = self.sync(provider)
            self.assertEqual((first['downloaded'], first['backlog']), (1, 1))
            second = self.sync(provider)
        self.assertEqual((second['downloaded'], second['already_known'], second['backlog']), (1, 4, 0))
        self.assertEqual(provider.downloads, ['file-00003', 'file-00004'])

    def test_unknown_old_gap_is_not_skipped_for_newer_records(self):
        records = files(7)
        self.seed(records[:1]+records[2:4])
        provider = PrefixProvider(records, 4)
        with patch('archive_app.sd_source.MAX_SEARCH_RESULTS', 4):
            result = self.sync(provider)
        self.assertEqual(provider.downloads, ['file-00001'])
        self.assertEqual(result['backlog'], 1)
        self.assertEqual(len(provider.queries), 1)

    def test_uncertain_rows_are_known_and_never_resent(self):
        records = files(6)
        self.seed(records[:4], status='upload_unknown')
        provider = PrefixProvider(records, 4)
        with patch('archive_app.sd_source.MAX_SEARCH_RESULTS', 4):
            result = self.sync(provider)
        self.assertEqual(result['already_known'], 4)
        self.assertEqual(provider.downloads, ['file-00004', 'file-00005'])

    def test_changed_closed_end_in_full_prefix_blocks_advancement(self):
        records = files(6)
        self.seed(records[:4])
        records[0]['end_time'] = (parse_time(records[0]['end_time'])+timedelta(seconds=1)).isoformat()
        provider = PrefixProvider(records, 4)
        with patch('archive_app.sd_source.MAX_SEARCH_RESULTS', 4), self.assertRaises(SDSourceError) as caught:
            self.sync(provider)
        self.assertEqual(caught.exception.code, 'sd_record_changed')
        self.assertEqual(provider.downloads, [])

    def test_full_known_prefix_without_cursor_advance_is_rejected(self):
        records = files(4)
        self.seed(records)
        provider = PrefixProvider(records, 4)
        provider.search = lambda *_: records
        with patch('archive_app.sd_source.MAX_SEARCH_RESULTS', 4), self.assertRaises(SDSourceError) as caught:
            self.sync(provider)
        self.assertEqual(caught.exception.code, 'sd_search_limit')
        self.assertEqual(provider.downloads, [])

    def test_parallel_download_sessions_share_final_search_canonical_prefix(self):
        records = files(7)
        self.seed(records[:4])
        shared, created, downloads = {'canonical': set()}, [], []
        class SharedProvider(PrefixProvider):
            def search(self, low, high):
                result = super().search(low, high)
                shared['canonical'] = self.canonical
                return result
            def download(self, item, path):
                self.canonical = shared['canonical']
                super().download(item, path)
                downloads.append(item['record_id'])
        def factory(source):
            provider = SharedProvider(records, 4)
            created.append(provider)
            return provider
        config = dict(self.config, sd_download_threads=2)
        with patch('archive_app.sd_source.MAX_SEARCH_RESULTS', 4), patch.object(SDSource, '_provider', factory), patch('archive_app.core.normalize', side_effect=self.normalize):
            result = SDSource(self.archive, 'middle', config).sync(now=NOW)
        self.assertEqual((result['downloaded'], result['already_known']), (3, 4))
        self.assertEqual(set(downloads), {item['record_id'] for item in records[4:]})
        self.assertEqual(len(created), 3)
        self.assertEqual(len(created[0].queries), 2)


if __name__ == '__main__':
    unittest.main()
