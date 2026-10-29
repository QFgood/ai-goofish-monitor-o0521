"""Opt-in real Chromium UI smoke with a local fixture DB; never touches Goofish."""
import os
from pathlib import Path
import socket
import subprocess
import time
from datetime import timedelta
import urllib.request

import pytest
from playwright.sync_api import sync_playwright, expect
from src.infrastructure.persistence.market_repository import MarketRepository
from src.services.item_lifecycle_service import ItemLifecycleService
from src.services.market_tracking_service import MarketTrackingService
from src.services.market_types import now, stamp

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(os.environ.get('SENTINEL_BROWSER_TESTS') != '1', reason='opt-in local Chromium and built frontend')
def test_market_dashboard_real_browser(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / 'fixture.sqlite3'
    repo = MarketRepository(str(db))
    service = MarketTrackingService(repo)
    task = service.create_task({'task_name':'离线 Fixture · Mac mini', 'keyword':'Mac mini', 'notifications':False})
    lifecycle = ItemLifecycleService(repo)
    for item_id, price in [('990001','3000'),('990002','2800')]:
        lifecycle.discover(task['id'], {'item_id':item_id,'title':'Mac mini M4 16GB 256GB · 离线 Fixture','price':price,'seller_id':'990099','seller_nickname':'离线卖家 Fixture'}, 'fixture-discovery', now()-timedelta(days=3))
        lifecycle.observe(item_id,'fixture-price-update',raw_status=0,price=price,at=now()-timedelta(days=2))
    lifecycle.observe('990001','fixture-sold',raw_status=1,price='2900',at=now()-timedelta(days=1))
    service.pause_all()  # application scheduler can never start real collection
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env.update(APP_DATABASE_FILE=str(db), WEB_USERNAME='fixture-admin', WEB_PASSWORD='offline-fixture-password',
               WEB_SESSION_SECRET='offline-fixture-secret-at-least-32bytes')
    log = (tmp_path/'server.log').open('w')
    server = subprocess.Popen([os.sys.executable,'-m','uvicorn','src.app:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(50):
            try:
                if opener.open(f'http://127.0.0.1:{port}/health',timeout=1).status==200:
                    break
            except OSError:
                pass
            time.sleep(.1)
        else:
            pytest.fail('local fixture server did not start')
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(executable_path='/usr/bin/chromium',headless=True)
            context=browser.new_context(viewport={'width':1440,'height':1000},locale='zh-CN')
            page=context.new_page()
            errors=[]
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{port}/login')
            page.locator('#username').fill('fixture-admin')
            page.locator('#password').fill('offline-fixture-password')
            page.locator('button[type=submit]').click()
            page.wait_for_url('**/dashboard')
            page.get_by_role('link',name='市场行情').click()
            expect(page.get_by_role('heading',name='市场行情',exact=True)).to_be_visible()
            expect(page.get_by_text('商品追踪明细 · 2 件')).to_be_visible()
            expect(page.get_by_role('img',name='市场价格曲线（元），空值处断开')).to_be_visible()
            assert page.locator('circle title').count()>0
            page.get_by_text('不同配置价格比较',exact=True).click()
            expect(page.get_by_role('columnheader',name='已售参考价中位数 / 样本')).to_be_visible()
            screenshot_dir=ROOT/'docs/screenshots'
            if os.environ.get('SENTINEL_CAPTURE_SCREENSHOTS')=='1':
                page.screenshot(path=str(screenshot_dir/'market-desktop.png'),full_page=True)
            page.get_by_role('button',name='Mac mini M4 16GB 256GB · 离线 Fixture',exact=True).first.click()
            expect(page.get_by_label('商品生命周期历史')).to_be_visible()
            expect(page.get_by_text('实际成交金额：未知',exact=False)).to_be_visible()
            page.get_by_role('button',name='关闭历史').click()
            page.set_viewport_size({'width':390,'height':844})
            page.evaluate("document.documentElement.classList.add('dark')")
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            if os.environ.get('SENTINEL_CAPTURE_SCREENSHOTS')=='1':
                page.screenshot(path=str(screenshot_dir/'market-mobile-dark.png'),full_page=True)
            page.get_by_role('button',name='新建市场任务').click()
            expect(page.get_by_role('heading',name='新建独立市场追踪任务（无需 LLM）')).to_be_visible()
            assert errors==[]
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=15)
        log.close()
