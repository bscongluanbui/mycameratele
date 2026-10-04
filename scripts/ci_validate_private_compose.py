"""Render Docker Compose only: synthetic config, no engine or real credentials."""
from pathlib import Path
import json, os, subprocess

root = Path(__file__).resolve().parents[1]
env = {k: v for k, v in os.environ.items() if not k.startswith(('TELEGRAM_', 'DASHBOARD_', 'ENABLE_UPLOAD', 'KEEP_CACHE', 'CACHE_RETENTION_', 'ERROR_RETENTION_', 'BOT_API_SPOOL_', 'COMPOSE_'))}
def render(local=False, values=None, include_api=False):
    args = ['docker', 'compose', '--env-file', '.env.example', '-f', 'compose.yaml']
    if local: args += ['-f', 'compose.local.yaml', '--profile', 'local-api']
    elif include_api: args += ['--profile', 'local-api']
    args += ['config', '--format', 'json']
    result = subprocess.run(args, cwd=root, env={**env, **(values or {})}, capture_output=True, text=True)
    return result, json.loads(result.stdout) if not result.returncode else None

result, cloud = render()
assert result.returncode == 0, 'Cloud test config failed'
for service in ('archive', 'dashboard'):
    settings = cloud['services'][service]['environment']
    assert settings['ENABLE_UPLOAD'] == 'false' and settings['KEEP_CACHE'] == 'false'
    assert settings['TENANT_ID']=='house01' and settings['TELEGRAM_DESTINATION']=='channel'
    assert settings['MEDIA_MODE']=='remux_copy' and settings['CACHE_RETENTION_HOURS']=='0'
    assert settings['ERROR_RETENTION_HOURS']=='72'
    assert settings['SD_SYNC_INTERVAL_SECONDS']=='900'
    assert settings['TELEGRAM_API_MODE'] == 'cloud'
    assert settings['TELEGRAM_UPLOAD_TRANSPORT'] == 'multipart'
    assert settings['TELEGRAM_LOCAL_UPLOAD_ROOT'] == ''
    assert 'build' not in cloud['services'][service]
assert cloud['services']['dashboard']['ports'][0]['host_ip']=='0.0.0.0'
assert cloud['services']['dashboard']['ports'][0]['published']=='8080'
assert cloud['services']['dashboard']['environment']['DASHBOARD_COOKIE_SECURE']=='false'
_,custom=render(values={'DASHBOARD_BIND_IP':'127.0.0.1','DASHBOARD_PORT':'8090'})
assert custom['services']['dashboard']['ports'][0]['host_ip']=='127.0.0.1'
assert custom['services']['dashboard']['ports'][0]['published']=='8090'
assert render(local=True)[0].returncode != 0, 'Production must require explicit credentials'
# Base/cloud Compose must not expose the archive cache to the Bot API, even
# when its dormant local-api service is explicitly included for rendering.
result,base_with_api=render(include_api=True)
assert result.returncode==0 and 'telegram-bot-api' in base_with_api['services']
assert all(v.get('target')!='/cache' for v in base_with_api['services']['telegram-bot-api']['volumes'])
fixture = {'TELEGRAM_BOT_TOKEN': 'synthetic-not-a-real-token', 'TELEGRAM_OWNER_USER_ID': '42',
           'TELEGRAM_ALLOWED_USER_IDS': '77,88', 'TELEGRAM_API_ID': '123',
           'TELEGRAM_API_HASH': 'synthetic-not-a-real-api-hash'}
result, local = render(True, fixture)
assert result.returncode == 0, 'Production fixture render failed'
assert local['name'] == 'ezviz-telegram-archive'
assert set(local['volumes']) == {'archive-state', 'archive-cache', 'bot-api-state', 'bot-api-spool'}
assert 'ports' not in local['services']['telegram-bot-api']
assert '--temp-dir=/var/lib/telegram-bot-api-spool' in local['services']['telegram-bot-api']['command']
assert '--temp-dir=/tmp' not in local['services']['telegram-bot-api']['command']
spool_mount=next(v for v in local['services']['telegram-bot-api']['volumes'] if v['target']=='/var/lib/telegram-bot-api-spool')
assert spool_mount['type']=='volume' and not spool_mount.get('read_only',False)
assert spool_mount['source']=='bot-api-spool'
worker_spool=next(v for v in local['services']['archive']['volumes'] if v['target']=='/var/lib/telegram-bot-api-spool')
assert worker_spool['type']=='volume' and worker_spool['read_only'] is True
assert worker_spool['source']=='bot-api-spool'
assert local['services']['archive']['environment']['BOT_API_SPOOL_DIR']=='/var/lib/telegram-bot-api-spool'
assert local['services']['archive']['environment']['BOT_API_SPOOL_MAX_GB']=='5'
api_cache=next(v for v in local['services']['telegram-bot-api']['volumes'] if v['target']=='/cache')
assert api_cache['type']=='volume' and api_cache['source']=='archive-cache' and api_cache['read_only'] is True
worker_cache=next(v for v in local['services']['archive']['volumes'] if v['target']=='/cache')
assert worker_cache['type']=='volume' and worker_cache['source']=='archive-cache' and not worker_cache.get('read_only',False)
assert len(local['services']['telegram-bot-api']['volumes'])==3
api_state=next(v for v in local['services']['telegram-bot-api']['volumes'] if v['target']=='/var/lib/telegram-bot-api')
assert api_state['source']=='bot-api-state' and not api_state.get('read_only',False)
init=local['services']['spool-init']
assert init['network_mode']=='none' and init['read_only'] is True and init['user']=='0:0'
assert set(init['cap_drop'])=={'ALL'} and set(init['cap_add'])=={'CHOWN','FOWNER'}
assert len(init['volumes'])==1 and init['volumes'][0]['source']=='bot-api-spool'
assert init['volumes'][0]['target']=='/spool'
assert init['entrypoint']==['python','-c']
assert local['services']['telegram-bot-api']['depends_on']['spool-init']['condition']=='service_completed_successfully'
for service in ('archive', 'dashboard'):
    settings = local['services'][service]['environment']
    assert settings['TELEGRAM_OWNER_USER_ID'] == '42' and settings['TELEGRAM_ALLOWED_USER_IDS'] == '77,88'
    assert settings['TELEGRAM_API_MODE'] == 'local' and settings['TELEGRAM_API_BASE'] == 'http://telegram-bot-api:8081'
    assert settings['TELEGRAM_MAX_BYTES'] == '2000000000'
    assert settings['TELEGRAM_UPLOAD_TRANSPORT'] == 'local_file'
    assert settings['TELEGRAM_LOCAL_UPLOAD_ROOT'] == '/cache'
    assert settings['KEEP_CACHE'] == 'false' and settings['CACHE_RETENTION_HOURS'] == '0'
    assert settings['ERROR_RETENTION_HOURS']=='72'
    assert settings['ENABLE_UPLOAD'] == 'false' and 'build' not in local['services'][service]
result,opt_out=render(True,{**fixture,'TELEGRAM_UPLOAD_TRANSPORT':'multipart'})
assert result.returncode==0, 'Multipart opt-out fixture render failed'
for service in ('archive','dashboard'):
    assert opt_out['services'][service]['environment']['TELEGRAM_UPLOAD_TRANSPORT']=='multipart'
    assert opt_out['services'][service]['environment']['TELEGRAM_LOCAL_UPLOAD_ROOT']=='/cache'
assert set(opt_out['volumes'])==set(local['volumes'])
assert opt_out['services']['telegram-bot-api']['volumes']==local['services']['telegram-bot-api']['volumes']
player_mount=next(v for v in local['services']['dashboard']['volumes'] if v['target']=='/var/lib/telegram-bot-api')
assert player_mount['type']=='volume' and player_mount['read_only'] is True
assert player_mount['source']=='bot-api-state'
assert all(v.get('target')!='/var/lib/telegram-bot-api' for v in local['services']['archive']['volumes'])
print('HOUSE01_COMPOSE: cloud=OK local=OK multipart_opt_out=OK channel=required-at-upload remux=streamcopy retention=0h errors=72h SD=900s volumes=preserved API_cache_mount=local-read-only MP4_transport=local_file spool=disk budget=5GB init=bounded exit=0')
