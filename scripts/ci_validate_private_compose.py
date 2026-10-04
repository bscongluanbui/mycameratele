"""Render Docker Compose only: synthetic config, no engine or real credentials."""
from pathlib import Path
import json, os, subprocess

root = Path(__file__).resolve().parents[1]
env = {k: v for k, v in os.environ.items() if not k.startswith(('TELEGRAM_', 'DASHBOARD_', 'ENABLE_UPLOAD', 'KEEP_CACHE', 'CACHE_RETENTION_', 'COMPOSE_'))}
def render(local=False, values=None):
    args = ['docker', 'compose', '--env-file', '.env.example', '-f', 'compose.yaml']
    if local: args += ['-f', 'compose.local.yaml', '--profile', 'local-api']
    args += ['config', '--format', 'json']
    result = subprocess.run(args, cwd=root, env={**env, **(values or {})}, capture_output=True, text=True)
    return result, json.loads(result.stdout) if not result.returncode else None

result, cloud = render()
assert result.returncode == 0, 'Cloud test config failed'
for service in ('archive', 'dashboard'):
    settings = cloud['services'][service]['environment']
    assert settings['ENABLE_UPLOAD'] == 'false' and settings['KEEP_CACHE'] == 'false'
    assert settings['TENANT_ID']=='house01' and settings['TELEGRAM_DESTINATION']=='channel'
    assert settings['MEDIA_MODE']=='remux_copy' and settings['CACHE_RETENTION_HOURS']=='1'
    assert settings['SD_SYNC_INTERVAL_SECONDS']=='900'
    assert settings['TELEGRAM_API_MODE'] == 'cloud'
    assert 'build' not in cloud['services'][service]
assert cloud['services']['dashboard']['ports'][0]['host_ip']=='0.0.0.0'
assert cloud['services']['dashboard']['ports'][0]['published']=='8080'
assert cloud['services']['dashboard']['environment']['DASHBOARD_COOKIE_SECURE']=='false'
_,custom=render(values={'DASHBOARD_BIND_IP':'127.0.0.1','DASHBOARD_PORT':'8090'})
assert custom['services']['dashboard']['ports'][0]['host_ip']=='127.0.0.1'
assert custom['services']['dashboard']['ports'][0]['published']=='8090'
assert render(local=True)[0].returncode != 0, 'Production must require explicit credentials'
fixture = {'TELEGRAM_BOT_TOKEN': 'synthetic-not-a-real-token', 'TELEGRAM_OWNER_USER_ID': '42',
           'TELEGRAM_ALLOWED_USER_IDS': '77,88', 'TELEGRAM_API_ID': '123',
           'TELEGRAM_API_HASH': 'synthetic-not-a-real-api-hash'}
result, local = render(True, fixture)
assert result.returncode == 0, 'Production fixture render failed'
assert local['name'] == 'ezviz-telegram-archive'
assert set(local['volumes']) == {'archive-state', 'archive-cache', 'bot-api-state'}
assert 'ports' not in local['services']['telegram-bot-api']
for service in ('archive', 'dashboard'):
    settings = local['services'][service]['environment']
    assert settings['TELEGRAM_OWNER_USER_ID'] == '42' and settings['TELEGRAM_ALLOWED_USER_IDS'] == '77,88'
    assert settings['TELEGRAM_API_MODE'] == 'local' and settings['TELEGRAM_API_BASE'] == 'http://telegram-bot-api:8081'
    assert settings['TELEGRAM_MAX_BYTES'] == '2000000000'
    assert settings['KEEP_CACHE'] == 'false' and settings['CACHE_RETENTION_HOURS'] == '1'
    assert settings['ENABLE_UPLOAD'] == 'false' and 'build' not in local['services'][service]
assert all(v.get('target')!='/cache' for v in local['services']['telegram-bot-api'].get('volumes',[]))
player_mount=next(v for v in local['services']['dashboard']['volumes'] if v['target']=='/var/lib/telegram-bot-api')
assert player_mount['type']=='volume' and player_mount['read_only'] is True
assert player_mount['source']=='bot-api-state'
assert all(v.get('target')!='/var/lib/telegram-bot-api' for v in local['services']['archive']['volumes'])
print('HOUSE01_COMPOSE: cloud=OK local=OK channel=required-at-upload remux=streamcopy retention=1h SD=900s volumes=preserved API_cache_mount=none exit=0')
