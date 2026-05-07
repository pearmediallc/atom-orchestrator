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

# ─── Status state machine ──────────────────────────────────────────────────
# A domain row's `status` column moves through a strict state machine that
# the bot's button handlers + Phase 7 worker drive. Encoded as constants so
# tests + workflow code share one source of truth.

STATUS_UNKNOWN = 'unknown'    # Legacy rows from before status column existed.
STATUS_PENDING = 'pending'    # Row inserted (Path B Mark Purchased) but
                              # Phase 7 hasn't started yet.
STATUS_DEPLOYING = 'deploying'  # Phase 7 worker is in flight.
STATUS_DEPLOYED = 'deployed'  # Phase 7 finished successfully.
STATUS_FAILED = 'failed'      # Phase 7 finished with an error.

_VALID_STATUSES = {
    STATUS_UNKNOWN, STATUS_PENDING, STATUS_DEPLOYING,
    STATUS_DEPLOYED, STATUS_FAILED,
}


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
    notes           TEXT,
    status          TEXT,
    latest_task_id  TEXT,
    latest_error    TEXT,
    updated_at      TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_domains_vertical ON domains(vertical);
CREATE INDEX IF NOT EXISTS idx_domains_status ON domains(status);
CREATE INDEX IF NOT EXISTS idx_domains_requested_by ON domains(requested_by);
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
    notes           TEXT,
    status          TEXT,
    latest_task_id  TEXT,
    latest_error    TEXT,
    updated_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_domains_vertical ON domains(vertical);
CREATE INDEX IF NOT EXISTS idx_domains_status ON domains(status);
CREATE INDEX IF NOT EXISTS idx_domains_requested_by ON domains(requested_by);
"""

# Columns added post-launch — these need to be ALTER-added on existing
# deployments (init_db only CREATEs IF NOT EXISTS, which doesn't touch
# already-created tables). _ensure_columns walks this list on every
# boot and adds anything missing from the live schema. Idempotent —
# safe to run repeatedly.
_POST_LAUNCH_COLUMNS = {
    'status':         ('TEXT', 'TEXT'),                  # (sqlite_type, postgres_type)
    'latest_task_id': ('TEXT', 'TEXT'),
    'latest_error':   ('TEXT', 'TEXT'),
    'updated_at':     ('TIMESTAMP', 'TIMESTAMPTZ'),
}

# Indices added post-launch (separate from column adds because Postgres
# CREATE INDEX IF NOT EXISTS is sufficient and idempotent).
_POST_LAUNCH_INDICES = {
    'idx_domains_status':       'CREATE INDEX IF NOT EXISTS idx_domains_status ON domains(status)',
    'idx_domains_requested_by': 'CREATE INDEX IF NOT EXISTS idx_domains_requested_by ON domains(requested_by)',
}


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

def _existing_columns(c) -> set:
    """Return the set of column names currently on the `domains` table.

    Backend-aware (sqlite3 PRAGMA vs Postgres information_schema). Used
    by _ensure_columns to skip columns that are already there so the
    migration is idempotent across many boots.
    """
    if _is_postgres():
        cur = c.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'domains'"
        )
        rows = cur.fetchall()
        cur.close()
        return {r['column_name'] for r in rows}
    cur = c.execute('PRAGMA table_info(domains)')
    return {row[1] for row in cur.fetchall()}


def _ensure_columns(c) -> None:
    """Idempotent ALTER TABLE — adds any column from _POST_LAUNCH_COLUMNS
    that is missing from the live schema.

    Why not Alembic: this codebase has 4 columns to migrate; pulling in
    a migration framework for that is more risk than reward. The
    invariant is simple — every column listed in _POST_LAUNCH_COLUMNS
    must exist after init_db() returns.

    Each ALTER TABLE is its own statement-level transaction in Postgres;
    if column N+1 fails, columns 1..N stay added and the next boot
    will pick up where this one left off. No half-migrated state.
    """
    existing = _existing_columns(c)
    for name, (sqlite_type, pg_type) in _POST_LAUNCH_COLUMNS.items():
        if name in existing:
            continue
        col_type = pg_type if _is_postgres() else sqlite_type
        _execute(c, f'ALTER TABLE domains ADD COLUMN {name} {col_type}')


def _ensure_indices(c) -> None:
    """Idempotent CREATE INDEX for post-launch indices.

    Both Postgres and SQLite support `CREATE INDEX IF NOT EXISTS` so
    re-running on every boot is a cheap no-op once the index exists.
    """
    for _name, ddl in _POST_LAUNCH_INDICES.items():
        _execute(c, ddl)


def _backfill_legacy_aws_account(c) -> None:
    """Set aws_account = 'auto-insurance' on rows where it is NULL or
    empty.

    Why this exists: workflow.py has long had
        target_account = record.get('aws_account') or 'auto-insurance'
    so legacy rows with NULL aws_account were already being silently
    routed to auto-insurance. This UPDATE makes that implicit fallback
    explicit so the next-batch fail-loud NULL check can run without
    rejecting the 743 pre-existing rows.

    Does NOT change runtime behaviour — the rows already behave as if
    aws_account were 'auto-insurance'. The UPDATE only changes what's
    recorded in the column.

    Idempotent: subsequent runs match 0 rows because aws_account is
    already populated.
    """
    _execute(
        c,
        "UPDATE domains SET aws_account = 'auto-insurance' "
        "WHERE aws_account IS NULL OR aws_account = ''"
    )


def init_db() -> None:
    """Idempotent — safe to call on every app boot.

    Order:
      1. CREATE TABLE / INDEX baseline (no-op if the table exists)
      2. ALTER TABLE add new columns (idempotent — skips anything present)
      3. CREATE INDEX for post-launch indices (idempotent)
      4. Backfill legacy NULL aws_account rows

    Each step uses its own connection so a failure in (2) doesn't roll
    back (1) — relevant because Postgres transactions are
    statement-level for DDL but a Python-level exception in step 2
    would still abort the connection's pending work otherwise.
    """
    with _conn() as c:
        if _is_postgres():
            cur = c.cursor()
            cur.execute(_POSTGRES_SCHEMA)
            cur.close()
        else:
            c.executescript(_SQLITE_SCHEMA)

    with _conn() as c:
        _ensure_columns(c)

    with _conn() as c:
        _ensure_indices(c)

    with _conn() as c:
        _backfill_legacy_aws_account(c)


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
               notes: Optional[str] = None,
               status: Optional[str] = None) -> int:
    """Insert a new domain; returns its row id.

    Both backends timestamp purchased_at AND updated_at to NOW().

    `status` is the initial workflow state (one of STATUS_* constants).
    Path B's confirm_purchased passes STATUS_PENDING; Phase 7 will
    transition it to STATUS_DEPLOYING then STATUS_DEPLOYED / FAILED.
    Bulk imports (import_csv) and tests pass None to keep status NULL —
    those rows are treated as 'unknown' by the runtime.

    Raises DuplicateDomainError if the domain is already in inventory —
    the only DB failure the bot's button handlers should treat as
    benign. All other failures escalate to the caller (caller is
    expected to log + warn + re-raise).
    """
    if status is not None and status not in _VALID_STATUSES:
        raise ValueError(
            f'status={status!r} not in {sorted(_VALID_STATUSES)}'
        )
    insert_sql = (
        'INSERT INTO domains (domain, vertical, aws_account, lander_url, '
        'requested_by, notes, status, purchased_at, updated_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)'
    )
    try:
        with _conn() as c:
            if _is_postgres():
                cur = c.cursor()
                cur.execute(_q(insert_sql) + ' RETURNING id',
                            (domain, vertical, aws_account, lander_url,
                             requested_by, notes, status))
                row = cur.fetchone()
                cur.close()
                return row['id']
            cur = c.execute(insert_sql,
                            (domain, vertical, aws_account, lander_url,
                             requested_by, notes, status))
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


def transition_status(domain: str, *, to_status: str,
                      task_id: Optional[str] = None,
                      error: Optional[str] = None) -> None:
    """Atomically move a domain row to a new workflow status.

    Always stamps updated_at = NOW() so callers can sort by recent
    activity. Optionally records the ATOM task_id (so Phase 7 progress
    is correlatable from the inventory row) and the latest_error
    (when transitioning into STATUS_FAILED).

    Silent on missing rows (UPDATE ... WHERE domain=?) — the caller
    knows whether the row should exist; this helper doesn't second-
    guess. Path A's mark_setup_complete already documented this
    contract.
    """
    if to_status not in _VALID_STATUSES:
        raise ValueError(
            f'to_status={to_status!r} not in {sorted(_VALID_STATUSES)}'
        )
    sql = (
        'UPDATE domains SET status = ?, '
        'latest_task_id = COALESCE(?, latest_task_id), '
        'latest_error = ?, '
        'updated_at = CURRENT_TIMESTAMP '
        'WHERE domain = ?'
    )
    with _conn() as c:
        cur = _execute(c, sql, (to_status, task_id, error, domain))
        if _is_postgres():
            cur.close()


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
