import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from src.infrastructure.persistence.market_repository import MarketRepository
from src.services.market_types import SellerListing, stamp, LoginExpired
from src.services.seller_revisit_service import SellerRevisitService
from src.services.market_runner import MarketRunner, MarketBusy
from src.services.market_platform import parse_seller_listing, validate_payload
from test_lifecycle import market, sample, item
import pytest


def test_seller_grouping_missing_items_and_restart_recovery(market):
    repo, _, task, lifecycle = market
    for item_id in ['1001', '1002', '1003']:
        lifecycle.discover(task['id'], sample(item_id), 'd1', '2026-10-01T08:30:00+08:00')
    adapter = AsyncMock()
    adapter.task = task
    adapter.seller_items.return_value = SellerListing(
        items=[{'item_id': '1001', 'raw_status': 1, 'price': '2700'},
               {'item_id': '1002', 'raw_status': 0, 'price': '2800'}],
        request_ok=True, page_complete=True, list_complete=True)
    # Simulate a worker crash after claiming; another worker cannot steal a live lease.
    queue = SellerRevisitService(repo)
    assert len(queue.claim_due('crashed', '2026-10-02T09:30:00+08:00')) == 3
    restarted = SellerRevisitService(MarketRepository(repo.db_path))
    assert restarted.claim_due('early', '2026-10-02T09:31:00+08:00') == []
    result = asyncio.run(restarted.run_due(adapter, 'recovered', '2026-10-02T10:00:00+08:00'))
    assert result == {'reviewed': 3, 'sold': 1}
    adapter.seller_items.assert_awaited_once_with('2001')
    assert item(repo)['listing_status'] == 'sold'
    assert item(repo, '1002')['last_listing_price'] == 280000
    assert item(repo, '1003')['listing_status'] == 'active'
    assert item(repo, '1003')['first_seen_sold_at'] is None


def test_failed_revisit_requeues_and_backfills_seller(market):
    repo, _, task, lifecycle = market
    lifecycle.discover(task['id'], sample(seller=None), 'd1', '2026-10-01T08:30:00+08:00')
    adapter = AsyncMock()
    adapter.task = task
    adapter.detail_seller.return_value = '2001'
    adapter.seller_items.side_effect = TimeoutError('fixture')
    queue = SellerRevisitService(repo)
    result = asyncio.run(queue.run_due(adapter, 'fail', '2026-10-02T09:30:00+08:00'))
    assert result == {'reviewed': 1, 'sold': 0}
    assert item(repo)['seller_id'] == '2001'
    assert item(repo)['listing_status'] == 'active'
    with repo.connection() as conn:
        row = conn.execute('SELECT * FROM market_revisit_jobs').fetchone()
        assert row['status'] == 'pending'
        assert row['failures'] == 1
        assert row['last_error'] == 'seller_request_failed'
        assert row['next_run_at'] == '2026-10-03T09:30:00+08:00'


def test_missing_seller_does_not_load_fabricated_profile(market):
    repo, _, task, lifecycle = market
    lifecycle.discover(task['id'], sample(seller=None), 'd1', '2026-10-01T08:30:00+08:00')
    adapter = AsyncMock()
    adapter.task = task
    adapter.detail_seller.return_value = None
    asyncio.run(SellerRevisitService(repo).run_due(adapter, 'review', '2026-10-02T09:30:00+08:00'))
    adapter.seller_items.assert_not_awaited()
    assert item(repo)['seller_id'] is None


def test_paused_tasks_and_tracking_deadline(market):
    repo, tasks, task, lifecycle = market
    lifecycle.discover(task['id'], sample(), 'd1', '2026-10-01T08:30:00+08:00')
    tasks.pause_all()
    queue = SellerRevisitService(repo)
    assert queue.claim_due('paused', '2026-10-02T09:30:00+08:00') == []
    tasks.update_task(task['id'], {'enabled': True})
    assert queue.claim_due('expired', '2026-12-01T09:30:00+08:00') == []
    with repo.connection() as conn:
        assert conn.execute('SELECT status FROM market_revisit_jobs').fetchone()[0] == 'expired'


def test_lease_prevents_parallel_work_and_releases_after_failure(market):
    repo, _, _, _ = market
    runner = MarketRunner(repo)
    async def check():
        async with runner.lease('test'):
            with pytest.raises(MarketBusy):
                async with runner.lease('test'):
                    pass
        async with runner.lease('test'):
            pass
    asyncio.run(check())
    with repo.connection() as conn:
        assert conn.execute('SELECT count(*) FROM market_leases').fetchone()[0] == 0


def test_login_failure_pauses_market_only_and_preserves_progress(market):
    repo, tasks, task, _ = market
    class InvalidAccount:
        async def __aenter__(self):
            raise LoginExpired('fixture_login_expired')
        async def __aexit__(self, *_):
            pass
    runner = MarketRunner(repo, adapter_factory=lambda *_: InvalidAccount())
    asyncio.run(runner.search(task['id']))
    assert tasks.tasks()[0]['enabled'] == 0
    with repo.connection() as conn:
        run = conn.execute('SELECT * FROM market_runs').fetchone()
        assert run['status'] == 'paused'
        assert run['error'] == 'login_expired'


def test_lightweight_listing_adapter_preserves_unknown_codes(load_json_fixture):
    cards = load_json_fixture('user_items.json')
    listing = asyncio.run(parse_seller_listing({'ret': ['SUCCESS::ok'], 'data': {'cardList': cards, 'nextPage': False}}))
    assert listing.list_complete
    assert listing.items[0]['raw_status'] == 0
    assert listing.items[1]['raw_status'] == 1
    cards[0]['cardData']['itemStatus'] = 77
    changed = asyncio.run(parse_seller_listing({'ret': ['SUCCESS::ok'], 'data': {'cardList': cards}}))
    assert changed.items[0]['raw_status'] == 77
    assert not changed.list_complete
    with pytest.raises(LoginExpired):
        validate_payload({'ret': ['FAIL_SYS_SESSION_EXPIRED']})


def test_stale_worker_cannot_write_after_losing_lease(market):
    from src.services.market_types import LeaseLost
    repo, _, task, lifecycle = market
    lifecycle.discover(task['id'], sample(), 'first')
    with repo.connection(True) as conn:
        conn.execute("INSERT INTO market_leases VALUES('worker','new-owner','2099-01-01T00:00:00+08:00')")
    with pytest.raises(LeaseLost):
        lifecycle.discover(task['id'], sample(price='1'), 'stale-run', worker_id='old-owner')
    assert item(repo)['last_listing_price'] == 300000


def test_two_concurrent_claimers_cannot_claim_the_same_item(market):
    from concurrent.futures import ThreadPoolExecutor
    repo, _, task, lifecycle = market
    lifecycle.discover(task['id'], sample(), 'd1', '2026-10-01T08:30:00+08:00')
    def claim(worker):
        return SellerRevisitService(repo).claim_due(worker, '2026-10-02T09:30:00+08:00')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ['worker1','worker2']))
    assert sum(len(r) for r in results) == 1


def test_browser_start_failure_records_unknown_and_bounded_retry(market, monkeypatch):
    from src.services import market_types, market_runner
    repo, _, task, lifecycle = market
    lifecycle.discover(task['id'], sample(), 'd1', '2026-10-01T08:30:00+08:00')
    current=datetime.fromisoformat('2026-10-02T09:30:00+08:00')
    monkeypatch.setattr(market_types,'now',lambda:current)
    monkeypatch.setattr(market_runner,'now',lambda:current)
    class BrokenBrowser:
        async def __aenter__(self):
            raise RuntimeError('fixture_browser_unavailable')
        async def __aexit__(self,*_):
            pass
    runner=MarketRunner(repo,adapter_factory=lambda *_:BrokenBrowser())
    asyncio.run(runner.revisits())
    assert item(repo)['listing_status']=='active'
    with repo.connection() as conn:
        job=conn.execute('SELECT * FROM market_revisit_jobs').fetchone()
        assert job['status']=='pending'
        assert job['failures']==1
        assert job['next_run_at']=='2026-10-03T09:30:00+08:00'
        observation=conn.execute('SELECT * FROM market_item_observations ORDER BY id DESC LIMIT 1').fetchone()
        assert not observation['request_ok']
        assert observation['parsed_status']=='unknown'
