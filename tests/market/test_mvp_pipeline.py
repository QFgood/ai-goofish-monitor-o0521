import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

from src.services import market_types, market_runner
from src.services.market_runner import MarketRunner
from src.services.market_statistics_service import MarketStatisticsService
from src.services.market_types import SellerListing
from test_lifecycle import market, sample, item


def test_discovery_next_day_price_drop_sold_archive_and_trend_without_llm(market, monkeypatch):
    repo, _, task, _ = market
    clock = [datetime.fromisoformat('2026-10-01T08:30:00+08:00')]
    monkeypatch.setattr(market_types, 'now', lambda: clock[0])
    monkeypatch.setattr(market_runner, 'now', lambda: clock[0])
    from src.infrastructure.external.ai_client import AIClient
    monkeypatch.setattr(AIClient, '_call_ai', AsyncMock(side_effect=AssertionError('LLM must not be called')))
    class Adapter:
        def __init__(self, task, repo):
            self.task = task
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def search(self):
            yield sample()
        async def seller_items(self, seller):
            assert seller == '2001'
            return SellerListing(items=[{'item_id':'1001','price':'2800','raw_status':0 if clock[0].day==2 else 1}], request_ok=True,page_complete=True,list_complete=True)
    runner = MarketRunner(repo, adapter_factory=Adapter)
    asyncio.run(runner.search(task['id']))
    assert item(repo)['next_review_at'] == '2026-10-02T09:30:00+08:00'
    clock[0] = datetime.fromisoformat('2026-10-02T09:30:00+08:00')
    asyncio.run(runner.revisits())
    assert item(repo)['last_listing_price'] == 280000
    clock[0] = datetime.fromisoformat('2026-10-03T09:30:00+08:00')
    asyncio.run(runner.revisits())
    assert item(repo)['listing_status'] == 'sold'
    stats = MarketStatisticsService(repo)
    points = stats.trends('2026-10-01','2026-10-03')['items']
    assert points[0]['active']['median_price'] == 300000
    assert points[1]['active']['median_price'] == 280000
    assert points[2]['sold']['median_price'] == 280000
    assert points[2]['sold_count'] == 1
    assert item(repo)['actual_transaction_price'] is None
    with repo.connection() as conn:
        assert conn.execute("SELECT count(*) FROM market_runs WHERE status='success'").fetchone()[0] == 3
    AIClient._call_ai.assert_not_awaited()


def test_daily_notice_dedup_and_no_sample_language(market):
    repo, _, task, _ = market
    notifier = AsyncMock(return_value={'wx':{'success':True}})
    runner = MarketRunner(repo, notifier=notifier)
    async def run():
        await runner.daily_summaries()
        await runner.daily_summaries()
    asyncio.run(run())
    notifier.assert_awaited_once()
    reason = notifier.call_args.args[1]
    assert '暂无有效样本' in reason
    assert '不等于真实成交价' in reason


def test_seven_unknown_reviews_become_stale_without_sale(market):
    repo, _, task, lifecycle = market
    lifecycle.discover(task['id'], sample(), 'd1', '2026-10-01T08:30:00+08:00')
    for day in range(2,9):
        lifecycle.observe('1001', f'review{day}', raw_status=None, at=f'2026-10-{day:02}T09:30:00+08:00')
    assert item(repo)['listing_status'] == 'stale_unconfirmed'
    assert item(repo)['first_seen_sold_at'] is None
    assert item(repo)['next_review_at'] is None
