"""Local source image validation. No production mounts, network traffic or credentials."""
import http.cookiejar
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
import urllib.request

import pytest

ROOT=Path(__file__).resolve().parents[2]


def docker(*args, input=None):
    env=os.environ.copy()
    for key in ['DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH']:
        env.pop(key,None)
    result=subprocess.run(['docker','--host=unix:///var/run/docker.sock',*args],input=input,text=True,capture_output=True,env=env)
    if result.returncode:
        raise RuntimeError('Docker smoke command failed; output suppressed to protect runtime configuration')
    return result.stdout


@pytest.mark.skipif(os.environ.get('SENTINEL_DOCKER_TESTS')!='1',reason='opt-in built local image and Docker socket')
def test_source_compose_nonroot_browser_auth_and_restart_persistence(tmp_path):
    image='goofish-sentinel:cloud-test'
    inspect=json.loads(docker('image','inspect',image))[0]
    assert inspect['Config']['User']=='sentinel'
    fixture=tmp_path/'deployment'
    fixture.mkdir()
    shutil.copy(ROOT/'compose.sentinel.yaml',fixture/'compose.sentinel.yaml')
    (fixture/'.env').write_text('WEB_USERNAME=fixture-admin\nWEB_PASSWORD=offline-fixture-password\n')
    (fixture/'config.json').write_text('[]')
    for name in ['data','state','prompts','logs','images','jsonl','price_history']:
        (fixture/name).mkdir()
    model=json.loads(docker('compose','--project-directory',str(fixture),'-f',str(fixture/'compose.sentinel.yaml'),'config','--format','json'))
    app=model['services']['app']
    assert app['build']['dockerfile']=='Dockerfile.sentinel'
    assert app['ports']==[{'mode':'ingress','host_ip':'127.0.0.1','target':8000,'published':'8000','protocol':'tcp'}]
    assert not app.get('privileged') and app.get('network_mode')!='host'
    assert not any('docker.sock' in v['source'] for v in app['volumes'])
    assert len(app['volumes'])==9
    assert app['healthcheck']['test'][0]=='CMD'
    name='sentinel-smoke-'+secrets.token_hex(5)
    network=name+'-internal'
    try:
        docker('network','create','--internal',network)
        mounts=[]
        for source,target in [('.env','.env'),('config.json','config.json'),*[(n,n) for n in ['data','state','prompts','logs','images','jsonl','price_history']]]:
            mounts+=['-v',f'{fixture/source}:/app/{target}']
        docker('run','-d','--name',name,'--network',network,'-p','127.0.0.1::8000','--env-file',str(fixture/'.env'),
               '-e','APP_DATABASE_FILE=/app/data/app.sqlite3',*mounts,image)
        inspected=json.loads(docker('inspect',name))[0]
        assert inspected['State']['Running']
        assert inspected['HostConfig']['PortBindings']['8000/tcp'][0]['HostIp']=='127.0.0.1'
        # Docker internal networks can suppress published-port NAT. Probe loopback
        # inside the container so this test cannot generate external traffic.
        client_script = r"""
import http.cookiejar, json, time, urllib.request, urllib.error
jar=http.cookiejar.MozillaCookieJar('/app/data/.smoke-cookies')
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(jar))
base='http://127.0.0.1:8000'
for attempt in range(100):
    try:
        if opener.open(base+'/health',timeout=2).status==200: break
    except OSError: pass
    time.sleep(.2)
else: raise AssertionError('health failed')
def request(path, body=None):
    req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'})
    with opener.open(req,timeout=5) as response: return json.load(response)
if __import__('os').path.exists(jar.filename):
    jar.load(ignore_discard=True)
    assert len(request('/api/market/tasks')['items'])==1
    jobs=request('/api/market/revisits')['items']
    assert len(jobs)==1 and jobs[0]['status']=='pending'
else:
    try: request('/api/market/tasks')
    except urllib.error.HTTPError as exc: assert exc.code==401
    else: raise AssertionError('unauthenticated API was not challenged')
    request('/auth/status',{'username':'fixture-admin','password':'offline-fixture-password'})
    task=request('/api/market/tasks',{'task_name':'Docker offline fixture','keyword':'fixture','enabled':False,'notifications':False})
    assert task['task']['mode']=='market_tracking'
    from src.infrastructure.persistence.market_repository import MarketRepository
    from src.services.market_tracking_service import MarketTrackingService
    from src.services.item_lifecycle_service import ItemLifecycleService
    repo=MarketRepository()
    manager=MarketTrackingService(repo)
    manager.update_task(task['task']['id'],{'enabled':True})
    ItemLifecycleService(repo).discover(task['task']['id'],{'item_id':'880001','title':'Offline fixture','price':'1234','seller_id':'880099'},'docker-fixture')
    manager.pause_all()
    jar.save(ignore_discard=True)
"""
        docker('exec','-i',name,'python','-',input=client_script)
        assert (fixture/'data/.session_secret').stat().st_mode & 0o777==0o600
        assert docker('exec',name,'id','-u').strip()=='1000'
        for module in ['market_runner.py','market_statistics_service.py','item_lifecycle_service.py']:
            expected=hashlib.sha256((ROOT/'src/services'/module).read_bytes()).hexdigest()
            code=f"import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('/app/src/services/{module}').read_bytes()).hexdigest())"
            assert docker('exec',name,'python','-c',code).strip()==expected
        docker('exec','-i',name,'python','-',input="""import asyncio
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True)
        page=await browser.new_page()
        await page.goto('data:text/html,<title>offline fixture</title>')
        assert await page.title()=='offline fixture'
        await browser.close()
asyncio.run(main())
""")
        docker('restart',name)
        docker('exec','-i',name,'python','-',input=client_script)

    finally:
        result=subprocess.run(['docker','--host=unix:///var/run/docker.sock','logs',name],capture_output=True,text=True)
        (tmp_path/'docker-fixture.log').write_text(result.stdout+result.stderr)
        subprocess.run(['docker','--host=unix:///var/run/docker.sock','rm','-f',name],capture_output=True)
        subprocess.run(['docker','--host=unix:///var/run/docker.sock','network','rm',network],capture_output=True)
