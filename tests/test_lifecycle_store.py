"""Tests for the lifecycle helpers added to inventory.store in Phase A.

These exercise the new schema columns (assigned_to, expire_at,
auto_renew_enabled, last_active_at, last_prompted_at,
last_namecheap_sync_at, lifecycle_state), the domain_events history
table, and the boot-time backfill that copies requested_by → assigned_to
on legacy rows.
"""
import datetime as dt

import pytest

from inventory import store
from lifecycle import states as S


# ─── Schema migration / backfill ───────────────────────────────────────────

def test_init_db_creates_lifecycle_columns(tmp_inventory):
    """Fresh init_db on SQLite must produce all Phase A columns."""
    store.add_domain(domain='example.com', requested_by='Slack:U_OWNER')
    row = tmp_inventory.get_domain('example.com')
    # All lifecycle columns must be present (None is fine, but the key
    # must exist — i.e. the column was created).
    for col in ('assigned_to', 'expire_at', 'auto_renew_enabled',
                'last_active_at', 'last_prompted_at',
                'last_namecheap_sync_at', 'lifecycle_state'):
        assert col in row, f'column {col!r} missing from domains schema'


def test_assigned_to_backfilled_from_requested_by(tmp_inventory):
    """Legacy rows (assigned_to NULL) get backfilled on next init_db."""
    # Insert via raw SQL to mimic a legacy row that was added before
    # add_domain accepted assigned_to.
    with store._conn() as c:
        c.execute(
            'INSERT INTO domains (domain, requested_by, purchased_at, '
            'updated_at) VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)',
            ('legacy.com', 'Slack:U_LEGACY_MDB'),
        )
    # Run init again — the backfill is part of init_db.
    store.init_db()
    row = tmp_inventory.get_domain('legacy.com')
    assert row['assigned_to'] == 'Slack:U_LEGACY_MDB'


def test_backfill_does_not_overwrite_existing_assigned_to(tmp_inventory):
    """assigned_to that's already set must NOT be clobbered by the backfill."""
    store.add_domain(
        domain='already-owned.com',
        requested_by='Slack:U_OLD_REQUESTER',
        assigned_to='U_NEW_OWNER',
    )
    store.init_db()
    row = tmp_inventory.get_domain('already-owned.com')
    assert row['assigned_to'] == 'U_NEW_OWNER'


def test_add_domain_accepts_assigned_to_kwarg(tmp_inventory):
    """Path B confirm_purchased passes assigned_to=requester now —
    verify the new add_domain signature stores it."""
    store.add_domain(
        domain='new.com', vertical='auto-insurance',
        requested_by='Slack:U_REQ', assigned_to='U_MDB',
    )
    row = tmp_inventory.get_domain('new.com')
    assert row['assigned_to'] == 'U_MDB'


# ─── set_lifecycle_state / bump_last_prompted_at ──────────────────────────

def test_set_lifecycle_state_persists_value(tmp_inventory):
    store.add_domain(domain='ex.com')
    store.set_lifecycle_state('ex.com', S.IDLE)
    assert tmp_inventory.get_domain('ex.com')['lifecycle_state'] == S.IDLE


def test_set_lifecycle_state_none_clears_column(tmp_inventory):
    """Passing None clears the column so the cron re-classifies fresh."""
    store.add_domain(domain='ex.com')
    store.set_lifecycle_state('ex.com', S.AWAITING_MDB_USAGE_RESPONSE)
    store.set_lifecycle_state('ex.com', None)
    assert tmp_inventory.get_domain('ex.com')['lifecycle_state'] is None


def test_bump_last_prompted_at_sets_timestamp(tmp_inventory):
    store.add_domain(domain='ex.com')
    assert tmp_inventory.get_domain('ex.com')['last_prompted_at'] is None
    store.bump_last_prompted_at('ex.com')
    assert tmp_inventory.get_domain('ex.com')['last_prompted_at'] is not None


# ─── update_namecheap_sync ────────────────────────────────────────────────

def test_update_namecheap_sync_persists_expiry_and_autorenew(tmp_inventory):
    store.add_domain(domain='ex.com')
    expire = dt.datetime(2027, 1, 15, 12, 0, 0)
    store.update_namecheap_sync(
        'ex.com', expire_at=expire, auto_renew_enabled=False,
    )
    row = tmp_inventory.get_domain('ex.com')
    # SQLite returns timestamps as ISO strings — just verify the year/month
    # made it through the round-trip.
    assert '2027' in str(row['expire_at'])
    # SQLite stores BOOLEAN as INTEGER 0/1.
    assert row['auto_renew_enabled'] in (0, False)
    assert row['last_namecheap_sync_at'] is not None


# ─── mark_active / assign_to ───────────────────────────────────────────────

def test_mark_active_stamps_last_active_at(tmp_inventory):
    store.add_domain(domain='ex.com')
    store.mark_active('ex.com')
    assert tmp_inventory.get_domain('ex.com')['last_active_at'] is not None


def test_assign_to_set_and_clear(tmp_inventory):
    store.add_domain(domain='ex.com')
    store.assign_to('ex.com', 'U_NEW')
    assert tmp_inventory.get_domain('ex.com')['assigned_to'] == 'U_NEW'
    store.assign_to('ex.com', None)
    assert tmp_inventory.get_domain('ex.com')['assigned_to'] is None


# ─── domain_events ─────────────────────────────────────────────────────────

def test_add_domain_writes_added_event_when_event_source_set(tmp_inventory):
    """Path B + HTTP API should produce an audit event so /domain-history
    can show how the domain entered inventory."""
    store.add_domain(
        domain='new.com', vertical='Auto Insurance',
        requested_by='Slack:U_NEERAJ',
        event_source='path_b_mark_purchased',
        event_metadata={'lander_url': 'https://x/y/'},
    )
    events = store.list_domain_events('new.com')
    assert len(events) == 1
    e = events[0]
    assert e['event_type'] == 'added'
    assert e['actor'] == 'Slack:U_NEERAJ'
    assert e['metadata']['source'] == 'path_b_mark_purchased'
    assert e['metadata']['lander_url'] == 'https://x/y/'


def test_add_domain_writes_no_event_when_event_source_none(tmp_inventory):
    """CSV bulk imports + tests pass event_source=None to avoid writing
    743 events with the same timestamp at boot."""
    store.add_domain(domain='quiet.com', vertical='Auto Insurance')
    events = store.list_domain_events('quiet.com')
    assert events == []


def test_add_domain_event_failure_does_not_break_insert(tmp_inventory, monkeypatch):
    """Audit event is best-effort — if record_event raises, the row
    still ends up in the domains table."""
    def boom(*args, **kwargs):
        raise RuntimeError('synthetic event-write failure')
    monkeypatch.setattr(store, 'record_event', boom)
    new_id = store.add_domain(
        domain='resilient.com', vertical='auto',
        event_source='path_b_mark_purchased',
    )
    assert new_id is not None
    # Domain row exists even though the event write blew up.
    assert tmp_inventory.get_domain('resilient.com') is not None


def test_record_event_writes_row(tmp_inventory):
    store.record_event(
        'ex.com', 'renewed',
        actor='U_UTKARSH',
        from_state=S.AWAITING_UTKARSH_RENEW,
        to_state=S.RENEWED,
        metadata={'task_id': 'abc-123'},
    )
    events = store.list_domain_events('ex.com')
    assert len(events) == 1
    e = events[0]
    assert e['event_type'] == 'renewed'
    assert e['actor'] == 'U_UTKARSH'
    assert e['from_state'] == S.AWAITING_UTKARSH_RENEW
    assert e['to_state'] == S.RENEWED
    assert e['metadata'] == {'task_id': 'abc-123'}


def test_record_event_handles_null_metadata(tmp_inventory):
    store.record_event('ex.com', 'assigned', actor='cron')
    events = store.list_domain_events('ex.com')
    assert len(events) == 1
    assert events[0]['metadata'] is None


def test_list_domain_events_returns_newest_first(tmp_inventory):
    store.record_event('ex.com', 'first', actor='cron')
    store.record_event('ex.com', 'second', actor='cron')
    store.record_event('ex.com', 'third', actor='cron')
    events = store.list_domain_events('ex.com')
    # Newest first — third should be on top.
    assert [e['event_type'] for e in events] == ['third', 'second', 'first']


def test_list_domain_events_filters_by_domain(tmp_inventory):
    store.record_event('a.com', 'evt-a', actor='cron')
    store.record_event('b.com', 'evt-b', actor='cron')
    a_events = store.list_domain_events('a.com')
    assert len(a_events) == 1
    assert a_events[0]['event_type'] == 'evt-a'


# ─── list_domains_for_lifecycle (excludes AWAITING_*) ─────────────────────

def test_list_domains_for_lifecycle_excludes_awaiting_states(tmp_inventory):
    """Cron must not re-touch domains waiting on a human click."""
    store.add_domain(domain='active.com')
    store.add_domain(domain='waiting.com')
    store.set_lifecycle_state('waiting.com', S.AWAITING_MDB_USAGE_RESPONSE)

    rows = store.list_domains_for_lifecycle(exclude_states=S.AWAITING_STATES)
    domains = {r['domain'] for r in rows}
    assert 'active.com' in domains
    assert 'waiting.com' not in domains


def test_list_domains_for_lifecycle_includes_null_state(tmp_inventory):
    """Fresh domains have NULL lifecycle_state — they must be picked up
    by the classifier on the first pass even when exclude_states is set."""
    store.add_domain(domain='fresh.com')
    rows = store.list_domains_for_lifecycle(exclude_states=S.AWAITING_STATES)
    assert any(r['domain'] == 'fresh.com' for r in rows)


# ─── get_domains_due_for_namecheap_sync ────────────────────────────────────

def test_namecheap_sync_picks_never_synced(tmp_inventory):
    store.add_domain(domain='never-synced.com')
    due = store.get_domains_due_for_namecheap_sync(limit=10)
    assert any(r['domain'] == 'never-synced.com' for r in due)


def test_namecheap_sync_skips_recently_synced(tmp_inventory):
    """A domain synced 1h ago shouldn't appear in 'due' list (unless
    near-expiry, which we control by leaving expire_at NULL)."""
    store.add_domain(domain='recent.com')
    store.update_namecheap_sync(
        'recent.com',
        expire_at=dt.datetime.now() + dt.timedelta(days=365),  # far away
        auto_renew_enabled=True,
    )
    due = store.get_domains_due_for_namecheap_sync(
        limit=10, max_age_days=7, near_expiry_days=60,
    )
    assert all(r['domain'] != 'recent.com' for r in due)


def test_namecheap_sync_picks_near_expiry_even_if_recently_synced(tmp_inventory):
    """Domain synced minutes ago but expiring next week should still be
    refreshed — its expiry is critical, the cron runs daily, and the
    cost of a re-sync is one extra HTTP call."""
    store.add_domain(domain='soon-expire.com')
    store.update_namecheap_sync(
        'soon-expire.com',
        expire_at=dt.datetime.now() + dt.timedelta(days=5),  # near
        auto_renew_enabled=True,
    )
    due = store.get_domains_due_for_namecheap_sync(
        limit=10, max_age_days=7, near_expiry_days=60,
    )
    assert any(r['domain'] == 'soon-expire.com' for r in due)


def test_namecheap_sync_respects_limit(tmp_inventory):
    for i in range(10):
        store.add_domain(domain=f'ex{i}.com')
    due = store.get_domains_due_for_namecheap_sync(limit=3)
    assert len(due) == 3
