import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from archive_app.core import Settings, validate_sd_collector_url


class CollectorSettingsTests(unittest.TestCase):
    def test_opt_in_only(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertEqual((settings.sd_collector_url, settings.sd_collector_token), ('', ''))

    def test_private_and_tailnet_endpoints(self):
        for value in ('http://100.92.56.98:8787', 'http://192.168.2.3:8787/',
                      'https://10.1.0.5:443', 'http://127.0.0.1:12345',
                      'http://[fd7a:115c:a1e0::1]:8787'):
            with self.subTest(value=value):
                self.assertEqual(validate_sd_collector_url(value), value.rstrip('/'))

    def test_reject_non_peer_or_credential_urls(self):
        for value in ('http://example.com:8787', 'http://8.8.8.8:8787',
                      'http://169.254.169.254:80', 'http://192.168.2.3',
                      'ftp://192.168.2.3:8787', 'http://user:pass@192.168.2.3:8787',
                      'http://192.168.2.3:8787/x', 'http://192.168.2.3:8787?token=x',
                      'http://192.168.2.3:8787#x', 'http://192.168.2.3:0',
                      'http://192.168.2.3:99999', 'http://[fe80::1%eth0]:8787'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_sd_collector_url(value)

    def test_token_required_without_leaking(self):
        for token in ('', 'short-secret', 'x' * 31, 'x' * 513, 'x' * 31 + '\n'):
            with self.subTest(length=len(token)), patch.dict(os.environ, {
                    'SD_COLLECTOR_URL': 'http://100.92.56.98:8787',
                    'SD_COLLECTOR_TOKEN': token}, clear=True):
                with self.assertRaises(ValueError) as error:
                    Settings.from_env()
                if token:
                    self.assertNotIn(token, str(error.exception))

    def test_token_file(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'token'
            filename.write_text('f' * 64 + '\n')
            with patch.dict(os.environ, {
                    'SD_COLLECTOR_URL': 'http://100.92.56.98:8787/',
                    'SD_COLLECTOR_TOKEN_FILE': str(filename)}, clear=True):
                settings = Settings.from_env()
            self.assertEqual(settings.sd_collector_url, 'http://100.92.56.98:8787')
            self.assertEqual(settings.sd_collector_token, 'f' * 64)


if __name__ == '__main__':
    unittest.main()
