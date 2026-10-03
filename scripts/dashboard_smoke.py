"""Fresh, isolated CI Compose only. Never run against a production dashboard.

Uses synthetic credentials on empty GitHub runner volumes, then verifies they
survive dashboard restart. Does not print credentials, cookies or CSRF tokens.
"""
import argparse
import http.client
import json


def run(phase, host='127.0.0.1', port=8080):
    cookie = ''
    csrf = ''
    username = 'ci_dashboard_admin'
    password = 'ci-synthetic-password-2026'

    def request(method, path, body=None, *, session=None, csrf_value=None):
        conn = http.client.HTTPConnection(host, port, timeout=30)
        headers = {'Cookie': cookie if session is None else session,
                   'X-CSRF-Token': csrf if csrf_value is None else csrf_value}
        if body is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(body).encode()
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        payload = json.loads(raw) if response.getheader('Content-Type', '').startswith('application/json') else raw
        result = (response.status, payload, response.getheader('Set-Cookie', ''))
        conn.close()
        return result

    assert request('GET', '/')[0] == 200
    assert request('GET', '/api/cameras')[0] == 401
    if phase == 'bootstrap':
        assert request('POST', '/api/login', {'username': 'admin', 'password': 'wrong'})[0] == 401
        code, data, header = request('POST', '/api/login', {'username': 'admin', 'password': 'admin'})
        assert code == 200 and data['account']['password_change_required']
        cookie, csrf = header.split(';')[0], data['csrf_token']
        assert request('GET', '/api/cameras')[0] == 409
        assert request('GET', '/api/account')[1]['username'] == 'admin'
        _, _, second_header = request('POST', '/api/login', {'username': 'admin', 'password': 'admin'})
        second_cookie = second_header.split(';')[0]
        fields = {'current_password': 'admin', 'username': username, 'new_password': password}
        assert request('POST', '/api/account', fields, csrf_value='wrong')[0] == 403
        code, data, header = request('POST', '/api/account', fields)
        assert code == 200 and data['credentials_updated'] and not data['authenticated']
        assert 'Max-Age=0' in header
        assert request('GET', '/api/account', session=second_cookie)[0] == 401
    assert request('POST', '/api/login', {'username': 'admin', 'password': 'admin'})[0] == 401
    code, data, header = request('POST', '/api/login', {'username': username, 'password': password})
    assert code == 200 and not data['account']['password_change_required']
    cookie, csrf = header.split(';')[0], data['csrf_token']
    assert 'HttpOnly' in header and 'SameSite=Strict' in header
    assert request('GET', '/api/cameras')[0] == 200
    assert request('GET', '/api/account')[1]['username'] == username
    assert request('POST', '/api/logout', {})[0] == 200
    assert request('GET', '/api/cameras')[0] == 401
    print(f'DASHBOARD_LOGIN: phase={phase} username+password=OK initial-change=OK sessions=revoked auth=persisted result=OK')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('bootstrap', 'persisted'))
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    run(args.phase, args.host, args.port)
