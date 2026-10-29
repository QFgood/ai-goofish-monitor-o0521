"""APScheduler wakes persistent SQLite schedules. Leases survive process crashes."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import uuid

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from src.services.market_types import now, stamp, daily_at, LoginExpired, AccessRestricted, LeaseLost
from src.services.item_lifecycle_service import ItemLifecycleService
from src.services.seller_revisit_service import SellerRevisitService
from src.services.market_platform import PlaywrightMarketAdapter
from src.services.market_tracking_service import MarketTrackingService


class MarketBusy(Exception):
    pass


class MarketRunner:
    def __init__(self, repository, adapter_factory=PlaywrightMarketAdapter, notifier=None):
        self.repo = repository
        self.factory = adapter_factory
        self.notifier = notifier
        self.scheduler = AsyncIOScheduler(timezone='Asia/Shanghai')
        self.background = set()

    def start(self):
        self.scheduler.add_job(self.tick, 'interval', seconds=60, max_instances=1, coalesce=True,
                               next_run_time=now() + timedelta(seconds=10), id='market_tick')
        self.scheduler.start()

    async def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        for task in list(self.background):
            task.cancel()
        if self.background:
            await asyncio.gather(*self.background, return_exceptions=True)

    def dispatch(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.background.add(task)
        def completed(future):
            self.background.discard(future)
            if not future.cancelled():
                future.exception()  # consume safe background failures; durable run row records progress
        task.add_done_callback(completed)
        return task

    @asynccontextmanager
    async def lease(self, kind, task_id=None):
        run_id = str(uuid.uuid4())
        at = stamp()
        expires = stamp(now() + timedelta(minutes=15))
        with self.repo.connection(True) as conn:
            result = conn.execute('''INSERT INTO market_leases(name,owner,expires_at) VALUES('worker',?,?)
                ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at
                WHERE market_leases.expires_at<?''', (run_id, expires, at))
            if not result.rowcount:
                raise MarketBusy('market_worker_busy')
            conn.execute("UPDATE market_runs SET status='interrupted',finished_at=?,error='lease_expired' WHERE status='running'", (at,))
            conn.execute('INSERT INTO market_runs(run_id,kind,task_id,started_at) VALUES(?,?,?,?)', (run_id, kind, task_id, at))
        parent = asyncio.current_task()
        async def renew():
            while True:
                await asyncio.sleep(30)
                try:
                    with self.repo.connection(True) as conn:
                        if not conn.execute("UPDATE market_leases SET expires_at=? WHERE name='worker' AND owner=?", (stamp(now() + timedelta(minutes=15)), run_id)).rowcount:
                            parent.cancel()
                            return
                        conn.execute('UPDATE market_revisit_jobs SET locked_until=? WHERE worker_id=?', (stamp(now() + timedelta(minutes=15)), run_id))
                except Exception:
                    parent.cancel()  # never continue writing after losing lease renewal
                    return
        heartbeat = asyncio.create_task(renew())
        try:
            yield run_id
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            with self.repo.connection(True) as conn:
                conn.execute("DELETE FROM market_leases WHERE name='worker' AND owner=?", (run_id,))
                conn.execute("UPDATE market_revisit_jobs SET status='pending',worker_id=NULL,locked_until=NULL WHERE status='running' AND worker_id=?", (run_id,))
                conn.execute("UPDATE market_runs SET status='interrupted',finished_at=?,error='worker_interrupted' WHERE run_id=? AND status='running'", (stamp(), run_id))

    def finish(self, run_id, status='success', error=None):
        with self.repo.connection(True) as conn:
            conn.execute('UPDATE market_runs SET status=?,error=?,finished_at=? WHERE run_id=?', (status, error, stamp(), run_id))

    async def search(self, task_id):
        async with self.lease('discovery', task_id) as run_id:
            with self.repo.connection() as conn:
                row = conn.execute('SELECT * FROM market_tasks WHERE id=? AND enabled=1 AND deleted_at IS NULL', (task_id,)).fetchone()
            if not row:
                self.finish(run_id, 'paused')
                return run_id
            task = dict(row)
            try:
                lifecycle = ItemLifecycleService(self.repo)
                paused = False
                async with self.factory(task, self.repo) as adapter:
                    async for product in adapter.search():
                        with self.repo.connection() as conn:
                            active = conn.execute('SELECT enabled,deleted_at FROM market_tasks WHERE id=?', (task_id,)).fetchone()
                        if not active['enabled'] or active['deleted_at']:
                            paused = True
                            break
                        with self.repo.connection() as conn:
                            owner = conn.execute("SELECT owner FROM market_leases WHERE name='worker'").fetchone()
                        if not owner or owner[0] != run_id:
                            raise LeaseLost('lease_lost')
                        fresh = lifecycle.discover(task_id, product, run_id, worker_id=run_id)
                        with self.repo.connection(True) as conn:
                            conn.execute('UPDATE market_runs SET discovered=discovered+? WHERE run_id=?', (int(fresh), run_id))
                if paused:
                    self.finish(run_id, 'paused')
                    return run_id
                with self.repo.connection(True) as conn:
                    conn.execute('UPDATE market_tasks SET next_discovery_at=?,failures=0,last_error=NULL WHERE id=?', (daily_at(now(), task['discovery_time'], tomorrow=True), task_id))
                self.finish(run_id)
            except LeaseLost:
                self.finish(run_id, 'interrupted', 'lease_lost')
            except (LoginExpired, AccessRestricted) as exc:
                await self.pause_access(run_id, task_id, 'login_expired' if isinstance(exc, LoginExpired) else 'access_restricted')
            except Exception:
                with self.repo.connection(True) as conn:
                    conn.execute('''UPDATE market_tasks SET failures=failures+1,last_error='discovery_failed',
                        enabled=CASE WHEN failures>=2 THEN 0 ELSE enabled END,next_discovery_at=? WHERE id=?''', (stamp(now() + timedelta(minutes=15)), task_id))
                self.finish(run_id, 'failed', 'discovery_failed')
                with self.repo.connection() as conn:
                    paused = conn.execute('SELECT enabled FROM market_tasks WHERE id=?', (task_id,)).fetchone()[0] == 0
                if paused:
                    await self.send_notice(task_id, 'task_paused', '市场发现任务连续失败，已自动暂停；请检查账号与最近运行结果。')
            return run_id

    async def revisits(self):
        async with self.lease('revisit') as run_id:
            tasks = [t for t in MarketTrackingService(self.repo).tasks() if t['enabled']]
            if not tasks:
                self.finish(run_id, 'paused')
                return run_id
            # Public seller lists use one managed account for this batch; no account rotation on restriction.
            task = tasks[-1]
            try:
                queue = SellerRevisitService(self.repo)
                claimed = queue.claim_due(run_id)
                if claimed:
                    async with self.factory(task, self.repo) as adapter:
                        await queue.run_due(adapter, run_id, claimed=claimed, require_lease=True)
                self.finish(run_id)
                await self.daily_summaries()
            except LeaseLost:
                self.finish(run_id, 'interrupted', 'lease_lost')
            except (LoginExpired, AccessRestricted) as exc:
                await self.pause_access(run_id, task['id'], 'login_expired' if isinstance(exc, LoginExpired) else 'access_restricted')
            except Exception:
                with self.repo.connection() as conn:
                    unfinished = conn.execute("SELECT item_id FROM market_revisit_jobs WHERE status='running' AND worker_id=?", (run_id,)).fetchall()
                lifecycle = ItemLifecycleService(self.repo)
                for job in unfinished:
                    lifecycle.observe(job['item_id'], run_id, request_ok=False, page_complete=False,
                                      list_complete=False, evidence='revisit_start_failed', worker_id=run_id, require_lease=True)
                self.finish(run_id, 'failed', 'revisit_failed')
            return run_id

    async def pause_access(self, run_id, task_id, reason):
        MarketTrackingService(self.repo).pause_all()
        self.finish(run_id, 'paused', reason)
        await self.send_notice(task_id, 'access_pause', '市场监控已暂停：登录态失效或平台限制，请人工检查后恢复。')

    async def send_notice(self, task_id, kind, reason):
        if self.notifier is None:
            return
        day = stamp()[:10]
        with self.repo.connection(True) as conn:
            task = conn.execute('SELECT * FROM market_tasks WHERE id=?', (task_id,)).fetchone()
            if not task or not task['notifications'] or task['deleted_at']:
                return
            conn.execute('INSERT OR IGNORE INTO market_notices(task_id,day,kind) VALUES(?,?,?)', (task_id, day, kind))
            notice = conn.execute('SELECT * FROM market_notices WHERE task_id=? AND day=? AND kind=?', (task_id, day, kind)).fetchone()
            if notice['state'] in {'sent', 'sending'} or notice['attempts'] >= 3:
                return
            conn.execute("UPDATE market_notices SET state='sending',attempts=attempts+1 WHERE task_id=? AND day=? AND kind=?", (task_id, day, kind))
        state = 'failed'
        try:
            result = await self.notifier({'商品标题': '闲鱼哨兵 · 每日行情报告', '当前售价': 'N/A', '商品链接': 'https://www.goofish.com/'}, reason)
            state = 'sent' if result and all(r.get('success') for r in result.values()) else 'failed'
        except Exception:
            pass
        with self.repo.connection(True) as conn:
            conn.execute('UPDATE market_notices SET state=? WHERE task_id=? AND day=? AND kind=?', (state, task_id, day, kind))

    async def daily_summaries(self):
        from src.services.market_statistics_service import MarketStatisticsService
        stats = MarketStatisticsService(self.repo)
        for task in MarketTrackingService(self.repo).tasks():
            if not task['enabled']:
                continue
            summary = stats.summary(task_id=task['id'])
            with self.repo.connection() as conn:
                discovered = conn.execute('SELECT count(*) FROM market_task_items WHERE task_id=? AND substr(first_seen_at,1,10)=?', (task['id'], stamp()[:10])).fetchone()[0]
            with self.repo.connection() as conn:
                failures = conn.execute('''SELECT count(*) FROM market_revisit_jobs j JOIN market_task_items ti USING(item_id)
                    WHERE ti.task_id=? AND j.failures>=3''', (task['id'],)).fetchone()[0]
            median = summary['sold_7d']['median_price']
            await self.send_notice(task['id'], 'daily', f"任务：{task['task_name']}\n今日新发现：{discovered} 件\n今日首次确认已售：{summary['sold_today']} 件\n连续无法确认（至少3次）：{failures} 件\n近7日已售参考价中位数：{('¥' + str(median / 100)) if median is not None else '暂无有效样本'}\n参考价为最后挂牌价，不等于真实成交价；确认日期不是实际成交日。")

    async def tick(self):
        current = asyncio.current_task()
        self.background.add(current)
        try:
            for task in MarketTrackingService(self.repo).tasks():
                if task['enabled'] and task['next_discovery_at'] <= stamp():
                    try:
                        await self.search(task['id'])
                    except MarketBusy:
                        return
            with self.repo.connection() as conn:
                due = conn.execute("""SELECT 1 FROM market_revisit_jobs j WHERE next_run_at<=?
                    AND (status='pending' OR (status='running' AND locked_until<?))
                    AND EXISTS(SELECT 1 FROM market_task_items ti JOIN market_tasks t ON t.id=ti.task_id
                        WHERE ti.item_id=j.item_id AND t.enabled=1 AND t.deleted_at IS NULL) LIMIT 1""", (stamp(), stamp())).fetchone()
            if due:
                try:
                    await self.revisits()
                except MarketBusy:
                    return
            if stamp()[11:16] >= '09:35':
                await self.daily_summaries()
        finally:
            self.background.discard(current)
