#!/usr/bin/env python3
"""Create an exclusive, permission-restricted SQLite backup without initializing a DB."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3


def backup_database(database, directory):
    os.umask(0o077)
    source = Path(database).resolve(strict=True)
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError('Backup directory must not be a symlink')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise ValueError('Backup directory must have mode 700')
    target = directory / ('app-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ') + '.sqlite3')
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as origin:
            with sqlite3.connect(target) as destination:
                origin.backup(destination)
                if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('Backup integrity check failed')
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default='data/app.sqlite3')
    parser.add_argument('--backup-dir', default='.sentinel-backups')
    args = parser.parse_args()
    print(backup_database(args.database, args.backup_dir))
