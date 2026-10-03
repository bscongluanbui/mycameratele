"""Mocked Telegram queue semantics and calendar-navigation tests; no network."""
import os
import shutil
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from archive_app.core import Archive, Settings, record_key
from archive_app.telegram import ApiRejected, Telegram


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.fixture_parent = Path(__file__).resolve().parent if os.name == "nt" else Path(tempfile.gettempdir()).resolve()
        self.root = self.fixture_parent / (".tmp-telegram-" + uuid.uuid4().hex)
        self.assertEqual(self.root.resolve().parent, self.fixture_parent)
        self.root.mkdir()
        self.input = self.root / "input"
        self.input.mkdir()
        self.source = self.input / "source.mp4"
        self.source.write_bytes(b"synthetic-source-fixture")
        self.settings = Settings(
            state_dir=self.root / "state", cache_dir=self.root / "cache", input_dir=self.input,
            timezone="UTC+07:00", keep_cache=False, enable_upload=True,
            token="fixture", chat_id="42", owner_user_id=42, allowed_users=(42,), min_free_bytes=0,
        )
        self.archive = Archive(self.settings)
        self.archive.state('telegram_owner_started:42','1')
        self.telegram = Telegram(self.settings)
        self.normalizer_patch = patch("archive_app.core.normalize", side_effect=self.fake_normalize)
        self.normalizer = self.normalizer_patch.start()
        self.request_patch = patch.object(self.telegram, "request")
        self.request = self.request_patch.start()

    def tearDown(self):
        self.request_patch.stop()
        self.normalizer_patch.stop()
        self.archive.close()
        self.assertEqual(self.root.resolve().parent, self.fixture_parent)
        shutil.rmtree(self.root)

    def fake_normalize(self, source, destination, settings):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"synthetic-normalized-fixture")
        return {"duration": 60, "codec_video": "h264", "codec_audio": "aac", "bytes": destination.stat().st_size}

    def ingest(self, *, record_id="sample-one", camera="Front_Camera", minute=0):
        start = datetime.fromisoformat("2026-10-03T10:00:00+07:00") + timedelta(minutes=minute)
        descriptor = {
            "camera": camera, "record_id": record_id, "path": str(self.source),
            "start_time": start.isoformat(), "end_time": (start + timedelta(minutes=1)).isoformat(),
        }
        self.archive.ingest_entry(descriptor)
        return record_key(descriptor)

    def row(self, key):
        return dict(self.archive.conn.execute("SELECT * FROM recordings WHERE key=?", (key,)).fetchone())

    def confirm(self, key, message_id=17):
        self.archive.mark_uploaded(key, self.settings.chat_id, message_id, "synthetic-file-id")

    def test_success_persists_telegram_identity_and_cleans_only_cache(self):
        key = self.ingest()
        cached = Path(self.row(key)["local_path"])
        self.request.return_value = {"chat": {"id": 42,"type":"private"}, "message_id": 17, "document": {"file_id": "confirmed-file-id","file_unique_id":"confirmed-unique-id"}}
        self.assertEqual(self.telegram.upload_one(self.archive), "uploaded")
        row = self.row(key)
        self.assertEqual(row["status"], "uploaded")
        self.assertEqual(row["file_id"], "confirmed-file-id")
        self.assertEqual(row["message_id"], 17)
        self.assertEqual(row['file_unique_id'],'confirmed-unique-id')
        self.assertEqual(row['media_type'],'document')
        self.assertFalse(cached.exists())
        self.assertTrue(self.source.is_file())
        args, kwargs = self.request.call_args
        self.assertEqual(args[0], "sendDocument")
        self.assertEqual(kwargs["file_field"], "document")
        self.assertIs(args[1]["disable_content_type_detection"], True)
        self.assertNotIn("supports_streaming", args[1])
        self.archive.close()
        self.archive = Archive(self.settings)
        self.assertEqual(self.row(key)["file_id"], "confirmed-file-id")

    def test_timeout_quarantines_upload_and_retains_cache_without_retry(self):
        key = self.ingest()
        cached = Path(self.row(key)["local_path"])
        self.request.side_effect = TimeoutError("synthetic timeout after request")
        self.assertEqual(self.telegram.upload_one(self.archive), "upload_unknown")
        self.assertEqual(self.row(key)["status"], "upload_unknown")
        self.assertIsNone(self.row(key)["message_id"])
        self.assertTrue(cached.is_file())
        self.assertIsNone(self.archive.claim_upload())
        self.assertIsNone(self.telegram.upload_one(self.archive))
        self.request.assert_called_once()

    def test_rate_limit_keeps_cache_and_schedules_future_retry(self):
        key = self.ingest()
        cached = Path(self.row(key)["local_path"])
        before = time.time()
        self.request.side_effect = ApiRejected(429, retry_after=60)
        self.assertEqual(self.telegram.upload_one(self.archive), "api_rejected")
        row = self.row(key)
        self.assertEqual(row["status"], "downloaded")
        self.assertGreaterEqual(row["retry_at"], before + 60)
        self.assertEqual(row["last_error"], "rate_limited")
        self.assertTrue(cached.is_file())
        self.assertIsNone(self.archive.claim_upload())

    def test_client_rejection_needs_review_and_keeps_cache(self):
        key = self.ingest()
        cached = Path(self.row(key)["local_path"])
        self.request.side_effect = ApiRejected(400)
        self.assertEqual(self.telegram.upload_one(self.archive), "api_rejected")
        self.assertEqual(self.row(key)["status"], "needs_review")
        self.assertTrue(cached.is_file())
        self.assertIsNone(self.archive.claim_upload())

    def test_repeated_ingest_does_not_reset_needs_review_or_requeue(self):
        key = self.ingest()
        cached = Path(self.row(key)["local_path"])
        contents_before = cached.read_bytes()
        self.request.side_effect = ApiRejected(400)
        self.assertEqual(self.telegram.upload_one(self.archive), "api_rejected")
        self.assertEqual(self.row(key)["status"], "needs_review")
        self.assertEqual(self.ingest(), key)
        self.assertEqual(self.row(key)["status"], "needs_review")
        self.assertEqual(cached.read_bytes(), contents_before)
        self.normalizer.assert_called_once()
        self.assertIsNone(self.archive.claim_upload())
        self.assertIsNone(self.telegram.upload_one(self.archive))
        self.request.assert_called_once()

    def test_server_rejection_is_ambiguous_and_keeps_cache(self):
        key = self.ingest()
        cached = Path(self.row(key)["local_path"])
        self.request.side_effect = ApiRejected(500)
        self.assertEqual(self.telegram.upload_one(self.archive), "api_rejected")
        self.assertEqual(self.row(key)["status"], "upload_unknown")
        self.assertTrue(cached.is_file())
        self.assertIsNone(self.archive.claim_upload())

    def test_incomplete_success_response_is_not_treated_as_uploaded(self):
        key = self.ingest()
        cached = Path(self.row(key)["local_path"])
        self.request.return_value = {"chat": {"id": int(self.settings.chat_id)}, "message_id": 17}
        self.assertEqual(self.telegram.upload_one(self.archive), "upload_unknown")
        self.assertEqual(self.row(key)["status"], "upload_unknown")
        self.assertTrue(cached.is_file())

    def test_oversize_file_requires_review_without_post(self):
        key = self.ingest()
        cached = Path(self.row(key)["local_path"])
        self.settings.max_bytes = 1
        self.assertEqual(self.telegram.upload_one(self.archive), "needs_review")
        self.assertEqual(self.row(key)["status"], "needs_review")
        self.assertTrue(cached.is_file())
        self.request.assert_not_called()

    def test_disabled_upload_does_not_claim_or_post(self):
        key = self.ingest()
        self.settings.enable_upload = False
        self.assertIsNone(self.telegram.upload_one(self.archive))
        self.assertEqual(self.row(key)["status"], "downloaded")
        self.request.assert_not_called()

    def test_other_codecs_use_document_without_transcoding(self):
        key = self.ingest()
        with self.archive.conn:
            self.archive.conn.execute("UPDATE recordings SET codec_video='hevc' WHERE key=?", (key,))
        self.request.return_value = {"chat": {"id": 42,"type":"private"}, "message_id": 19, "document": {"file_id": "document-file-id","file_unique_id":"document-unique-id"}}
        self.assertEqual(self.telegram.upload_one(self.archive), "uploaded")
        args, kwargs = self.request.call_args
        self.assertEqual(args[0], "sendDocument")
        self.assertEqual(kwargs["file_field"], "document")
        self.assertEqual(self.row(key)["file_id"], "document-file-id")

    def test_menu_root_and_calendar_follow_recording_date(self):
        key = self.ingest()
        self.confirm(key)
        _, root = self.telegram.menu(self.archive, "root")
        self.assertEqual(root[0], [{"text": "Front_Camera", "callback_data": "c:"+self.telegram.camera_token("Front_Camera")+":asc"}])
        _, months = self.telegram.menu(self.archive, "y:2026")
        self.assertEqual(len(months), 12)
        self.assertEqual(months[9][0]["callback_data"], "m:2026-10")
        _, days = self.telegram.menu(self.archive, "m:2026-10")
        self.assertEqual(len(days), 31)
        self.assertEqual(days[2][0]["callback_data"], "d:2026-10-03")
        self.request.assert_not_called()

    def test_menu_root_includes_both_years_for_recording_crossing_new_year(self):
        descriptor = {
            "camera": "Front_Camera", "record_id": "new-year-crossing", "path": str(self.source),
            "start_time": "2026-12-31T23:59:00+07:00", "end_time": "2027-01-01T00:01:00+07:00",
        }
        self.archive.ingest_entry(descriptor)
        self.confirm(record_key(descriptor))
        _, root = self.telegram.menu(self.archive, "root")
        _, years = self.telegram.menu(self.archive, root[0][0]["callback_data"])
        self.assertEqual([row[0]["text"] for row in years if row[0].get("callback_data", "").startswith("y:")], ["2026", "2027"])
        self.assertEqual(len(self.archive.list_day("2026-12-31")), 1)
        self.assertEqual(len(self.archive.list_day("2027-01-01")), 1)

    def test_menu_root_does_not_include_next_year_for_exact_midnight_end(self):
        descriptor = {
            "camera": "Front_Camera", "record_id": "new-year-exclusive-end", "path": str(self.source),
            "start_time": "2026-12-31T23:59:00+07:00", "end_time": "2027-01-01T00:00:00+07:00",
        }
        self.archive.ingest_entry(descriptor)
        self.confirm(record_key(descriptor))
        _, root = self.telegram.menu(self.archive, "root")
        _, years = self.telegram.menu(self.archive, root[0][0]["callback_data"])
        self.assertEqual([row[0]["text"] for row in years if row[0].get("callback_data", "").startswith("y:")], ["2026"])
        self.assertEqual(self.archive.list_day("2027-01-01"), [])

    def test_camera_and_clip_menu_produce_stable_private_replay_callback(self):
        key = self.ingest()
        self.confirm(key)
        _, cameras = self.telegram.menu(self.archive, "d:2026-10-03")
        callback = cameras[0][0]["callback_data"]
        self.assertEqual(cameras[0][0]["text"], "Front_Camera")
        self.assertRegex(callback, r"^p:2026-10-03:[0-9a-f]{12}:0$")
        title, clips = self.telegram.menu(self.archive, callback)
        self.assertIn("Front_Camera", title)
        self.assertEqual(clips, [[{"text": "10:00:00 ▶", "callback_data": "v:"+key[:32]},
                                 {"text": "⬇ Tải", "callback_data": "f:"+key[:32]},
                                 {"text": "🗑 Xóa", "callback_data": "x:"+key[:32]}]])
        self.assertEqual(self.telegram.menu(self.archive, callback), (title, clips))

    def test_old_camera_callback_stays_bound_after_alphabetically_earlier_camera_is_added(self):
        original = self.ingest(camera="Zulu_Camera")
        self.confirm(original, message_id=71)
        _, original_menu = self.telegram.menu(self.archive, "d:2026-10-03")
        original_callback = original_menu[0][0]["callback_data"]
        added = self.ingest(record_id="new-earlier-camera", camera="Alpha_Camera")
        self.confirm(added, message_id=72)
        title, buttons = self.telegram.menu(self.archive, original_callback)
        self.assertIn("Zulu_Camera", title)
        self.assertNotIn("Alpha_Camera", title)
        self.assertEqual(buttons[0][0]["callback_data"], "v:"+original[:32])
        _, refreshed = self.telegram.menu(self.archive, "d:2026-10-03")
        callback_for_original = next(row[0]["callback_data"] for row in refreshed if row[0]["text"] == "Zulu_Camera")
        self.assertEqual(callback_for_original, original_callback)

    def test_clip_pages_have_ten_rows_and_valid_navigation(self):
        keys=[]
        for minute in range(12):
            key = self.ingest(record_id=f"page-record-{minute}", minute=minute)
            keys.append(key)
            self.confirm(key, message_id=100 + minute)
        _, cameras = self.telegram.menu(self.archive, "d:2026-10-03")
        callback = cameras[0][0]["callback_data"]
        next_callback = callback.rsplit(":", 1)[0] + ":1"
        _, first = self.telegram.menu(self.archive, callback)
        self.assertEqual(len(first), 11)
        self.assertEqual(first[-1], [{"text": "→", "callback_data": next_callback}])
        _, second = self.telegram.menu(self.archive, next_callback)
        self.assertEqual(len(second), 3)
        self.assertEqual(second[0][0]["callback_data"], "v:"+keys[10][:32])
        self.assertEqual(second[-1], [{"text": "←", "callback_data": callback}])
        for row in first + second:
            for button in row:
                if "callback_data" in button:
                    self.assertLessEqual(len(button["callback_data"].encode()), 64)

    def test_invalid_menu_page_is_rejected(self):
        key = self.ingest()
        self.confirm(key)
        _, cameras = self.telegram.menu(self.archive, "d:2026-10-03")
        callback = cameras[0][0]["callback_data"]
        invalid_negative_page = callback.rsplit(":", 1)[0] + ":-1"
        for selection in ("p:2026-10-03:-1:0", "p:2026-10-03:not-a-camera:0", invalid_negative_page):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                self.telegram.menu(self.archive, selection)

    def test_expired_callback_is_skipped_and_next_status_update_advances_offset(self):
        self.settings.allowed_users = (42,)
        updates = [
            {"update_id": 100, "callback_query": {"id": "expired-callback", "from": {"id": 42}, "data": "root", "message": {"chat": {"id": 42, "type": "private"}}}},
            {"update_id": 101, "message": {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "/status"}},
        ]
        replies = []
        def fake_request(method, fields, **kwargs):
            if method == "getUpdates":
                return updates
            if method == "answerCallbackQuery":
                raise ApiRejected(400)
            if method == "sendMessage":
                replies.append(fields)
                return {"message_id": 55}
            raise AssertionError(f"Unexpected method {method}")
        self.request.side_effect = fake_request
        self.telegram.poll(self.archive)
        self.assertEqual(self.archive.state("telegram_offset"), "102")
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["chat_id"], 42)
        self.assertIn("queue", replies[0]["text"])

    def test_invalid_callback_fallback_403_does_not_pin_cursor_or_block_next_update(self):
        self.settings.allowed_users = (42,)
        updates = [
            {"update_id": 200, "callback_query": {"id": "invalid-menu", "from": {"id": 42}, "data": "not-a-menu", "message": {"chat": {"id": 42, "type": "private"}}}},
            {"update_id": 201, "message": {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "/status"}},
        ]
        sent = []
        def fake_request(method, fields, **kwargs):
            if method == "getUpdates":
                return updates
            if method == "answerCallbackQuery":
                return True
            if method == "sendMessage":
                sent.append(fields)
                if "queue" not in fields["text"]:
                    raise ApiRejected(403)
                return {"message_id": 65}
            raise AssertionError(f"Unexpected method {method}")
        self.request.side_effect = fake_request
        self.telegram.poll(self.archive)
        self.assertEqual(self.archive.state("telegram_offset"), "202")
        self.assertEqual(len(sent), 2)
        self.assertNotIn("queue", sent[0]["text"])
        self.assertIn("queue", sent[1]["text"])

    def test_rate_limited_callback_fallback_preserves_cursor_for_retry(self):
        self.settings.allowed_users = (42,)
        self.archive.state("telegram_offset", 250)
        updates = [{"update_id": 250, "callback_query": {"id": "rate-limited-fallback", "from": {"id": 42}, "data": "not-a-menu", "message": {"chat": {"id": 42, "type": "private"}}}}]
        def fake_request(method, fields, **kwargs):
            if method == "getUpdates":
                return updates
            if method == "answerCallbackQuery":
                return True
            if method == "sendMessage":
                raise ApiRejected(429, retry_after=30)
            raise AssertionError(f"Unexpected method {method}")
        self.request.side_effect = fake_request
        with self.assertRaises(ApiRejected) as rejected:
            self.telegram.poll(self.archive)
        self.assertEqual(rejected.exception.code, 429)
        self.assertEqual(self.archive.state("telegram_offset"), "250")


if __name__ == "__main__":
    unittest.main()
