import csv
import io
from src.services.market_statistics_service import MarketStatisticsService
from test_lifecycle import market, sample


def test_no_samples_are_null_and_dates_not_filled_with_fake_prices(market):
    repo, _, _, _ = market
    stats = MarketStatisticsService(repo)
    summary = stats.summary('2026-10-03T12:00:00+08:00')
    assert summary['active']['median_price'] is None
    assert summary['sold_7d']['avg_price'] is None
    points = stats.trends('2026-10-01', '2026-10-03')['items']
    assert len(points) == 3
    assert all(p['sold']['sample_count'] == 0 and p['sold']['median_price'] is None for p in points)


def test_statistics_medians_unique_sold_dates_rolling_and_variant_isolation(market):
    repo, _, task, lifecycle = market
    for item_id, price in [('1001','3000'),('1002','2000'),('1003','4000')]:
        lifecycle.discover(task['id'], sample(item_id, price), 'discover', '2026-10-01T08:30:00+08:00')
        lifecycle.observe(item_id, 'sold', raw_status=1, at='2026-10-02T09:30:00+08:00')
        lifecycle.observe(item_id, 'sold-again', raw_status=1, at='2026-10-03T09:30:00+08:00')
    lifecycle.discover(task['id'], {**sample('1004','9999'), 'title': 'Mac mini M6 24GB 512GB'}, 'other', '2026-10-01T08:30:00+08:00')
    stats = MarketStatisticsService(repo)
    variant = 'Mac mini / M6 / 16GB / 256GB'
    summary = stats.summary('2026-10-03T12:00:00+08:00', product_variant=variant)
    assert summary['sold_total'] == summary['sold_7d']['sample_count'] == 3
    assert summary['sold_7d']['median_price'] == summary['sold_7d']['avg_price'] == 300000
    points = stats.trends('2026-10-01','2026-10-04', product_variant=variant)['items']
    assert points[0]['active']['median_price'] == 300000
    assert points[1]['sold_count'] == 3
    assert points[2]['sold_count'] == 0
    assert points[2]['sold']['median_price'] is None
    assert points[2]['sold_rolling_7d']['median_price'] == 300000
    assert points[3]['cumulative_sold_count'] == 3
    other = stats.summary('2026-10-03T12:00:00+08:00', product_variant='Mac mini / M6 / 24GB / 512GB')
    assert other['active']['median_price'] == 999900
    assert other['sold_total'] == 0


def test_same_day_latest_price_and_sold_display_not_used_for_reference(market):
    repo, _, task, lifecycle = market
    lifecycle.discover(task['id'], sample(), 'first', '2026-10-01T08:30:00+08:00')
    lifecycle.discover(task['id'], sample(price='2000'), 'second', '2026-10-01T10:30:00+08:00')
    lifecycle.observe('1001','sold',price='1',raw_status=1,at='2026-10-02T09:30:00+08:00')
    points = MarketStatisticsService(repo).trends('2026-10-01','2026-10-02')['items']
    assert points[0]['active']['sample_count'] == 1
    assert points[0]['active']['median_price'] == 200000
    assert points[1]['sold']['median_price'] == 200000


def test_csv_filters_formula_injection_and_history_pagination(market):
    repo, _, task, lifecycle = market
    lifecycle.discover(task['id'], {**sample(), 'title': '=HYPERLINK("fixture")'}, 'first')
    stats = MarketStatisticsService(repo)
    text = ''.join(stats.export(task_id=task['id']))
    rows = list(csv.DictReader(io.StringIO(text.lstrip('\ufeff'))))
    assert len(rows) == 1
    assert rows[0]['title'].startswith("'=")
    assert rows[0]['actual_transaction_price'] == ''
    assert rows[0]['price_unit'] == 'fen'
    assert stats.history('1001', limit=1)['total'] == 1
    assert stats.items(product_variant='missing')['total'] == 0


def test_historical_price_filter_uses_observation_price_not_current_price(market):
    repo, _, task, lifecycle = market
    lifecycle.discover(task['id'],sample(price='3000'),'first','2026-10-01T08:30:00+08:00')
    lifecycle.discover(task['id'],sample(price='2000'),'second','2026-10-02T08:30:00+08:00')
    points=MarketStatisticsService(repo).trends('2026-10-01','2026-10-02',min_price=250000)['items']
    assert points[0]['active']['median_price']==300000
    assert points[1]['active']['median_price'] is None
