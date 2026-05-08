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


# Baseline schema — CREATE TABLE only. No CREATE INDEX statements
# for columns that may need ALTER on an existing prod table; those
# live in _POST_LAUNCH_INDICES so they run AFTER _ensure_columns has
# guaranteed the columns exist.
#
# Phase7_tasks indices stay here because the table is brand-new — if
# the table is being created right now, all its columns exist
# immediately and the index can be created in the same batch.
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

CREATE TABLE IF NOT EXISTS phase7_tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    domain          TEXT NOT NULL,
    kind            TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    status          TEXT NOT NULL,
    attempt         INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 1,
    atom_task_id    TEXT,
    error           TEXT,
    worker_id       TEXT,
    heartbeat_at    TIMESTAMP,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at      TIMESTAMP,
    finished_at     TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_phase7_tasks_status ON phase7_tasks(status);
CREATE INDEX IF NOT EXISTS idx_phase7_tasks_domain ON phase7_tasks(domain);

-- domain_events: append-only audit log for the lifecycle classifier and
-- Slack handlers. Every assignment / renewal / extension / inventory move
-- writes a row. Future /domain-history <domain> command will replay the
-- timeline. Metadata is JSON text on SQLite, JSONB on Postgres.
CREATE TABLE IF NOT EXISTS domain_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    domain       TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    actor        TEXT,
    from_state   TEXT,
    to_state     TEXT,
    metadata     TEXT,
    occurred_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_domain_events_domain      ON domain_events(domain);
CREATE INDEX IF NOT EXISTS idx_domain_events_occurred_at ON domain_events(occurred_at DESC);
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

CREATE TABLE IF NOT EXISTS phase7_tasks (
    id              SERIAL PRIMARY KEY,
    domain          TEXT NOT NULL,
    kind            TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    status          TEXT NOT NULL,
    attempt         INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 1,
    atom_task_id    TEXT,
    error           TEXT,
    worker_id       TEXT,
    heartbeat_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_phase7_tasks_status ON phase7_tasks(status);
CREATE INDEX IF NOT EXISTS idx_phase7_tasks_domain ON phase7_tasks(domain);

CREATE TABLE IF NOT EXISTS domain_events (
    id           SERIAL PRIMARY KEY,
    domain       TEXT        NOT NULL,
    event_type   TEXT        NOT NULL,
    actor        TEXT,
    from_state   TEXT,
    to_state     TEXT,
    metadata     JSONB,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_domain_events_domain      ON domain_events(domain);
CREATE INDEX IF NOT EXISTS idx_domain_events_occurred_at ON domain_events(occurred_at DESC);
"""

# Columns added post-launch — these need to be ALTER-added on existing
# deployments (init_db only CREATEs IF NOT EXISTS, which doesn't touch
# already-created tables). _ensure_columns walks this list on every
# boot and adds anything missing from the live schema. Idempotent —
# safe to run repeatedly.
_POST_LAUNCH_COLUMNS = {
    'status':                 ('TEXT',      'TEXT'),       # (sqlite_type, postgres_type)
    'latest_task_id':         ('TEXT',      'TEXT'),
    'latest_error':           ('TEXT',      'TEXT'),
    'updated_at':             ('TIMESTAMP', 'TIMESTAMPTZ'),
    # Phase A (lifecycle bot) — daily classifier + Slack handlers fill these.
    'assigned_to':            ('TEXT',      'TEXT'),
    'expire_at':              ('TIMESTAMP', 'TIMESTAMPTZ'),
    'auto_renew_enabled':     ('INTEGER',   'BOOLEAN'),
    'last_active_at':         ('TIMESTAMP', 'TIMESTAMPTZ'),
    'last_prompted_at':       ('TIMESTAMP', 'TIMESTAMPTZ'),
    'last_namecheap_sync_at': ('TIMESTAMP', 'TIMESTAMPTZ'),
    'lifecycle_state':        ('TEXT',      'TEXT'),
}

# Indices added post-launch (separate from column adds because Postgres
# CREATE INDEX IF NOT EXISTS is sufficient and idempotent).
_POST_LAUNCH_INDICES = {
    'idx_domains_status':          'CREATE INDEX IF NOT EXISTS idx_domains_status ON domains(status)',
    'idx_domains_requested_by':    'CREATE INDEX IF NOT EXISTS idx_domains_requested_by ON domains(requested_by)',
    'idx_domains_lifecycle_state': 'CREATE INDEX IF NOT EXISTS idx_domains_lifecycle_state ON domains(lifecycle_state)',
    'idx_domains_expire_at':       'CREATE INDEX IF NOT EXISTS idx_domains_expire_at ON domains(expire_at)',
    'idx_domains_assigned_to':     'CREATE INDEX IF NOT EXISTS idx_domains_assigned_to ON domains(assigned_to)',
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


def _backfill_assigned_to_from_requested_by(c) -> None:
    """Seed lifecycle's `assigned_to` column from `requested_by` on rows
    where it's still NULL.

    Why: the lifecycle classifier needs an MDB to DM. Pre-Phase-A rows
    only have `requested_by` (set on Path B Mark Purchased + by the CSV
    import). Copying that into `assigned_to` gives the bot something
    to work with from day one.

    `requested_by` values from confirm_purchased look like
    'Slack:U_ABCDEF' (see slack_bot.routes._send_purchase…). Lifecycle's
    DM helper strips that prefix at send time — we keep the value
    verbatim here so requested_by stays the source of truth.

    Idempotent: subsequent runs match 0 rows because assigned_to is
    populated.
    """
    _execute(
        c,
        'UPDATE domains SET assigned_to = requested_by '
        "WHERE assigned_to IS NULL "
        "AND requested_by IS NOT NULL "
        "AND requested_by <> ''"
    )


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

    with _conn() as c:
        _backfill_assigned_to_from_requested_by(c)


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
               status: Optional[str] = None,
               assigned_to: Optional[str] = None) -> int:
    """Insert a new domain; returns its row id.

    Both backends timestamp purchased_at AND updated_at to NOW().

    `status` is the initial workflow state (one of STATUS_* constants).
    Path B's confirm_purchased passes STATUS_PENDING; Phase 7 will
    transition it to STATUS_DEPLOYING then STATUS_DEPLOYED / FAILED.
    Bulk imports (import_csv) and tests pass None to keep status NULL —
    those rows are treated as 'unknown' by the runtime.

    `assigned_to` is the Slack ID (or 'Slack:Uxxx' string) of the MDB
    this domain belongs to. The lifecycle classifier DMs this user when
    the domain expires or goes idle. Phase B's Slack flow passes this
    on Mark Purchased so new rows have an owner from day one. Falls
    back to NULL for legacy CSV imports — the boot-time backfill copies
    requested_by → assigned_to for those.

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
        'requested_by, notes, status, assigned_to, '
        'purchased_at, updated_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)'
    )
    try:
        with _conn() as c:
            if _is_postgres():
                cur = c.cursor()
                cur.execute(_q(insert_sql) + ' RETURNING id',
                            (domain, vertical, aws_account, lander_url,
                             requested_by, notes, status, assigned_to))
                row = cur.fetchone()
                cur.close()
                return row['id']
            cur = c.execute(insert_sql,
                            (domain, vertical, aws_account, lander_url,
                             requested_by, notes, status, assigned_to))
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


# ─── Phase A — lifecycle helpers ───────────────────────────────────────────
# Used by lifecycle/scan.py (daily classifier) and lifecycle/handlers.py
# (Slack button handlers in Phase B). All silent on missing rows: callers
# already know whether the row should exist.

import datetime as _dt
import json as _json
from typing import Iterable as _Iterable


def set_lifecycle_state(domain: str, state: Optional[str]) -> None:
    """Move a domain into a new lifecycle_state. None clears the column
    (used when a domain returns to ACTIVE/IDLE and we want the cron to
    re-classify cleanly on next pass)."""
    with _conn() as c:
        cur = _execute(
            c,
            'UPDATE domains SET lifecycle_state = ?, '
            'updated_at = CURRENT_TIMESTAMP WHERE domain = ?',
            (state, domain),
        )
        if _is_postgres():
            cur.close()


def bump_last_prompted_at(domain: str) -> None:
    """Stamp last_prompted_at = NOW(). Drives the 23h dedup guard so the
    classifier won't re-DM the same MDB twice in one day."""
    with _conn() as c:
        cur = _execute(
            c,
            'UPDATE domains SET last_prompted_at = CURRENT_TIMESTAMP, '
            'updated_at = CURRENT_TIMESTAMP WHERE domain = ?',
            (domain,),
        )
        if _is_postgres():
            cur.close()


def update_namecheap_sync(
    domain: str,
    expire_at: Optional[_dt.datetime],
    auto_renew_enabled: Optional[bool],
) -> None:
    """Persist the result of a Namecheap domains.getInfo call. Always
    stamps last_namecheap_sync_at so the classifier can avoid re-syncing
    a domain it just looked up."""
    with _conn() as c:
        cur = _execute(
            c,
            'UPDATE domains SET expire_at = ?, auto_renew_enabled = ?, '
            'last_namecheap_sync_at = CURRENT_TIMESTAMP, '
            'updated_at = CURRENT_TIMESTAMP WHERE domain = ?',
            (expire_at, auto_renew_enabled, domain),
        )
        if _is_postgres():
            cur.close()


def mark_active(domain: str) -> None:
    """Stamp last_active_at = NOW(). Called when the classifier sees
    spend > threshold for a domain. Used by future reporting + as a
    tiebreaker on rotation reuse decisions."""
    with _conn() as c:
        cur = _execute(
            c,
            'UPDATE domains SET last_active_at = CURRENT_TIMESTAMP, '
            'updated_at = CURRENT_TIMESTAMP WHERE domain = ?',
            (domain,),
        )
        if _is_postgres():
            cur.close()


def assign_to(domain: str, mdb_slack_id: Optional[str]) -> None:
    """Set or clear the MDB the domain belongs to. Pass None to release
    a domain into the inventory pool. Caller is responsible for writing
    the matching domain_events row."""
    with _conn() as c:
        cur = _execute(
            c,
            'UPDATE domains SET assigned_to = ?, '
            'updated_at = CURRENT_TIMESTAMP WHERE domain = ?',
            (mdb_slack_id, domain),
        )
        if _is_postgres():
            cur.close()


def record_event(
    domain: str, event_type: str, *,
    actor: Optional[str] = None,
    from_state: Optional[str] = None,
    to_state: Optional[str] = None,
    metadata: Optional[Dict] = None,
) -> None:
    """Append a row to domain_events. Always pair with the state-changing
    UPDATE that the event describes — record_event does NOT mutate the
    domains row itself.

    `actor` is a Slack user ID, 'cron', or None for system actions.
    `metadata` is any JSON-serializable dict (RedTrack stats, button
    payload, error details). Stored as JSONB on Postgres / TEXT on SQLite.
    """
    meta_json = _json.dumps(metadata) if metadata is not None else None
    with _conn() as c:
        cur = _execute(
            c,
            'INSERT INTO domain_events '
            '(domain, event_type, actor, from_state, to_state, metadata) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (domain, event_type, actor, from_state, to_state, meta_json),
        )
        if _is_postgres():
            cur.close()


def list_domain_events(domain: str, limit: int = 100) -> List[Dict]:
    """Read back the event timeline for one domain, newest first.
    Powers a future /domain-history slash command and lets tests
    assert that handlers wrote the right events.

    Metadata is JSON-decoded for caller convenience (Postgres' RealDictCursor
    returns it as a dict already; SQLite returns the raw TEXT)."""
    with _conn() as c:
        cur = _execute(
            c,
            'SELECT * FROM domain_events WHERE domain = ? '
            'ORDER BY occurred_at DESC, id DESC LIMIT ?',
            (domain, limit),
        )
        rows = cur.fetchall()
        if _is_postgres():
            cur.close()
    out: List[Dict] = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get('metadata'), str):
            try:
                d['metadata'] = _json.loads(d['metadata'])
            except (ValueError, TypeError):
                pass
        out.append(d)
    return out


def list_domains_for_lifecycle(
    *, exclude_states: Optional[_Iterable[str]] = None,
) -> List[Dict]:
    """Return every domain with its lifecycle columns hydrated. The
    classifier walks this list every cron run.

    `exclude_states` skips domains the cron must not re-classify (e.g.
    AWAITING_* states — those are waiting on a human click and the
    classifier pulling them out from under that flow would re-prompt).
    """
    sql = 'SELECT * FROM domains'
    params: tuple = ()
    excluded = list(exclude_states or [])
    if excluded:
        placeholders = ','.join(['?'] * len(excluded))
        sql += (f' WHERE lifecycle_state IS NULL '
                f'OR lifecycle_state NOT IN ({placeholders})')
        params = tuple(excluded)
    with _conn() as c:
        cur = _execute(c, sql, params)
        rows = cur.fetchall()
        if _is_postgres():
            cur.close()
    return [dict(r) for r in rows]


def get_awaiting_domains_past_sla(
    *, awaiting_states: _Iterable[str], hours_ago: int,
    limit: int = 200,
) -> List[Dict]:
    """Return rows whose lifecycle_state is in `awaiting_states` AND
    whose `last_prompted_at` is older than `hours_ago` hours.

    Drives the SLA escalator: any MDB DM that's gone unanswered for >48h
    gets re-routed to TL with override buttons. Limit caps how many
    escalations we'll fire in one cron pass — prevents an unexpected
    backlog from spamming TL with hundreds of cards.

    Rows with `last_prompted_at IS NULL` are excluded (the prompt was
    never sent, so the SLA clock hasn't started).
    """
    states = list(awaiting_states)
    if not states:
        return []
    placeholders = ','.join(['?'] * len(states))

    if _is_postgres():
        sql = (
            f'SELECT * FROM domains '
            f'WHERE lifecycle_state IN ({placeholders}) '
            f'  AND last_prompted_at IS NOT NULL '
            f"  AND last_prompted_at < NOW() - INTERVAL '{int(hours_ago)} hours' "
            f'ORDER BY last_prompted_at ASC '
            f'LIMIT ?'
        )
    else:
        sql = (
            f'SELECT * FROM domains '
            f'WHERE lifecycle_state IN ({placeholders}) '
            f'  AND last_prompted_at IS NOT NULL '
            f"  AND last_prompted_at < datetime('now', '-{int(hours_ago)} hours') "
            f'ORDER BY last_prompted_at ASC '
            f'LIMIT ?'
        )
    with _conn() as c:
        cur = _execute(c, sql, tuple(states) + (limit,))
        rows = cur.fetchall()
        if _is_postgres():
            cur.close()
    return [dict(r) for r in rows]


def get_domains_due_for_namecheap_sync(
    *, max_age_days: int = 7, near_expiry_days: int = 60, limit: int = 50,
) -> List[Dict]:
    """Return the next batch of domains the Namecheap sync should refresh.

    Picks rows that are:
      • never synced (last_namecheap_sync_at IS NULL), or
      • near expiry (expire_at within the next near_expiry_days), or
      • stale (synced > max_age_days ago).

    Oldest sync first so we make even progress through the inventory.
    Bounded by `limit` to stay under Namecheap's ~50 req/min ceiling
    (one cron pass = one batch).
    """
    if _is_postgres():
        sql = (
            'SELECT * FROM domains WHERE '
            '  last_namecheap_sync_at IS NULL '
            "  OR (expire_at IS NOT NULL "
            f"      AND expire_at < NOW() + INTERVAL '{int(near_expiry_days)} days') "
            f"  OR last_namecheap_sync_at < NOW() - INTERVAL '{int(max_age_days)} days' "
            'ORDER BY last_namecheap_sync_at ASC NULLS FIRST '
            'LIMIT ?'
        )
    else:
        sql = (
            'SELECT * FROM domains WHERE '
            '  last_namecheap_sync_at IS NULL '
            "  OR (expire_at IS NOT NULL "
            f"      AND expire_at < datetime('now', '+{int(near_expiry_days)} days')) "
            f"  OR last_namecheap_sync_at < datetime('now', '-{int(max_age_days)} days') "
            'ORDER BY last_namecheap_sync_at ASC '
            'LIMIT ?'
        )
    with _conn() as c:
        cur = _execute(c, sql, (limit,))
        rows = cur.fetchall()
        if _is_postgres():
            cur.close()
    return [dict(r) for r in rows]
