"""Offline fixed-clock regression for next-day eligibility and schedule changes."""
import asyncio
import pytest
import src.app as app_module
from src.domain.models.task import TaskCreate,TaskUpdate
from src.services.task_service import TaskService
from src.services.result_revisit_service import ResultRevisitService,register_result
from src.services.market_types import stamp as real_stamp
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage


@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    db=str(tmp_path/'reschedule.sqlite3')
    monkeypatch.setenv('APP_DATABASE_FILE',db)
    monkeypatch.setattr('src.services.result_revisit_service.stamp',lambda value=None:real_stamp(value or '2026-10-30T11:00:00+08:00'))
    service=TaskService(SqliteTaskRepository(db_path=db))
    task=asyncio.run(service.create_task(TaskCreate(task_name='Fixture',keyword='fixture',decision_mode='keyword',keyword_rules=['fixture'],
        revisit_enabled=True,revisit_cron='30 9 * * *')))
    with sqlite_connection() as conn:
        register_result(conn,task.model_dump(),{'商品信息':{'商品ID':'1001','当前售价':'5000'},'爬取时间':'2026-10-30T10:37:00+08:00'})
        conn.commit()
    return service,task,db


def due(task_id):
    with sqlite_connection() as conn:
        return conn.execute('SELECT next_review_at FROM result_listing_memberships WHERE task_id=?',(task_id,)).fetchone()[0]


def test_first_registration_waits_until_tomorrow_and_rule_edit_updates_hour(setup):
    service,task,_=setup
    assert due(task.id)=='2026-10-31T09:30:00+08:00'
    asyncio.run(service.update_task(task.id,TaskUpdate(revisit_cron='0 12 * * *')))
    assert due(task.id)=='2026-10-31T12:00:00+08:00'
    with sqlite_connection() as conn:
        assert conn.execute('SELECT count(*) FROM result_listing_observations').fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM result_items').fetchone()[0]==0


def test_upgrading_repairs_old_mismatched_queue_without_triggering_revisit(setup):
    _,task,_=setup
    with sqlite_connection() as conn:
        conn.execute('UPDATE tasks SET revisit_cron=? WHERE id=?',('0 12 * * *',task.id))
        conn.execute('UPDATE result_listing_memberships SET review_not_before=NULL');conn.commit()
    repaired={**task.model_dump(),'revisit_cron':'0 12 * * *'}
    ResultRevisitService().reconcile_schedule(repaired,at='2026-10-30T11:00:00+08:00')
    assert due(task.id)=='2026-10-31T12:00:00+08:00'
    ResultRevisitService().reconcile_schedule(repaired,at='2026-10-30T11:00:00+08:00')
    assert due(task.id)=='2026-10-31T12:00:00+08:00'


def test_valid_overdue_queue_is_not_postponed_by_unrelated_save(setup):
    service,task,_=setup
    with sqlite_connection() as conn:
        conn.execute("UPDATE result_listing_memberships SET next_review_at='2026-10-29T09:30:00+08:00'");conn.commit()
    asyncio.run(service.update_task(task.id,TaskUpdate(revisit_limit=10)))
    assert due(task.id)=='2026-10-29T09:30:00+08:00'


def test_existing_eligible_items_use_new_rule_without_waiting_an_extra_day(setup):
    service,task,_=setup
    with sqlite_connection() as conn:
        conn.execute("UPDATE result_listing_memberships SET review_not_before='2026-10-29T00:00:00+08:00'");conn.commit()
    asyncio.run(service.update_task(task.id,TaskUpdate(revisit_cron='0 12 * * *')))
    assert due(task.id)=='2026-10-30T12:00:00+08:00'


def test_active_lock_sold_and_expired_memberships_are_not_rewritten(setup):
    _,task,_=setup
    runner=ResultRevisitService()
    for change in ["UPDATE result_listing_items SET locked_until='2026-10-30T12:00:00+08:00'",
                   "UPDATE result_listing_items SET locked_until=NULL,listing_status='sold'",
                   "UPDATE result_listing_items SET listing_status='active'; UPDATE result_listing_memberships SET tracking_until='2026-10-29'"]:
        with sqlite_connection() as conn:
            conn.executescript(change);conn.commit()
        runner.reconcile_schedule({**task.model_dump(),'revisit_cron':'0 12 * * *'},force=True)
        assert due(task.id)=='2026-10-31T09:30:00+08:00'


def test_schedule_edit_does_not_change_other_tasks_or_collection_rule(setup):
    service,task,_=setup
    other=asyncio.run(service.create_task(TaskCreate(task_name='Other',keyword='other',decision_mode='keyword',keyword_rules=['other'],
        revisit_enabled=True,revisit_cron='0 20 * * *',cron='0 8 * * *')))
    with sqlite_connection() as conn:
        register_result(conn,other.model_dump(),{'商品信息':{'商品ID':'1001','当前售价':'5000'}});conn.commit()
    asyncio.run(service.update_task(task.id,TaskUpdate(revisit_cron='0 12 * * *')))
    assert due(other.id)=='2026-10-31T20:00:00+08:00'
    assert asyncio.run(service.get_task(other.id)).cron=='0 8 * * *'


def test_additive_membership_column_upgrade_preserves_existing_dates_and_repeats(tmp_path,monkeypatch):
    monkeypatch.setenv('APP_DATABASE_FILE',str(tmp_path/'legacy.sqlite3'));monkeypatch.chdir(tmp_path)
    # Old membership schema, before the eligibility column existed.
    with sqlite_connection() as conn:
        conn.execute('''CREATE TABLE result_listing_memberships(task_id INTEGER NOT NULL,item_id TEXT NOT NULL,
            filename TEXT NOT NULL,next_review_at TEXT,tracking_until TEXT NOT NULL,archived INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(task_id,item_id))''')
        conn.execute("INSERT INTO result_listing_memberships VALUES(1,'1001','fixture_full_data.jsonl','2026-10-31T09:30:00+08:00','2026-11-30',0)")
        conn.commit()
    bootstrap_sqlite_storage();bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        row=conn.execute('SELECT * FROM result_listing_memberships').fetchone()
        assert row['next_review_at']=='2026-10-31T09:30:00+08:00'
        assert row['review_not_before'] is None


def test_startup_repairs_saved_rule_without_starting_platform_work(setup,monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    _,task,_=setup
    with sqlite_connection() as conn:
        conn.execute('UPDATE tasks SET revisit_cron=? WHERE id=?',('0 12 * * *',task.id));conn.commit()
    scheduler=SimpleNamespace(reload_jobs=AsyncMock(),start=lambda:None,stop=lambda:None)
    monkeypatch.setattr(app_module,'scheduler_service',scheduler)
    monkeypatch.setattr(app_module,'process_service',SimpleNamespace(stop_all=AsyncMock()))
    monkeypatch.setattr(app_module,'cleanup_task_logs',lambda **_:None)
    async def run():
        async with app_module.lifespan(SimpleNamespace(state=SimpleNamespace())):
            assert due(task.id)=='2026-10-31T12:00:00+08:00'
    asyncio.run(run())
    with sqlite_connection() as conn:
        assert conn.execute('SELECT count(*) FROM result_revisit_runs').fetchone()[0]==0
