"""Persistent administrator auth and real loopback HTTP; synthetic credentials."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from archive_app.core import Settings
from archive_app.dashboard import DashboardHandler, DashboardServer
from archive_app.dashboard_auth import DashboardAuth, PBKDF2_ITERATIONS


class AuthFixture(unittest.TestCase):
    def setUp(self):
        self.parent = (Path(__file__).parent if os.name == 'nt' else Path(tempfile.gettempdir())).resolve()
        self.root = self.parent / ('.tmp-dashboard-auth-' + uuid.uuid4().hex)
        self.root.mkdir()

    def tearDown(self):
        self.assertEqual(self.root.resolve().parent, self.parent)
        shutil.rmtree(self.root)

    def row(self, auth):
        with closing(sqlite3.connect(auth.path)) as connection:
            connection.row_factory = sqlite3.Row
            return dict(connection.execute('SELECT * FROM administrator').fetchone())


class DashboardAuthStoreTests(AuthFixture):
    def test_default_credentials_bootstrap_one_row_and_private_mode(self):
        auth = DashboardAuth(self.root)
        self.assertEqual(auth.authenticate('admin', 'admin'),
                         {'username': 'admin', 'password_change_required': True, 'version': 1})
        self.assertEqual(auth.path.name, 'dashboard_auth.sqlite')
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(auth.path.stat().st_mode), 0o600)
        row = self.row(auth)
        self.assertEqual(row['iterations'], 600000)
        self.assertEqual(PBKDF2_ITERATIONS, 600000)
        self.assertEqual(len(row['password_hash']), 32)
        self.assertEqual(len(row['password_salt']), 32)
        self.assertNotIn(b'admin', row['password_hash'])

    def test_independent_installs_have_unique_salts_and_hashes(self):
        one, two = DashboardAuth(self.root / 'one'), DashboardAuth(self.root / 'two')
        self.assertNotEqual(self.row(one)['password_salt'], self.row(two)['password_salt'])
        self.assertNotEqual(self.row(one)['password_hash'], self.row(two)['password_hash'])

    def test_concurrent_bootstrap_creates_one_initial_account(self):
        with ThreadPoolExecutor(max_workers=6) as executor:
            stores = list(executor.map(lambda _: DashboardAuth(self.root), range(6)))
        self.assertTrue(all(store.account()['version'] == 1 for store in stores))
        self.assertIsNotNone(stores[0].authenticate('admin', 'admin'))
        with closing(sqlite3.connect(stores[0].path)) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM administrator').fetchone()[0], 1)

    def test_credential_change_persists_and_does_not_reset_after_restart(self):
        auth = DashboardAuth(self.root)
        before = self.row(auth)
        auth.change('admin', 'admin_new', 'changed-password-123', 1)
        restarted = DashboardAuth(self.root)
        self.assertIsNone(restarted.authenticate('admin', 'admin'))
        self.assertEqual(restarted.authenticate('admin_new', 'changed-password-123'),
                         {'username': 'admin_new', 'password_change_required': False, 'version': 2})
        after = self.row(restarted)
        self.assertNotEqual(before['password_salt'], after['password_salt'])
        self.assertNotEqual(before['password_hash'], after['password_hash'])

    def test_wrong_credentials_and_invalid_types_are_not_authenticated(self):
        auth = DashboardAuth(self.root)
        for username, password in [('wrong', 'admin'), ('admin', 'wrong'), (None, None),
                                   ('admin', True), ('admin', []), ('admin', 'x' * 129),
                                   ('admin', ''), ('é', 'admin'), ('admin', '\ud800')]:
            with self.subTest(username=username):
                self.assertIsNone(auth.authenticate(username, password))

    def test_update_validation_and_wrong_current_password_do_not_mutate_state(self):
        auth = DashboardAuth(self.root)
        before = self.row(auth)
        for username, password in [('ab', 'valid-password'), ('space name', 'valid-password'),
                                   ('a' * 65, 'valid-password'), ('valid_name', 'short'),
                                   ('valid_name', 'x' * 129), (False, 'valid-password'),
                                   ('valid_name', None)]:
            with self.subTest(username=username), self.assertRaises(ValueError):
                auth.change('admin', username, password, 1)
        with self.assertRaises(PermissionError):
            auth.change('wrong', 'valid_name', 'valid-password', 1)
        self.assertEqual(self.row(auth), before)

    def test_password_bounds_and_username_allowed_characters(self):
        auth = DashboardAuth(self.root)
        auth.change('admin', 'A_b.c-9', '12345678', 1)
        self.assertIsNotNone(auth.authenticate('A_b.c-9', '12345678'))
        auth.change('12345678', 'x' * 64, 'x' * 128, 2)
        self.assertIsNotNone(auth.authenticate('x' * 64, 'x' * 128))

    def test_stale_credential_version_cannot_change_account(self):
        auth = DashboardAuth(self.root)
        auth.change('admin', 'admin', 'new-password', 1)
        with self.assertRaises(PermissionError):
            auth.change('new-password', 'other', 'another-password', 1)
        self.assertEqual(auth.account()['version'], 2)

    def test_concurrent_credential_changes_have_one_winner_and_increment_once(self):
        auth = DashboardAuth(self.root)
        def change(index):
            try:
                auth.change('admin', 'admin_' + str(index), 'changed-password-' + str(index), 1)
                return True
            except PermissionError:
                return False
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(change, range(2)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(auth.account()['version'], 2)


class DashboardAuthHTTPTests(AuthFixture):
    def setUp(self):
        super().setUp()
        self.settings = Settings(self.root / 'state', self.root / 'cache', self.root / 'input', 'UTC+07:00', min_free_bytes=0)
        self.settings.input_dir.mkdir()
        with patch.dict(os.environ, {'DASHBOARD_COOKIE_SECURE': 'false'}):
            self.server = DashboardServer(('127.0.0.1', 0), self.settings, token='obsolete-token-never-authorizes')
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
        self.thread.start()
        self.cookie = ''
        self.csrf = ''

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        super().tearDown()

    def request(self, method, path, body=None, headers=None, server=None):
        server = server or self.server
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=15)
        supplied = {'Cookie': self.cookie, 'X-CSRF-Token': self.csrf, **(headers or {})}
        if body is not None:
            supplied['Content-Type'] = 'application/json'
            body = json.dumps(body, ensure_ascii=True).encode()
        conn.request(method, path, body=body, headers=supplied)
        response = conn.getresponse()
        raw = response.read()
        content = json.loads(raw) if response.getheader('Content-Type', '').startswith('application/json') else raw
        result = response.status, content, dict(response.getheaders())
        conn.close()
        return result

    def login(self, username='admin', password='admin'):
        result = self.request('POST', '/api/login', {'username': username, 'password': password})
        self.assertEqual(result[0], 200)
        self.cookie = result[2]['Set-Cookie'].split(';')[0]
        self.csrf = result[1]['csrf_token']
        return result

    def change(self, **changes):
        return self.request('POST', '/api/account',
                            {'current_password': 'admin', 'username': 'new_admin',
                             'new_password': 'synthetic-password-123', **changes})

    def test_public_health_assets_and_unauthenticated_private_endpoints(self):
        self.assertEqual(self.request('GET', '/healthz')[1]['version'], '2.4')
        self.assertEqual(self.request('GET', '/')[0], 200)
        for path in ['/api/account', '/api/status', '/api/cameras', '/api/archive']:
            with self.subTest(path=path):
                code, body, _ = self.request('GET', path)
                self.assertEqual(code, 401)
                self.assertEqual(body['code'], 'authentication_required')

    def test_initial_login_requires_change_and_returns_only_public_account(self):
        code, body, headers = self.login()
        self.assertEqual(body['account'], {'username': 'admin', 'password_change_required': True})
        self.assertEqual(set(body), {'authenticated', 'csrf_token', 'account'})
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertNotIn('Secure', headers['Set-Cookie'])
        account = self.request('GET', '/api/account')[1]
        self.assertEqual(account, {'username': 'admin', 'password_change_required': True, 'csrf_token': self.csrf})

    def test_first_login_gate_covers_read_and_write_protected_api(self):
        self.login()
        for method, path, body in [('GET', '/api/status', None), ('GET', '/api/cameras', None),
                                   ('GET', '/api/calendar?camera=fixture', None), ('GET', '/api/archive', None),
                                   ('POST', '/api/cameras', {'id': 'fixture', 'name': 'fixture'}),
                                   ('PATCH', '/api/cameras/fixture', {'name': 'changed'})]:
            with self.subTest(path=path):
                code, response, _ = self.request(method, path, body)
                self.assertEqual(code, 409)
                self.assertEqual(response['code'], 'password_change_required')
        self.assertEqual(self.request('POST', '/api/cameras', {'id': 'fixture'},
                                      {'X-CSRF-Token': 'wrong'})[0], 409)
        self.assertEqual(self.request('POST', '/api/logout', {})[0], 200)

    def test_credentials_changed_forces_login_and_invalidates_all_sessions(self):
        self.login()
        first_cookie = self.cookie
        self.login()
        second_cookie = self.cookie
        code, body, headers = self.change()
        self.assertEqual((code, body), (200, {'authenticated': False, 'credentials_updated': True}))
        self.assertIn('Max-Age=0', headers['Set-Cookie'])
        self.assertEqual(self.server.sessions, {})
        for cookie in [first_cookie, second_cookie]:
            self.assertEqual(self.request('GET', '/api/account', headers={'Cookie': cookie})[0], 401)
        self.assertEqual(self.request('POST', '/api/login', {'username': 'admin', 'password': 'admin'})[0], 401)
        self.login('new_admin', 'synthetic-password-123')
        self.assertFalse(self.request('GET', '/api/account')[1]['password_change_required'])
        self.assertEqual(self.request('GET', '/api/cameras')[0], 200)

    def test_change_credentials_revokes_sessions_in_other_dashboard_instance(self):
        other = DashboardServer(('127.0.0.1', 0), self.settings)
        thread = threading.Thread(target=other.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
        thread.start()
        try:
            _, _, headers = self.request('POST', '/api/login', {'username': 'admin', 'password': 'admin'}, server=other)
            old_cookie = headers['Set-Cookie'].split(';')[0]
            self.login()
            self.assertEqual(self.change()[0], 200)
            code, body, _ = self.request('GET', '/api/account', headers={'Cookie': old_cookie}, server=other)
            self.assertEqual(code, 401)
            self.assertEqual(body['code'], 'authentication_required')
            self.assertEqual(other.sessions, {})
        finally:
            other.shutdown(); other.server_close(); thread.join(2)

    def test_wrong_current_password_retains_session_and_first_login_gate(self):
        self.login()
        code, body, _ = self.change(current_password='wrong')
        self.assertEqual(code, 401)
        self.assertEqual(body['code'], 'current_password_invalid')
        self.assertTrue(self.request('GET', '/api/account')[1]['password_change_required'])
        self.assertEqual(self.request('GET', '/api/cameras')[0], 409)

    def test_change_requires_csrf_and_same_origin_without_mutating_account(self):
        self.login()
        self.csrf = 'wrong'
        self.assertEqual(self.change()[0], 403)
        self.csrf = self.request('GET', '/api/account')[1]['csrf_token']
        data = {'current_password': 'admin', 'username': 'new_admin', 'new_password': 'synthetic-password-123'}
        self.assertEqual(self.request('POST', '/api/account', data, {'Origin': 'https://outsider.example'})[0], 403)
        self.assertEqual(self.request('GET', '/api/account')[1]['username'], 'admin')

    def test_cross_origin_login_is_rejected_and_matching_origin_is_accepted(self):
        data = {'username': 'admin', 'password': 'admin'}
        self.assertEqual(self.request('POST', '/api/login', data, {'Origin': 'https://outside.example'})[0], 403)
        origin = 'http://127.0.0.1:' + str(self.server.server_port)
        self.assertEqual(self.request('POST', '/api/login', data, {'Origin': origin})[0], 200)

    def test_login_uniform_failures_and_no_legacy_token_bypass(self):
        bodies = [ {'username': 'admin', 'password': 'wrong'}, {'username': 'other', 'password': 'admin'},
                   {'token': 'obsolete-token-never-authorizes'}, {}, {'username': True, 'password': []},
                   {'username': 'admin', 'password': 'x' * 129} ]
        results = [self.request('POST', '/api/login', body)[:2] for body in bodies]
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(results[0][0], 401)
        self.assertFalse((self.settings.state_dir / 'dashboard_token').exists())

    def test_stale_authentication_result_does_not_issue_a_session(self):
        authenticate = self.server.auth.authenticate
        def changed_during_login(username, password):
            old = authenticate(username, password)
            self.server.auth.change('admin', 'other_admin', 'new-password-123', 1)
            return old
        with patch.object(self.server.auth, 'authenticate', side_effect=changed_during_login):
            code, body, headers = self.request('POST', '/api/login', {'username': 'admin', 'password': 'admin'})
        self.assertEqual(code, 401)
        self.assertEqual(body['code'], 'invalid_credentials')
        self.assertNotIn('Set-Cookie', headers)
        self.assertEqual(self.server.sessions, {})

    def test_login_rate_limit_uses_network_peer_and_ignores_spoofed_forwarded_ip(self):
        wrong = {'username': 'admin', 'password': 'wrong'}
        for index in range(10):
            self.assertEqual(self.request('POST', '/api/login', wrong,
                                          {'X-Forwarded-For': '192.0.2.' + str(index + 1)})[0], 401)
        self.assertEqual(self.request('POST', '/api/login', {'username': 'admin', 'password': 'admin'})[0], 429)
        self.server.login_attempts['127.0.0.1'] = [time.time() - 61] * 10
        self.assertEqual(self.login()[0], 200)

    def test_wrong_current_password_rate_limit(self):
        self.login()
        for _ in range(10):
            self.assertEqual(self.change(current_password='wrong')[0], 401)
        self.assertEqual(self.change()[0], 429)
        self.server.account_attempts['127.0.0.1'] = [time.time() - 61] * 10
        self.assertEqual(self.change()[0], 200)

    def test_validation_errors_do_not_clear_initial_gate(self):
        self.login()
        for changes in [{'username': 'a'}, {'username': 'space name'}, {'new_password': 'admin'},
                        {'new_password': 'x' * 129}, {'new_password': None}]:
            with self.subTest(changes=changes):
                self.assertEqual(self.change(**changes)[0], 400)
        self.assertTrue(self.request('GET', '/api/account')[1]['password_change_required'])

    def test_cookie_secure_setting_and_forwarded_proto_not_trusted(self):
        data = {'username': 'admin', 'password': 'admin'}
        self.assertNotIn('Secure', self.request('POST', '/api/login', data, {'X-Forwarded-Proto': 'https'})[2]['Set-Cookie'])
        self.server.cookie_secure = True
        _, _, headers = self.login()
        self.assertIn('; Secure', headers['Set-Cookie'])
        self.assertIn('; Secure', self.request('POST', '/api/logout', {})[2]['Set-Cookie'])

    def test_expired_session_and_logout_cookie_no_longer_authorize(self):
        self.login()
        sid = self.cookie.split('=', 1)[1]
        self.server.sessions[sid]['expires'] = time.time() - 1
        self.assertEqual(self.request('GET', '/api/account')[0], 401)
        self.login()
        self.assertEqual(self.request('POST', '/api/logout', {})[0], 200)
        self.assertEqual(self.request('GET', '/api/account')[0], 401)

    def test_server_restart_keeps_changed_account_and_does_not_bootstrap_admin(self):
        self.login()
        self.assertEqual(self.change()[0], 200)
        restarted = DashboardServer(('127.0.0.1', 0), self.settings)
        try:
            self.assertIsNone(restarted.auth.authenticate('admin', 'admin'))
            self.assertFalse(restarted.auth.authenticate('new_admin', 'synthetic-password-123')['password_change_required'])
        finally:
            restarted.server_close()


class DashboardCookieTests(unittest.TestCase):
    def test_direct_tls_enables_secure_cookie_without_forwarded_headers(self):
        handler = object.__new__(DashboardHandler)
        handler.server = type('Server', (), {'cookie_secure': False})()
        handler.connection = object()
        with patch('archive_app.dashboard.ssl.SSLSocket', object):
            self.assertIn('; Secure', handler.session_cookie('fixture', 12))


if __name__ == '__main__':
    unittest.main()
