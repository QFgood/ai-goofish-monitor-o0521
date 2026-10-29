"""Reproducible statistics in integer fen; first sold confirmation is not trade date."""
import csv
import io
from collections import defaultdict
from datetime import date, timedelta
from statistics import median

from src.services.market_types import stamp

PRICE_NOTE = '已售参考价（最后挂牌价），不等于实际成交价；已售确认日期不一定是实际成交日。'


def summarize(rows, price='price'):
    rows = list(rows)
    valid = [r for r in rows if r.get(price) is not None]
    values = [r[price] for r in valid]
    return {'sample_count': len(values), 'total_count': len(rows),
            'avg_price': round(sum(values) / len(values), 2) if values else None,
            'median_price': median(values) if values else None,
            'low_sample': 0 < len(values) < 5,
            'samples': [{'item_id': r['item_id'], 'price': r[price]} for r in valid]}


def filters_sql(filters, alias='i'):
    conditions, values = ['1=1'], []
    for key in ['product_variant', 'product_category', 'region', 'listing_status']:
        if filters.get(key):
            conditions.append(f'{alias}.{key}=?')
            values.append(filters[key])
    if not filters.get('include_excluded', False):
        conditions.append(f'{alias}.excluded=0')
    if filters.get('task_id') is not None:
        conditions.append(f'EXISTS(SELECT 1 FROM market_task_items ti WHERE ti.item_id={alias}.item_id AND ti.task_id=?)')
        values.append(filters['task_id'])
    for name, operator in [('min_price', '>='), ('max_price', '<=')]:
        if filters.get(name) is not None:
            conditions.append(f"(CASE WHEN {alias}.listing_status='sold' THEN {alias}.sold_reference_price ELSE {alias}.last_listing_price END){operator}?")
            values.append(filters[name])
    return ' AND '.join(conditions), values


class MarketStatisticsService:
    def __init__(self, repository):
        self.repo = repository

    def items(self, page=1, limit=20, **filters):
        where, values = filters_sql(filters)
        with self.repo.connection() as conn:
            total = conn.execute(f'SELECT count(*) FROM market_items i WHERE {where}', values).fetchone()[0]
            rows = conn.execute(f'''SELECT i.*,j.status AS revisit_status,j.last_error AS revisit_error,j.last_result AS revisit_result,
                j.failures AS revisit_failures FROM market_items i LEFT JOIN market_revisit_jobs j USING(item_id)
                WHERE {where} ORDER BY i.first_seen_at DESC,i.item_id LIMIT ? OFFSET ?''', (*values, limit, (page-1)*limit)).fetchall()
        return {'items': [dict(r) for r in rows], 'total': total, 'page': page, 'limit': limit, 'price_unit': 'fen', 'note': PRICE_NOTE}

    def history(self, item_id, page=1, limit=100):
        with self.repo.connection() as conn:
            item = conn.execute('SELECT * FROM market_items WHERE item_id=?', (item_id,)).fetchone()
            if not item:
                raise ValueError('market_item_not_found')
            total = conn.execute('SELECT count(*) FROM market_item_observations WHERE item_id=?', (item_id,)).fetchone()[0]
            observations = conn.execute('SELECT * FROM market_item_observations WHERE item_id=? ORDER BY observed_at DESC,id DESC LIMIT ? OFFSET ?', (item_id, limit, (page-1)*limit)).fetchall()
            events = conn.execute('SELECT * FROM market_status_events WHERE item_id=? ORDER BY observed_at DESC,id DESC LIMIT ? OFFSET ?', (item_id, limit, (page-1)*limit)).fetchall()
            changes = conn.execute('SELECT * FROM market_classification_events WHERE item_id=? ORDER BY id DESC LIMIT ? OFFSET ?', (item_id, limit, (page-1)*limit)).fetchall()
        return {'item': dict(item), 'items': [dict(r) for r in observations], 'events': [dict(r) for r in events],
                'classification_events': [dict(r) for r in changes], 'total': total, 'page': page, 'limit': limit, 'price_unit': 'fen', 'note': PRICE_NOTE}

    @staticmethod
    def _price_selected(row, field, filters):
        value = row[field]
        if filters.get('min_price') is not None and (value is None or value < filters['min_price']):
            return False
        if filters.get('max_price') is not None and (value is None or value > filters['max_price']):
            return False
        return True

    def trends(self, start, end, **filters):
        start, end = date.fromisoformat(start), date.fromisoformat(end)
        if start > end or (end-start).days > 366:
            raise ValueError('date_range_must_be_0_to_366_days')
        where, values = filters_sql({k:v for k,v in filters.items() if k not in {'min_price','max_price'}})
        low = (start-timedelta(days=6)).isoformat() + 'T00:00:00+08:00'
        high = (end+timedelta(days=1)).isoformat() + 'T00:00:00+08:00'
        with self.repo.connection() as conn:
            observations = conn.execute(f'''SELECT o.* FROM market_item_observations o JOIN market_items i USING(item_id)
                WHERE {where} AND o.observed_at>=? AND o.observed_at<? AND o.request_ok=1 AND o.page_complete=1
                AND o.parsed_status IN ('active','sold','offline') ORDER BY o.observed_at,o.id''', (*values, low, high)).fetchall()
            sold = [dict(r) for r in conn.execute(f'''SELECT i.* FROM market_items i WHERE {where}
                AND first_seen_sold_at IS NOT NULL AND first_seen_sold_at<?''', (*values, high))]
        latest = {}
        for observation in observations:
            latest[(observation['observed_at'][:10], observation['item_id'])] = dict(observation)
        active_days, sold_days = defaultdict(list), defaultdict(list)
        for (day, _), observation in latest.items():
            if observation['parsed_status'] == 'active' and self._price_selected(observation, 'price', filters):
                active_days[day].append(observation)
        for item in sold:
            if not self._price_selected(item, 'sold_reference_price', filters):
                continue
            sold_days[item['first_seen_sold_at'][:10]].append(item)
        cumulative = sum(len(v) for day, v in sold_days.items() if day < start.isoformat())
        points = []
        day = start
        while day <= end:
            label = day.isoformat()
            daily_sold = sold_days[label]
            cumulative += len(daily_sold)
            rolling = [r for key, group in sold_days.items() if (day-timedelta(days=6)).isoformat() <= key <= label for r in group]
            points.append({'day': label, 'active': summarize(active_days[label]),
                           'sold': summarize(daily_sold, 'sold_reference_price'),
                           'sold_count': len(daily_sold), 'cumulative_sold_count': cumulative,
                           'sold_rolling_7d': summarize(rolling, 'sold_reference_price')})
            day += timedelta(days=1)
        return {'items': points, 'price_unit': 'fen', 'note': PRICE_NOTE,
                'date_basis': 'Asia/Shanghai; first_seen_sold_at', 'missing_values': 'null'}

    def summary(self, at=None, **filters):
        today = date.fromisoformat(stamp(at)[:10])
        where, values = filters_sql({k:v for k,v in filters.items() if k not in {'min_price','max_price'}})
        with self.repo.connection() as conn:
            rows = [dict(r) for r in conn.execute(f'SELECT i.*,j.status AS tracking_status FROM market_items i LEFT JOIN market_revisit_jobs j USING(item_id) WHERE {where}', values)]
        active = [r for r in rows if r['listing_status'] == 'active' and r['tracking_status'] != 'expired' and self._price_selected(r, 'last_listing_price', filters)]
        sold = [r for r in rows if self._price_selected(r, 'sold_reference_price', filters) and r['first_seen_sold_at'] and r['first_seen_sold_at'][:10] <= today.isoformat()]
        def window(days, previous=False):
            end = today-timedelta(days=days if previous else 0)
            start = end-timedelta(days=days-1)
            return [r for r in sold if start.isoformat() <= r['first_seen_sold_at'][:10] <= end.isoformat()]
        changes = {}
        for days in [7, 30]:
            current = summarize(window(days), 'sold_reference_price')
            prior = summarize(window(days, True), 'sold_reference_price')
            a, b = current['median_price'], prior['median_price']
            changes[str(days)] = {'current': a, 'previous': b, 'amount': a-b if a is not None and b is not None else None,
                                 'percent': round((a-b)/b*100, 2) if a is not None and b else None,
                                 'current_samples': current['sample_count'], 'previous_samples': prior['sample_count']}
        active_ids = {r['item_id'] for r in active}
        sold_ids = {r['item_id'] for r in sold}
        variants = defaultdict(list)
        for row in rows:
            variants[row['product_variant']].append(row)
        comparison = [{'product_variant': variant,
                       'active': summarize([r for r in group if r['item_id'] in active_ids], 'last_listing_price'),
                       'sold': summarize([r for r in group if r['item_id'] in sold_ids], 'sold_reference_price')}
                      for variant, group in sorted(variants.items())]
        return {'active': summarize(active, 'last_listing_price'), 'sold_total': len(sold),
                'sold_today': sum(r['first_seen_sold_at'][:10] == today.isoformat() for r in sold),
                'sold_7d': summarize(window(7), 'sold_reference_price'), 'sold_30d': summarize(window(30), 'sold_reference_price'),
                'changes': changes, 'variants': comparison, 'note': PRICE_NOTE, 'price_unit': 'fen'}

    def export(self, **filters):
        where, values = filters_sql(filters)
        fields = ['item_id','seller_id','seller_nickname','title','product_variant','region','listing_status',
                  'first_seen_at','first_listed_price','last_listing_price','first_seen_sold_at','sold_reference_price',
                  'sold_display_price','actual_transaction_price','price_source','item_url','seller_url']
        def cell(value):
            if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
                return "'" + value
            return '' if value is None else value
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(fields + ['price_unit','price_note'])
        yield '\ufeff' + buffer.getvalue()
        last_id = ''
        while True:
            # Streaming iterators may resume on different pool threads. Never
            # keep a SQLite connection open across a yield; keyset-page instead.
            with self.repo.connection() as conn:
                rows = conn.execute(f'SELECT i.* FROM market_items i WHERE {where} AND i.item_id>? ORDER BY i.item_id LIMIT 500', (*values, last_id)).fetchall()
            if not rows:
                break
            for row in rows:
                buffer.seek(0)
                buffer.truncate()
                writer.writerow([cell(row[k]) for k in fields] + ['fen', PRICE_NOTE])
                yield buffer.getvalue()
            last_id = rows[-1]['item_id']
