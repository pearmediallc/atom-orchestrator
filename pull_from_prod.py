"""One-shot: copy domains + domain_events + phase7_tasks rows from the
prod Postgres into local SQLite at ./inventory.db.

Usage (you provide the prod DATABASE_URL once, on the command line — it
is NOT read from your .env to keep the local-test DATABASE_URL empty
slot uncontaminated):

    python pull_from_prod.py --prod-url 'postgresql://USER:PASS@HOST/DB?sslmode=require'

Safety:
  • READ-ONLY against Postgres — only SELECTs, never writes back.
  • Idempotent against SQLite — uses INSERT OR REPLACE so re-running
    just refreshes local with the latest prod snapshot.
  • Does NOT touch your .env. Your local Config.DATABASE_URL stays
    empty so the orchestrator keeps using local SQLite.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import decimal
import json
import os
import sqlite3
import sys
from contextlib import closing
from typing import Any


SQLITE_PATH = './inventory.db'

# Tables to mirror, in dependency order. domains FK'd by phase7_tasks +
# domain_events, so domains has to land first.
TABLES = ['domains', 'phase7_tasks', 'domain_events']


def _columns_for(sqlite_path: str, table: str) -> list[str]:
    """Read SQLite's column list so we mirror exactly the schema the
    local store will read, regardless of any prod-only columns that
    haven't been migrated to SQLite yet (those get dropped).
    """
    with closing(sqlite3.connect(sqlite_path)) as con:
        cur = con.execute(f'PRAGMA table_info({table})')
        return [r[1] for r in cur.fetchall()]


def _coerce(value: Any) -> Any:
    """Convert a Postgres cell into something sqlite3 can bind.

    sqlite3 natively binds None, int, float, str, bytes. Anything else
    raises sqlite3.InterfaceError. Postgres types we encounter that need
    coercion:
      • dict / list      — JSONB columns (e.g. domain_events.metadata)
      • datetime / date  — sqlite3 can bind these IF detect_types is set,
                           but it's not in our local store, so we
                           serialise to ISO 8601 strings to match what
                           the rest of the code expects.
      • Decimal          — convert to float (no fractional spend column
                           needs the lossless precision here).
    """
    if value is None or isinstance(value, (int, float, str, bytes)):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, bool):
        return int(value)
    return str(value)


def _pull_table(pg_cur, sqlite_con, table: str, columns: list[str]) -> int:
    """SELECT * FROM <prod table>, REPLACE INTO <local table>. Returns
    row count written. Coerces JSONB / datetime / Decimal so sqlite3
    can bind them.
    """
    col_list = ', '.join(columns)
    pg_cur.execute(f'SELECT {col_list} FROM {table}')
    rows = pg_cur.fetchall()
    if not rows:
        return 0

    coerced = [tuple(_coerce(cell) for cell in row) for row in rows]
    placeholders = ', '.join(['?'] * len(columns))
    insert_sql = (
        f'INSERT OR REPLACE INTO {table} ({col_list}) '
        f'VALUES ({placeholders})'
    )
    sqlite_con.executemany(insert_sql, coerced)
    sqlite_con.commit()
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--prod-url',
        default=os.getenv('PROD_DATABASE_URL', ''),
        help='Prod Postgres URL (postgresql://...). Or set PROD_DATABASE_URL.',
    )
    parser.add_argument(
        '--sqlite-path', default=SQLITE_PATH,
        help=f'Local SQLite file (default: {SQLITE_PATH})',
    )
    args = parser.parse_args()

    if not args.prod_url.startswith(('postgres://', 'postgresql://')):
        print('error: --prod-url must be a postgres:// or postgresql:// URL',
              file=sys.stderr)
        print('       (set PROD_DATABASE_URL env var or pass --prod-url)',
              file=sys.stderr)
        return 2

    # Ensure the local SQLite schema exists before we try to write to it.
    # init_db() is idempotent — creates tables on first run, no-ops after.
    from inventory import store  # imports config; doesn't read prod-url
    store.init_db()

    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        print('error: psycopg2 is not installed. Activate the venv first:',
              file=sys.stderr)
        print('       source venv/bin/activate && pip install psycopg2-binary',
              file=sys.stderr)
        return 2

    print(f'Connecting to prod Postgres...')
    pg_conn = psycopg2.connect(args.prod_url)
    pg_conn.set_session(readonly=True)
    pg_cur = pg_conn.cursor()
    sqlite_con = sqlite3.connect(args.sqlite_path)

    total = 0
    try:
        for table in TABLES:
            cols = _columns_for(args.sqlite_path, table)
            if not cols:
                print(f'  skip {table}: not in local schema')
                continue
            n = _pull_table(pg_cur, sqlite_con, table, cols)
            print(f'  {table}: {n} rows')
            total += n
    finally:
        pg_cur.close()
        pg_conn.close()
        sqlite_con.close()

    print(f'\ndone — {total} total rows mirrored into {args.sqlite_path}')
    print('local orchestrator will see prod data on its next query '
          '(no restart needed — store opens a fresh connection per call)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
