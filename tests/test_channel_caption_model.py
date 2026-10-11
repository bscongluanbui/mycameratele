"""Compact recording-time channel captions; no Telegram, camera, or media I/O."""
from datetime import datetime, timedelta
from pathlib import Path
import unittest
from unittest.mock import Mock

from archive_app.core import Settings, get_zone
from archive_app.telegram import Telegram


class ChannelCaptionModelTests(unittest.TestCase):
    def setUp(self):
        self.settings=Settings(Path('/state'),Path('/cache'),Path('/input'),'UTC+07:00',
                               multi_channel_routing=True)
        self.telegram=Telegram(self.settings)
        self.archive=Mock()
        self.archive.camera_name.return_value='CAM GIỮA NHÀ'

    def recording(self,start='2026-10-05T14:30:00+07:00',duration=300):
        beginning=datetime.fromisoformat(start)
        ending=beginning+timedelta(seconds=duration)
        return {'camera':'cam-internal-slug','key':'0123456789abcdef'*4,
                'start_ms':round(beginning.timestamp()*1000),
                'end_ms':round(ending.timestamp()*1000),'file_size':987654321,
                'uploaded_at':1980000000,'local_path':'/private/path/media.mp4'}

    def test_same_day_matches_short_document_example(self):
        self.assertEqual(self.telegram.caption(self.archive,self.recording()),
            '🎥 CAM GIỮA NHÀ\n📅 05/10/2026\n🕒 14:30:00 – 14:35:00\n⏱ 05 phút')
        self.archive.camera_name.assert_called_once_with('cam-internal-slug')

    def test_no_internal_identity_size_hash_upload_date_or_tags(self):
        row=self.recording()
        caption=self.telegram.caption(self.archive,row)
        self.assertEqual(len(caption.splitlines()),4)
        for noise in (row['camera'],row['key'],row['local_path'],'987','MB','#','Archive:','1980000000'):
            with self.subTest(noise=noise):self.assertNotIn(noise,caption)

    def test_cross_midnight_uses_start_date_and_full_end_date(self):
        self.assertEqual(self.telegram.caption(self.archive,
            self.recording('2026-10-05T23:59:00+07:00',120)),
            '🎥 CAM GIỮA NHÀ\n📅 05/10/2026\n🕒 23:59:00 – 06/10/2026 00:01:00\n⏱ 02 phút')

    def test_year_transition_and_future_date_keep_full_end_date(self):
        self.assertEqual(self.telegram.caption(self.archive,
            self.recording('2069-12-31T23:59:59+07:00',2)),
            '🎥 CAM GIỮA NHÀ\n📅 31/12/2069\n🕒 23:59:59 – 01/01/2070 00:00:01\n⏱ 02 giây')

    def test_duration_preserves_seconds_and_supports_hours(self):
        for duration,label in ((1,'01 giây'),(59,'59 giây'),(60,'01 phút'),
                               (90,'01 phút 30 giây'),(3600,'01 giờ'),
                               (3661,'01 giờ 01 phút 01 giây'),(90000,'25 giờ')):
            with self.subTest(duration=duration):
                self.assertEqual(self.telegram.caption(self.archive,self.recording(duration=duration)).splitlines()[-1],
                                 '⏱ '+label)

    def test_subsecond_duration_has_nonempty_label(self):
        self.assertTrue(self.telegram.caption(self.archive,self.recording(duration=.75)).endswith('⏱ 01 giây'))

    def test_local_display_zone_not_upload_or_source_zone_controls_date(self):
        row=self.recording('2026-10-05T18:30:00+00:00',300)
        self.assertEqual(self.telegram.caption(self.archive,row),
            '🎥 CAM GIỮA NHÀ\n📅 06/10/2026\n🕒 01:30:00 – 01:35:00\n⏱ 05 phút')

    def test_display_name_rename_does_not_mutate_recording_identity(self):
        row=self.recording()
        before=dict(row)
        self.archive.camera_name.return_value='CAM MỚI ✓'
        self.assertTrue(self.telegram.caption(self.archive,row).startswith('🎥 CAM MỚI ✓\n'))
        self.assertEqual(row,before)

    def test_non_multi_channel_caption_is_unchanged(self):
        self.settings.multi_channel_routing=False
        row=self.recording()
        zone=get_zone(self.settings.timezone)
        beginning=datetime.fromisoformat('2026-10-05T14:30:00+07:00').astimezone(zone)
        ending=beginning+timedelta(minutes=5)
        self.assertEqual(self.telegram.caption(self.archive,row),
            f"CAM GIỮA NHÀ | {beginning.isoformat()} → {ending.isoformat()}\nArchive: {row['key']}")


if __name__=='__main__':unittest.main()
