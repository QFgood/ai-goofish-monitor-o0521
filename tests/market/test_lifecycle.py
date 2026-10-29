import json
import sqlite3

import pytest
from src.infrastructure.persistence.market_repository import MarketRepository
from src.infrastructure.persistence.sqlite_connection import sqlite_connection, init_schema
from src.services.item_lifecycle_service import ItemLifecycleService
from src.services.market_tracking_service import MarketTrackingService
from src.services.market_types import cents
from src.services.product_variant_service import classify_product


@pytest.fixture
def market(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = MarketRepository(str(tmp_path / 'market.sqlite3'))
    tasks = MarketTrackingService(repo)
    task = tasks.create_task({'task_name': 'Mac mini', 'keyword': 'Mac mini M6 16GB'}, '2026-10-01T08:00:00+08:00')
    return repo, tasks, task, ItemLifecycleService(repo)


def sample(item_id='1001', price='3000', seller='2001'):
    return {'item_id': item_id, 'title': 'Mac mini M6 16GB 256GB', 'price': price,
            'seller_id': seller, 'seller_nickname': 'fixture nickname'}


def item(repo, item_id='1001'):
    with repo.connection() as conn:
        return dict(conn.execute('SELECT * FROM market_items WHERE item_id=?', (item_id,)).fetchone())


def test_discovery_deduplication_multiple_tasks_and_nickname(market):
    repo, tasks, task, service = market
    assert service.discover(task['id'], sample(), 'run1', '2026-10-01T08:30:00+08:00')
    assert not service.discover(task['id'], sample(), 'run1', '2026-10-01T08:30:00+08:00')
    second = tasks.create_task({'task_name': 'Other keyword', 'keyword': 'mini M6'})
    assert not service.discover(second['id'], {**sample(), 'seller_nickname': 'new nickname'}, 'run2', '2026-10-02T08:30:00+08:00')
    with repo.connection() as conn:
        assert conn.execute('SELECT count(*) FROM market_items').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM market_task_items').fetchone()[0] == 2
        assert conn.execute('SELECT count(*) FROM market_item_observations').fetchone()[0] == 2
    result = item(repo)
    assert result['seller_id'] == '2001'
    assert result['seller_nickname'] == 'new nickname'
    assert result['first_listed_price'] == 300000
    assert result['next_review_at'] == '2026-10-02T09:30:00+08:00'


def test_lowering_prices_then_sold_reference_is_last_confirmed_listing(market):
    repo, _, task, service = market
    service.discover(task['id'], sample(), 'run1', '2026-10-01T08:30:00+08:00')
    service.observe('1001', 'r2', price='2800', raw_status=0, at='2026-10-02T09:30:00+08:00')
    service.observe('1001', 'r3', price='2600', raw_status=0, at='2026-10-03T09:30:00+08:00')
    assert service.observe('1001', 'r4', price='2500', raw_status=1, at='2026-10-04T09:30:00+08:00')
    assert not service.observe('1001', 'r5', price='2400', raw_status=1, at='2026-10-05T09:30:00+08:00')
    result = item(repo)
    assert result['listing_status'] == 'sold'
    assert result['sold_reference_price'] == result['last_listing_price'] == 260000
    assert result['sold_display_price'] == 240000
    assert result['actual_transaction_price'] is None
    assert result['first_seen_sold_at'] == '2026-10-04T09:30:00+08:00'
    assert result['next_review_at'] is None
    with repo.connection() as conn:
        assert conn.execute('SELECT count(*) FROM market_item_observations').fetchone()[0] == 5
        assert conn.execute("SELECT count(*) FROM market_status_events WHERE new_status='sold'").fetchone()[0] == 1
    service.discover(task['id'], sample(price='2700'), 'relisted', '2026-10-06T08:30:00+08:00')
    assert item(repo)['listing_status'] == 'active'
    assert item(repo)['sold_reference_price'] == 260000  # first archive never silently rewritten


@pytest.mark.parametrize('kwargs', [
    {'raw_status': None}, {'raw_status': 99}, {'raw_status': 1, 'request_ok': False},
    {'raw_status': 1, 'page_complete': False}, {'raw_status': True},
])
def test_absence_failure_unknown_cannot_imply_sold(market, kwargs):
    repo, _, task, service = market
    service.discover(task['id'], sample(), 'first', '2026-10-01T08:30:00+08:00')
    assert not service.observe('1001', 'check', at='2026-10-02T09:30:00+08:00', **kwargs)
    assert item(repo)['listing_status'] == 'active'
    assert item(repo)['first_seen_sold_at'] is None


def test_missing_seller_is_preserved_not_fabricated(market):
    repo, _, task, service = market
    service.discover(task['id'], sample(seller=None), 'r1')
    assert item(repo)['seller_id'] is None
    assert item(repo)['seller_url'] is None


def test_old_evidence_does_not_regress_state(market):
    repo, _, task, service = market
    service.discover(task['id'], sample(), 'r1', '2026-10-01T08:30:00+08:00')
    service.observe('1001', 'r3', price='2500', raw_status=1, at='2026-10-03T09:30:00+08:00')
    service.observe('1001', 'r2', price='2700', raw_status=0, at='2026-10-02T09:30:00+08:00')
    assert item(repo)['listing_status'] == 'sold'


def test_delete_preserves_history_and_manual_classification_is_audited(market):
    repo, tasks, task, service = market
    service.discover(task['id'], sample(), 'r1')
    service.correct_classification('1001', {'product_variant': 'manually corrected', 'excluded': True}, 'fixture_admin')
    service.discover(task['id'], sample(), 'r2')
    assert item(repo)['product_variant'] == 'manually corrected'
    with repo.connection() as conn:
        assert conn.execute('SELECT count(*) FROM market_classification_events').fetchone()[0] == 1
    tasks.delete_task(task['id'])
    assert tasks.tasks() == []
    assert item(repo)['item_id'] == '1001'


@pytest.mark.parametrize('title,expected', [
    ('Mac mini M6 16GB 256GB', 'Mac mini / M6 / 16GB / 256GB'),
    ('Mac mini M6 24G+512G', 'Mac mini / M6 / 24GB / 512GB'),
    ('Mac mini M4 Pro 内存24GB SSD 1TB', 'Mac mini / M4 PRO / 24GB / 1024GB'),
    ('Mac mini 16GB', 'unknown'), ('Mac mini M4 16GB/24GB 256GB', 'unknown'),
    ('USB扩展坞', 'unknown'),
])
def test_variants(title, expected):
    assert classify_product(title)['product_variant'] == expected


def test_accessory_fault_exclusion_and_decimal_prices():
    assert classify_product('Mac mini 空盒')['excluded']
    assert classify_product('Mac mini M4 无法开机')['excluded']
    assert not classify_product('Mac mini M4 无故障 带电源')['excluded']
    assert cents('¥1,234.56') == 123456
    assert cents('0.35万') == 350000
    for invalid in ['NaN', '-1', '1e5', '999999999999999999', '0', True]:
        assert cents(invalid) is None


def test_additive_upgrade_is_repeatable_preserves_legacy_rows(market):
    repo, _, _, _ = market
    with repo.connection(True) as conn:
        conn.execute("INSERT INTO app_metadata VALUES('fixture','preserve')")
        conn.execute("INSERT INTO price_snapshots(keyword_slug,keyword,task_name,snapshot_time,snapshot_day,run_id,item_id,price,tags_json) VALUES('a','a','a','2026-01-01','2026-01-01','legacy','old',10,'[]')")
        legacy = conn.execute('SELECT * FROM price_snapshots').fetchall()
    for _ in range(2):
        with sqlite_connection(repo.db_path) as conn:
            init_schema(conn)
    with repo.connection() as conn:
        assert conn.execute("SELECT value FROM app_metadata WHERE key='fixture'").fetchone()[0] == 'preserve'
        assert [tuple(r) for r in conn.execute('SELECT * FROM price_snapshots')] == [tuple(r) for r in legacy]
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


def test_real_legacy_task_survives_market_upgrade(market, sample_task_payload):
    import asyncio
    from src.domain.models.task import Task
    from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
    repo, _, _, _ = market
    legacy = SqliteTaskRepository(db_path=repo.db_path, legacy_config_file=None)
    saved = asyncio.run(legacy.save(Task(**sample_task_payload)))
    original = saved.model_dump()
    for _ in range(2):
        with sqlite_connection(repo.db_path) as conn:
            init_schema(conn)
    assert asyncio.run(legacy.find_by_id(saved.id)).model_dump() == original


def test_sqlite_backup_retains_rows_and_rejects_missing_source(market, tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('sentinel_backup', Path(__file__).resolve().parents[2]/'scripts/sentinel_backup.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repo, _, _, _ = market
    target = module.backup_database(repo.db_path, tmp_path/'backups')
    assert target.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(target) as copied:
        assert copied.execute('SELECT count(*) FROM market_tasks').fetchone()[0] == 1
    with pytest.raises(FileNotFoundError):
        module.backup_database(tmp_path/'missing.sqlite3', tmp_path/'backups')
    assert not (tmp_path/'missing.sqlite3').exists()


def test_first_sold_observation_cannot_invent_a_listing_or_transaction_price(market):
    repo, _, task, service=market
    service.discover(task['id'],{**sample(),'raw_status':1},'already-sold')
    result=item(repo)
    assert result['sold_display_price']==300000
    assert result['first_listed_price'] is None
    assert result['sold_reference_price'] is None
    assert result['actual_transaction_price'] is None
