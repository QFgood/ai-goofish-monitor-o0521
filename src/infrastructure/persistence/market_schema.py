"""Additive market_tracking v1 schema; legacy tables are never rewritten."""
STATEMENTS = (
    '''CREATE TABLE IF NOT EXISTS market_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL DEFAULT 'market_tracking'
        CHECK(mode='market_tracking'), task_name TEXT NOT NULL, keyword TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1, tracking_days INTEGER NOT NULL DEFAULT 30,
        max_pages INTEGER NOT NULL DEFAULT 1, account_state_file TEXT,
        discovery_time TEXT NOT NULL DEFAULT '08:30', revisit_time TEXT NOT NULL DEFAULT '09:30',
        next_discovery_at TEXT NOT NULL, notifications INTEGER NOT NULL DEFAULT 1,
        failures INTEGER NOT NULL DEFAULT 0, last_error TEXT, deleted_at TEXT, created_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS market_items (
        item_id TEXT PRIMARY KEY, seller_id TEXT, seller_nickname TEXT, title TEXT NOT NULL,
        item_url TEXT NOT NULL, seller_url TEXT, region TEXT, publish_time TEXT,
        first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
        first_listed_price INTEGER, last_observed_price INTEGER, last_listing_price INTEGER,
        last_confirmed_active_at TEXT, first_seen_sold_at TEXT, sold_display_price INTEGER,
        sold_reference_price INTEGER, actual_transaction_price INTEGER DEFAULT NULL
        CHECK(actual_transaction_price IS NULL), price_source TEXT,
        product_category TEXT NOT NULL DEFAULT 'unknown', product_variant TEXT NOT NULL DEFAULT 'unknown',
        attributes_json TEXT NOT NULL DEFAULT '{}', classification_manual INTEGER NOT NULL DEFAULT 0,
        excluded INTEGER NOT NULL DEFAULT 0, exclusion_reason TEXT,
        listing_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK(listing_status IN ('active','sold','offline','unknown','stale_unconfirmed')),
        last_review_at TEXT, next_review_at TEXT)''',
    '''CREATE TABLE IF NOT EXISTS market_task_items (
        task_id INTEGER NOT NULL REFERENCES market_tasks(id),
        item_id TEXT NOT NULL REFERENCES market_items(item_id),
        first_seen_at TEXT NOT NULL, tracking_until TEXT NOT NULL,
        PRIMARY KEY(task_id,item_id))''',
    '''CREATE TABLE IF NOT EXISTS market_item_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL REFERENCES market_items(item_id),
        run_id TEXT NOT NULL, observed_at TEXT NOT NULL, price INTEGER, raw_status TEXT,
        parsed_status TEXT NOT NULL, source TEXT NOT NULL, request_ok INTEGER NOT NULL,
        page_complete INTEGER NOT NULL, list_complete INTEGER NOT NULL,
        evidence TEXT NOT NULL, UNIQUE(item_id,run_id,source))''',
    '''CREATE TABLE IF NOT EXISTS market_status_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL REFERENCES market_items(item_id),
        run_id TEXT NOT NULL, old_status TEXT NOT NULL, new_status TEXT NOT NULL,
        observed_at TEXT NOT NULL, source TEXT NOT NULL, evidence TEXT NOT NULL,
        UNIQUE(item_id,run_id))''',
    '''CREATE TABLE IF NOT EXISTS market_revisit_jobs (
        item_id TEXT PRIMARY KEY REFERENCES market_items(item_id), next_run_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','done','expired')),
        attempts INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
        last_error TEXT, worker_id TEXT, locked_until TEXT, last_result TEXT)''',
    '''CREATE TABLE IF NOT EXISTS market_runs (
        run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, task_id INTEGER REFERENCES market_tasks(id),
        started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL DEFAULT 'running',
        discovered INTEGER NOT NULL DEFAULT 0, reviewed INTEGER NOT NULL DEFAULT 0,
        sold INTEGER NOT NULL DEFAULT 0, error TEXT)''',
    '''CREATE TABLE IF NOT EXISTS market_leases (
        name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS market_classification_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL REFERENCES market_items(item_id),
        changed_at TEXT NOT NULL, actor TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS market_notices (
        task_id INTEGER NOT NULL REFERENCES market_tasks(id), day TEXT NOT NULL, kind TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(task_id,day,kind))''',
    'CREATE INDEX IF NOT EXISTS idx_market_seller ON market_items(seller_id)',
    'CREATE INDEX IF NOT EXISTS idx_market_variant ON market_items(product_variant,excluded,listing_status)',
    'CREATE INDEX IF NOT EXISTS idx_market_sold ON market_items(first_seen_sold_at)',
    'CREATE INDEX IF NOT EXISTS idx_market_observations ON market_item_observations(item_id,observed_at,id)',
    'CREATE INDEX IF NOT EXISTS idx_market_observations_time ON market_item_observations(observed_at)',
    'CREATE INDEX IF NOT EXISTS idx_market_due ON market_revisit_jobs(status,next_run_at,locked_until)',
    'CREATE INDEX IF NOT EXISTS idx_market_membership ON market_task_items(item_id,task_id)',
)


def migrate_market(conn):
    for statement in STATEMENTS:
        conn.execute(statement)
    conn.execute("INSERT OR IGNORE INTO app_metadata(key,value) VALUES('migration:market_tracking:v1','done')")
