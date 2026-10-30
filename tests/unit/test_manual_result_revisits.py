"""Manual checks use offline adapters, shared leases and the ordinary task limit."""
import asyncio
from unittest.mock import Mock

from test_result_revisits import env,Adapter,record
from src.domain.models.task import TaskUpdate
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.services.task_service import TaskService
from src.services.result_storage_service import save_result_record
from src.services.result_revisit_service import ResultRevisitService,tracked_records
from src.services.result_revisit_manager import ResultRevisitManager


def save(items=('1001',)):
    for item in items:asyncio.run(save_result_record(record(item),'mini'))


def test_manual_revisit_checks_before_due_without_enabling_automatic_schedule(env):
    asyncio.run(TaskService(SqliteTaskRepository()).update_task(env['id'],TaskUpdate(revisit_enabled=False)))
    save()
    adapter=Adapter()
    runner=ResultRevisitService(lambda _:adapter)
    assert asyncio.run(runner.run(env['id']))['status']=='disabled'
    result=asyncio.run(runner.run(env['id'],manual=True))
    assert result['reviewed']==1 and result['sold']==1
    with sqlite_connection() as conn:
        assert conn.execute('SELECT revisit_enabled FROM tasks').fetchone()[0]==0
        assert conn.execute('SELECT sold_display_price FROM result_listing_items').fetchone()[0]==450000
    assert adapter.calls==['1001']


def test_manual_limit_and_future_automatic_plan_are_preserved(env):
    save(('1001','1002','1003'))
    with sqlite_connection() as conn:
        before={r['item_id']:r['next_review_at'] for r in conn.execute('SELECT * FROM result_listing_memberships')}
    adapter=Adapter({'request_ok':True,'page_complete':True,'raw_status':0,'price':'4800'})
    runner=ResultRevisitService(lambda _:adapter)
    result=asyncio.run(runner.run(env['id'],manual=True))
    assert result['reviewed']==2 and adapter.calls==['1001','1002']
    with sqlite_connection() as conn:
        after={r['item_id']:r['next_review_at'] for r in conn.execute('SELECT * FROM result_listing_memberships')}
    assert before==after
    second=Adapter({'request_ok':True,'page_complete':True,'raw_status':0,'price':'4800'})
    asyncio.run(ResultRevisitService(lambda _:second).run(env['id'],manual=True))
    assert second.calls[0]=='1003'  # Fresh registrations are checked before repeating earlier tests.


def test_empty_manual_revisit_does_not_open_browser(env):
    factory=Mock()
    result=asyncio.run(ResultRevisitService(factory).run(env['id'],manual=True))
    assert result['status']=='empty' and result['reviewed']==0
    factory.assert_not_called()


def test_manual_and_scheduled_runs_share_a_single_durable_lock(env):
    save()
    async def run():
        adapter=Adapter()
        entered=asyncio.Event();release=asyncio.Event()
        original=adapter.item_listing
        async def slow(item):entered.set();await release.wait();return await original(item)
        adapter.item_listing=slow
        service=ResultRevisitService(lambda _:adapter)
        manager=ResultRevisitManager(service)
        first=manager.start(env['id'])
        await asyncio.wait_for(entered.wait(),timeout=3)
        assert first['status']=='running'
        assert manager.start(env['id'])['status']=='busy'
        assert (await service.run(env['id']))['status']=='busy'
        pending=list(manager.workers)
        release.set();await asyncio.gather(*pending)
        assert adapter.calls==['1001']
        await manager.stop()
    asyncio.run(run())


def test_manual_worker_shutdown_releases_lease_even_before_it_starts(env):
    save()
    async def run():
        manager=ResultRevisitManager(ResultRevisitService(lambda _:Adapter()))
        ticket=manager.start(env['id'])
        await manager.stop()
        with sqlite_connection() as conn:
            row=conn.execute('SELECT * FROM result_revisit_runs WHERE run_id=?',(ticket['run_id'],)).fetchone()
            assert row['status']=='interrupted'
            assert conn.execute('SELECT worker_id FROM result_listing_items').fetchone()[0] is None
    asyncio.run(run())


def test_manual_only_task_does_not_advertise_an_automatic_next_review(env):
    save()
    with sqlite_connection() as conn:
        conn.execute('UPDATE tasks SET revisit_enabled=0');conn.commit()
    adapter=Adapter({'request_ok':True,'page_complete':True,'raw_status':0,'price':'4800'})
    asyncio.run(ResultRevisitService(lambda _:adapter).run(env['id'],manual=True))
    assert tracked_records([record()])[0]['listing_tracking']['next_review_at'] is None
