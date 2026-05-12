"""Tests for the Phase E.4 dual-read mechanism — code that prefers
domain_assignments but falls back to legacy domains.assigned_to during
the migration window.
"""
import pytest

from inventory import store
from lifecycle import dm as _dm
from lifecycle import classifier
from lifecycle import states as S


# ─── get_mdb_slack_ids_for_domain ────────────────────────────────────────

def test_helper_prefers_domain_assignments(tmp_inventory):
    """When the new table has data, use it. Legacy column is ignored."""
    store.add_domain(domain='ex.com', assigned_to='LegacyName')
    store.upsert_slack_user('U_NEW', 'New MDB', 'new', 'new@pearmediallc.com')
    store.assign_domain('ex.com', 'U_NEW')

    ids = _dm.get_mdb_slack_ids_for_domain('ex.com')
    assert ids == ['U_NEW']


def test_helper_returns_all_active_assignees(tmp_inventory):
    """Multi-MDB: returns every active row for the domain."""
    store.add_domain(domain='ex.com')
    for uid in ('U_A', 'U_B', 'U_C'):
        store.upsert_slack_user(uid, f'name {uid}', uid.lower(),
                                f'{uid.lower()}@pearmediallc.com')
        store.assign_domain('ex.com', uid)
    ids = _dm.get_mdb_slack_ids_for_domain('ex.com')
    assert set(ids) == {'U_A', 'U_B', 'U_C'}


def test_helper_skips_deleted_users(tmp_inventory):
    """Soft-deleted Slack users (left the workspace) are filtered out."""
    store.add_domain(domain='ex.com')
    store.upsert_slack_user('U_LEFT', 'Left Person', 'left',
                            'left@pearmediallc.com')
    store.upsert_slack_user('U_STAY', 'Stayed', 'stay',
                            'stay@pearmediallc.com')
    store.assign_domain('ex.com', 'U_LEFT')
    store.assign_domain('ex.com', 'U_STAY')
    store.mark_slack_user_deleted('U_LEFT')

    ids = _dm.get_mdb_slack_ids_for_domain('ex.com')
    assert ids == ['U_STAY']


def test_helper_falls_back_to_legacy_slack_id_format(tmp_inventory):
    """No domain_assignments rows. Legacy column holds a Slack ID
    directly (the Path B case before E.3 backfill). Use as-is."""
    store.add_domain(domain='ex.com', assigned_to='U_NEERAJ')
    ids = _dm.get_mdb_slack_ids_for_domain('ex.com')
    assert ids == ['U_NEERAJ']


def test_helper_falls_back_to_legacy_with_slack_prefix(tmp_inventory):
    """Legacy 'Slack:UXXX' format (from confirm_purchased) is normalised."""
    store.add_domain(domain='ex.com', assigned_to='Slack:U09U534JS2F')
    assert _dm.get_mdb_slack_ids_for_domain('ex.com') == ['U09U534JS2F']


def test_helper_falls_back_to_legacy_name_via_alias(tmp_inventory):
    """Legacy column has a human name. Helper resolves via slack_users
    real_name match."""
    store.upsert_slack_user('U_ANUS', 'Anusree Madhu', 'anusree',
                            'anusree.madhu@pearmediallc.com')
    store.add_domain(domain='ex.com', assigned_to='Anusree Madhu')
    assert _dm.get_mdb_slack_ids_for_domain('ex.com') == ['U_ANUS']


def test_helper_returns_empty_when_unresolvable(tmp_inventory):
    """No assignments, no legacy, OR legacy is an unresolvable name —
    return []. Domain treated as unassigned (inventory pool)."""
    store.add_domain(domain='nobody.com', assigned_to=None)
    assert _dm.get_mdb_slack_ids_for_domain('nobody.com') == []

    store.add_domain(domain='unknown.com', assigned_to='Some Random Person')
    assert _dm.get_mdb_slack_ids_for_domain('unknown.com') == []


def test_helper_handles_missing_row(tmp_inventory):
    """Domain not in inventory at all — helper doesn't crash."""
    assert _dm.get_mdb_slack_ids_for_domain('never-existed.com') == []


# ─── bulk_current_assignments ────────────────────────────────────────────

def test_bulk_groups_by_domain(tmp_inventory):
    store.upsert_slack_user('U_A', 'A', 'a', 'a@pearmediallc.com')
    store.upsert_slack_user('U_B', 'B', 'b', 'b@pearmediallc.com')
    store.add_domain(domain='one.com')
    store.add_domain(domain='two.com')
    store.assign_domain('one.com', 'U_A')
    store.assign_domain('one.com', 'U_B')
    store.assign_domain('two.com', 'U_A')

    out = store.bulk_current_assignments()
    assert set(out['one.com']) == {'U_A', 'U_B'}
    assert out['two.com'] == ['U_A']


def test_bulk_excludes_ended_assignments(tmp_inventory):
    store.upsert_slack_user('U_X', 'X', 'x', 'x@pearmediallc.com')
    store.add_domain(domain='ex.com')
    store.assign_domain('ex.com', 'U_X')
    store.end_assignment('ex.com', 'U_X')
    out = store.bulk_current_assignments()
    assert 'ex.com' not in out


def test_bulk_excludes_deleted_users(tmp_inventory):
    """Domains whose only assignee is now deleted should not appear in
    bulk_current_assignments — they're effectively unassigned."""
    store.upsert_slack_user('U_GONE', 'Gone', 'g', 'g@pearmediallc.com')
    store.add_domain(domain='orphan.com')
    store.assign_domain('orphan.com', 'U_GONE')
    store.mark_slack_user_deleted('U_GONE')
    out = store.bulk_current_assignments()
    assert 'orphan.com' not in out


# ─── classifier respects new schema ──────────────────────────────────────

def test_classifier_treats_assigned_via_new_schema(tmp_inventory):
    """Even if legacy assigned_to is empty, presence of an active
    assignment makes the classifier treat the row as assigned."""
    row = {
        'domain': 'ex.com',
        'lifecycle_state': None,
        'assigned_to': None,             # legacy empty
        'expire_at': None,
        'last_active_at': None,
        'purchased_at': None,
    }
    new_state = classifier.classify_domain(
        row, {}, current_assignees=['U_NEW'],
    )
    # Has assignee AND no spend AND no purchased_at to anchor grace →
    # classifier returns None (skip — defensive)
    assert new_state is None  # not INVENTORY


def test_classifier_unassigned_when_both_empty(tmp_inventory):
    """No new assignees AND no legacy → INVENTORY."""
    row = {
        'domain': 'ex.com', 'lifecycle_state': None,
        'assigned_to': None, 'expire_at': None,
        'last_active_at': None, 'purchased_at': None,
    }
    new_state = classifier.classify_domain(
        row, {}, current_assignees=[],
    )
    assert new_state == S.INVENTORY


def test_classifier_falls_back_to_legacy_when_new_empty(tmp_inventory):
    """During migration: new table empty for this row, but legacy
    column has data → treated as assigned (don't push to INVENTORY)."""
    import datetime as dt
    row = {
        'domain': 'ex.com', 'lifecycle_state': None,
        'assigned_to': 'U_LEGACY',       # legacy populated
        'expire_at': None,
        'last_active_at': None,
        'purchased_at': dt.datetime(2025, 1, 1),  # past grace
    }
    new_state = classifier.classify_domain(
        row, {}, current_assignees=[],  # new schema empty
    )
    # No spend + past grace → IDLE (since "assigned" per legacy)
    assert new_state == S.IDLE


# ─── list_domains_with_no_active_assignment ──────────────────────────────

def test_pool_excludes_rows_with_active_assignments(tmp_inventory):
    store.upsert_slack_user('U_X', 'X', 'x', 'x@pearmediallc.com')
    store.add_domain(domain='taken.com')
    store.assign_domain('taken.com', 'U_X')
    store.add_domain(domain='free.com')

    pool = store.list_domains_with_no_active_assignment()
    domains = {r['domain'] for r in pool}
    assert 'free.com' in domains
    assert 'taken.com' not in domains


def test_pool_excludes_rows_with_legacy_assigned_to(tmp_inventory):
    """Migration safety: a row with legacy assigned_to set (but no new
    schema entry) must NOT appear in the inventory pool."""
    store.add_domain(domain='legacy-owned.com', assigned_to='Neeraj')
    pool = store.list_domains_with_no_active_assignment()
    assert all(r['domain'] != 'legacy-owned.com' for r in pool)
