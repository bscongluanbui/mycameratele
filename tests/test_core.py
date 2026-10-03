"""Pure-stdlib behavior tests: all media and Telegram operations are mocked.

The input is a synthetic fixture, never camera footage or a live account.
"""
import os
import json
import sqlite3
import shutil
import tempfile
import unittest
import uuid
from datetime import timedelta, timezone
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from archive_app.core import Archive, Settings, parse_time, record_key, resolve_input


def entry(path, *, record_id="sample-001", camera="Front_Camera", start="2026-10-03T10:00:00+07:00", end="2026-10-03T10:01:00+07:00"):
    return {"record_id": record_id, "camera": camera, "start_time": start, "end_time": end, "path": str(path)}


class TimestampTests(unittest.TestCase):
    def test_offset_is_preserved(self):
        timestamp = parse_time("2026-10-03T10:00:00+07:00")
        self.assertEqual(timestamp.utcoffset(), timedelta(hours=7))
        self.assertEqual(timestamp.astimezone(timezone.utc).hour, 3)

    def test_z_is_utc(self):
        self.assertEqual(parse_time("2026-10-03T03:00:00Z").utcoffset(), timedelta(0))

    def test_naive_timestamp_is_rejected(self):
        with self.assertRaises((TypeError, ValueError)):
            parse_time("2026-10-03T10:00:00")

    def test_null_empty_and_date_only_are_rejected(self):
        for timestamp in (None, "", "2026-10-03", "not-a-timestamp"):
            with self.subTest(timestamp=timestamp), self.assertRaises((TypeError, ValueError)):
                parse_time(timestamp)


class IdentityTests(unittest.TestCase):
    def test_record_identity_ignores_path_and_end_time_correction(self):
        first = entry("/inbox/one.mp4")
        second = dict(first, path="/another/mount/two.mp4", end_time="2026-10-03T10:02:00+07:00")
        self.assertEqual(record_key(first), record_key(second))
        self.assertEqual(len(record_key(first)), 64)
        int(record_key(first), 16)

    def test_record_identity_separates_cameras(self):
        self.assertNotEqual(record_key(entry("unused", camera="Front")), record_key(entry("unused", camera="Back")))

    def test_record_identity_separates_record_ids(self):
        self.assertNotEqual(record_key(entry("unused", record_id="one")), record_key(entry("unused", record_id="two")))


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        # Containers mount application/tests read-only; POSIX fixtures use /tmp.
        # Windows sandbox fixtures stay in the explicitly writable workspace.
        self.fixture_parent = Path(__file__).resolve().parent if os.name == "nt" else Path(tempfile.gettempdir()).resolve()
        # Python 3.14 mkdtemp(mode=0o700) creates Windows ACLs which exclude
        # sandbox tokens; ordinary mkdir inherits the workspace ACL instead.
        self.root = self.fixture_parent / (".tmp-core-" + uuid.uuid4().hex)
        self.assertEqual(self.root.resolve().parent, self.fixture_parent)
        self.root.mkdir()
        self.input = self.root / "input"
        self.cache = self.root / "cache"
        self.state = self.root / "state"
        self.input.mkdir()
        self.source = self.input / "source.mp4"
        self.source.write_bytes(b"synthetic-recording-fixture")
        self.environment = patch.dict(os.environ, {
            "STATE_DIR": str(self.state), "CACHE_DIR": str(self.cache), "INPUT_DIR": str(self.input),
            "DISPLAY_TIMEZONE": "UTC+07:00", "KEEP_CACHE": "false",
            "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "",
        }, clear=True)
        self.environment.start()
        self.settings = Settings.from_env()
        self.archive = Archive(self.settings)
        self.normalizer_patch = patch("archive_app.core.normalize", side_effect=self.fake_normalize)
        self.normalizer = self.normalizer_patch.start()

    def tearDown(self):
        self.normalizer_patch.stop()
        self.archive.close()
        self.environment.stop()
        self.assertEqual(self.root.resolve().parent, self.fixture_parent)
        shutil.rmtree(self.root)

    def fake_normalize(self, source, destination, settings):
        self.assertEqual(Path(source), self.source.resolve())
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"synthetic-normalized-mp4-fixture")
        return {"duration": 60.0, "codec_video": "h264", "codec_audio": "aac", "bytes": destination.stat().st_size}

    def row(self, key):
        with closing(sqlite3.connect(self.state / "archive.db")) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM recordings WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def ingest(self, **overrides):
        descriptor = entry(self.source, **overrides)
        result = self.archive.ingest_entry(descriptor)
        self.assertEqual(result["record_key"], record_key(descriptor))
        return result["record_key"]

    def upload(self, key):
        self.archive.mark_uploaded(key, "-100123456789", 17, "test-file-id")

    def test_settings_paths_follow_environment(self):
        self.assertEqual(self.settings.state_dir, self.state)
        self.assertEqual(self.settings.cache_dir, self.cache)
        self.assertEqual(self.settings.input_dir, self.input)

    def test_input_path_inside_root_is_resolved(self):
        self.assertEqual(resolve_input(self.source, self.input), self.source.resolve())

    def test_input_path_escape_is_rejected(self):
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"outside-fixture")
        for path in (outside, self.input / ".." / "outside.mp4"):
            with self.subTest(path=path), self.assertRaises((TypeError, ValueError)):
                resolve_input(path, self.input)

    def test_missing_input_is_rejected(self):
        with self.assertRaises((FileNotFoundError, ValueError)):
            self.archive.ingest_entry(entry(self.input / "missing.mp4"))
        self.normalizer.assert_not_called()

    def test_ingest_creates_downloaded_row_and_cache(self):
        key = self.ingest()
        row = self.row(key)
        self.assertEqual(row["status"], "downloaded")
        self.assertTrue(Path(row["local_path"]).is_file())
        self.normalizer.assert_called_once()

    def test_worker_manifest_isolates_bad_entry_and_ingests_both_valid_neighbors(self):
        first = entry(self.source, record_id="manifest-first")
        bad = entry(self.source, record_id="manifest-bad", end=None)
        last = entry(self.source, record_id="manifest-last")
        manifest = self.input / "manifest.json"
        manifest.write_text(json.dumps({"recordings": [first, bad, last]}), encoding="utf-8")
        results = self.archive.ingest_manifest(manifest, continue_on_error=True)
        self.assertEqual([result["status"] for result in results], ["downloaded", "failed", "downloaded"])
        self.assertEqual(self.row(record_key(first))["status"], "downloaded")
        self.assertIsNone(self.row(record_key(bad)))
        self.assertEqual(self.row(record_key(last))["status"], "downloaded")
        self.assertEqual(self.normalizer.call_count, 2)

    def test_default_strict_manifest_stops_at_invalid_entry(self):
        first = entry(self.source, record_id="strict-first")
        bad = entry(self.source, record_id="strict-bad", end=None)
        last = entry(self.source, record_id="strict-last")
        manifest = self.input / "strict-manifest.json"
        manifest.write_text(json.dumps({"recordings": [first, bad, last]}), encoding="utf-8")
        with self.assertRaises((TypeError, ValueError)):
            self.archive.ingest_manifest(manifest)
        self.assertEqual(self.row(record_key(first))["status"], "downloaded")
        self.assertIsNone(self.row(record_key(bad)))
        self.assertIsNone(self.row(record_key(last)))
        self.normalizer.assert_called_once()

    def test_repeated_ingest_is_idempotent_and_closed_end_correction_requires_review(self):
        first = self.ingest()
        second = self.ingest()
        self.assertEqual(first, second)
        with self.assertRaises(ValueError):
            self.ingest(end="2026-10-03T10:02:00+07:00")
        self.normalizer.assert_called_once()
        with closing(sqlite3.connect(self.state / "archive.db")) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM recordings").fetchone()[0], 1)

    def test_invalid_interval_is_rejected_before_normalizing(self):
        for end in (None, "2026-10-03T10:00:00+07:00", "2026-10-03T09:59:00+07:00"):
            with self.subTest(end=end), self.assertRaises((TypeError, ValueError)):
                self.archive.ingest_entry(entry(self.source, end=end))
        self.normalizer.assert_not_called()

    def test_naive_recording_times_are_rejected(self):
        with self.assertRaises((TypeError, ValueError)):
            self.archive.ingest_entry(entry(self.source, start="2026-10-03T10:00:00"))
        self.normalizer.assert_not_called()

    def test_camera_path_components_are_rejected(self):
        for camera in ("../escape", "with/slash", "with\\slash", ""):
            with self.subTest(camera=camera), self.assertRaises((TypeError, ValueError)):
                self.archive.ingest_entry(entry(self.source, camera=camera))
        self.normalizer.assert_not_called()

    def test_downloaded_recordings_are_not_listed_as_uploaded(self):
        self.ingest()
        self.assertEqual(self.archive.list_day("2026-10-03"), [])

    def test_upload_state_and_telegram_identity_are_durable(self):
        key = self.ingest()
        self.upload(key)
        self.archive.close()
        self.archive = Archive(self.settings)
        row = self.row(key)
        self.assertEqual(row["status"], "uploaded")
        self.assertEqual(str(row["chat_id"]), "-100123456789")
        self.assertEqual(row["message_id"], 17)
        self.assertEqual(row["file_id"], "test-file-id")
        self.assertEqual(len(self.archive.list_day("2026-10-03")), 1)

    def test_utc_recording_crossing_local_midnight_is_listed_on_both_days(self):
        key = self.ingest(start="2026-10-02T16:59:00Z", end="2026-10-02T17:01:00Z")
        self.upload(key)
        self.assertEqual(len(self.archive.list_day("2026-10-02")), 1)
        self.assertEqual(len(self.archive.list_day("2026-10-03")), 1)
        self.assertEqual(self.archive.list_day("2026-10-04"), [])

    def test_interval_ending_at_local_midnight_is_not_in_next_day(self):
        key = self.ingest(start="2026-10-02T16:59:00Z", end="2026-10-02T17:00:00Z")
        self.upload(key)
        self.assertEqual(len(self.archive.list_day("2026-10-02")), 1)
        self.assertEqual(self.archive.list_day("2026-10-03"), [])

    def test_day_filter_uses_camera(self):
        key = self.ingest(camera="Front_Camera")
        self.upload(key)
        self.assertEqual(len(self.archive.list_day("2026-10-03", camera="Front_Camera")), 1)
        self.assertEqual(self.archive.list_day("2026-10-03", camera="Back_Camera"), [])

    def test_upload_claim_is_atomic_and_not_claimed_twice(self):
        key = self.ingest()
        other = Archive(self.settings)
        try:
            claimed = self.archive.claim_upload()
            self.assertEqual(claimed["key"], key)
            self.assertEqual(self.row(key)["status"], "uploading")
            self.assertIsNone(other.claim_upload())
        finally:
            other.close()

    def test_interrupted_upload_becomes_unknown_and_is_not_retried(self):
        key = self.ingest()
        self.archive.claim_upload()
        self.assertEqual(self.archive.recover_uploads(), 1)
        self.assertEqual(self.row(key)["status"], "upload_unknown")
        self.assertIsNone(self.archive.claim_upload())
        self.assertEqual(self.archive.recover_uploads(), 0)

    def test_downloaded_cache_is_not_cleaned(self):
        key = self.ingest()
        cached = Path(self.row(key)["local_path"])
        self.assertFalse(self.archive.cleanup(key))
        self.assertTrue(cached.is_file())

    def test_unknown_upload_cache_is_not_cleaned(self):
        key = self.ingest()
        self.archive.claim_upload()
        self.archive.recover_uploads()
        cached = Path(self.row(key)["local_path"])
        self.assertFalse(self.archive.cleanup(key))
        self.assertTrue(cached.is_file())

    def test_uploaded_cache_is_cleaned_but_input_and_row_remain(self):
        key = self.ingest()
        cached = Path(self.row(key)["local_path"])
        self.upload(key)
        self.assertTrue(self.archive.cleanup(key))
        self.assertFalse(cached.exists())
        self.assertTrue(self.source.is_file())
        self.assertEqual(self.row(key)["status"], "uploaded")
        self.assertEqual(len(self.archive.list_day("2026-10-03")), 1)

    def test_uploaded_without_durable_telegram_metadata_is_not_cleaned(self):
        key = self.ingest()
        cached = Path(self.row(key)["local_path"])
        self.archive.conn.execute("UPDATE recordings SET status='uploaded', message_id=NULL, file_id=NULL, chat_id=NULL WHERE key=?", (key,))
        self.archive.conn.commit()
        self.assertFalse(self.archive.cleanup(key))
        self.assertTrue(cached.is_file())

    def test_cleanup_never_deletes_a_path_outside_cache(self):
        key = self.ingest()
        outside = self.root / "outside-must-remain.mp4"
        outside.write_bytes(b"outside-cache-fixture")
        self.upload(key)
        self.archive.conn.execute("UPDATE recordings SET local_path=? WHERE key=?", (str(outside), key))
        self.archive.conn.commit()
        try:
            self.assertFalse(self.archive.cleanup(key))
        except ValueError:
            pass
        self.assertTrue(outside.is_file())


if __name__ == "__main__":
    unittest.main()
