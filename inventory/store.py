"""SQLite-backed CRUD for owned domains.

This is the local-dev store. Phase 5 may swap the backend for Google Sheets
(to keep the existing audit trail) or a hosted DB. Keep the public functions
stable so the rest of the code doesn't have to care which backend is live.
"""
import sqlite3
from contextlib import contextmanager
from typing import Optional, List, Dict
from config import Config


SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    domain          TEXT UNIQUE NOT NULL,
    vertical        TEXT,
    aws_account     TEXT,
    lander_url      TEXT,
    purchased_at    TIMESTAMP,
    setup_at        TIMESTAMP,
    requested_by    TEXT,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_domains_vertical ON domains(vertical);
"""


@contextmanager
def _conn():
    c = sqlite3.connect(Config.INVENTORY_DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    """Idempotent — safe to call on every app boot."""
    with _conn() as c:
        c.executescript(SCHEMA)


def add_domain(domain: str, vertical: Optional[str] = None,
               aws_account: Optional[str] = None,
               lander_url: Optional[str] = None,
               requested_by: Optional[str] = None,
               notes: Optional[str] = None) -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO domains (domain, vertical, aws_account, lander_url,
                                    requested_by, notes, purchased_at)
               VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (domain, vertical, aws_account, lander_url, requested_by, notes),
        )
        return cur.lastrowid


def list_domains(vertical: Optional[str] = None) -> List[Dict]:
    with _conn() as c:
        if vertical:
            rows = c.execute(
                'SELECT * FROM domains WHERE vertical = ? ORDER BY purchased_at DESC',
                (vertical,),
            ).fetchall()
        else:
            rows = c.execute(
                'SELECT * FROM domains ORDER BY purchased_at DESC'
            ).fetchall()
    return [dict(r) for r in rows]


def get_domain(domain: str) -> Optional[Dict]:
    with _conn() as c:
        row = c.execute(
            'SELECT * FROM domains WHERE domain = ?', (domain,)
        ).fetchone()
    return dict(row) if row else None


def mark_setup_complete(domain: str) -> None:
    with _conn() as c:
        c.execute(
            'UPDATE domains SET setup_at = CURRENT_TIMESTAMP WHERE domain = ?',
            (domain,),
        )
