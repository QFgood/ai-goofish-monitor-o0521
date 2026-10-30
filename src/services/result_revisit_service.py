"""Bounded, durable revisits of registered result IDs, independent of AI/search."""
import asyncio
import copy
import json
import re
from datetime import datetime, timedelta
from uuid import uuid4

from src.core.cron_utils import build_cron_trigger
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.infrastructure.persistence.storage_names import build_result_filename
from src.services.market_types import stamp, cents, platform_status, LoginExpired, AccessRestricted


def next_review(task, at, *, tomorrow=False):
    after = datetime.fromisoformat(stamp(at))
    if tomorrow:
        after = (after + timedelta(days=1)).replace(hour=0, minute=0, second=0)
    else:
        after += timedelta(seconds=1)
    return stamp(build_cron_trigger(task['revisit_cron'], timezone='Asia/Shanghai').get_next_fire_time(None, after))


def register_result(conn, task, record, at=None):
    """No network. A seller nickname is display-only, never an identity."""
    product = record.get('商品信息') or {}
    item_id = str(product.get('商品ID') or '')
    if not re.fullmatch(r'\d{1,40}', item_id) or not task['revisit_enabled']:
        return
    at = stamp(at)
    try:
        first = stamp(record.get('爬取时间') or at)
    except (ValueError, TypeError):
        first = at
    seller = record.get('卖家信息') or {}
    seller_id = str(seller.get('卖家ID') or product.get('卖家ID') or seller.get('seller_id') or '')
    seller_id = seller_id if re.fullmatch(r'\d{1,40}', seller_id) else None
    conn.execute('''INSERT INTO result_listing_items(item_id,seller_id,seller_nickname,item_url,
        first_seen_at,last_seen_at,last_listing_price) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(item_id) DO UPDATE SET seller_id=coalesce(excluded.seller_id,result_listing_items.seller_id),
        seller_nickname=CASE WHEN excluded.last_seen_at>=coalesce(result_listing_items.last_review_at,'')
            THEN coalesce(excluded.seller_nickname,result_listing_items.seller_nickname) ELSE result_listing_items.seller_nickname END,
        last_listing_price=CASE WHEN result_listing_items.listing_status!='sold'
            AND excluded.last_seen_at>=coalesce(result_listing_items.last_review_at,'')
            THEN coalesce(excluded.last_listing_price,result_listing_items.last_listing_price) ELSE result_listing_items.last_listing_price END,
        first_seen_at=min(result_listing_items.first_seen_at,excluded.first_seen_at),
        last_seen_at=max(result_listing_items.last_seen_at,excluded.last_seen_at)''',
        (item_id,seller_id,seller.get('卖家昵称') or product.get('卖家昵称'),
         f'https://www.goofish.com/item?id={item_id}',first,first,cents(product.get('当前售价'))))
    conn.execute('''INSERT INTO result_listing_memberships(task_id,item_id,filename,next_review_at,tracking_until)
        VALUES(?,?,?,?,?) ON CONFLICT(task_id,item_id) DO UPDATE SET archived=0''',
        (task['id'], item_id, build_result_filename(task['keyword']), next_review(task,at,tomorrow=True),
         stamp(datetime.fromisoformat(at)+timedelta(days=task['revisit_days']))))


def register_saved_result(conn, record, keyword):
    task_id = record.get('任务ID')
    if task_id is not None:
        rows = conn.execute('SELECT * FROM tasks WHERE id=? AND revisit_enabled=1', (task_id,)).fetchall()
    else:
        rows = conn.execute('SELECT * FROM tasks WHERE task_name=? AND keyword=? AND revisit_enabled=1',
                            (record.get('任务名称',''), keyword)).fetchall()
    for row in rows:
        register_result(conn, dict(row), record)


def tracked_records(records, listing_state='active', include_hidden=False):
    """Attach lifecycle facts and select by platform status, never result_items.status."""
    with sqlite_connection() as conn:
        rows = {r['item_id']:dict(r) for r in conn.execute('SELECT * FROM result_listing_items')}
        due = {r['item_id']:r['due'] for r in conn.execute('''SELECT item_id,min(next_review_at) AS due
            FROM result_listing_memberships WHERE archived=0 GROUP BY item_id''')}
    selected=[]
    for original in records:
        if listing_state=='sold' and not include_hidden and original.get('_hidden_reason') in {'manual','rule'}:
            continue
        record=copy.deepcopy(original)
        item_id=str((record.get('商品信息') or {}).get('商品ID') or '')
        state=rows.get(item_id)
        sold=bool(state and state['listing_status']=='sold')
        if (listing_state=='sold') != sold:
            continue
        if state:
            state.pop('worker_id',None); state.pop('locked_until',None)
            state['next_review_at']=due.get(item_id)
            record['listing_tracking']=state
            if listing_state=='sold':
                record['商品信息']['当前售价']=state['sold_display_price']/100 if state['sold_display_price'] is not None else None
            elif state['last_review_at'] and state['last_listing_price'] is not None:
                record['商品信息']['当前售价']=state['last_listing_price']/100
        selected.append(record)
    return selected


class ResultRevisitService:
    def __init__(self, adapter_factory=None):
        self.adapter_factory=adapter_factory

    def sync_existing(self, task):
        """Enabling revisits also registers existing saved results, without page visits."""
        with sqlite_connection() as conn:
            rows=conn.execute('SELECT raw_json FROM result_items WHERE result_filename=?',
                              (build_result_filename(task['keyword']),)).fetchall()
            for row in rows:
                register_result(conn,task,json.loads(row['raw_json']))
            conn.commit()

    def observe(self, item_id, run_id, observation, task, at=None):
        at=stamp(at)
        ok=observation.get('request_ok') is True and observation.get('page_complete') is True
        status=platform_status(observation.get('raw_status')) if ok else 'unknown'
        price=cents(observation.get('price')) if ok else None
        evidence=observation.get('evidence','unknown')
        with sqlite_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old=conn.execute('SELECT * FROM result_listing_items WHERE item_id=?',(item_id,)).fetchone()
            if not old or old['worker_id']!=run_id:
                return False
            added=conn.execute('''INSERT OR IGNORE INTO result_listing_observations(item_id,run_id,observed_at,
                raw_status,parsed_status,price,source,request_ok,page_complete,evidence) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (item_id,run_id,at,json.dumps(observation.get('raw_status')),status,price,
                 observation.get('source','item_detail'),int(observation.get('request_ok') is True),
                 int(observation.get('page_complete') is True),evidence)).rowcount
            if not added:
                return False
            new=old['listing_status'] if status=='unknown' else status
            if new!=old['listing_status']:
                conn.execute('''INSERT OR IGNORE INTO result_listing_events(item_id,run_id,old_status,new_status,observed_at,evidence)
                    VALUES(?,?,?,?,?,?)''',(item_id,run_id,old['listing_status'],new,at,evidence))
            seller=str(observation.get('seller_id') or '')
            seller=seller if re.fullmatch(r'\d{1,40}',seller) else None
            conn.execute('''UPDATE result_listing_items SET listing_status=?,last_review_at=?,last_result=?,
                last_listing_price=CASE WHEN ?='active' THEN coalesce(?,last_listing_price) ELSE last_listing_price END,
                sold_display_price=CASE WHEN ?='sold' THEN coalesce(?,sold_display_price) ELSE sold_display_price END,
                first_seen_sold_at=CASE WHEN ?='sold' THEN coalesce(first_seen_sold_at,?) ELSE first_seen_sold_at END,
                seller_id=coalesce(?,seller_id),seller_nickname=coalesce(?,seller_nickname),
                worker_id=NULL,locked_until=NULL WHERE item_id=?''',
                (new,at,evidence,status,price,status,price,status,at,seller,
                 observation.get('seller_nickname') if ok else None,item_id))
            # A shared item is revisited once, updating every subscribing task's due date.
            for membership in conn.execute('''SELECT m.task_id,t.revisit_cron FROM result_listing_memberships m
                JOIN tasks t ON t.id=m.task_id WHERE m.item_id=? AND m.archived=0''',(item_id,)).fetchall():
                due=None if new=='sold' else next_review(dict(membership),at)
                conn.execute('UPDATE result_listing_memberships SET next_review_at=? WHERE task_id=? AND item_id=?',
                             (due,membership['task_id'],item_id))
            conn.commit()
            return status=='sold' and old['first_seen_sold_at'] is None

    async def run(self, task_id):
        bootstrap_sqlite_storage()
        run_id=str(uuid4()); at=stamp()
        with sqlite_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT * FROM tasks WHERE id=? AND enabled=1 AND revisit_enabled=1',(task_id,)).fetchone()
            if not row:
                return {'status':'disabled','reviewed':0,'sold':0}
            if conn.execute("SELECT 1 FROM result_revisit_runs WHERE task_id=? AND status='running' AND lease_until>?",(task_id,at)).fetchone():
                return {'status':'busy','reviewed':0,'sold':0}
            task=dict(row)
            conn.execute("UPDATE result_revisit_runs SET status='interrupted' WHERE task_id=? AND status='running'",(task_id,))
            conn.execute('INSERT INTO result_revisit_runs(run_id,task_id,started_at,lease_until,status) VALUES(?,?,?,?,?)',
                         (run_id,task_id,at,stamp(datetime.fromisoformat(at)+timedelta(minutes=2)),'running'))
            conn.commit()
        counts={'run_id':run_id,'status':'completed','reviewed':0,'sold':0}
        try:
            self.sync_existing(task)
            with sqlite_connection() as conn:
                targets=conn.execute('''SELECT m.item_id FROM result_listing_memberships m JOIN result_listing_items i USING(item_id)
                    WHERE m.task_id=? AND m.archived=0 AND m.next_review_at<=? AND m.tracking_until>=?
                    AND i.listing_status!='sold' AND (i.locked_until IS NULL OR i.locked_until<?)
                    ORDER BY m.next_review_at,m.item_id LIMIT ?''',(task_id,at,at,at,task['revisit_limit'])).fetchall()
            if targets:
                if self.adapter_factory is None:
                    from src.services.market_platform import PlaywrightMarketAdapter
                    factory=lambda t:PlaywrightMarketAdapter(t,None)
                else:
                    factory=self.adapter_factory
                async with factory(task) as adapter:
                    for target in targets:
                        current=stamp(); lease=stamp(datetime.fromisoformat(current)+timedelta(minutes=2))
                        with sqlite_connection() as conn:
                            conn.execute('BEGIN IMMEDIATE')
                            enabled=conn.execute('SELECT enabled AND revisit_enabled FROM tasks WHERE id=?',(task_id,)).fetchone()
                            if not enabled or not enabled[0]:
                                break
                            owner=conn.execute("UPDATE result_revisit_runs SET lease_until=? WHERE run_id=? AND status='running'",(lease,run_id)).rowcount
                            if not owner:
                                break
                            claimed=conn.execute('''UPDATE result_listing_items SET worker_id=?,locked_until=? WHERE item_id=?
                                AND (locked_until IS NULL OR locked_until<?)''',(run_id,lease,target['item_id'],current)).rowcount
                            conn.commit()
                        if not claimed:
                            continue
                        try:
                            observation=await asyncio.wait_for(adapter.item_listing(target['item_id']),timeout=60)
                        except (LoginExpired,AccessRestricted):
                            raise
                        except Exception:
                            observation={'request_ok':False,'page_complete':False,'evidence':'detail_request_failed'}
                        newly_sold=self.observe(target['item_id'],run_id,observation,task)
                        counts['reviewed']+=1; counts['sold']+=int(newly_sold)
                        with sqlite_connection() as conn:
                            conn.execute('UPDATE result_revisit_runs SET reviewed=?,sold=? WHERE run_id=?',
                                         (counts['reviewed'],counts['sold'],run_id));conn.commit()
        except (LoginExpired,AccessRestricted):
            counts['status']='access_paused'
            with sqlite_connection() as conn:
                conn.execute('UPDATE tasks SET revisit_enabled=0 WHERE id=?',(task_id,))
                conn.execute("UPDATE result_listing_items SET last_result='access_paused' WHERE worker_id=?",(run_id,))
                conn.commit()
        except asyncio.CancelledError:
            counts['status']='interrupted'
            raise
        except Exception:
            counts['status']='failed'
        finally:
            with sqlite_connection() as conn:
                conn.execute('UPDATE result_listing_items SET worker_id=NULL,locked_until=NULL WHERE worker_id=?',(run_id,))
                conn.execute('UPDATE result_revisit_runs SET status=?,finished_at=?,reviewed=?,sold=? WHERE run_id=?',
                             (counts['status'],stamp(),counts['reviewed'],counts['sold'],run_id));conn.commit()
        return counts
