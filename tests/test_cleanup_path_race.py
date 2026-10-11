"""Deterministic cleanup races; synthetic files and Telegram providers only."""
import os
from pathlib import Path
import shutil
import stat as file_stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from archive_app import core


class CleanupPathRaceTests(unittest.TestCase):
    def setUp(self):
        self.parent = (Path(__file__).parent if os.name == 'nt'
                       else Path(tempfile.gettempdir())).resolve()
        self.root = self.parent / ('.tmp-cleanup-path-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.path = self.root / 'confirmed.source'
        self.path.write_bytes(b'closed camera recording')

    def tearDown(self):
        self.assertEqual(self.root.resolve().parent, self.parent)
        shutil.rmtree(self.root)

    def target_stat_race(self, mutation):
        original = Path.stat
        events = []

        def stat(path, *args, **kwargs):
            if (path == self.path
                    and sys._getframe(1).f_code.co_name == '_confined_path'):
                events.append(kwargs.copy())
                mutation()
            return original(path, *args, **kwargs)

        return patch.object(Path, 'stat', stat), events

    def test_regular_file_returns_confined_absolute_path(self):
        self.assertEqual(core._confined_path(self.path, self.root), self.path)

    def test_missing_allowed_returns_confined_path(self):
        self.path.unlink()
        self.assertEqual(core._confined_path(self.path, self.root, allow_missing=True), self.path)

    def test_missing_disallowed_raises_file_not_found(self):
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            core._confined_path(self.path, self.root)

    def test_unlink_between_guards_and_stat_is_allowed_only_when_missing_allowed(self):
        context, events = self.target_stat_race(self.path.unlink)
        with context:
            self.assertEqual(core._confined_path(self.path, self.root, allow_missing=True), self.path)
        self.assertEqual(events, [{'follow_symlinks': False}])
        self.assertFalse(self.path.exists())

    def test_unlink_between_guards_and_stat_still_rejects_missing_disallowed(self):
        context, events = self.target_stat_race(self.path.unlink)
        with context, self.assertRaises(FileNotFoundError):
            core._confined_path(self.path, self.root)
        self.assertEqual(events, [{'follow_symlinks': False}])

    def test_replacement_directory_between_guards_and_stat_is_not_a_missing_file(self):
        def replace():
            self.path.unlink()
            self.path.mkdir()

        context, events = self.target_stat_race(replace)
        with context, self.assertRaisesRegex(ValueError, 'regular file'):
            core._confined_path(self.path, self.root, allow_missing=True)
        self.assertEqual(events, [{'follow_symlinks': False}])

    def test_directory_rejected_even_when_missing_is_allowed(self):
        for allowed in (False, True):
            with self.subTest(allowed=allowed), self.assertRaisesRegex(ValueError, 'regular file'):
                core._confined_path(self.root, self.root, allow_missing=allowed)

    def test_outside_path_rejected_before_stat(self):
        outside = self.root.parent / 'outside-fixture.source'
        with self.assertRaisesRegex(ValueError, 'outside'):
            core._confined_path(outside, self.root, allow_missing=True)

    def test_junction_guard_preserved_before_metadata_validation(self):
        original = core._is_link
        with patch.object(core, '_is_link',
                          side_effect=lambda path: path == self.path or original(path)):
            with self.assertRaisesRegex(ValueError, 'symlinks or junctions'):
                core._confined_path(self.path, self.root, allow_missing=True)

    def test_symlink_to_regular_file_rejected(self):
        link = self.root / 'alias.source'
        try:
            link.symlink_to(self.path)
        except OSError as error:
            self.skipTest('Host symlink fixture unavailable: ' + type(error).__name__)
        with self.assertRaisesRegex(ValueError, 'symlinks or junctions'):
            core._confined_path(link, self.root, allow_missing=True)

    def test_symlink_parent_directory_rejected(self):
        directory = self.root / 'real-directory'
        directory.mkdir()
        (directory / 'clip.source').write_bytes(b'recording')
        link = self.root / 'alias-directory'
        try:
            link.symlink_to(directory, target_is_directory=True)
        except OSError as error:
            self.skipTest('Host symlink fixture unavailable: ' + type(error).__name__)
        with self.assertRaisesRegex(ValueError, 'symlinks or junctions'):
            core._confined_path(link / 'clip.source', self.root, allow_missing=True)

    def test_metadata_permission_error_is_not_swallowed_as_missing(self):
        def deny():
            raise PermissionError('synthetic metadata permission error')

        context, events = self.target_stat_race(deny)
        with context, self.assertRaises(PermissionError):
            core._confined_path(self.path, self.root, allow_missing=True)
        self.assertEqual(events, [{'follow_symlinks': False}])

    def test_late_symlink_metadata_rejected_without_following_target(self):
        original = Path.stat
        events = []

        def replaced_with_link(path, *args, **kwargs):
            if (path == self.path
                    and sys._getframe(1).f_code.co_name == '_confined_path'):
                events.append(kwargs.copy())
                return os.stat_result((file_stat.S_IFLNK, 0, 0, 1, 0, 0, 0, 0, 0, 0))
            return original(path, *args, **kwargs)

        with patch.object(Path, 'stat', replaced_with_link), self.assertRaisesRegex(ValueError, 'regular file'):
            core._confined_path(self.path, self.root, allow_missing=True)
        self.assertEqual(events, [{'follow_symlinks': False}])

    def test_confirmed_archive_cleanup_handles_staged_source_stat_race(self):
        settings = core.Settings(self.root / 'state', self.root / 'cache', self.root / 'input',
                                 'UTC+07:00', min_free_bytes=0, keep_cache=False)
        archive = core.Archive(settings)
        archive.add_camera({'id': 'fixture-camera', 'name': 'Synthetic camera'})
        entry = {'camera': 'fixture-camera', 'record_id': 'closed-file', 'source': 'camera-sd'}
        key = core.record_key(entry)
        stage = settings.cache_dir / 'sd-stage' / entry['camera']
        stage.mkdir(parents=True)
        source = stage / (key + '.' + uuid.uuid4().hex + '.source')
        source.write_bytes(b'closed recording source')
        managed = settings.cache_dir / (key + '.mp4')
        managed.write_bytes(b'confirmed managed recording')
        unrelated = settings.cache_dir / 'another-recording.mp4'
        unrelated.write_bytes(b'keep unrelated recording')
        with archive.conn:
            archive.conn.execute('''INSERT INTO recordings
                (key,camera,record_id,start_ms,end_ms,source_path,local_path,status,created_at)
                VALUES(?,?,?,1000,61000,?,?,'downloaded',1)''',
                (key, entry['camera'], entry['record_id'], str(source), str(managed)))
        archive.mark_uploaded(key, 42, 100, 'synthetic-file-id',
                              file_unique_id='synthetic-unique-id', media_type='video')
        before = dict(archive.conn.execute('SELECT * FROM recordings WHERE key=?', (key,)).fetchone())
        original_stat, original_unlink = Path.stat, Path.unlink
        events = []

        def stat(path, *args, **kwargs):
            if (path == source
                    and sys._getframe(1).f_code.co_name == '_confined_path'):
                self.assertEqual(kwargs, {'follow_symlinks': False})
                events.append(str(path))
                # The independent SD worker has seen the committed Telegram
                # receipt and removes its staging copy at this exact boundary.
                original_unlink(path)
            return original_stat(path, *args, **kwargs)

        try:
            with patch.object(Path, 'stat', stat):
                self.assertTrue(archive.cleanup(key))
            self.assertEqual(events, [str(source)])
            self.assertFalse(source.exists())
            self.assertFalse(managed.exists())
            self.assertEqual(unrelated.read_bytes(), b'keep unrelated recording')
            after = dict(archive.conn.execute('SELECT * FROM recordings WHERE key=?', (key,)).fetchone())
            self.assertEqual({k: v for k, v in before.items() if k not in ('cleaned_at', 'cleanup_revision')},
                             {k: v for k, v in after.items() if k not in ('cleaned_at', 'cleanup_revision')})
            self.assertIsNotNone(after['cleaned_at'])
            self.assertEqual(after['cleanup_revision'], 1)
            self.assertFalse(archive.cleanup(key))
        finally:
            archive.close()


if __name__ == '__main__':
    unittest.main()
