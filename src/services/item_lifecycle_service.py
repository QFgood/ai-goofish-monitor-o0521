"""Atomic lifecycle writes; result_items.status is intentionally never consulted."""
import json
import re
from datetime import datetime, timedelta

from src.services.market_types import stamp, now, daily_at, cents, platform_status, LeaseLost
from src.services.product_variant_service import classify_product


class ItemLifecycleService:
    def __init__(self, repository):
        self.repo = repository

    def discover(self, task_id, item, run_id, at=None, worker_id=None):
        at = stamp(at)
        item_id = str(item.get('item_id') or item.get('商品ID') or '')
        if not re.fullmatch(r'\d{1,40}', item_id):
            return False
        title = str(item.get('title') or item.get('商品标题') or '')[:2000]
        seller = str(item.get('seller_id') or '')
        seller = seller if re.fullmatch(r'\d{1,40}', seller) else None
        price = cents(item.get('price', item.get('当前售价')))
        with self.repo.connection(True) as conn:
            if worker_id:
                self._fence(conn, worker_id)
            task = conn.execute('SELECT * FROM market_tasks WHERE id=? AND deleted_at IS NULL AND enabled=1', (task_id,)).fetchone()
            if not task:
                return False
            existing = conn.execute('SELECT * FROM market_items WHERE item_id=?', (item_id,)).fetchone()
            fresh = existing is None
            classification = classify_product(title)
            if fresh:
                conn.execute('''INSERT INTO market_items(item_id,title,item_url,first_seen_at,last_seen_at,
                    first_listed_price,product_category,product_variant,attributes_json,excluded,exclusion_reason)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                    (item_id, title, f'https://www.goofish.com/item?id={item_id}', at, at, price if platform_status(item.get('raw_status', 0)) == 'active' else None,
                     *classification.values()))
            conn.execute('''INSERT OR IGNORE INTO market_task_items(task_id,item_id,first_seen_at,tracking_until)
                VALUES(?,?,?,?)''', (task_id, item_id, at, stamp(datetime.fromisoformat(at) + timedelta(days=task['tracking_days']))))
            duplicate = conn.execute('SELECT 1 FROM market_item_observations WHERE item_id=? AND run_id=? AND source=?',
                                     (item_id, run_id, 'search')).fetchone()
            if not duplicate:
                conn.execute('''UPDATE market_items SET title=?,seller_id=coalesce(seller_id,?),
                    seller_nickname=coalesce(?,seller_nickname),region=coalesce(?,region),publish_time=coalesce(?,publish_time)
                    WHERE item_id=?''', (title, seller, item.get('seller_nickname', item.get('卖家昵称')),
                      item.get('region', item.get('发货地区')), item.get('publish_time', item.get('发布时间')), item_id))
                if not fresh and not existing['classification_manual']:
                    conn.execute('''UPDATE market_items SET product_category=?,product_variant=?,attributes_json=?,
                        excluded=?,exclusion_reason=? WHERE item_id=?''', (*classification.values(), item_id))
                conn.execute("UPDATE market_items SET seller_url='https://www.goofish.com/personal?userId='||seller_id WHERE item_id=? AND seller_id IS NOT NULL", (item_id,))
                self._observe(conn, item_id, run_id, at, price, item.get('raw_status', 0), 'search',
                              True, True, True, 'search_listing', task['revisit_time'])
            return fresh

    def observe(self, item_id, run_id, *, at=None, price=None, raw_status=None,
                source='seller_item_list', request_ok=True, page_complete=True, list_complete=True,
                evidence='item_missing', revisit_time='09:30', worker_id=None, require_lease=False):
        with self.repo.connection(True) as conn:
            if require_lease:
                self._fence(conn, worker_id)
            if worker_id:
                job = conn.execute('SELECT worker_id FROM market_revisit_jobs WHERE item_id=?', (str(item_id),)).fetchone()
                if not job or job[0] != worker_id:
                    raise LeaseLost('revisit_lease_lost')
            return self._observe(conn, str(item_id), run_id, stamp(at), cents(price), raw_status, source,
                                 request_ok, page_complete, list_complete, evidence, revisit_time)

    def _observe(self, conn, item_id, run_id, at, price, raw, source, ok, page, complete, evidence, clock):
        item = conn.execute('SELECT * FROM market_items WHERE item_id=?', (item_id,)).fetchone()
        if item is None:
            raise ValueError('market_item_not_found')
        parsed = platform_status(raw) if ok and page else 'unknown'
        # Offline requires an explicit adapter assertion, never absence or an unknown numeric code.
        if ok and page and evidence == 'explicit_offline':
            parsed = 'offline'
        inserted = conn.execute('''INSERT OR IGNORE INTO market_item_observations
            (item_id,run_id,observed_at,price,raw_status,parsed_status,source,request_ok,page_complete,list_complete,evidence)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (item_id, run_id, at, price if ok and page else None,
                json.dumps(raw), parsed, source, int(ok), int(page), int(complete), evidence)).rowcount
        if not inserted:
            return False
        # Out-of-order evidence is retained without regressing the current projection.
        last = conn.execute('SELECT max(observed_at) FROM market_item_observations WHERE item_id=? AND id != last_insert_rowid()', (item_id,)).fetchone()[0]
        if last and at < last:
            return False
        new_status = item['listing_status'] if parsed == 'unknown' else parsed
        if new_status != item['listing_status']:
            self._event(conn, item_id, run_id, item['listing_status'], new_status, at, source, evidence)
        updates = {'listing_status': new_status}
        if ok and page and (parsed != 'unknown' or evidence in {'explicit_item_status', 'search_listing'}):
            updates['last_seen_at'] = at
            if price is not None:
                updates['last_observed_price'] = price
        if parsed == 'active':
            updates['last_confirmed_active_at'] = at
            if price is not None:
                updates['last_listing_price'] = price
                if item['first_listed_price'] is None:
                    updates['first_listed_price'] = price
        if parsed == 'sold' and not item['first_seen_sold_at']:
            updates.update(first_seen_sold_at=at, sold_display_price=price,
                           sold_reference_price=item['last_listing_price'],
                           price_source='last_listing_price' if item['last_listing_price'] is not None else None)
        elif parsed == 'sold' and price is not None:
            updates['sold_display_price'] = price
        if source != 'search':
            updates['last_review_at'] = at
        policy_clock = conn.execute('''SELECT min(t.revisit_time) FROM market_task_items ti JOIN market_tasks t ON t.id=ti.task_id
            WHERE ti.item_id=? AND t.enabled=1 AND t.deleted_at IS NULL''', (item_id,)).fetchone()[0]
        next_at = daily_at(at, policy_clock or clock, tomorrow=True)
        job = conn.execute('SELECT * FROM market_revisit_jobs WHERE item_id=?', (item_id,)).fetchone()
        failures = (job['failures'] if job else 0) + 1 if parsed == 'unknown' else 0
        until = conn.execute('''SELECT max(ti.tracking_until) FROM market_task_items ti JOIN market_tasks t ON t.id=ti.task_id
            WHERE ti.item_id=? AND t.deleted_at IS NULL AND t.enabled=1''', (item_id,)).fetchone()[0]
        terminal = new_status == 'sold'
        expired = bool(until and next_at > until)
        if failures >= 7 or (expired and parsed == 'unknown'):
            if new_status != 'stale_unconfirmed':
                self._event(conn, item_id, run_id, item['listing_status'], 'stale_unconfirmed', at, source, 'unconfirmed_limit')
            updates['listing_status'] = 'stale_unconfirmed'
            terminal = True
        due = None if terminal or expired else next_at
        # A daily rediscovery must never postpone an already due seller review.
        if source == 'search' and job and job['status'] == 'pending' and due:
            due = min(job['next_run_at'], due)
        updates['next_review_at'] = due
        conn.execute('UPDATE market_items SET ' + ','.join(f'{key}=?' for key in updates) + ' WHERE item_id=?', (*updates.values(), item_id))
        conn.execute('''INSERT INTO market_revisit_jobs(item_id,next_run_at,status,failures,last_error,last_result)
            VALUES(?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET next_run_at=excluded.next_run_at,
            status=excluded.status,failures=excluded.failures,last_error=excluded.last_error,
            last_result=excluded.last_result,worker_id=NULL,locked_until=NULL''',
            (item_id, due or at, 'done' if terminal else ('expired' if expired else 'pending'), failures,
             evidence if parsed == 'unknown' else None, parsed))
        return parsed == 'sold' and item['first_seen_sold_at'] is None

    @staticmethod
    def _fence(conn, worker_id):
        lease = conn.execute("SELECT * FROM market_leases WHERE name='worker'").fetchone()
        if not lease or lease['owner'] != worker_id or lease['expires_at'] <= stamp():
            raise LeaseLost('worker_lease_lost')

    @staticmethod
    def _event(conn, item_id, run_id, old, new, at, source, evidence):
        conn.execute('''INSERT OR IGNORE INTO market_status_events
            (item_id,run_id,old_status,new_status,observed_at,source,evidence) VALUES(?,?,?,?,?,?,?)''',
            (item_id, run_id, old, new, at, source, evidence))

    def correct_classification(self, item_id, changes, actor, at=None):
        with self.repo.connection(True) as conn:
            old = conn.execute('SELECT * FROM market_items WHERE item_id=?', (item_id,)).fetchone()
            if old is None:
                raise ValueError('market_item_not_found')
            keys = ['product_category', 'product_variant', 'excluded']
            before = {k: old[k] for k in keys}
            after = {**before, **{k: v for k, v in changes.items() if k in keys}}
            conn.execute('UPDATE market_items SET product_category=?,product_variant=?,excluded=?,classification_manual=1 WHERE item_id=?', (*after.values(), item_id))
            conn.execute('''INSERT INTO market_classification_events(item_id,changed_at,actor,before_json,after_json)
                VALUES(?,?,?,?,?)''', (item_id, stamp(at), actor, json.dumps(before), json.dumps(after)))
            return after
