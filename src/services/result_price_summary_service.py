"""Prices of the actual filtered results; no LLM, transaction-price inference or deletion."""
import hashlib
import math
from datetime import datetime
from collections import defaultdict
from zoneinfo import ZoneInfo
from statistics import median

from src.services.price_history_service import parse_price_value, load_price_snapshots, _summarize_prices
from src.services.result_storage_service import load_all_result_records
from src.infrastructure.persistence.sqlite_connection import sqlite_connection


def valid_price(value):
    try:
        value = parse_price_value(value)
        return value if value is not None and math.isfinite(value) and value > 0 else None
    except (ValueError, OverflowError):
        return None


def _record_key(record):
    product = record.get('商品信息', {})
    # Legacy rows without an ID/link remain separate; never drop or invent IDs.
    return product.get('商品ID') or product.get('商品链接') or id(record)


def select_priced_results(records, *, min_price=None, max_price=None, exclude_low_price=True):
    source = records
    latest = {}
    for record in sorted(records, key=lambda r: r.get('爬取时间', '')):
        product = record.get('商品信息', {})
        latest[_record_key(record)] = record
    records = [r for r in source if latest.get(_record_key(r)) is r]
    prices = [p for r in records if (p := valid_price(r.get('商品信息', {}).get('当前售价'))) is not None]
    threshold = round(median(prices) * .2, 2) if exclude_low_price and len(prices) >= 5 else None
    kept, excluded = [], 0
    for record in records:
        price = valid_price(record.get('商品信息', {}).get('当前售价'))
        rejected = ((min_price is not None and (price is None or price < min_price))
                    or (max_price is not None and (price is None or price > max_price))
                    or (threshold is not None and price is not None and price < threshold))
        if rejected:
            excluded += 1
        else:
            kept.append(record)
    return kept, {'excluded_count': excluded, 'low_price_threshold': threshold,
                  'rule': '样本至少5件时，排除低于样本中位数20%的疑似引流价；不代表已确认欺诈'}


def histogram(prices):
    if not prices:
        return []
    minimum, maximum = min(prices), max(prices)
    target = min(12, max(1, math.ceil(math.log2(len(prices)) + 1)))
    rough = max((maximum-minimum)/target, 1)
    scale = 10 ** math.floor(math.log10(rough))
    step = next(n*scale for n in [1, 2, 5, 10] if n*scale >= rough)
    low = math.floor(minimum/step)*step
    count = min(14, math.floor((maximum-low)/step)+1)
    bins = [{'min_price': round(low+i*step, 2), 'max_price': round(low+(i+1)*step, 2),
             'count': 0, 'item_ids': []} for i in range(count)]
    return bins


def build_price_distribution(records):
    entries = [(valid_price(r.get('商品信息', {}).get('当前售价')), str(r.get('商品信息', {}).get('商品ID') or '')) for r in records]
    entries = [(p, i) for p, i in entries if p is not None]
    bins = histogram([p for p, _ in entries])
    if bins:
        low, step = bins[0]['min_price'], bins[0]['max_price']-bins[0]['min_price']
        for price, item_id in entries:
            index = min(len(bins)-1, int((price-low)//step))
            bins[index]['count'] += 1
            bins[index]['item_ids'].append(item_id)
    return bins


def summarize_result_prices(records):
    return _summarize_prices([{'price': p} for r in records
        if (p := valid_price(r.get('商品信息', {}).get('当前售价'))) is not None])


def apply_filtered_market_context(enriched, records):
    summary = summarize_result_prices(records)
    for row in enriched:
        row['price_insight'] = {**(row.get('price_insight') or {}),
            'market_avg_price': summary['avg_price'], 'market_median_price': summary['median_price']}
    return enriched


async def result_price_insights(filename, *, ai_recommended_only=False, keyword_recommended_only=False,
                                include_hidden=False, min_price=None, max_price=None, exclude_low_price=True):
    records = await load_all_result_records(filename, ai_recommended_only=ai_recommended_only,
        keyword_recommended_only=keyword_recommended_only, include_hidden=include_hidden, sort_by='crawl_time', sort_order='desc')
    selected, policy = select_priced_results(records, min_price=min_price, max_price=max_price, exclude_low_price=exclude_low_price)
    ids = {str(r.get('商品信息', {}).get('商品ID') or r.get('商品信息', {}).get('商品链接') or '') for r in selected}
    keyword = filename.removesuffix('_full_data.jsonl')
    snapshots = load_price_snapshots(keyword)
    # Use each item's last observation per day; never invent samples or overweight rapid retries.
    daily_latest = {}
    eligible = []
    for snapshot in snapshots:
        if str(snapshot.get('item_id')) not in ids:
            continue
        try:
            timestamp = datetime.fromisoformat(snapshot['snapshot_time'])
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
            timestamp = timestamp.astimezone(ZoneInfo('Asia/Shanghai'))
        except (ValueError, KeyError, TypeError):
            continue
        eligible.append((timestamp, snapshot))
    end = max((time for time, _ in eligible), default=None)
    latest = end.isoformat() if end else None
    if end:
        for timestamp, row in sorted(eligible, key=lambda entry: entry[0]):
            price = valid_price(row.get('price'))
            if price is None or (end-timestamp).total_seconds() > 30*86400:
                continue
            if (min_price is not None and price < min_price) or (max_price is not None and price > max_price):
                continue
            if policy['low_price_threshold'] is not None and price < policy['low_price_threshold']:
                continue
            daily_latest[(timestamp.date().isoformat(), row['item_id'])] = dict(row, price=price)
    historical = list(daily_latest.values())
    by_day = defaultdict(list)
    for (day, _), row in daily_latest.items():
        by_day[day].append(row)
    return {'market_summary': summarize_result_prices(selected),
            'history_summary': {'unique_items': len({r['item_id'] for r in historical}), **_summarize_prices(historical)},
            'price_distribution': build_price_distribution(selected), 'price_filter': policy,
            'daily_trend': [{'day': day, **_summarize_prices(rows)} for day, rows in sorted(by_day.items())], 'latest_snapshot_at': latest, 'total_items': len(selected),
            'summary_scope': '全部筛选结果（非仅当前分页）',
            'history_basis': '最近快照起30天内，每商品每天最后挂牌价；仅包含当前筛选商品',
            'price_note': '均为挂牌观察价，不是成交价'}


async def send_result_price_summary(keyword, task_name, run_id, sender):
    from src.infrastructure.persistence.storage_names import build_result_filename
    filename = build_result_filename(keyword)
    insights = await result_price_insights(filename)
    if insights['market_summary']['sample_count'] == 0:
        return False
    key = 'price_summary:' + hashlib.sha256((keyword+'\0'+run_id).encode()).hexdigest()
    with sqlite_connection() as conn:
        if not conn.execute("INSERT OR IGNORE INTO app_metadata(key,value) VALUES(?, 'sending')", (key,)).rowcount:
            return False
        conn.commit()
    current, history = insights['market_summary'], insights['history_summary']
    history_text = f"¥{history['avg_price']}（{history['sample_count']}个商品日样本）" if history['avg_price'] is not None else '暂无有效样本'
    reason = (f"任务：{task_name}\n当前均价：¥{current['avg_price']}（{current['sample_count']}件）\n"
              f"历史均价：{history_text}\n疑似低价/价格范围排除：{insights['price_filter']['excluded_count']}件\n"
              "历史按每商品每日最后挂牌价统计；参考价不等于真实成交价。")
    try:
        await sender({'商品标题': f'价格摘要 · {task_name}', '当前售价': 'N/A', '商品链接': 'https://www.goofish.com/'}, reason)
    except Exception:
        with sqlite_connection() as conn:
            conn.execute("UPDATE app_metadata SET value='failed' WHERE key=?", (key,))
            conn.commit()
        return False
    with sqlite_connection() as conn:
        conn.execute("UPDATE app_metadata SET value='sent' WHERE key=?", (key,))
        conn.commit()
    return True
