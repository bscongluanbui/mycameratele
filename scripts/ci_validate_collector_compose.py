from pathlib import Path
import json,os,subprocess
root=Path.cwd()
env={k:v for k,v in os.environ.items() if not k.startswith(('COMPOSE_','COLLECTOR_','HCNETSDK_','REMOTE_SD_','SD_COLLECTOR_'))}
fixture={'COLLECTOR_IMAGE':'ghcr.io/bscongluanbui/mycameratele:fixture','HCNETSDK_HOST_DIR':str(root), 'COLLECTOR_TOKEN_HOST_FILE':str(root/'Dockerfile')}
r=subprocess.run(['docker','compose','--env-file','.env.example','-f','compose.collector.yaml','config','--format','json'],env={**env,**fixture},text=True,capture_output=True)
assert r.returncode==0,r.stderr
v=json.loads(r.stdout);s=v['services']['collector']
assert s['command']==['collector','--host','0.0.0.0','--port','8787']
assert s['ports'][0]['host_ip']=='100.92.56.98'
assert s['user']=='10001:10001' and s['read_only']
assert not s.get('env_file') and not any(n.startswith('TELEGRAM_') for n in s['environment'])
assert s['environment']['COLLECTOR_CACHE_MAX_GB']=='2'
assert s['environment']['COLLECTOR_MIN_FREE_GB']=='2'
assert len(s['volumes'])==3
fixture={'REMOTE_SD_COLLECTOR_URL':'http://100.92.56.98:8787','SD_COLLECTOR_TOKEN_HOST_FILE':str(root/'Dockerfile')}
r=subprocess.run(['docker','compose','--env-file','.env.example','-f','compose.yaml','-f','compose.remote-sd.yaml','config','--format','json'],env={**env,**fixture},text=True,capture_output=True)
assert r.returncode==0,r.stderr
v=json.loads(r.stdout)
assert v['services']['archive']['environment']['SD_COLLECTOR_URL']==fixture['REMOTE_SD_COLLECTOR_URL']
assert not v['services']['dashboard']['environment'].get('SD_COLLECTOR_URL')
assert not v['services']['dashboard']['environment'].get('SD_COLLECTOR_TOKEN_FILE')
assert v['services']['archive']['environment']['MEDIA_MODE']=='remux_copy'
print('LAN_COLLECTOR_COMPOSE: tailscale-only nonroot no-bot-token archive-only bounded-cache remux-copy exit=0')
