import os
import unittest
from unittest.mock import patch
from archive_app.core import Settings


class SDSettingsTests(unittest.TestCase):
    def test_defaults_keep_speed_and_concurrency_opt_in(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertEqual((settings.sd_max_files_per_job,
                          settings.sd_download_speed_kbps,
                          settings.sd_download_workers), (10, 0, 1))

    def test_accept_supported_limits(self):
        for batch, speed, workers in ((1, 0, 1), (10, 8192, 5), (100, 32768, 2), (5, 256, 3)):
            with self.subTest(batch=batch, speed=speed, workers=workers), patch.dict(os.environ, {
                'SD_MAX_FILES_PER_JOB': str(batch), 'SD_DOWNLOAD_SPEED_KBPS': str(speed),
                'SD_DOWNLOAD_WORKERS': str(workers)}, clear=True):
                s = Settings.from_env()
                self.assertEqual((s.sd_max_files_per_job, s.sd_download_speed_kbps,
                                  s.sd_download_workers), (batch, speed, workers))

    def test_reject_invalid_values(self):
        limits = {'SD_MAX_FILES_PER_JOB': ('0', '101', '-1', 'true', '1.5', ''),
                  'SD_DOWNLOAD_SPEED_KBPS': ('1', '255', '32769', '-1', 'true', '8192.0', ''),
                  'SD_DOWNLOAD_WORKERS': ('0', '6', '-1', 'false', '2.0', '')}
        for name, values in limits.items():
            for value in values:
                with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}, clear=True):
                    with self.assertRaises(ValueError):
                        Settings.from_env()


if __name__ == '__main__':
    unittest.main()
