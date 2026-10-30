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
    from src.domain.models.task import TaskCreate
    from src.services.task_service import TaskService
    from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
    from src.services.result_revisit_service import ResultRevisitService
    from src.infrastructure.persistence.sqlite_connection import sqlite_connection
    task=asyncio.run(TaskService(SqliteTaskRepository()).create_task(TaskCreate(task_name='离线 Fixture · 回访任务',
        keyword='mini',decision_mode='keyword',keyword_rules=['mini'],revisit_enabled=True,enabled=False)))
    asyncio.run(save_result_record({'任务名称':task.task_name,'爬取时间':stamp(),'商品信息':{
        '商品ID':'990006','商品标题':'离线 Fixture · 已售商品','当前售价':'6000',
        '商品链接':'https://www.goofish.com/item?id=990006'},'卖家信息':{'卖家ID':'880001','卖家昵称':'离线卖家'}},'mini'))
    with sqlite_connection() as conn:
        conn.execute("UPDATE result_listing_items SET worker_id='browser-fixture' WHERE item_id='990006'");conn.commit()
    ResultRevisitService().observe('990006','browser-fixture',{'raw_status':1,'price':'4500',
        'request_ok':True,'page_complete':True,'evidence':'offline_fixture'},task.model_dump())
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
        with sqlite_connection() as conn:
            conn.execute("UPDATE result_listing_memberships SET next_review_at='2026-10-31T12:00:00+08:00' WHERE item_id='990001'")
            conn.execute('UPDATE tasks SET enabled=1 WHERE id=?',(task.id,));conn.commit()
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
            expect(page.get_by_text('下次回访：2026-10-31 12:00（上海时间）',exact=False).first).to_be_visible()
            if os.environ.get('SENTINEL_CAPTURE_SCREENSHOTS')=='1':
                pass  # card screenshot belongs to the previous PR
            checkbox=page.get_by_label('排除疑似低价引流（至少5件样本，低于中位数20%）')
            checkbox.uncheck()
            expect(page.get_by_text('离线 Fixture · 25元商品',exact=True)).to_be_visible()
            checkbox.check()
            expect(page.get_by_text('离线 Fixture · 25元商品',exact=True)).to_have_count(0)
            page.get_by_role('button',name='¥5000～＜¥5100，1件',exact=True).click()
            expect(page.get_by_text('有效价格 1 件',exact=True)).to_be_visible()
            page.get_by_role('button',name='清除价格范围',exact=True).click()
            expect(page.get_by_text('有效价格 5 件',exact=True)).to_be_visible()
            page.get_by_role('button',name='已售展示价',exact=True).click()
            expect(page.get_by_text('已售均价',exact=True)).to_be_visible()
            expect(page.get_by_text('¥4,500',exact=True).first).to_be_visible()
            expect(page.get_by_text('离线 Fixture · 已售商品',exact=True)).to_be_visible()
            expect(page.get_by_text('离线 Fixture · 5000元商品',exact=True)).to_have_count(0)
            expect(page.get_by_role('link',name='卖家主页 · 离线卖家',exact=True)).to_be_visible()
            screenshot_dir=ROOT/'docs/screenshots'
            if os.environ.get('SENTINEL_CAPTURE_SCREENSHOTS')=='1':
                page.wait_for_timeout(500)  # allow viewport/theme/dialog transitions to finish
                pass  # previous PR owns the result-distribution screenshot
            page.set_viewport_size({'width':390,'height':844})
            page.evaluate("document.documentElement.classList.add('dark')")
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            expect(page.get_by_text('¥4,500',exact=True).first).to_have_css('color','rgb(241, 245, 249)')
            if os.environ.get('SENTINEL_CAPTURE_SCREENSHOTS')=='1':
                page.wait_for_timeout(500)  # allow viewport/theme/dialog transitions to finish
                pass  # previous PR owns the mobile result screenshot
            page.reload()
            expect(page.get_by_role('heading',name='结果查看',exact=True)).to_be_visible()
            assert page.locator('#password').count()==0
            page.set_viewport_size({'width':1440,'height':1000})
            with sqlite_connection() as conn:
                conn.execute('UPDATE tasks SET enabled=0 WHERE id=?',(task.id,));conn.commit()
            page.goto(f'http://127.0.0.1:{port}/tasks')
            page.locator('button[aria-label="编辑 离线 Fixture · 回访任务"]:visible').click()
            expect(page.get_by_label('启用定时回访',exact=True)).to_be_checked()
            expect(page.get_by_label('回访频率',exact=True)).to_have_value('daily')
            expect(page.get_by_label('回访小时',exact=True)).to_have_value('9')
            expect(page.get_by_label('回访分钟',exact=True)).to_have_value('30')
            assert page.get_by_label('回访定时规则（Cron）',exact=True).count()==0
            page.get_by_label('回访频率',exact=True).select_option('weekend')
            page.get_by_label('回访小时',exact=True).select_option('12')
            page.get_by_label('回访分钟',exact=True).select_option('5')
            expect(page.get_by_text('周末（周六、周日） 12:05（上海时间）',exact=True)).to_be_visible()
            expect(page.get_by_label('每次最多检查商品数',exact=True)).to_have_value('20')
            expect(page.get_by_label('采集频率',exact=True)).to_have_value('manual')
            page.get_by_label('采集频率',exact=True).select_option('daily')
            page.get_by_label('采集小时',exact=True).select_option('8')
            page.get_by_label('采集分钟',exact=True).select_option('15')
            assert page.locator('#cron').count()==0

            if os.environ.get('SENTINEL_CAPTURE_SCREENSHOTS')=='1':
                page.wait_for_timeout(500)  # allow viewport/theme/dialog transitions to finish
                pass  # existing schedule screenshots belong to the previous PR
            page.set_viewport_size({'width':390,'height':844})
            page.get_by_label('回访频率',exact=True).scroll_into_view_if_needed()
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            if os.environ.get('SENTINEL_CAPTURE_SCREENSHOTS')=='1':
                page.wait_for_timeout(500)
                pass  # existing schedule screenshots belong to the previous PR
            page.get_by_label('采集频率',exact=True).scroll_into_view_if_needed()
            if os.environ.get('SENTINEL_CAPTURE_SCREENSHOTS')=='1':
                page.wait_for_timeout(500)
                pass  # existing schedule screenshots belong to the previous PR
            page.set_viewport_size({'width':1440,'height':1000})
            # Submit through the real form and API, then reopen to check persisted selections.
            page.locator('button[type="submit"][form="task-form"]').click()
            expect(page.get_by_role('dialog')).to_have_count(0)
            page.locator('button[aria-label="编辑 离线 Fixture · 回访任务"]:visible').click()
            expect(page.get_by_label('回访频率',exact=True)).to_have_value('weekend')
            expect(page.get_by_label('回访小时',exact=True)).to_have_value('12')
            expect(page.get_by_label('回访分钟',exact=True)).to_have_value('5')
            with sqlite_connection() as conn:
                assert conn.execute('SELECT revisit_cron FROM tasks WHERE id=?',(task.id,)).fetchone()[0]=='5 12 * * sat,sun'
                assert conn.execute('SELECT cron FROM tasks WHERE id=?',(task.id,)).fetchone()[0]=='15 8 * * *'
            # Complex legacy rules survive saving unrelated fields.
            page.keyboard.press('Escape')
            response=page.request.patch(f'http://127.0.0.1:{port}/api/tasks/{task.id}',data={'revisit_cron':'*/20 * * * *','cron':'0 8,12,18 * * *'})
            assert response.status==200
            page.reload()
            page.locator('button[aria-label="编辑 离线 Fixture · 回访任务"]:visible').click()
            expect(page.get_by_label('回访频率',exact=True)).to_have_value('custom')
            expect(page.get_by_label('采集频率',exact=True)).to_have_value('custom')
            expect(page.get_by_label('回访小时',exact=True)).to_be_disabled()
            page.get_by_label('每次最多检查商品数',exact=True).fill('21')
            page.locator('button[type="submit"][form="task-form"]').click()
            expect(page.get_by_role('dialog')).to_have_count(0)
            with sqlite_connection() as conn:
                saved=conn.execute('SELECT revisit_cron,revisit_limit FROM tasks WHERE id=?',(task.id,)).fetchone()
                assert tuple(saved)==('*/20 * * * *',21)
                assert conn.execute('SELECT cron FROM tasks WHERE id=?',(task.id,)).fetchone()[0]=='0 8,12,18 * * *'
            # Manual action UI: route only this POST to an offline fixture result.
            # Enabling the DB row directly does not reload the scheduler's disabled jobs.
            posted=[]
            fixture_run='00000000-0000-4000-8000-000000000007'
            def manual_fixture(route):
                assert route.request.method=='POST'
                posted.append(route.request.url)
                with sqlite_connection() as conn:
                    conn.execute("INSERT INTO result_revisit_runs(run_id,task_id,started_at,lease_until,status) VALUES(?,?,?,?,'running')",
                        (fixture_run,task.id,stamp(),stamp(now()+timedelta(minutes=2))))
                    conn.commit()
                route.fulfill(status=202,json={'run_id':fixture_run,'status':'running'})
            page.route(f'http://127.0.0.1:{port}/api/tasks/{task.id}/revisits/run',manual_fixture)
            with sqlite_connection() as conn:
                conn.execute('UPDATE tasks SET enabled=1,revisit_enabled=0 WHERE id=?',(task.id,));conn.commit()
            page.reload()
            revisit=page.locator('button[aria-label="回访 离线 Fixture · 回访任务"]:visible')
            start=page.locator('button[aria-label="启动 离线 Fixture · 回访任务"]:visible')
            expect(revisit).to_be_enabled()
            assert revisit.bounding_box()['y']>start.bounding_box()['y']
            revisit.click()
            expect(revisit).to_be_disabled()
            expect(page.locator('[role="status"]:visible').filter(has_text='回访中 · 已检查 0 件')).to_be_visible()
            if os.environ.get('SENTINEL_CAPTURE_SCREENSHOTS')=='1':
                page.wait_for_timeout(500)
                page.screenshot(path=str(screenshot_dir/'manual-revisit-desktop.png'),full_page=True)
            with sqlite_connection() as conn:
                conn.execute("UPDATE result_revisit_runs SET status='completed',reviewed=2,sold=1,finished_at=? WHERE run_id=?",(stamp(),fixture_run));conn.commit()
            expect(page.locator('[role="status"]:visible').filter(has_text='回访完成：检查 2 件，新确认已售 1 件')).to_be_visible(timeout=10000)
            expect(revisit).to_be_enabled()
            assert len(posted)==1
            page.reload()
            expect(page.locator('[role="status"]:visible').filter(has_text='回访完成：检查 2 件，新确认已售 1 件')).to_be_visible()
            page.set_viewport_size({'width':390,'height':844})
            revisit=page.locator('button[aria-label="回访 离线 Fixture · 回访任务"]:visible')
            expect(revisit).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            if os.environ.get('SENTINEL_CAPTURE_SCREENSHOTS')=='1':
                page.wait_for_timeout(500)
                page.screenshot(path=str(screenshot_dir/'manual-revisit-mobile.png'),full_page=True)
            assert errors==[]
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=15)
        log.close()
