"""Additive bounded revisits for ordinary search tasks; no legacy table rewrites."""
TASK_COLUMNS = {
    'revisit_enabled': 'INTEGER NOT NULL DEFAULT 0',
    'revisit_cron': "TEXT NOT NULL DEFAULT '30 9 * * *'",
    'revisit_limit': 'INTEGER NOT NULL DEFAULT 20',
    'revisit_days': 'INTEGER NOT NULL DEFAULT 30',
}


def migrate_result_revisits(conn):
    columns = {r[1] for r in conn.execute('PRAGMA table_info(tasks)')}
    for name, definition in TASK_COLUMNS.items():
        if name not in columns:
            conn.execute(f'ALTER TABLE tasks ADD COLUMN {name} {definition}')
    for sql in (
        '''CREATE TABLE IF NOT EXISTS result_listing_items (
            item_id TEXT PRIMARY KEY, seller_id TEXT, seller_nickname TEXT, item_url TEXT NOT NULL,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, listing_status TEXT NOT NULL DEFAULT 'active',
            last_listing_price INTEGER, sold_display_price INTEGER, first_seen_sold_at TEXT,
            last_review_at TEXT, last_result TEXT, worker_id TEXT, locked_until TEXT)''',
        '''CREATE TABLE IF NOT EXISTS result_listing_memberships (
            task_id INTEGER NOT NULL, item_id TEXT NOT NULL REFERENCES result_listing_items(item_id),
            filename TEXT NOT NULL, next_review_at TEXT, tracking_until TEXT NOT NULL, review_not_before TEXT,
            archived INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(task_id,item_id))''',
        '''CREATE TABLE IF NOT EXISTS result_listing_observations (
            id INTEGER PRIMARY KEY, item_id TEXT NOT NULL REFERENCES result_listing_items(item_id),
            run_id TEXT NOT NULL, observed_at TEXT NOT NULL, raw_status TEXT, parsed_status TEXT NOT NULL,
            price INTEGER, source TEXT NOT NULL, request_ok INTEGER NOT NULL, page_complete INTEGER NOT NULL,
            evidence TEXT NOT NULL, UNIQUE(item_id,run_id))''',
        '''CREATE TABLE IF NOT EXISTS result_listing_events (
            id INTEGER PRIMARY KEY, item_id TEXT NOT NULL REFERENCES result_listing_items(item_id),
            run_id TEXT NOT NULL, old_status TEXT NOT NULL, new_status TEXT NOT NULL,
            observed_at TEXT NOT NULL, evidence TEXT NOT NULL, UNIQUE(item_id,run_id))''',
        '''CREATE TABLE IF NOT EXISTS result_revisit_runs (
            run_id TEXT PRIMARY KEY, task_id INTEGER NOT NULL, started_at TEXT NOT NULL,
            finished_at TEXT, lease_until TEXT, status TEXT NOT NULL, reviewed INTEGER NOT NULL DEFAULT 0,
            sold INTEGER NOT NULL DEFAULT 0, error TEXT)''',
        'CREATE INDEX IF NOT EXISTS idx_result_revisit_due ON result_listing_memberships(task_id,archived,next_review_at)',
        'CREATE INDEX IF NOT EXISTS idx_result_revisit_history ON result_listing_observations(item_id,observed_at)',
    ):
        conn.execute(sql)
    columns = {r[1] for r in conn.execute('PRAGMA table_info(result_listing_memberships)')}
    if 'review_not_before' not in columns:
        conn.execute('ALTER TABLE result_listing_memberships ADD COLUMN review_not_before TEXT')
    conn.execute("INSERT OR IGNORE INTO app_metadata(key,value) VALUES('migration:result_revisits:v1','done')")
