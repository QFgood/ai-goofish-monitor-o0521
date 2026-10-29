"""Independent market task configuration; original AI/keyword tasks are untouched."""
from src.services.market_types import stamp, daily_at


class MarketTrackingService:
    def __init__(self, repository):
        self.repo = repository

    def create_task(self, data, at=None):
        at = stamp(at)
        values = dict(task_name=data['task_name'], keyword=data['keyword'], enabled=int(data.get('enabled', True)),
                      tracking_days=data.get('tracking_days', 30), max_pages=data.get('max_pages', 1),
                      account_state_file=data.get('account_state_file'), discovery_time=data.get('discovery_time', '08:30'),
                      revisit_time=data.get('revisit_time', '09:30'), notifications=int(data.get('notifications', True)))
        with self.repo.connection(True) as conn:
            cursor = conn.execute('''INSERT INTO market_tasks
                (task_name,keyword,enabled,tracking_days,max_pages,account_state_file,discovery_time,revisit_time,notifications,next_discovery_at,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (*values.values(), daily_at(at, values['discovery_time']), at))
            return dict(conn.execute('SELECT * FROM market_tasks WHERE id=?', (cursor.lastrowid,)).fetchone())

    def tasks(self):
        with self.repo.connection() as conn:
            tasks = [dict(r) for r in conn.execute('SELECT * FROM market_tasks WHERE deleted_at IS NULL ORDER BY id DESC')]
            for task in tasks:
                row = conn.execute('SELECT * FROM market_runs WHERE task_id=? ORDER BY started_at DESC,rowid DESC LIMIT 1', (task['id'],)).fetchone()
                task['last_run'] = dict(row) if row else None
            return tasks

    def update_task(self, task_id, data):
        allowed = {'enabled', 'notifications'}
        data = {k: int(v) for k, v in data.items() if k in allowed}
        with self.repo.connection(True) as conn:
            old = conn.execute('SELECT id FROM market_tasks WHERE id=? AND deleted_at IS NULL', (task_id,)).fetchone()
            if not old:
                raise ValueError('market_task_not_found')
            if data:
                conn.execute('UPDATE market_tasks SET ' + ','.join(f'{k}=?' for k in data) + ' WHERE id=?', (*data.values(), task_id))
            if data.get('enabled') == 1:
                conn.execute('UPDATE market_tasks SET failures=0,last_error=NULL WHERE id=?', (task_id,))

    def pause_all(self):
        with self.repo.connection(True) as conn:
            conn.execute('UPDATE market_tasks SET enabled=0 WHERE deleted_at IS NULL')

    def delete_task(self, task_id):
        with self.repo.connection(True) as conn:
            if not conn.execute('UPDATE market_tasks SET enabled=0,deleted_at=? WHERE id=? AND deleted_at IS NULL', (stamp(), task_id)).rowcount:
                raise ValueError('market_task_not_found')
        # Intentionally keep task memberships, observations, prices and sold archives.
