"""Domain inventory store — supports both SQLite (local dev / tests) and
PostgreSQL (production).

Backend selection is controlled by Config.DATABASE_URL:
  • DATABASE_URL set and starts with postgres:// or postgresql:// → Postgres
  • Otherwise → SQLite at Config.INVENTORY_DB_PATH (current behaviour)

The public API (init_db / add_domain / list_domains / get_domain /
mark_setup_complete) is identical for both backends — callers don't care
which one is live. The Repository pattern in action.

Why both:
  • SQLite stays for tests + local dev (zero setup, fast, isolated per test).
  • Postgres is for production deploy on Render — survives restarts,
    multi-instance safe, automatic backups via the hosting provider.

Migration: see migrate_sqlite_to_postgres.py.
"""
import sqlite3
from contextlib import contextmanager
from typing import Optional, List, Dict

from config import Config

# psycopg2 is only needed when DATABASE_URL points at Postgres.
# Import lazily so SQLite-only environments don't have to install it.
try:
    import psycopg2
    import psycopg2.extras
    _PSYCOPG2_AVAILABLE = True
except ImportError:
    _PSYCOPG2_AVAILABLE = False


# ─── Public exception types ────────────────────────────────────────────────
# The store hides backend specifics (psycopg2 vs sqlite3) so callers can
# catch one stable exception per failure mode instead of branching on the
# active driver. This is the contract that lets routes.py distinguish a
# benign duplicate-row attempt (caller should swallow + inform) from a
# real DB outage (caller must escalate).

class StoreError(Exception):
    """Base class for inventory-store failures."""


class DuplicateDomainError(StoreError):
    """Raised by add_domain when the domain already exists.

    This is the ONLY DB error a caller is expected to swallow — the
    domain row already being there is a benign idempotency case (e.g.
    user clicked Mark Purchased twice). All other StoreError subclasses
    indicate real problems and must be escalated.
    """


class StoreUnavailable(StoreError):
    """Raised when the underlying DB connection / driver is broken
    (network outage, missing psycopg2, bad DATABASE_URL, etc.). Callers
    should treat this as 'service degraded', not 'request invalid'.
    """


def _is_postgres() -> bool:
    """True iff DATABASE_URL points at a Postgres instance."""
    url = (Config.DATABASE_URL or '').lower()
    return url.startswith('postgres://') or url.startswith('postgresql://')


# ─── Schema ────────────────────────────────────────────────────────────────
# Two slightly-different DDL strings — same logical schema, different
# dialects. The data shape is identical.

_SQLITE_SCHEMA = """
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

_POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
    id              SERIAL PRIMARY KEY,
    domain          TEXT UNIQUE NOT NULL,
    vertical        TEXT,
    aws_account     TEXT,
    lander_url      TEXT,
    purchased_at    TIMESTAMPTZ,
    setup_at        TIMESTAMPTZ,
    requested_by    TEXT,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_domains_vertical ON domains(vertical);
"""


# ─── Connection management ─────────────────────────────────────────────────

@contextmanager
def _conn():
    """Yield a DB connection appropriate for the configured backend.

    Auto-commits on context exit when no exception was raised; rolls
    back otherwise.
    """
    if _is_postgres():
        if not _PSYCOPG2_AVAILABLE:
            raise RuntimeError(
                'DATABASE_URL points at Postgres but psycopg2 is not installed. '
                'Run: pip install psycopg2-binary'
            )
        c = psycopg2.connect(
            Config.DATABASE_URL,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
    else:
        c = sqlite3.connect(Config.INVENTORY_DB_PATH)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


def _ph() -> str:
    """Parameter placeholder for the active backend.

    SQLite uses '?', Postgres uses '%s'. We write queries with '?' and
    swap to '%s' when targeting Postgres — keeps the query strings
    readable and minimizes per-query branching.
    """
    return '%s' if _is_postgres() else '?'


def _q(query: str) -> str:
    """Translate ?-style placeholders to the active backend's style."""
    return query.replace('?', _ph()) if _is_postgres() else query


def _execute(c, query: str, params: tuple = ()):
    """Execute a query using the right cursor for the backend."""
    if _is_postgres():
        cur = c.cursor()
        cur.execute(_q(query), params)
        return cur
    return c.execute(query, params)


# ─── Public API ────────────────────────────────────────────────────────────

def init_db() -> None:
    """Idempotent — safe to call on every app boot."""
    with _conn() as c:
        if _is_postgres():
            cur = c.cursor()
            cur.execute(_POSTGRES_SCHEMA)
            cur.close()
        else:
            c.executescript(_SQLITE_SCHEMA)


def health_check() -> None:
    """Cheap read against the DB to confirm connectivity.

    Raises StoreUnavailable on any failure (driver missing, connection
    refused, auth failure, etc.). Used by /health so the load balancer
    can drain a pod whose DB went away.

    Intentionally does NOT raise the underlying psycopg2/sqlite3 error
    type — those leak the active backend into callers. We translate to
    StoreUnavailable so /health stays backend-agnostic.
    """
    try:
        with _conn() as c:
            cur = _execute(c, 'SELECT 1')
            cur.fetchone()
            if _is_postgres():
                cur.close()
    except Exception as e:
        raise StoreUnavailable(f'{type(e).__name__}: {e}') from e


def add_domain(domain: str, vertical: Optional[str] = None,
               aws_account: Optional[str] = None,
               lander_url: Optional[str] = None,
               requested_by: Optional[str] = None,
               notes: Optional[str] = None) -> int:
    """Insert a new domain; returns its row id.

    Both backends timestamp purchased_at to NOW() at insert time.

    Raises DuplicateDomainError if the domain is already in inventory —
    the only DB failure the bot's button handlers should treat as
    benign. All other failures escalate to the caller as the original
    driver exception (caller is expected to log + warn + re-raise).
    """
    insert_sql = (
        'INSERT INTO domains (domain, vertical, aws_account, lander_url, '
        'requested_by, notes, purchased_at) '
        'VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)'
    )
    try:
        with _conn() as c:
            if _is_postgres():
                cur = c.cursor()
                cur.execute(_q(insert_sql) + ' RETURNING id',
                            (domain, vertical, aws_account, lander_url,
                             requested_by, notes))
                row = cur.fetchone()
                cur.close()
                return row['id']
            cur = c.execute(insert_sql,
                            (domain, vertical, aws_account, lander_url,
                             requested_by, notes))
            return cur.lastrowid
    except sqlite3.IntegrityError as e:
        # SQLite raises IntegrityError on UNIQUE violations.
        raise DuplicateDomainError(
            f'Domain {domain!r} already exists in inventory'
        ) from e
    except Exception as e:
        # Postgres' UniqueViolation is psycopg2.errors.UniqueViolation,
        # a subclass of psycopg2.IntegrityError. We detect via class
        # name to avoid hard-importing psycopg2 in the SQLite-only
        # path (the import is already optional above).
        if _PSYCOPG2_AVAILABLE and isinstance(e, psycopg2.IntegrityError):
            raise DuplicateDomainError(
                f'Domain {domain!r} already exists in inventory'
            ) from e
        raise


def list_domains(vertical: Optional[str] = None) -> List[Dict]:
    """Return all domains, optionally filtered by vertical, newest first."""
    with _conn() as c:
        if vertical:
            cur = _execute(
                c,
                'SELECT * FROM domains WHERE vertical = ? '
                'ORDER BY purchased_at DESC NULLS LAST'
                if _is_postgres()
                else 'SELECT * FROM domains WHERE vertical = ? '
                     'ORDER BY purchased_at DESC',
                (vertical,),
            )
        else:
            cur = _execute(
                c,
                'SELECT * FROM domains '
                'ORDER BY purchased_at DESC NULLS LAST'
                if _is_postgres()
                else 'SELECT * FROM domains ORDER BY purchased_at DESC',
            )
        rows = cur.fetchall()
        if _is_postgres():
            cur.close()
    return [dict(r) for r in rows]


def get_domain(domain: str) -> Optional[Dict]:
    """Return one domain row by name, or None."""
    with _conn() as c:
        cur = _execute(c, 'SELECT * FROM domains WHERE domain = ?', (domain,))
        row = cur.fetchone()
        if _is_postgres():
            cur.close()
    return dict(row) if row else None


def mark_setup_complete(domain: str, lander_url: Optional[str] = None) -> None:
    """Stamp setup_at = NOW() on the given domain (no-op if not found).

    When `lander_url` is provided, also update the row's lander_url so Path A
    deployments persist what was actually deployed (Path A's confirm_deployed
    didn't store this previously, leaving the column NULL on Path-A rows).
    """
    with _conn() as c:
        if lander_url:
            cur = _execute(
                c,
                'UPDATE domains SET setup_at = CURRENT_TIMESTAMP, '
                'lander_url = ? WHERE domain = ?',
                (lander_url, domain),
            )
        else:
            cur = _execute(
                c,
                'UPDATE domains SET setup_at = CURRENT_TIMESTAMP WHERE domain = ?',
                (domain,),
            )
        if _is_postgres():
            cur.close()
