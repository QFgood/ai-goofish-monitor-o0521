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
    task=request('/api/tasks')[0]
    assert task['revisit_enabled'] is True and task['revisit_limit']==2 and task['enabled'] is False
    from src.infrastructure.persistence.sqlite_connection import sqlite_connection
    with sqlite_connection() as conn:
        assert conn.execute('SELECT count(*) FROM result_listing_memberships').fetchone()[0]==1
        assert conn.execute('SELECT seller_id FROM result_listing_items').fetchone()[0]=='889999'
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
    ordinary=request('/api/tasks/',{'task_name':'Docker bounded offline','keyword':'bounded-fixture',
        'decision_mode':'keyword','keyword_rules':['mini'],'enabled':False,'revisit_enabled':True,'revisit_limit':2})
    from src.services.result_storage_service import save_result_record
    import asyncio
    asyncio.run(save_result_record({'任务ID':ordinary['task']['id'],'任务名称':'Docker bounded offline',
        '商品信息':{'商品ID':'889001','商品标题':'Offline Fixture','当前售价':'5000',
            '商品链接':'https://www.goofish.com/item?id=889001'},'卖家信息':{'卖家ID':'889999','卖家昵称':'Offline seller'}},'bounded-fixture'))
    manual=request('/api/tasks/',{'task_name':'Docker empty manual fixture','keyword':'empty-manual',
        'decision_mode':'keyword','keyword_rules':['fixture'],'enabled':True,'revisit_enabled':False})
    accepted=request('/api/tasks/'+str(manual['task']['id'])+'/revisits/run',{})
    for _ in range(50):
        current=next(t for t in request('/api/tasks') if t['id']==manual['task']['id'])
        if current['revisit_run'] and current['revisit_run']['status']!='running':break
        time.sleep(.05)
    assert current['revisit_run']['status']=='empty' and current['revisit_run']['reviewed']==0
    assert current['revisit_enabled'] is False
    jar.save(ignore_discard=True)
"""
        docker('exec','-i',name,'python','-',input=client_script)
        assert (fixture/'data/.session_secret').stat().st_mode & 0o777==0o600
        assert docker('exec',name,'id','-u').strip()=='1000'
        for module in ['src/app.py','src/api/auth.py','src/api/routes/results.py','src/services/result_price_summary_service.py','src/services/result_revisit_service.py',
                       'src/infrastructure/persistence/result_revisit_schema.py','src/services/result_revisit_manager.py','src/api/routes/tasks.py']:
            expected=hashlib.sha256((ROOT/module).read_bytes()).hexdigest()
            code=f"import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('/app/{module}').read_bytes()).hexdigest())"
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

        # Recreate only this fixture app in home mode; existing data stays mounted.
        docker('rm','-f',name)
        docker('run','-d','--name',name,'--network',network,'--network-alias','app',
               '--env-file',str(fixture/'.env'),'-e','APP_DATABASE_FILE=/app/data/app.sqlite3',
               '-e','WEB_AUTH_ENABLED=false',*mounts,image)
        proxy=name+'-gateway'
        caddy='caddy:2.11.2-alpine@sha256:834468128c7696cec0ceea6172f7d692daf645ae51983ca76e39da54a97c570d'
        docker('run','-d','--name',proxy,'--network',network,'-e','NO_PROXY=app,localhost,127.0.0.1',
               '-v',f'{ROOT}/deploy/lan/Caddyfile.home:/etc/caddy/Caddyfile:ro',caddy)
        docker('exec','-i',name,'python','-',input=f"""import json,time,urllib.request
opener=urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
base='http://{proxy}'
for _ in range(100):
    try:
        with opener.open(base+'/auth/session',timeout=2) as response:
            assert json.load(response)['authentication_required'] is False
        break
    except OSError: time.sleep(.2)
else: raise AssertionError('home gateway not ready')
with opener.open(base+'/api/results/files',timeout=5) as response: assert response.status==200
""")

    finally:
        result=subprocess.run(['docker','--host=unix:///var/run/docker.sock','logs',name],capture_output=True,text=True)
        (tmp_path/'docker-fixture.log').write_text(result.stdout+result.stderr)
        subprocess.run(['docker','--host=unix:///var/run/docker.sock','rm','-f',name+'-gateway',name],capture_output=True)
        subprocess.run(['docker','--host=unix:///var/run/docker.sock','network','rm',network],capture_output=True)


@pytest.mark.skipif(os.environ.get('SENTINEL_DOCKER_TESTS')!='1',reason='opt-in Compose CLI')
def test_source_lan_compose_keeps_gateway_only_binding(tmp_path):
    fixture=tmp_path/'lan'
    fixture.mkdir()
    shutil.copy(ROOT/'compose.sentinel.lan.yaml',fixture/'compose.sentinel.lan.yaml')
    (fixture/'.env').write_text('WEB_USERNAME=fixture-admin\nWEB_PASSWORD=offline-fixture-password\n')
    gateway=fixture/'gateway.env'
    hashed='$2a$14$'+'a'*53  # redacted syntax fixture, never used to authenticate
    gateway.write_text("GATEWAY_USERNAME='lanadmin'\nGATEWAY_PASSWORD_HASH='"+hashed+"'\n")
    model=json.loads(docker('compose','--project-directory',str(fixture),'--env-file',str(fixture/'.env'),
                            '--env-file',str(gateway),'-f',str(fixture/'compose.sentinel.lan.yaml'),'config','--format','json'))
    app,proxy=model['services']['app'],model['services']['gateway']
    assert 'ports' not in app
    assert app['build']['dockerfile']=='Dockerfile.sentinel'
    assert proxy['ports']==[{'mode':'ingress','host_ip':'192.168.9.202','target':80,'published':'8000','protocol':'tcp'}]
    assert 'GATEWAY_PASSWORD_HASH' not in proxy.get('environment',{})
    assert app['environment']['WEB_AUTH_ENABLED']=='false'
    assert proxy['volumes'][0]['source'].endswith('Caddyfile.home')
    assert proxy['image'].endswith('@sha256:834468128c7696cec0ceea6172f7d692daf645ae51983ca76e39da54a97c570d')
