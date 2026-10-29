"""Durable, atomically claimed revisit queue; one public listing load per seller."""
import re
from collections import defaultdict
from datetime import datetime, timedelta

from src.services.item_lifecycle_service import ItemLifecycleService
from src.services.market_types import stamp, SellerListing, LoginExpired, AccessRestricted, LeaseLost


class SellerRevisitService:
    def __init__(self, repository):
        self.repo = repository
        self.lifecycle = ItemLifecycleService(repository)

    def claim_due(self, worker_id, at=None, limit=200):
        at = stamp(at)
        lease = stamp(datetime.fromisoformat(at) + timedelta(minutes=15))
        with self.repo.connection(True) as conn:
            expired = conn.execute('''SELECT j.*,i.listing_status FROM market_revisit_jobs j
                JOIN market_items i USING(item_id) WHERE j.status IN ('pending','running')
                AND NOT EXISTS(SELECT 1 FROM market_task_items ti JOIN market_tasks t ON t.id=ti.task_id
                    WHERE ti.item_id=j.item_id AND t.deleted_at IS NULL AND ti.tracking_until>=?)
                AND (j.locked_until IS NULL OR j.locked_until<?)''', (at, at)).fetchall()
            for row in expired:
                conn.execute("UPDATE market_revisit_jobs SET status='expired',worker_id=NULL,locked_until=NULL WHERE item_id=?", (row['item_id'],))
                conn.execute('UPDATE market_items SET next_review_at=NULL WHERE item_id=?', (row['item_id'],))
                if row['failures'] and row['listing_status'] not in {'sold', 'stale_unconfirmed'}:
                    self.lifecycle._event(conn, row['item_id'], worker_id, row['listing_status'], 'stale_unconfirmed', at, 'policy', 'tracking_expired_unconfirmed')
                    conn.execute("UPDATE market_items SET listing_status='stale_unconfirmed' WHERE item_id=?", (row['item_id'],))
            rows = conn.execute('''SELECT j.*,i.seller_id FROM market_revisit_jobs j JOIN market_items i USING(item_id)
                WHERE j.next_run_at<=? AND (j.status='pending' OR (j.status='running' AND j.locked_until<?))
                AND EXISTS(SELECT 1 FROM market_task_items ti JOIN market_tasks t ON t.id=ti.task_id
                    WHERE ti.item_id=j.item_id AND t.enabled=1 AND t.deleted_at IS NULL AND ti.tracking_until>=?)
                ORDER BY j.next_run_at,j.item_id LIMIT ?''', (at, at, at, limit)).fetchall()
            for row in rows:
                conn.execute("UPDATE market_revisit_jobs SET status='running',worker_id=?,locked_until=?,attempts=attempts+1 WHERE item_id=?", (worker_id, lease, row['item_id']))
            return [dict(row) for row in rows]

    async def run_due(self, adapter, run_id, at=None, limit=200, claimed=None, require_lease=False):
        fixed_at = at is not None
        at = stamp(at)
        rows = claimed if claimed is not None else self.claim_due(run_id, at, limit)
        groups = defaultdict(list)
        for row in rows:
            seller = row['seller_id']
            if not seller:
                try:
                    candidate = str(await adapter.detail_seller(row['item_id']) or '')
                    seller = candidate if re.fullmatch(r'\d{1,40}', candidate) else None
                    if seller:
                        with self.repo.connection(True) as conn:
                            conn.execute('''UPDATE market_items SET seller_id=?,seller_url=? WHERE item_id=? AND seller_id IS NULL''',
                                         (seller, 'https://www.goofish.com/personal?userId=' + seller, row['item_id']))
                except (LoginExpired, AccessRestricted):
                    raise
                except Exception:
                    seller = None
            groups[seller].append(row)
        counts = {'reviewed': 0, 'sold': 0}
        for seller, targets in groups.items():
            with self.repo.connection() as conn:
                if not conn.execute('SELECT 1 FROM market_tasks WHERE enabled=1 AND deleted_at IS NULL LIMIT 1').fetchone():
                    break
            if seller:
                try:
                    listing = await adapter.seller_items(seller)
                except (LoginExpired, AccessRestricted):
                    raise
                except Exception:
                    listing = SellerListing(error='seller_request_failed')
            else:
                listing = SellerListing(error='seller_id_pending')
            lookup = {str(i['item_id']): i for i in listing.items}
            observed_at = at if fixed_at else stamp()
            for row in targets:
                # User pause/delete takes effect between sellers/items, not merely at startup.
                with self.repo.connection() as conn:
                    enabled = conn.execute('''SELECT 1 FROM market_task_items ti JOIN market_tasks t ON t.id=ti.task_id
                        WHERE ti.item_id=? AND t.enabled=1 AND t.deleted_at IS NULL AND ti.tracking_until>=?''', (row['item_id'], observed_at)).fetchone()
                    owner = conn.execute('SELECT worker_id FROM market_revisit_jobs WHERE item_id=?', (row['item_id'],)).fetchone()
                if not owner or owner[0] != run_id:
                    continue
                if not enabled:
                    with self.repo.connection(True) as conn:
                        conn.execute("UPDATE market_revisit_jobs SET status='pending',worker_id=NULL,locked_until=NULL WHERE item_id=? AND worker_id=?", (row['item_id'], run_id))
                    continue
                found = lookup.get(row['item_id'])
                sold = self.lifecycle.observe(row['item_id'], run_id, at=observed_at,
                    price=found.get('price') if found else None, raw_status=found.get('raw_status') if found else None,
                    request_ok=listing.request_ok, page_complete=listing.page_complete,
                    list_complete=listing.list_complete, source=listing.source,
                    evidence='explicit_item_status' if found else (listing.error or 'item_missing'),
                    revisit_time=adapter.task.get('revisit_time', '09:30'), worker_id=run_id, require_lease=require_lease)
                counts['reviewed'] += 1
                counts['sold'] += int(sold)
                with self.repo.connection(True) as conn:
                    conn.execute('UPDATE market_runs SET reviewed=reviewed+1,sold=sold+? WHERE run_id=?', (int(sold), run_id))
        return counts
