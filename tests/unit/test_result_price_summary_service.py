import asyncio
from unittest.mock import AsyncMock

import pytest
from src.services.result_price_summary_service import select_priced_results, build_price_distribution, result_price_insights, send_result_price_summary
from src.services.result_storage_service import save_result_record
from src.services.price_history_service import record_market_snapshots


def record(item_id, price, recommended=False, time='2026-10-29T08:30:00'):
    return {'爬取时间':time,'搜索关键字':'mini','任务名称':'Mini',
            '商品信息':{'商品ID':str(item_id),'商品标题':f'Fixture {item_id}','当前售价':str(price),'商品链接':f'https://www.goofish.com/item?id={item_id}'},
            'ai_analysis':{'is_recommended':recommended,'analysis_source':'ai'},'卖家信息':{}}


def test_bait_price_exclusion_is_reversible_and_preserves_original_data():
    records=[record(i,p) for i,p in enumerate([25,5000,5100,5200,5300,5400])]
    selected, policy=select_priced_results(records)
    assert len(selected)==5 and policy['excluded_count']==1
    assert policy['low_price_threshold']==1030
    assert len(records)==6
    assert len(select_priced_results(records,exclude_low_price=False)[0])==6
    assert len(select_priced_results(records,min_price=5100,max_price=5300)[0])==3
    # A tiny sample cannot justify inferring low-price bait.
    assert len(select_priced_results(records[:2])[0])==2


def test_distribution_boundaries_invalid_prices_and_equal_values():
    for prices in [[5000]*8,[25,5000,5100,5200,5300,5400],[.01,.02,.03],[]]:
        bins=build_price_distribution([record(i,p) for i,p in enumerate(prices)])
        assert sum(b['count'] for b in bins)==len(prices)
        assert len(bins)<=14
    invalid=[record(i,p) for i,p in enumerate(['NaN','Infinity','-1','0','暂无'])]
    assert build_price_distribution(invalid)==[]


def test_summary_filters_all_pages_and_deduplicates_history_per_day(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    async def seed():
        for i,p in enumerate([25,5000,5100,5200,5300,5400]):
            await save_result_record(record(i,p,recommended=i==2),'mini')
    asyncio.run(seed())
    for run, timestamp,price in [('first','2026-10-28T08:00:00',5500),('retry','2026-10-28T09:00:00',5300),('today','2026-10-29T09:00:00',5100)]:
        record_market_snapshots(keyword='mini',task_name='Mini',items=[record(2,price)['商品信息']],run_id=run,snapshot_time=timestamp)
    stats=asyncio.run(result_price_insights('mini_full_data.jsonl'))
    assert stats['market_summary']['sample_count']==5
    assert stats['market_summary']['avg_price']==5200
    assert sum(b['count'] for b in stats['price_distribution'])==5
    assert stats['history_summary']['sample_count']==2
    assert stats['history_summary']['avg_price']==5200
    selected=asyncio.run(result_price_insights('mini_full_data.jsonl',ai_recommended_only=True))
    assert selected['market_summary']['sample_count']==1
    assert selected['market_summary']['avg_price']==5100


def test_price_summary_notification_once_per_run_and_no_recommendations(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    async def run():
        await save_result_record(record('1',5200),'mini')
        notifier=AsyncMock()
        assert await send_result_price_summary('mini','Mini','run1',notifier)
        assert not await send_result_price_summary('mini','Mini','run1',notifier)
        notifier.assert_awaited_once()
        data,reason=notifier.call_args.args
        assert '当前均价：¥5200' in reason and '历史均价：暂无有效样本' in reason
        assert '推荐' not in reason
        assert not await send_result_price_summary('empty','Empty','run1',notifier)
    asyncio.run(run())


def test_summary_has_only_four_real_metrics_and_uses_sold_display_prices(tmp_path,monkeypatch):
    from src.domain.models.task import TaskCreate
    from src.services.task_service import TaskService
    from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
    from src.infrastructure.persistence.sqlite_connection import sqlite_connection
    from src.services.result_revisit_service import ResultRevisitService
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('APP_DATABASE_FILE',str(tmp_path/'summary.sqlite3'))
    async def run():
        task=await TaskService(SqliteTaskRepository()).create_task(TaskCreate(task_name='Mini',keyword='mini',
            decision_mode='keyword',keyword_rules=['mini'],enabled=False,revisit_enabled=True))
        for item,price in [('1',5200),('2',6500),('3',7000),('4',8000)]:
            await save_result_record(record(item,price),'mini')
        with sqlite_connection() as conn:
            conn.execute("UPDATE result_listing_items SET worker_id='fixture' WHERE item_id!='1'");conn.commit()
        for item,price in [('2',4000),('3',5000),('4',None)]:
            ResultRevisitService().observe(item,'fixture',{'raw_status':1,'price':price,'request_ok':True,'page_complete':True},task.model_dump())
        record_market_snapshots(keyword='mini',task_name='Mini',items=[record('1',5300)['商品信息']],
            run_id='history',snapshot_time='2026-10-29T08:00:00')
        sender=AsyncMock()
        assert await send_result_price_summary('mini','Mini','four-metrics',sender)
        data,text=sender.call_args.args
        assert data=={'notification_type':'price_summary','商品标题':'价格摘要 · Mini'}
        assert text.splitlines()==['当前均价：¥5200.00','历史均价：¥5300.00','已售出均价：¥4500.00','售出最低价：¥4000.00']
        assert not await send_result_price_summary('mini','Mini','four-metrics',sender)
        sender.assert_awaited_once()
        with sqlite_connection() as conn:
            conn.execute("UPDATE result_items SET status='hidden' WHERE item_id='1'");conn.commit()
        assert await send_result_price_summary('mini','Mini','sold-only',sender)
        assert sender.call_args.args[1].splitlines()[0]=='当前均价：暂无有效样本'
        assert '已售出均价：¥4500.00' in sender.call_args.args[1]
    asyncio.run(run())
