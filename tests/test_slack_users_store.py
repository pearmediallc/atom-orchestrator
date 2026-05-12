"""Tests for the Phase E slack_users + domain_assignments store helpers.

Pure SQL behaviour — no Slack client, no HTTP. Each test runs against
the autouse tmp SQLite via the conftest fixture.
"""
import json
import pytest

from inventory import store


# ─── slack_users — UPSERT + lookup + alias ────────────────────────────────

def test_upsert_inserts_then_updates(tmp_inventory):
    """Two calls with the same slack_user_id → row exists once,
    last_synced_at advances on the second call."""
    store.upsert_slack_user('U_ABC', 'Old Name', 'oldhandle',
                            'old@pearmediallc.com')
    row1 = store.get_slack_user('U_ABC')
    assert row1['real_name'] == 'Old Name'
    assert row1['email'] == 'old@pearmediallc.com'
    first_synced = row1['last_synced_at']

    store.upsert_slack_user('U_ABC', 'New Name', 'newhandle',
                            'new@pearmediallc.com')
    row2 = store.get_slack_user('U_ABC')
    assert row2['real_name'] == 'New Name'
    assert row2['email'] == 'new@pearmediallc.com'
    # first_seen_at should NOT have changed
    assert row2['first_seen_at'] == row1['first_seen_at']


def test_upsert_handles_none_email(tmp_inventory):
    """Users without an email (e.g. ones who haven't completed setup)
    should still upsert cleanly, with email = NULL."""
    store.upsert_slack_user('U_NOEMAIL', 'No Email', 'no_email', None)
    row = store.get_slack_user('U_NOEMAIL')
    assert row['email'] is None


def test_mark_deleted_does_not_remove_row(tmp_inventory):
    """Soft delete — the row stays so old domain_assignments references
    remain valid for audit history."""
    store.upsert_slack_user('U_QUIT', 'Quit Person', 'q', 'q@pearmediallc.com')
    store.mark_slack_user_deleted('U_QUIT')
    row = store.get_slack_user('U_QUIT')
    assert row is not None
    # SQLite stores BOOLEAN as INTEGER 0/1
    assert row['deleted'] in (1, True)


def test_lookup_by_alias_exact_real_name(tmp_inventory):
    store.upsert_slack_user('U_ANUS', 'Anusree Madhu', 'anusree',
                            'anusree.madhu@pearmediallc.com')
    assert store.lookup_slack_id_by_alias('Anusree Madhu') == 'U_ANUS'
    # Case-insensitive
    assert store.lookup_slack_id_by_alias('ANUSREE MADHU') == 'U_ANUS'
    # Whitespace tolerant
    assert store.lookup_slack_id_by_alias('  anusree madhu  ') == 'U_ANUS'


def test_lookup_by_alias_uses_alias_array(tmp_inventory):
    """Once we add 'Anushree Madhu' (typo) as an alias for Anusree,
    next lookup with the typo should hit instantly."""
    store.upsert_slack_user('U_ANUS', 'Anusree Madhu', 'anusree',
                            'anusree.madhu@pearmediallc.com')
    # Initial lookup of the typo: misses
    assert store.lookup_slack_id_by_alias('Anushree Madhu') is None

    store.add_alias_to_slack_user('U_ANUS', 'Anushree Madhu')

    # Second lookup: resolved via alias map
    assert store.lookup_slack_id_by_alias('Anushree Madhu') == 'U_ANUS'


def test_add_alias_idempotent(tmp_inventory):
    """Adding the same alias twice should be a no-op — no duplicate in
    the JSON array."""
    store.upsert_slack_user('U_X', 'X', 'x', 'x@pearmediallc.com')
    store.add_alias_to_slack_user('U_X', 'Xxx')
    store.add_alias_to_slack_user('U_X', 'Xxx')          # same
    store.add_alias_to_slack_user('U_X', 'XXX')          # same, different case
    row = store.get_slack_user('U_X')
    aliases = json.loads(row['name_aliases'])
    assert aliases == ['Xxx']


def test_add_alias_to_missing_user_silently_no_ops(tmp_inventory):
    """Calling add_alias for an unknown user shouldn't crash — just
    silently does nothing. Useful so backfill code can stay simple."""
    store.add_alias_to_slack_user('U_MISSING', 'Some Name')  # no exception


def test_lookup_returns_none_on_unknown(tmp_inventory):
    assert store.lookup_slack_id_by_alias('nobody here') is None
    assert store.lookup_slack_id_by_alias('') is None
    assert store.lookup_slack_id_by_alias('   ') is None


def test_list_slack_users_excludes_deleted_by_default(tmp_inventory):
    store.upsert_slack_user('U_A', 'A Active', 'a', 'a@pearmediallc.com')
    store.upsert_slack_user('U_B', 'B Quit', 'b', 'b@pearmediallc.com')
    store.mark_slack_user_deleted('U_B')

    active = store.list_slack_users()
    ids = {u['slack_user_id'] for u in active}
    assert 'U_A' in ids
    assert 'U_B' not in ids

    everyone = store.list_slack_users(include_deleted=True)
    ids_all = {u['slack_user_id'] for u in everyone}
    assert 'U_A' in ids_all
    assert 'U_B' in ids_all


# ─── domain_assignments — assign / end / list ─────────────────────────────

def test_assign_domain_inserts_active_row(tmp_inventory):
    store.upsert_slack_user('U_MDB', 'Test MDB', 't', 't@pearmediallc.com')
    new_id = store.assign_domain('ex.com', 'U_MDB',
                                  assigned_by='cron', notes='initial backfill')
    assert new_id > 0
    current = store.current_assignments_for_domain('ex.com')
    assert len(current) == 1
    assert current[0]['slack_user_id'] == 'U_MDB'
    assert current[0]['real_name'] == 'Test MDB'    # JOINed
    assert current[0]['notes'] == 'initial backfill'


def test_assign_multiple_keeps_them_all_active(tmp_inventory):
    """Multi-MDB by default — assign_domain without end_others keeps
    every existing active assignment."""
    for uid in ('U_A', 'U_B', 'U_C'):
        store.upsert_slack_user(uid, f'name {uid}', uid.lower(),
                                f'{uid.lower()}@pearmediallc.com')
        store.assign_domain('ex.com', uid)
    current = store.current_assignments_for_domain('ex.com')
    assert {a['slack_user_id'] for a in current} == {'U_A', 'U_B', 'U_C'}


def test_assign_with_end_others_replaces(tmp_inventory):
    """end_others=True closes existing assignments first — exclusive
    single-MDB mode for /reassign-domain's 'transfer' semantics."""
    for uid in ('U_OLD1', 'U_OLD2'):
        store.upsert_slack_user(uid, f'name {uid}', uid.lower(),
                                f'{uid.lower()}@pearmediallc.com')
        store.assign_domain('ex.com', uid)
    store.upsert_slack_user('U_NEW', 'New MDB', 'new', 'new@pearmediallc.com')
    store.assign_domain('ex.com', 'U_NEW', end_others=True)

    current = store.current_assignments_for_domain('ex.com')
    assert [a['slack_user_id'] for a in current] == ['U_NEW']

    # History preserved
    history = store.list_assignments('ex.com')
    assert {h['slack_user_id'] for h in history} == \
        {'U_OLD1', 'U_OLD2', 'U_NEW'}


def test_end_assignment_marks_specific_user_inactive(tmp_inventory):
    store.upsert_slack_user('U_KEEP', 'Keep', 'k', 'k@pearmediallc.com')
    store.upsert_slack_user('U_GONE', 'Gone', 'g', 'g@pearmediallc.com')
    store.assign_domain('ex.com', 'U_KEEP')
    store.assign_domain('ex.com', 'U_GONE')

    rowcount = store.end_assignment('ex.com', 'U_GONE', by='U_TL')
    assert rowcount == 1

    current = store.current_assignments_for_domain('ex.com')
    assert [a['slack_user_id'] for a in current] == ['U_KEEP']


def test_list_assignments_returns_all_history_newest_first(tmp_inventory):
    store.upsert_slack_user('U_A', 'A', 'a', 'a@pearmediallc.com')
    store.upsert_slack_user('U_B', 'B', 'b', 'b@pearmediallc.com')
    store.assign_domain('ex.com', 'U_A')
    store.end_assignment('ex.com', 'U_A')
    store.assign_domain('ex.com', 'U_B')
    history = store.list_assignments('ex.com')
    # Most recent first
    assert [h['slack_user_id'] for h in history] == ['U_B', 'U_A']


def test_current_assignments_empty_for_unknown_domain(tmp_inventory):
    assert store.current_assignments_for_domain('never-seen.com') == []
    assert store.list_assignments('never-seen.com') == []
