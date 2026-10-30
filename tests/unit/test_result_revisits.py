"""Offline contract tests: no account, AI, requests or notifications."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.domain.models.task import TaskCreate, TaskUpdate, TaskGenerateRequest
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.services.task_service import TaskService
from src.services.result_storage_service import save_result_record
from src.services.result_revisit_service import ResultRevisitService, register_result, tracked_records
from src.services.result_price_summary_service import result_price_insights
from src.services.market_platform import parse_item_listing
from src.services.market_types import stamp, LoginExpired, PlatformAccessError


def record(item='1001',price=5000,seller='老昵称'):
    return {'任务名称':'Mini','爬取时间':'2026-10-29T08:30:00+08:00','搜索关键字':'mini',
            '商品信息':{'商品ID':item,'商品标题':'离线 Fixture','当前售价':str(price),
                      '商品链接':'https://www.goofish.com/item?id='+item},
            '卖家信息':{'卖家ID':'888','卖家昵称':seller},'ai_analysis':{'is_recommended':False}}


@pytest.fixture
def env(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('APP_DATABASE_FILE',str(tmp_path/'test.sqlite3'))
    bootstrap_sqlite_storage()
    service=TaskService(SqliteTaskRepository())
    task=asyncio.run(service.create_task(TaskCreate(task_name='Mini',keyword='mini',decision_mode='keyword',
        keyword_rules=['mini'],revisit_enabled=True,revisit_limit=2)))
    return task.model_dump()


def seed(task,items=('1001',)):
    for item in items:
        asyncio.run(save_result_record(record(item),'mini'))
    with sqlite_connection() as conn:
        conn.execute("UPDATE result_listing_memberships SET next_review_at='2000-01-01T00:00:00+08:00'")
        conn.commit()


class Adapter:
    def __init__(self, observation=None, failure=None):
        self.observation=observation or {'request_ok':True,'page_complete':True,'raw_status':1,'price':'4500',
            'seller_id':'888','seller_nickname':'新昵称','source':'item_detail','evidence':'explicit_detail_item_status'}
        self.failure=failure; self.calls=[]
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def item_listing(self,item_id):
        self.calls.append(item_id)
        if self.failure:raise self.failure
        return self.observation


def test_registration_idempotent_seller_date_links_and_next_day(env):
    asyncio.run(save_result_record(record(),'mini'))
    asyncio.run(save_result_record(record(),'mini'))
    with sqlite_connection() as conn:
        item=dict(conn.execute('SELECT * FROM result_listing_items').fetchone())
        membership=dict(conn.execute('SELECT * FROM result_listing_memberships').fetchone())
        assert conn.execute('SELECT count(*) FROM result_listing_items').fetchone()[0]==1
    assert item['seller_id']=='888' and item['seller_nickname']=='老昵称'
    assert item['item_url'].endswith('id=1001') and item['first_seen_at'].startswith('2026-10-29')
    assert membership['next_review_at'][:10]>stamp()[:10]
    assert tracked_records([record()])[0]['listing_tracking']['seller_id']=='888'


def test_sold_page_price_not_last_listing_repeated_sold_count_once(env):
    seed(env)
    adapter=Adapter(); service=ResultRevisitService(lambda _:adapter)
    result=asyncio.run(service.run(env['id']))
    assert result['sold']==1 and result['reviewed']==1
    assert asyncio.run(service.run(env['id']))['reviewed']==0
    with sqlite_connection() as conn:
        item=dict(conn.execute('SELECT * FROM result_listing_items').fetchone())
        assert item['last_listing_price']==500000
        assert item['sold_display_price']==450000
        assert item['seller_id']=='888' and item['seller_nickname']=='新昵称'
        assert conn.execute('SELECT count(*) FROM result_listing_events').fetchone()[0]==1
    sold=asyncio.run(result_price_insights('mini_full_data.jsonl',listing_state='sold'))
    assert sold['market_summary']['avg_price']==4500
    assert sold['history_summary']['avg_price']==4500
    assert sum(b['count'] for b in sold['price_distribution'])==1
    assert asyncio.run(result_price_insights('mini_full_data.jsonl'))['total_items']==0


@pytest.mark.parametrize('observation',[
    {'request_ok':False,'page_complete':False,'raw_status':1,'price':1},
    {'request_ok':True,'page_complete':False,'raw_status':1,'price':1},
    {'request_ok':True,'page_complete':True,'raw_status':None},
    {'request_ok':True,'page_complete':True,'raw_status':987,'price':1},
])
def test_missing_timeout_incomplete_unknown_never_become_sold(env,observation):
    seed(env); adapter=Adapter(observation)
    result=asyncio.run(ResultRevisitService(lambda _:adapter).run(env['id']))
    assert result['sold']==0
    with sqlite_connection() as conn:
        row=conn.execute('SELECT * FROM result_listing_items').fetchone()
        assert row['listing_status']=='active' and row['sold_display_price'] is None
        assert row['first_seen_sold_at'] is None
        assert conn.execute('SELECT count(*) FROM result_listing_observations').fetchone()[0]==1


def test_bounded_batch_durable_queue_and_failed_item_retry(env):
    seed(env,('1001','1002','1003'))
    adapter=Adapter(failure=OSError('offline'))
    first=asyncio.run(ResultRevisitService(lambda _:adapter).run(env['id']))
    assert first['reviewed']==2 and len(adapter.calls)==2
    recovered=Adapter()
    second=asyncio.run(ResultRevisitService(lambda _:recovered).run(env['id']))
    assert second['reviewed']==1 and recovered.calls==['1003']
    with sqlite_connection() as conn:
        assert conn.execute("SELECT count(*) FROM result_listing_memberships WHERE next_review_at IS NOT NULL").fetchone()[0]==2


def test_sold_without_display_price_has_no_fallback_or_zero(env):
    seed(env); adapter=Adapter({'request_ok':True,'page_complete':True,'raw_status':1,'price':None})
    asyncio.run(ResultRevisitService(lambda _:adapter).run(env['id']))
    summary=asyncio.run(result_price_insights('mini_full_data.jsonl',listing_state='sold'))
    assert summary['market_summary']['avg_price'] is None
    assert summary['market_summary']['sample_count']==0 and summary['missing_price_count']==1
    assert summary['price_distribution']==[]


def test_expired_or_paused_tasks_and_missing_seller(env):
    no_seller=record();no_seller['卖家信息']={'卖家昵称':'同名'}
    asyncio.run(save_result_record(no_seller,'mini'))
    with sqlite_connection() as conn:
        assert conn.execute('SELECT seller_id FROM result_listing_items').fetchone()[0] is None
        conn.execute("UPDATE result_listing_memberships SET tracking_until='2000-01-01',next_review_at='1999-01-01'");conn.commit()
    adapter=Adapter()
    assert asyncio.run(ResultRevisitService(lambda _:adapter).run(env['id']))['reviewed']==0
    assert adapter.calls==[]


def test_access_expired_pauses_only_revisit_and_preserves_queue(env):
    seed(env); adapter=Adapter(failure=LoginExpired('login_expired'))
    result=asyncio.run(ResultRevisitService(lambda _:adapter).run(env['id']))
    assert result['status']=='access_paused'
    with sqlite_connection() as conn:
        row=conn.execute('SELECT * FROM tasks').fetchone()
        assert row['enabled']==1 and row['revisit_enabled']==0
        assert conn.execute('SELECT worker_id FROM result_listing_items').fetchone()[0] is None


def test_two_tasks_share_identity_delete_preserves_history_and_upgrade(env):
    seed(env)
    second=asyncio.run(TaskService(SqliteTaskRepository()).create_task(TaskCreate(task_name='Other',keyword='mini',
        decision_mode='keyword',keyword_rules=['mini'],revisit_enabled=True)))
    bootstrap_sqlite_storage();bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        assert conn.execute('SELECT count(*) FROM result_listing_items').fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM result_listing_memberships').fetchone()[0]==2
    asyncio.run(TaskService(SqliteTaskRepository()).delete_task(env['id']))
    with sqlite_connection() as conn:
        assert conn.execute('SELECT count(*) FROM result_listing_items').fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM result_items').fetchone()[0]==1
        assert conn.execute('SELECT archived FROM result_listing_memberships WHERE task_id=?',(env['id'],)).fetchone()[0]==1


def test_detail_adapter_strict_identity_status_and_no_price_guess():
    data={'ret':['SUCCESS::ok'],'data':{'itemDO':{'itemId':'1001','itemStatus':1,'price':'4500'},'sellerDO':{'sellerId':'888'}}}
    assert parse_item_listing(data,'1001')['price']=='4500'
    assert parse_item_listing(data,'1001')['raw_status'] is None
    data['data']['itemDO'].pop('price')
    assert parse_item_listing(data,'1001')['price'] is None
    with pytest.raises(PlatformAccessError):parse_item_listing(data,'wrong')
    data['ret']=['FAIL_SYS_SESSION_EXPIRED']
    with pytest.raises(LoginExpired):parse_item_listing(data,'1001')


@pytest.mark.parametrize('payload',[{'revisit_limit':101},{'revisit_limit':0},{'revisit_days':0},{'revisit_cron':'garbage'}])
def test_revisit_settings_validated(payload):
    with pytest.raises(ValidationError):TaskUpdate(**payload)


def test_concurrent_run_skips_busy_task_and_checks_once(env):
    seed(env)
    adapter=Adapter()
    started=asyncio.Event(); release=asyncio.Event()
    original=adapter.item_listing
    async def slow(item):
        started.set(); await release.wait(); return await original(item)
    adapter.item_listing=slow
    async def run():
        worker=asyncio.create_task(ResultRevisitService(lambda _:adapter).run(env['id']))
        await started.wait()
        busy=await ResultRevisitService(lambda _:adapter).run(env['id'])
        assert busy['status']=='busy'
        release.set();assert (await worker)['reviewed']==1
    asyncio.run(run())
    assert adapter.calls==['1001']


def test_crashed_worker_lease_expires_and_queue_recovers(env):
    seed(env)
    with sqlite_connection() as conn:
        conn.execute("INSERT INTO result_revisit_runs(run_id,task_id,started_at,lease_until,status) VALUES('dead',?,'2000','2000','running')",(env['id'],))
        conn.execute("UPDATE result_listing_items SET worker_id='dead',locked_until='2000'");conn.commit()
    assert asyncio.run(ResultRevisitService(lambda _:Adapter()).run(env['id']))['sold']==1
    with sqlite_connection() as conn:
        assert conn.execute("SELECT status FROM result_revisit_runs WHERE run_id='dead'").fetchone()[0]=='interrupted'


def test_scheduler_restores_revisit_rule_without_search_rule(env):
    from src.services.scheduler_service import SchedulerService
    from src.domain.models.task import Task
    class Scheduler:
        timezone='Asia/Shanghai'
        def remove_all_jobs(self):pass
        def add_job(self,*args,**kwargs):self.jobs.append(kwargs)
        jobs=[]
    service=SchedulerService(None);service.scheduler=Scheduler()
    asyncio.run(service.reload_jobs([Task(**{**env,'cron':None})]))
    assert service.scheduler.jobs[0]['id']==f"revisit_{env['id']}"
    assert service.scheduler.jobs[0]['max_instances']==1
    assert str(service.scheduler.jobs[0]['trigger'].timezone)=='Asia/Shanghai'


def test_expired_search_result_does_not_hide_sold_archive(env):
    seed(env)
    asyncio.run(ResultRevisitService(lambda _:Adapter()).run(env['id']))
    with sqlite_connection() as conn:
        conn.execute("UPDATE result_items SET status='expired'");conn.commit()
    assert asyncio.run(result_price_insights('mini_full_data.jsonl',listing_state='sold'))['total_items']==1
    with sqlite_connection() as conn:
        conn.execute("UPDATE result_items SET status='hidden'");conn.commit()
    assert asyncio.run(result_price_insights('mini_full_data.jsonl',listing_state='sold'))['total_items']==0
    assert asyncio.run(result_price_insights('mini_full_data.jsonl',listing_state='sold',include_hidden=True))['total_items']==1


def test_active_revisit_price_replaces_old_saved_price(env):
    seed(env)
    adapter=Adapter({'raw_status':0,'price':'4200','request_ok':True,'page_complete':True})
    assert asyncio.run(ResultRevisitService(lambda _:adapter).run(env['id']))['sold']==0
    summary=asyncio.run(result_price_insights('mini_full_data.jsonl'))
    assert summary['market_summary']['avg_price']==4200
    assert summary['history_summary']['avg_price']==4200
    assert tracked_records([record()])[0]['商品信息']['当前售价']==4200


def test_platform_adapter_reads_seller_once_and_only_registered_detail_ids(monkeypatch):
    from src.services.market_platform import PlaywrightMarketAdapter
    from src.services.market_types import SellerListing
    from types import SimpleNamespace
    adapter=PlaywrightMarketAdapter({},None)
    adapter.check_access=AsyncMock()
    adapter.seller_items=AsyncMock(return_value=SellerListing(items=[{'item_id':'1001','raw_status':1},
        {'item_id':'1002','raw_status':0}],request_ok=True,page_complete=True))
    monkeypatch.setattr('src.services.market_platform.asyncio.sleep',AsyncMock())
    class Page:
        def expect_response(self,*args,**kwargs):return self
        async def __aenter__(self):
            future=asyncio.get_running_loop().create_future();future.set_result(self)
            return SimpleNamespace(value=future)
        async def __aexit__(self,*args):pass
        async def goto(self,url,**kwargs):self.item_id=url.split('id=')[1]
        async def close(self):pass
        ok=True
        async def json(self):
            return {'ret':['SUCCESS::ok'],'data':{'itemDO':{'itemId':self.item_id,'itemStatus':1,'price':'4300'},'sellerDO':{'sellerId':'888'}}}
    class Context:
        async def new_page(self):return Page()
    adapter.context=Context()
    async def run():
        sold=await adapter.item_listing('1001')
        active=await adapter.item_listing('1002')
        missing=await adapter.item_listing('1003')
        assert sold['raw_status']==1 and sold['price']=='4300'
        assert active['raw_status']==0  # detail code1 must not override explicit seller code0
        assert missing['raw_status'] is None
        adapter.seller_items.assert_awaited_once_with('888',max_pages=1)
    asyncio.run(run())


def test_seller_single_page_cap_never_scrolls_for_extra_page(monkeypatch):
    from src.services.market_platform import PlaywrightMarketAdapter
    from types import SimpleNamespace
    adapter=PlaywrightMarketAdapter({},None)
    adapter.check_access=AsyncMock()
    monkeypatch.setattr('src.services.market_platform.asyncio.sleep',AsyncMock())
    class Page:
        evaluate=AsyncMock()
        ok=True
        async def goto(self,*args,**kwargs):pass
        async def close(self):pass
        def expect_response(self,*args,**kwargs):return self
        async def __aenter__(self):
            future=asyncio.get_running_loop().create_future();future.set_result(self)
            return SimpleNamespace(value=future)
        async def __aexit__(self,*args):pass
        async def json(self):
            return {'ret':['SUCCESS::ok'],'data':{'nextPage':True,'cardList':[
                {'cardData':{'id':'1001','title':'Fixture','itemStatus':1,'priceInfo':{'price':'4500'}}}]}}
    page=Page()
    adapter.context=SimpleNamespace(new_page=AsyncMock(return_value=page))
    listing=asyncio.run(adapter.seller_items('888',max_pages=1))
    assert listing.request_ok and not listing.list_complete
    assert listing.items[0]['raw_status']==1
    page.evaluate.assert_not_awaited()


def test_upgrade_old_tasks_and_snapshots_is_additive_and_repeatable(env):
    import sqlite3
    from src.domain.models.task import Task
    from src.infrastructure.persistence.sqlite_connection import SCHEMA_STATEMENTS,init_schema
    conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
    try:
        for statement in SCHEMA_STATEMENTS:conn.execute(statement)
        values=SqliteTaskRepository()._task_values(Task(**env))
        for key in ('revisit_enabled','revisit_cron','revisit_limit','revisit_days'):values.pop(key)
        names=list(values)
        conn.execute('INSERT INTO tasks('+','.join(names)+') VALUES('+','.join('?' for _ in names)+')',list(values.values()))
        conn.execute('''INSERT INTO price_snapshots(keyword_slug,keyword,task_name,snapshot_time,snapshot_day,run_id,item_id,price,tags_json)
            VALUES('mini','mini','Mini','2026-10-29T08:00:00','2026-10-29','legacy','1001',5000,'[]')''')
        init_schema(conn);init_schema(conn)
        task=conn.execute('SELECT * FROM tasks').fetchone()
        assert task['task_name']=='Mini' and task['revisit_enabled']==0
        assert task['revisit_limit']==20 and task['revisit_cron']=='30 9 * * *'
        assert conn.execute('SELECT price FROM price_snapshots').fetchone()[0]==5000
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0]==1
    finally:conn.close()


def test_deleted_registered_task_id_is_not_reused_by_new_task(env):
    seed(env)
    service=TaskService(SqliteTaskRepository())
    asyncio.run(service.delete_task(env['id']))
    replacement=asyncio.run(service.create_task(TaskCreate(task_name='Replacement',keyword='other',
        decision_mode='keyword',keyword_rules=['other'])))
    assert replacement.id>env['id']
    assert asyncio.run(ResultRevisitService(lambda _:Adapter()).run(env['id']))['status']=='disabled'
