"""Vendor SDK libraries stay in the native child; parent HTTPS stays verified."""
import io
import json
import os
from pathlib import Path
import ssl
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from archive_app.core import Settings, get_zone
from archive_app.sd_source import HCNetSDKSource
from archive_app.telegram import Telegram


class SDKTLSIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mycam-sdk-tls-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.sdk = self.root / 'sdk'
        self.cache = self.root / 'cache'
        self.parent_environment = {
            'HCNETSDK_DIR': str(self.sdk),
            'LD_LIBRARY_PATH': '/system-only/lib',
            'SSL_CERT_FILE': '/system-only/ca.pem',
            'SSL_CERT_DIR': '/system-only/certs',
            'TELEGRAM_BOT_TOKEN': 'synthetic-secret-not-a-token',
            'TELEGRAM_BOT_TOKEN_FILE': '/synthetic/private/token',
            'TELEGRAM_API_HASH': 'synthetic-private-app-hash',
            'TELEGRAM_OWNER_USER_ID': '42',
            'TELEGRAM_EXTRA_FUTURE_SECRET': 'synthetic-future-secret',
            'DASHBOARD_PASSWORD': 'synthetic-dashboard-secret',
            'DASHBOARD_TOKEN': 'synthetic-dashboard-token',
            'UNRELATED_SETTING': 'retained',
        }

    def provider(self):
        return HCNetSDKSource('192.168.1.2', 8000, 'admin',
                             'synthetic-camera-password', 1, get_zone('UTC+07:00'),
                             1024, worker_command=[sys.executable, '-m', 'synthetic_native_helper'],
                             cache_dir=self.cache)

    @staticmethod
    def fake_process():
        process = Mock()
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO()
        process.poll.return_value = 0
        process.wait.return_value = 0
        return process

    def test_vendor_search_path_is_child_only_and_parent_ca_environment_is_unchanged(self):
        with patch.dict(os.environ, self.parent_environment, clear=True):
            parent_before = dict(os.environ)
            provider = self.provider()
            with patch('archive_app.sd_source.subprocess.Popen', return_value=self.fake_process()) as popen, \
                    patch.object(provider, '_exchange', return_value=None):
                with provider:
                    provider.reader.join(timeout=1)
                    self.assertFalse(provider.reader.is_alive())
                    child = popen.call_args.kwargs['env']
                    expected = os.pathsep.join((str(self.sdk), str(self.sdk/'HCNetSDKCom'), '/system-only/lib'))
                    self.assertEqual(child['LD_LIBRARY_PATH'], expected)
                    self.assertEqual(child['MYCAM_NATIVE_PARENT_PID'], str(os.getpid()))
                    self.assertEqual(child['CACHE_DIR'], str(self.cache))
                    self.assertEqual(child['SSL_CERT_FILE'], parent_before['SSL_CERT_FILE'])
                    self.assertEqual(child['SSL_CERT_DIR'], parent_before['SSL_CERT_DIR'])
                    self.assertEqual(dict(os.environ), parent_before)
                self.assertEqual(dict(os.environ), parent_before)

    def test_native_child_strips_all_telegram_and_dashboard_secrets(self):
        with patch.dict(os.environ, self.parent_environment, clear=True):
            provider = self.provider()
            with patch('archive_app.sd_source.subprocess.Popen', return_value=self.fake_process()) as popen, \
                    patch.object(provider, '_exchange', return_value=None) as exchange:
                with provider:
                    provider.reader.join(timeout=1)
                    child = popen.call_args.kwargs['env']
                    self.assertFalse(any(name.startswith('TELEGRAM_') for name in child))
                    self.assertNotIn('DASHBOARD_PASSWORD', child)
                    self.assertNotIn('DASHBOARD_TOKEN', child)
                    self.assertEqual(child['UNRELATED_SETTING'], 'retained')
                    self.assertNotIn('synthetic-camera-password', json.dumps(popen.call_args.args))
                    self.assertNotIn('synthetic-camera-password', json.dumps(child))
                    self.assertEqual(exchange.call_args.args[0]['password'], 'synthetic-camera-password')
                    self.assertEqual(provider.config['password'], '')

    def test_child_sdk_path_works_without_inherited_ld_library_path(self):
        with patch.dict(os.environ, {'HCNETSDK_DIR':str(self.sdk)}, clear=True):
            provider = self.provider()
            with patch('archive_app.sd_source.subprocess.Popen', return_value=self.fake_process()) as popen, \
                    patch.object(provider, '_exchange', return_value=None):
                with provider:
                    provider.reader.join(timeout=1)
                    self.assertEqual(popen.call_args.kwargs['env']['LD_LIBRARY_PATH'],
                                     os.pathsep.join((str(self.sdk), str(self.sdk/'HCNetSDKCom'))))
                    self.assertNotIn('LD_LIBRARY_PATH', os.environ)

    def test_sdk_compose_does_not_pollute_parent_library_or_ca_paths(self):
        overlay = (Path(__file__).resolve().parents[1]/'compose.sdk.yaml').read_text(encoding='utf-8')
        self.assertIn('HCNETSDK_DIR: /opt/hcnetsdk', overlay)
        self.assertNotIn('LD_LIBRARY_PATH:', overlay)
        self.assertNotIn('SSL_CERT_FILE:', overlay)
        self.assertNotIn('SSL_CERT_DIR:', overlay)
        self.assertIn('read_only: true', overlay)

    def test_parent_telegram_https_keeps_certificate_verification_enabled(self):
        context = ssl._create_default_https_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        settings = Settings(self.root/'state', self.cache, self.root/'input', 'UTC+07:00',
                            token='synthetic-not-a-token', owner_user_id=42)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok":true,"result":{"id":900,"is_bot":true}}'
        with patch('archive_app.telegram.urllib.request.urlopen', return_value=response) as urlopen, \
                patch('ssl._create_unverified_context', side_effect=AssertionError('TLS verification must remain enabled')):
            self.assertEqual(Telegram(settings).request('getMe', {})['id'], 900)
        self.assertNotIn('context', urlopen.call_args.kwargs)
        self.assertEqual(urlopen.call_args.args[0].full_url.split(':',1)[0], 'https')


if __name__ == '__main__':
    unittest.main()
