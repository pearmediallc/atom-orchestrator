"""One-shot migration: copy all rows from SQLite (./inventory.db) to the
Postgres instance configured via DATABASE_URL.

Usage:
    # 1. Make sure DATABASE_URL is set in your environment OR .env
    # 2. Make sure the local SQLite file exists (default ./inventory.db)
    # 3. Dry run first — lists what it would migrate, no writes:
    python migrate_sqlite_to_postgres.py
    # 4. Real migration:
    python migrate_sqlite_to_postgres.py --apply

What it does:
    • Reads every row from SQLite domains table.
    • Connects to the Postgres DB and runs init_db() (idempotent CREATE TABLE).
    • For each SQLite row, inserts into Postgres preserving timestamps.
    • Skips rows whose `domain` already exists in Postgres (safe to re-run).
    • Reports how many rows migrated, skipped, and any errors.

Safety:
    • Refuses to run if DATABASE_URL is empty.
    • Refuses to run if DATABASE_URL is the SQLite path (typo guard).
    • Does NOT delete anything from SQLite — leaves the local file alone.

This is a one-shot. Once Postgres is the source of truth and the bot is
deployed using DATABASE_URL, you can delete this script and the local
inventory.db file.
"""
import argparse
import os
import sqlite3
import sys
from contextlib import contextmanager

from config import Config


def _validate_env():
    if not Config.DATABASE_URL:
        sys.exit('DATABASE_URL is empty. Set it in .env or environment first.')
    if Config.DATABASE_URL.lower().startswith('sqlite'):
        sys.exit('DATABASE_URL appears to be a SQLite URL — this script only '
                 'migrates TO Postgres. Set DATABASE_URL=postgresql://…')
    if not os.path.exists(Config.INVENTORY_DB_PATH):
        sys.exit(f'SQLite source not found at {Config.INVENTORY_DB_PATH}. '
                 'Nothing to migrate.')


def _import_psycopg2():
    try:
        import psycopg2
        import psycopg2.extras
        return psycopg2, psycopg2.extras
    except ImportError:
        sys.exit('psycopg2 not installed. Run: pip install psycopg2-binary')


@contextmanager
def _sqlite_conn():
    c = sqlite3.connect(Config.INVENTORY_DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


def _read_sqlite_rows() -> list:
    with _sqlite_conn() as c:
        rows = c.execute('SELECT * FROM domains ORDER BY id').fetchall()
    return [dict(r) for r in rows]


def _ensure_postgres_schema(psycopg2):
    """Force the bot's init_db() to run against Postgres."""
    # We import inventory.store after Config has DATABASE_URL set so the
    # store correctly detects Postgres mode.
    from inventory import store
    store.init_db()


def _existing_domains_in_postgres(psycopg2) -> set:
    conn = psycopg2.connect(Config.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT domain FROM domains')
            return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()


def _insert_rows_to_postgres(psycopg2, rows: list) -> tuple:
    """Insert rows one at a time, skipping duplicates. Returns (inserted, skipped)."""
    existing = _existing_domains_in_postgres(psycopg2)
    inserted = 0
    skipped = 0

    conn = psycopg2.connect(Config.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            for r in rows:
                if r['domain'] in existing:
                    skipped += 1
                    continue
                cur.execute(
                    'INSERT INTO domains (domain, vertical, aws_account, '
                    'lander_url, requested_by, notes, purchased_at, setup_at) '
                    'VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                    (
                        r['domain'], r.get('vertical'), r.get('aws_account'),
                        r.get('lander_url'), r.get('requested_by'),
                        r.get('notes'),
                        r.get('purchased_at'), r.get('setup_at'),
                    ),
                )
                inserted += 1
        conn.commit()
    finally:
        conn.close()
    return inserted, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--apply', action='store_true',
                        help='Actually migrate (default is dry run)')
    args = parser.parse_args()

    _validate_env()
    psycopg2, _extras = _import_psycopg2()

    print(f'Source SQLite : {Config.INVENTORY_DB_PATH}')
    # Hide credentials in the URL when printing
    safe_url = Config.DATABASE_URL
    if '@' in safe_url:
        safe_url = safe_url.split('@', 1)[0].split('://')[0] + '://***@' + safe_url.split('@', 1)[1]
    print(f'Target Postgres: {safe_url}')
    print('Mode           :', 'APPLY' if args.apply else 'DRY RUN')
    print('-' * 64)

    rows = _read_sqlite_rows()
    print(f'SQLite rows to consider: {len(rows)}')

    if not args.apply:
        print('\nDRY RUN — no writes performed. Re-run with --apply to migrate.')
        return 0

    print('\nEnsuring Postgres schema is up to date...')
    _ensure_postgres_schema(psycopg2)

    print('Migrating rows...')
    inserted, skipped = _insert_rows_to_postgres(psycopg2, rows)
    print(f'  ✓ Inserted: {inserted}')
    print(f'  ⊙ Skipped (already in Postgres): {skipped}')
    print('\nMigration complete.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
