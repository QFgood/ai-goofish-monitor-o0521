"""Own manual background workers; SQLite leases also exclude scheduled workers."""
import asyncio

from src.services.result_revisit_service import ResultRevisitService


class ResultRevisitManager:
    def __init__(self, service=None):
        self.service=service or ResultRevisitService()
        self.workers={}

    def start(self, task_id):
        prepared=self.service.prepare_run(task_id,manual=True)
        if 'task' not in prepared:
            return prepared
        worker=asyncio.create_task(self._execute(prepared))
        self.workers[worker]=prepared
        worker.add_done_callback(self._finished)
        return {'run_id':prepared['run_id'],'status':'running','reviewed':0,'sold':0}

    def _finished(self, worker):
        self.workers.pop(worker,None)
        if not worker.cancelled():
            worker.exception()  # Consume worker errors; SQLite lease recovery remains available.

    async def _execute(self, prepared):
        result=await self.service.execute_run(prepared)
        from src.api.routes.websocket import broadcast_message
        try:
            await broadcast_message('results_updated',{'task_id':prepared['task']['id']})
            await broadcast_message('tasks_updated',{'task_id':prepared['task']['id']})
        except Exception:
            pass  # Polling reads the durable result if a websocket disconnected.
        return result

    async def stop(self):
        pending=dict(self.workers)
        for worker in pending:
            worker.cancel()
        if pending:
            await asyncio.gather(*pending,return_exceptions=True)
            from src.infrastructure.persistence.sqlite_connection import sqlite_connection
            from src.services.market_types import stamp
            with sqlite_connection(self.service.db_path) as conn:
                for prepared in pending.values():
                    run_id=prepared['run_id']
                    conn.execute("UPDATE result_revisit_runs SET status='interrupted',finished_at=? WHERE run_id=? AND status='running'",(stamp(),run_id))
                    conn.execute('UPDATE result_listing_items SET worker_id=NULL,locked_until=NULL WHERE worker_id=?',(run_id,))
                conn.commit()
