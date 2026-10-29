"""Short SQLite transactions. Network work must stay outside these contexts."""
from contextlib import contextmanager

from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection, get_database_path


class MarketRepository:
    def __init__(self, db_path=None):
        self.db_path = db_path or get_database_path()
        bootstrap_sqlite_storage(self.db_path)

    @contextmanager
    def connection(self, write=False):
        with sqlite_connection(self.db_path) as conn:
            try:
                if write:
                    conn.execute('BEGIN IMMEDIATE')
                yield conn
                if write:
                    conn.commit()
            except BaseException:
                conn.rollback()
                raise
