"""Tests for the lifecycle helpers added to inventory.store in Phase A.

These exercise the new schema columns (assigned_to, expire_at,
auto_renew_enabled, last_active_at, last_prompted_at,
last_namecheap_sync_at, lifecycle_state) and the domain_events history
table.
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


def test_update_namecheap_sync_overwrites_purchased_at_when_given(tmp_inventory):
    """When Namecheap returns a CreatedDate, purchased_at is overwritten
    with the real registration date (legacy rows carry the import date)."""
    store.add_domain(domain='ex.com')  # stamps purchased_at = NOW()
    real_reg = dt.datetime(2021, 3, 9, 0, 0, 0)
    store.update_namecheap_sync(
        'ex.com', expire_at=dt.datetime(2027, 1, 1),
        auto_renew_enabled=None, purchased_at=real_reg,
    )
    row = tmp_inventory.get_domain('ex.com')
    assert '2021-03-09' in str(row['purchased_at'])


def test_update_namecheap_sync_leaves_purchased_at_when_none(tmp_inventory):
    """When CreatedDate is absent (purchased_at=None), the existing
    purchased_at is left untouched — NOT nulled."""
    store.add_domain(domain='ex.com')
    before = tmp_inventory.get_domain('ex.com')['purchased_at']
    store.update_namecheap_sync(
        'ex.com', expire_at=dt.datetime(2027, 1, 1),
        auto_renew_enabled=None,  # purchased_at defaults to None
    )
    after = tmp_inventory.get_domain('ex.com')['purchased_at']
    assert after == before


def test_update_namecheap_sync_failed_fetch_preserves_expire_at(tmp_inventory):
    """Regression (2026-05-14): the backfill's 'unknown' path calls this
    with everything None just to bump the sync timestamp. A transient
    Namecheap failure must NOT null out a previously-good expire_at."""
    store.add_domain(domain='ex.com')
    store.update_namecheap_sync(
        'ex.com', expire_at=dt.datetime(2027, 6, 1), auto_renew_enabled=None,
    )
    assert '2027' in str(tmp_inventory.get_domain('ex.com')['expire_at'])

    # Simulate the 'unknown' path — fetch failed, everything None.
    store.update_namecheap_sync(
        'ex.com', expire_at=None, auto_renew_enabled=None,
    )
    row = tmp_inventory.get_domain('ex.com')
    # expire_at survives; only the sync timestamp moved.
    assert '2027' in str(row['expire_at'])
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


# ─── Phase F — atomic state guard + prompt fan-out ledger ─────────────────

def test_transition_lifecycle_state_atomic(tmp_inventory):
    """transition_lifecycle_state moves the row only if it's still in
    the expected from_state — the DB-level first-click-wins guard."""
    store.add_domain(domain='d.com')
    store.set_lifecycle_state('d.com', S.AWAITING_MDB_INVENTORY_RESPONSE)

    # Wins from the matching state.
    assert store.transition_lifecycle_state(
        'd.com', S.AWAITING_MDB_INVENTORY_RESPONSE, S.INVENTORY) is True
    assert tmp_inventory.get_domain('d.com')['lifecycle_state'] == S.INVENTORY

    # A second attempt from the now-stale original state loses, and the
    # row is left exactly as the winner set it.
    assert store.transition_lifecycle_state(
        'd.com', S.AWAITING_MDB_INVENTORY_RESPONSE, S.EXTENDED_30) is False
    assert tmp_inventory.get_domain('d.com')['lifecycle_state'] == S.INVENTORY


def test_transition_lifecycle_state_from_null(tmp_inventory):
    """from_state=None is NULL-safe — matches a freshly-classified row."""
    store.add_domain(domain='d.com')  # lifecycle_state starts NULL
    assert store.transition_lifecycle_state(
        'd.com', None, S.ACTIVE) is True
    assert tmp_inventory.get_domain('d.com')['lifecycle_state'] == S.ACTIVE
    # Re-running from None now loses (row is no longer NULL).
    assert store.transition_lifecycle_state('d.com', None, S.IDLE) is False


def test_prompt_recipients_round_trip(tmp_inventory):
    """record / get / clear the fan-out ledger; re-recording replaces."""
    store.add_domain(domain='d.com')
    recips = [
        {'recipient_slack_id': 'U_A', 'channel_id': 'D_A',
         'message_ts': '1.1', 'is_tl': False},
        {'recipient_slack_id': 'U_TL', 'channel_id': 'D_TL',
         'message_ts': '2.2', 'is_tl': True},
    ]
    store.record_prompt_recipients('d.com', recips)
    got = store.get_prompt_recipients('d.com')
    assert {r['recipient_slack_id'] for r in got} == {'U_A', 'U_TL'}

    # Re-recording is DELETE + INSERT — it replaces, never appends.
    store.record_prompt_recipients('d.com', recips[:1])
    assert len(store.get_prompt_recipients('d.com')) == 1

    store.clear_prompt_recipients('d.com')
    assert store.get_prompt_recipients('d.com') == []
