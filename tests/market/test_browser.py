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
def test_results_distribution_home_browser(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / 'fixture.sqlite3'
    repo = MarketRepository(str(db))
    monkeypatch.setenv('APP_DATABASE_FILE',str(db))
    import asyncio
    from src.services.result_storage_service import save_result_record
    from src.services.price_history_service import record_market_snapshots
    async def seed():
        for index, price in enumerate([25,5000,5100,5200,5300,5400]):
            item={'商品ID':str(990000+index),'商品标题':f'离线 Fixture · {price}元商品','当前售价':str(price),
                  '商品链接':f'https://www.goofish.com/item?id={990000+index}',
                  '商品主图链接':'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200"><rect width="400" height="200" fill="lightblue"/></svg>'}
            await save_result_record({'爬取时间':stamp(),'搜索关键字':'mini','任务名称':'离线 Fixture · Mini',
                '商品信息':item,'卖家信息':{},'ai_analysis':{'is_recommended':False,'reason':'离线 Fixture','analysis_source':'ai'}},'mini')
            record_market_snapshots(keyword='mini',task_name='离线 Fixture · Mini',items=[item],run_id='browser',snapshot_time=stamp())
    asyncio.run(seed())
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env.update(APP_DATABASE_FILE=str(db), WEB_USERNAME='fixture-admin', WEB_PASSWORD='offline-fixture-password',
               WEB_SESSION_SECRET='offline-fixture-secret-at-least-32bytes',WEB_AUTH_ENABLED='false')
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
            page.goto(f'http://127.0.0.1:{port}/market')
            page.wait_for_url('**/results')
            expect(page.get_by_role('heading',name='结果查看',exact=True)).to_be_visible()
            assert page.get_by_role('link',name='市场行情').count()==0
            expect(page.get_by_role('img',name='商品价格区间分布柱状图')).to_be_visible()
            expect(page.get_by_text('¥5,200',exact=True).first).to_be_visible()
            assert page.get_by_text('离线 Fixture · 25元商品',exact=True).count()==0
            checkbox=page.get_by_label('排除疑似低价引流（至少5件样本，低于中位数20%）')
            checkbox.uncheck()
            expect(page.get_by_text('离线 Fixture · 25元商品',exact=True)).to_be_visible()
            checkbox.check()
            expect(page.get_by_text('离线 Fixture · 25元商品',exact=True)).to_have_count(0)
            page.get_by_role('button',name='¥5000～＜¥5100，1件',exact=True).click()
            expect(page.get_by_text('有效价格 1 件',exact=True)).to_be_visible()
            page.get_by_role('button',name='清除价格范围',exact=True).click()
            expect(page.get_by_text('有效价格 5 件',exact=True)).to_be_visible()
            screenshot_dir=ROOT/'docs/screenshots'
            if os.environ.get('SENTINEL_CAPTURE_SCREENSHOTS')=='1':
                page.screenshot(path=str(screenshot_dir/'results-distribution.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            page.evaluate("document.documentElement.classList.add('dark')")
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            if os.environ.get('SENTINEL_CAPTURE_SCREENSHOTS')=='1':
                page.screenshot(path=str(screenshot_dir/'results-distribution-mobile.png'),full_page=True)
            page.reload()
            expect(page.get_by_role('heading',name='结果查看',exact=True)).to_be_visible()
            assert page.locator('#password').count()==0
            assert errors==[]
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=15)
        log.close()
