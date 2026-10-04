"""Disk-spool admission fixtures: no HTTP traffic, no live Bot API state."""
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from archive_app.upload_spool import check_upload_spool, SpoolBudgetError
from archive_app.sync import SyncQueue
from tests import test_telegram as fixtures


class UploadSpoolTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parent if os.name == 'nt' else Path(tempfile.gettempdir()).resolve()
        self.root = parent / ('.tmp-spool-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.spool = self.root / 'spool'
        self.spool.mkdir()
        self.settings = SimpleNamespace(api_mode='local', bot_api_spool_root=self.spool,
                                        bot_api_spool_max_bytes=100, min_free_bytes=0)

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_counts_nested_regular_files_without_changing_them(self):
        nested = self.spool / 'temp'
        nested.mkdir()
        a, b = self.spool / 'a', nested / 'b'
        a.write_bytes(b'abc')
        b.write_bytes(b'1234567')
        result = check_upload_spool(self.settings, 90)
        self.assertEqual(result['used_bytes'], 10)
        self.assertEqual(result['budget_bytes'], 100)
        self.assertEqual(a.read_bytes(), b'abc')
        self.assertEqual(b.read_bytes(), b'1234567')

    def test_rejects_aggregate_budget_before_any_write(self):
        (self.spool / 'active').write_bytes(b'x' * 11)
        with self.assertRaisesRegex(SpoolBudgetError, '^upload_spool_budget$'):
            check_upload_spool(self.settings, 90)
        self.assertEqual((self.spool / 'active').stat().st_size, 11)

    def test_includes_configured_disk_free_reserve(self):
        self.settings.min_free_bytes = 10
        with patch('archive_app.upload_spool._used_bytes', return_value=(0, 99)):
            with self.assertRaises(SpoolBudgetError):
                check_upload_spool(self.settings, 90)
        with patch('archive_app.upload_spool._used_bytes', return_value=(0, 100)):
            self.assertEqual(check_upload_spool(self.settings, 90)['free_bytes'], 100)

    def test_missing_root_and_file_root_are_unsent_errors(self):
        self.settings.bot_api_spool_root = self.root / 'missing'
        with self.assertRaises(SpoolBudgetError):
            check_upload_spool(self.settings, 1)
        self.settings.bot_api_spool_root = self.root / 'not-directory'
        self.settings.bot_api_spool_root.write_bytes(b'x')
        with self.assertRaises(SpoolBudgetError):
            check_upload_spool(self.settings, 1)

    def test_unreadable_spool_error_is_sanitized(self):
        with patch('archive_app.upload_spool._used_bytes', side_effect=PermissionError('fixture-private-path')):
            with self.assertRaisesRegex(SpoolBudgetError, '^upload_spool_budget$'):
                check_upload_spool(self.settings, 1)

    def test_cloud_and_legacy_local_bypass_optional_spool(self):
        self.settings.bot_api_spool_root = self.root / 'missing'
        self.settings.api_mode = 'cloud'
        self.assertIsNone(check_upload_spool(self.settings, 1))
        self.settings.api_mode = 'local'
        self.settings.bot_api_spool_root = None
        self.assertIsNone(check_upload_spool(self.settings, 1))

    def test_validates_integer_byte_budget(self):
        for value in (0, -1, True, 1.2, 101):
            with self.subTest(incoming=value), self.assertRaises(SpoolBudgetError):
                check_upload_spool(self.settings, value)

    def _symlink(self, source, target):
        try:
            source.symlink_to(target, target_is_directory=target.is_dir())
        except (OSError, NotImplementedError):
            self.skipTest('Host does not support fixture symlinks')

    def test_rejects_symlink_file_without_following_external_content(self):
        outside = self.root / 'outside'
        outside.write_bytes(b'private fixture')
        self._symlink(self.spool / 'link', outside)
        with self.assertRaises(SpoolBudgetError):
            check_upload_spool(self.settings, 1)
        self.assertEqual(outside.read_bytes(), b'private fixture')

    def test_rejects_symlink_root_and_ancestor(self):
        link = self.root / 'alias'
        self._symlink(link, self.spool)
        for path in (link, link / 'nested'):
            (self.spool / 'nested').mkdir(exist_ok=True)
            self.settings.bot_api_spool_root = path
            with self.subTest(path=path), self.assertRaises(SpoolBudgetError):
                check_upload_spool(self.settings, 1)

    def test_ignores_files_unlinked_by_bot_api_during_scan(self):
        vanished = SimpleNamespace(stat=lambda **_: (_ for _ in ()).throw(FileNotFoundError()))
        class Entries:
            def __enter__(self): return iter([vanished])
            def __exit__(self, *args): return False
        with patch('archive_app.upload_spool.os.scandir', return_value=Entries()):
            self.assertEqual(check_upload_spool(self.settings, 90)['used_bytes'], 0)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX-only special-file fixture')
    def test_rejects_nonregular_spool_entry(self):
        os.mkfifo(self.spool / 'fifo')
        with self.assertRaises(SpoolBudgetError):
            check_upload_spool(self.settings, 1)


class TelegramSpoolTests(unittest.TestCase):
    # Reuse only fixture helpers, not the parent's full discovered test suite.
    fake_normalize = fixtures.TelegramTests.fake_normalize
    ingest = fixtures.TelegramTests.ingest
    row = fixtures.TelegramTests.row

    def setUp(self):
        fixtures.TelegramTests.setUp(self)
        self.spool = self.root / 'spool'
        self.spool.mkdir()
        self.settings.api_mode = 'local'
        self.settings.bot_api_spool_root = self.spool
        self.settings.bot_api_spool_max_bytes = 100
        self.request.return_value = {'chat': {'id': 42, 'type': 'private'}, 'message_id': 17,
                                     'document': {'file_id': 'fixture-id', 'file_unique_id': 'fixture-unique'}}

    def tearDown(self):
        fixtures.TelegramTests.tearDown(self)

    def test_full_spool_requeues_unsent_upload_and_preserves_cache(self):
        key = self.ingest()
        path = Path(self.row(key)['local_path'])
        (self.spool / 'active').write_bytes(b'x' * 90)
        with patch('archive_app.telegram.time.time', return_value=2000000000):
            self.assertEqual(self.telegram.upload_one(self.archive), 'upload_spool_budget')
        row = self.row(key)
        self.assertEqual(row['status'], 'downloaded')
        self.assertEqual(row['retry_at'], 2000000060)
        self.assertEqual(row['last_error'], 'upload_spool_budget')
        self.assertIsNone(row['file_id'])
        self.assertTrue(path.is_file())
        self.assertEqual((self.spool / 'active').stat().st_size, 90)
        self.request.assert_not_called()

    def test_unavailable_spool_is_known_unsent_not_upload_unknown(self):
        key = self.ingest()
        self.settings.bot_api_spool_root = self.root / 'missing'
        self.assertEqual(self.telegram.upload_one(self.archive), 'upload_spool_budget')
        self.assertEqual(self.row(key)['status'], 'downloaded')
        self.request.assert_not_called()

    def test_room_available_uploads_without_deleting_api_spool(self):
        key = self.ingest()
        active = self.spool / 'other-request'
        active.write_bytes(b'active')
        self.assertEqual(self.telegram.upload_one(self.archive), 'uploaded')
        self.assertEqual(self.row(key)['status'], 'uploaded')
        self.assertEqual(active.read_bytes(), b'active')
        self.assertEqual(self.request.call_args.args[0], 'sendDocument')

    def test_cloud_upload_does_not_depend_on_local_spool(self):
        key = self.ingest()
        self.settings.api_mode = 'cloud'
        self.settings.bot_api_spool_root = self.root / 'missing'
        self.assertEqual(self.telegram.upload_one(self.archive), 'uploaded')
        self.assertEqual(self.row(key)['status'], 'uploaded')

    def test_container_validation_precedes_spool_admission(self):
        key = self.ingest()
        self.settings.media_mode = 'remux_copy'
        with patch('archive_app.telegram.check_upload_spool') as check:
            self.assertEqual(self.telegram.upload_one(self.archive), 'needs_review')
        self.assertEqual(self.row(key)['last_error'], 'mp4_remux_required')
        check.assert_not_called()
        self.request.assert_not_called()

    def test_retry_after_capacity_released_sends_once(self):
        key = self.ingest()
        active = self.spool / 'active'
        active.write_bytes(b'x' * 90)
        with patch('archive_app.telegram.time.time', return_value=2000000000):
            self.assertEqual(self.telegram.upload_one(self.archive), 'upload_spool_budget')
        active.unlink()
        with patch('archive_app.telegram.time.time', return_value=2000000061):
            self.assertEqual(self.telegram.upload_one(self.archive), 'uploaded')
        self.assertEqual(self.row(key)['status'], 'uploaded')
        self.assertEqual(self.request.call_count, 1)

    def test_sync_status_classifies_spool_block_and_delayed_retry_not_rate_limit(self):
        key = self.ingest()
        (self.spool / 'active').write_bytes(b'x' * 90)
        queue = SyncQueue(self.archive)
        camera = 'Front_Camera'
        snapshot = {'backend': 'hcnetsdk', 'searched': 0, 'downloaded': 0,
                    'imported': 0, 'already_known': 0, 'deferred': 0, 'backlog': 0}
        with (patch.object(self.archive, 'probe_camera', return_value={'tcp': {'8000': 'open'}}),
              patch('archive_app.sd_source.SDSource.sync', return_value=snapshot),
              patch('archive_app.telegram.time.time', return_value=2000000000) as clock):
            queue.enqueue(camera)
            self.assertTrue(queue.run_once(self.telegram))
            job = queue.status(camera)['latest'][camera]
            self.assertEqual((job['state'], job['code']), ('blocked', 'upload_spool_budget'))
            self.assertEqual(job['statistics']['spool_blocked'], 1)
            self.assertEqual(self.row(key)['status'], 'downloaded')
            self.assertTrue(Path(self.row(key)['local_path']).is_file())
            self.request.assert_not_called()
            # A fresh job while retry_at is pending also keeps the correct code,
            # even though this job never attempts upload admission itself.
            clock.return_value = 2000000001
            queue.enqueue(camera)
            self.assertTrue(queue.run_once(self.telegram))
            delayed = queue.status(camera)['latest'][camera]
            self.assertEqual((delayed['state'], delayed['code']), ('blocked', 'upload_spool_budget'))
            self.assertEqual(delayed['statistics']['spool_blocked'], 0)
            self.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
