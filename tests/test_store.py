"""Unit tests for inventory.store — robustness layer added 2026-05-08.

These tests cover the typed exception contract (DuplicateDomainError,
StoreUnavailable) and the health_check probe used by /health to drain
load-balancer traffic when the DB is unreachable.

Each test runs against an isolated SQLite store via the `tmp_inventory`
fixture (see tests/conftest.py), so they don't touch production
Postgres and are safe to run in any environment.
"""
import pytest

from inventory import store


# ─── add_domain — duplicate handling ──────────────────────────────────────

def test_add_domain_returns_row_id(tmp_inventory):
    row_id = tmp_inventory.add_domain(
        domain='unique-1.com', vertical='auto-insurance',
    )
    assert isinstance(row_id, int)
    assert row_id > 0


def test_add_domain_duplicate_raises_DuplicateDomainError(tmp_inventory):
    """The contract: callers can catch DuplicateDomainError to handle
    benign re-clicks (Mark Purchased twice on the same row) without
    swallowing every other DB error class."""
    tmp_inventory.add_domain(domain='dup.com', vertical='x')
    with pytest.raises(store.DuplicateDomainError) as exc_info:
        tmp_inventory.add_domain(domain='dup.com', vertical='x')
    # The exception message must include the domain so logs are useful.
    assert 'dup.com' in str(exc_info.value)


def test_DuplicateDomainError_is_subclass_of_StoreError(tmp_inventory):
    """Single base class lets routes write `except store.StoreError` to
    catch any backend failure as a fallback when domain logic doesn't
    care about the specific subclass."""
    assert issubclass(store.DuplicateDomainError, store.StoreError)
    assert issubclass(store.StoreUnavailable, store.StoreError)


# ─── health_check ─────────────────────────────────────────────────────────

def test_health_check_succeeds_on_working_db(tmp_inventory):
    """A reachable, initialised DB returns None (no exception)."""
    # Should not raise.
    tmp_inventory.health_check()


def test_health_check_raises_StoreUnavailable_when_db_path_unwritable(
    monkeypatch, tmp_path,
):
    """If sqlite can't open the DB file (permissions / missing dir),
    health_check translates the error to StoreUnavailable so /health
    can return 503 with a stable reason."""
    from config import Config
    # Point at a path inside a non-existent parent directory — sqlite3
    # raises OperationalError, which our translator must wrap.
    monkeypatch.setattr(
        Config, 'INVENTORY_DB_PATH',
        str(tmp_path / 'no-such-dir' / 'inventory.db'),
    )
    monkeypatch.setattr(Config, 'DATABASE_URL', '')  # SQLite path

    with pytest.raises(store.StoreUnavailable):
        store.health_check()


def test_health_check_raises_StoreUnavailable_on_bad_postgres_url(
    monkeypatch,
):
    """When DATABASE_URL points at a Postgres that's unreachable,
    health_check must raise StoreUnavailable, NOT bubble up the raw
    psycopg2 exception (callers shouldn't have to know which backend
    is active)."""
    from config import Config
    monkeypatch.setattr(
        Config, 'DATABASE_URL',
        'postgresql://user:pwd@127.0.0.1:1/nonexistent',
    )
    with pytest.raises(store.StoreUnavailable):
        store.health_check()


# ─── mark_setup_complete must propagate non-benign DB errors ─────────────

def test_mark_setup_complete_no_op_on_missing_domain(tmp_inventory):
    """An UPDATE on a non-existent row is a benign no-op (0 rows
    affected). Callers rely on this — Path A's confirm_deployed clicks
    a button before Path B has inserted the row in some test scenarios.
    Must NOT raise."""
    # Should not raise even though 'never-inserted.com' isn't in the table.
    tmp_inventory.mark_setup_complete('never-inserted.com')


def test_mark_setup_complete_persists_lander_url(tmp_inventory):
    """When a lander_url is supplied, the column must be updated — the
    Path A false-positive fix relies on this."""
    tmp_inventory.add_domain(
        domain='lander-test.com', vertical='auto-insurance',
    )
    tmp_inventory.mark_setup_complete(
        'lander-test.com', lander_url='https://src.com/folder/',
    )
    row = tmp_inventory.get_domain('lander-test.com')
    assert row['lander_url'] == 'https://src.com/folder/'
    assert row['setup_at'] is not None


# ─── Schema bump (status / latest_task_id / latest_error / updated_at) ────

def test_status_column_exists_after_init(tmp_inventory):
    """Post-launch ALTER TABLE migration must add the status column."""
    tmp_inventory.add_domain(domain='status-col-test.com')
    row = tmp_inventory.get_domain('status-col-test.com')
    # All four post-launch columns must be readable.
    assert 'status' in row
    assert 'latest_task_id' in row
    assert 'latest_error' in row
    assert 'updated_at' in row


def test_init_db_is_idempotent(tmp_inventory):
    """Running init_db a second time must not error or corrupt data —
    the ALTER TABLE checks must skip already-present columns and the
    backfill must leave non-NULL aws_account untouched."""
    tmp_inventory.add_domain(
        domain='already-here.com', aws_account='other-vertical',
    )
    # Run the migration again.
    tmp_inventory.init_db()
    row = tmp_inventory.get_domain('already-here.com')
    # The pre-existing aws_account must be preserved — the backfill
    # only touches NULL/empty rows.
    assert row['aws_account'] == 'other-vertical'


def test_init_db_backfills_legacy_null_aws_account(tmp_inventory):
    """Rows that pre-date the explicit-aws_account contract must be
    backfilled to 'auto-insurance' so the workflow's fail-loud NULL
    check doesn't reject them."""
    # Insert with explicit NULL.
    tmp_inventory.add_domain(domain='legacy.com', aws_account=None)
    # Re-run init_db — backfill should fire.
    tmp_inventory.init_db()
    row = tmp_inventory.get_domain('legacy.com')
    assert row['aws_account'] == 'auto-insurance'


def test_init_db_backfill_does_not_touch_non_null_rows(tmp_inventory):
    tmp_inventory.add_domain(
        domain='explicit.com', aws_account='other-vertical',
    )
    tmp_inventory.add_domain(
        domain='also-explicit.com', aws_account='auto-insurance',
    )
    tmp_inventory.init_db()
    assert tmp_inventory.get_domain('explicit.com')['aws_account'] == 'other-vertical'
    assert tmp_inventory.get_domain('also-explicit.com')['aws_account'] == 'auto-insurance'


# ─── add_domain status param ──────────────────────────────────────────────

def test_add_domain_with_status_pending(tmp_inventory):
    tmp_inventory.add_domain(
        domain='with-status.com', status=tmp_inventory.STATUS_PENDING,
    )
    row = tmp_inventory.get_domain('with-status.com')
    assert row['status'] == 'pending'
    assert row['updated_at'] is not None


def test_add_domain_rejects_invalid_status(tmp_inventory):
    with pytest.raises(ValueError) as exc_info:
        tmp_inventory.add_domain(domain='bad-status.com', status='garbage')
    assert 'garbage' in str(exc_info.value)


# ─── transition_status ────────────────────────────────────────────────────

def test_transition_status_moves_state_and_stamps_updated_at(tmp_inventory):
    tmp_inventory.add_domain(
        domain='trans.com', status=tmp_inventory.STATUS_PENDING,
    )
    before = tmp_inventory.get_domain('trans.com')['updated_at']
    tmp_inventory.transition_status(
        'trans.com', to_status=tmp_inventory.STATUS_DEPLOYING,
        task_id='task-abc',
    )
    after = tmp_inventory.get_domain('trans.com')
    assert after['status'] == 'deploying'
    assert after['latest_task_id'] == 'task-abc'
    # updated_at moved forward (allowing equality on backends with
    # second-resolution timestamps).
    assert after['updated_at'] >= before


def test_transition_status_records_error_on_failure(tmp_inventory):
    tmp_inventory.add_domain(
        domain='err.com', status=tmp_inventory.STATUS_DEPLOYING,
    )
    tmp_inventory.transition_status(
        'err.com', to_status=tmp_inventory.STATUS_FAILED,
        error='ATOM cert validation timed out after 30 min',
    )
    row = tmp_inventory.get_domain('err.com')
    assert row['status'] == 'failed'
    assert 'cert validation timed out' in row['latest_error']


def test_transition_status_rejects_unknown_status(tmp_inventory):
    tmp_inventory.add_domain(domain='x.com')
    with pytest.raises(ValueError):
        tmp_inventory.transition_status('x.com', to_status='garbage')


def test_transition_status_no_op_on_missing_row(tmp_inventory):
    # Should NOT raise — UPDATE of 0 rows is benign by contract.
    tmp_inventory.transition_status(
        'never-inserted.com',
        to_status=tmp_inventory.STATUS_DEPLOYED,
    )
