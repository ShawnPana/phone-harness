import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
from phone_harness.cloud import _request

parser=argparse.ArgumentParser()
parser.add_argument('--private-dir',type=Path,required=True)
root=parser.parse_args().private_dir.resolve()
root.mkdir(mode=0o700,exist_ok=True)
root.chmod(0o700)
uid='248dcd0ee6ba'  # Main's previously created isolated access-test account.
label='main-sdk-tunnel-20260910'
assert not (root/'result.json').exists(), 'this test already has a receipt'
report={'started_unix_s':time.time(),'uid':uid,'sessions_created':0,'real_phones':0,
        'passed':False,'test_key_revoked':False,'tunnel_stopped':False}
key=None
tunnel=None
base=None
remote=r'''
import json,shlex,urllib.request
from pathlib import Path
values={}
for line in Path('/etc/phone-cloud.env').read_text().splitlines():
    if line.strip() and not line.startswith('#') and '=' in line:
        k,v=line.split('=',1); values[k]=shlex.split(v)[0] if shlex.split(v) else ''
assert values['PHONE_CLOUD_ENV']=='development'
assert values['PHONE_CLOUD_BILLING_MODE']=='disabled'
assert values['PHONE_CLOUD_SHLUT_TARGET']=='staging'
uid='248dcd0ee6ba'; label='main-sdk-tunnel-20260910'
def call(method,path,body=None):
    request=urllib.request.Request('http://127.0.0.1:8723'+path,method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization':'Bearer '+values['PHONE_CLOUD_TOKEN'],'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=10) as response:return json.load(response)
keys=call('GET','/admin/users/'+uid+'/keys')
assert not any(k['label']==label for k in keys), 'test key already exists'
value=call('POST','/admin/users/'+uid+'/keys',{'label':label})
print(json.dumps({'uid':uid,'key':value['key']}))
'''

def status_without_key():
    try:
        with urllib.request.urlopen(base+'/sessions',timeout=5) as response:return response.status
    except urllib.error.HTTPError as error:
        code=error.code;error.close();return code

try:
    mint=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',
                         'phone-harness-development.exe.xyz','sudo python3 -'],
                        input=remote.encode(),capture_output=True,timeout=30)
    assert mint.returncode==0, 'isolated test-key setup failed; inspect matching label before retrying'
    account=json.loads(mint.stdout);key=account['key']
    fd=os.open(root/'account.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as output:output.write(mint.stdout)
    # Fail closed if the documented local forwarding port is already occupied.
    probe=socket.socket();probe.bind(('127.0.0.1',18723));probe.close()
    tunnel=subprocess.Popen(['ssh','-N','-T','-o','BatchMode=yes','-o','ExitOnForwardFailure=yes',
        '-o','ConnectTimeout=10','-o','ServerAliveInterval=30','-o','ServerAliveCountMax=3',
        '-L','127.0.0.1:18723:127.0.0.1:8723','phone-harness-development.exe.xyz'],
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    base='http://127.0.0.1:18723'
    deadline=time.monotonic()+15
    while True:
        assert tunnel.poll() is None, 'owned SSH tunnel exited'
        try:
            with urllib.request.urlopen(base+'/health',timeout=1) as response:
                assert json.load(response)=={'ok':True}
                break
        except (urllib.error.URLError,TimeoutError):
            if time.monotonic()>deadline:raise TimeoutError('SSH tunnel did not become ready')
            time.sleep(.1)
    report['missing_account_key_status']=status_without_key()
    assert report['missing_account_key_status']==401
    env={**os.environ,'PHONE_CLOUD_URL':base,'PHONE_CLOUD_TOKEN':key}
    env.pop('PYTHONPATH',None)
    result=subprocess.run([str(Path(sys.executable).parent/'phone-harness'),'cloud','ls'],
                          env=env,cwd=root,capture_output=True,text=True,timeout=15)
    assert key not in result.stdout+result.stderr, 'client printed account credential'
    assert result.returncode==0, 'installed client could not list isolated account'
    assert result.stdout.strip()=='no live sessions', 'isolated account unexpectedly has sessions'
    report['installed_cli_list_pass']=True
    report['package_path']=__import__('phone_harness.cloud',fromlist=['__file__']).__file__
    report['passed']=True
finally:
    if key:
        # Revoke through the remote authenticated admin route even if the tunnel failed.
        revoke_remote=remote[:remote.index("keys=call(")]+"\nprint(json.dumps(call('POST','/admin/users/'+uid+'/keys/revoke',{'key_hash':"+repr(hashlib.sha256(key.encode()).hexdigest())+"})))\n"
        cleanup=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',
            'phone-harness-development.exe.xyz','sudo python3 -'],input=revoke_remote.encode(),
            capture_output=True,timeout=30)
        report['test_key_revoked']=cleanup.returncode==0 and json.loads(cleanup.stdout).get('revoked') is True
        if report['test_key_revoked'] and tunnel and tunnel.poll() is None and base:
            request=urllib.request.Request(base+'/sessions',headers={'Authorization':'Bearer '+key})
            try:
                with urllib.request.urlopen(request,timeout=5) as response:report['revoked_key_status']=response.status
            except urllib.error.HTTPError as error:
                report['revoked_key_status']=error.code;error.close()
    if tunnel:
        if tunnel.poll() is None:
            tunnel.terminate()
            try:tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:tunnel.kill();tunnel.wait(timeout=5)
        report['tunnel_stopped']=tunnel.poll() is not None
    report['ended_unix_s']=time.time()
    (root/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))

assert report['passed'] and report['test_key_revoked'] and report['revoked_key_status']==401 and report['tunnel_stopped']
print('MEASURED sdk_private_auth_pass=1 revoked_key_denied=1 sessions_created=0 real_phones=0')
