"""Tests for lifecycle.backfill_assignments — the legacy assigned_to → domain_assignments migration.

Most coverage focuses on the resolver since that's where every edge case lives.
End-to-end integration test verifies the apply-path writes the right
rows + aliases.
"""
import pytest

from inventory import store
from lifecycle import backfill_assignments as bf


# ─── Helpers ─────────────────────────────────────────────────────────────

@pytest.fixture
def populated_slack_users(tmp_inventory):
    """Tmp DB with a handful of Slack users that mimic the real
    workspace pattern (Pearmedia employees + 1 first-name collision)."""
    for uid, real, display, email in [
        ('U_ANUS', 'Anusree Madhu',  'anusree',  'anusree@pearmediallc.com'),
        ('U_RAJAT', 'Rajat Grover',  'rajat',    'rajat@pearmediallc.com'),
        ('U_AKAR', 'Akarshann Wadhawan', 'akarshann', 'akarshann@pearmediallc.com'),
        ('U_RAHUL1', 'Rahul Chawla', 'rahul.c',  'rahul.chawla@pearmediallc.com'),
        ('U_RAHUL2', 'Rahul Kumar',  'rahul.k',  'rahul.kumar@pearmediallc.com'),
        ('U_UTKAR', 'Utkarsh Mishra', 'utkarsh', 'utkarsh@pearmediallc.com'),
    ]:
        store.upsert_slack_user(uid, real, display, email)
    return store


# ─── _looks_like_slack_id ────────────────────────────────────────────────

@pytest.mark.parametrize('val,expected', [
    ('U09U534JS2F',   True),
    ('U0A0A9KT65R',   True),
    ('Slack:U09U534JS2F', True),
    ('Anusree Madhu', False),
    ('U',             False),
    ('U12345',        False),       # too short
    ('user123',       False),       # doesn't start with U
])
def test_looks_like_slack_id(val, expected):
    assert bf._looks_like_slack_id(val) is expected


# ─── _split_multi_mdb ────────────────────────────────────────────────────

@pytest.mark.parametrize('value,expected', [
    ('Anusree Madhu',                    ['Anusree Madhu']),
    ('Nitin, Neeraj, Tanish',            ['Nitin', 'Neeraj', 'Tanish']),
    ('Sagar Rana, Tarushi',              ['Sagar Rana', 'Tarushi']),
    ('yash/ujjwala',                     ['yash', 'ujjwala']),
    ('Aryan,Ujjwala,Anusree',            ['Aryan', 'Ujjwala', 'Anusree']),
    ('  spaced ,   tokens  ',            ['spaced', 'tokens']),
    ('A and B',                          ['A', 'B']),
    ('A & B',                            ['A', 'B']),
    ('',                                 []),
])
def test_split_multi_mdb(value, expected):
    assert bf._split_multi_mdb(value) == expected


# ─── Resolver — every code path ──────────────────────────────────────────

def test_resolves_exact_match(populated_slack_users):
    pool = bf._build_fuzzy_pool(populated_slack_users.list_slack_users())
    idx = bf._build_firstname_index(populated_slack_users.list_slack_users())

    resolved, unresolved, aliases = bf.resolve_value(
        'Anusree Madhu', fuzzy_pool=pool, firstname_idx=idx,
    )
    assert resolved == ['U_ANUS']
    assert unresolved == []
    assert aliases == []  # exact match doesn't need alias caching


def test_resolves_already_slack_id_passes_through(populated_slack_users):
    pool = bf._build_fuzzy_pool(populated_slack_users.list_slack_users())
    idx = bf._build_firstname_index(populated_slack_users.list_slack_users())
    resolved, _, _ = bf.resolve_value(
        'U09U534JS2F', fuzzy_pool=pool, firstname_idx=idx,
    )
    assert resolved == ['U09U534JS2F']


def test_resolves_slack_prefix_format(populated_slack_users):
    pool = bf._build_fuzzy_pool(populated_slack_users.list_slack_users())
    idx = bf._build_firstname_index(populated_slack_users.list_slack_users())
    resolved, _, _ = bf.resolve_value(
        'Slack:U09U534JS2F', fuzzy_pool=pool, firstname_idx=idx,
    )
    assert resolved == ['U09U534JS2F']


def test_resolves_firstname_unique(populated_slack_users):
    """Single first name resolves to the only person with that first name.

    'anusree' actually matches the display_name 'anusree' → reaches the
    earlier exact_or_alias path. To exercise the firstname-unique path
    we need a token that's NOT a real_name OR display_name in the table,
    only a FIRST WORD of one. 'Mukund' fits — no Slack user has that as
    a display_name, but Utkarsh's real_name 'Utkarsh Mishra' doesn't
    start with it; we need to add a Mukund fixture user first."""
    store.upsert_slack_user('U_MUKUND', 'Mukund Arora', 'mukund_handle',
                            'mukund@pearmediallc.com')
    pool = bf._build_fuzzy_pool(store.list_slack_users())
    idx = bf._build_firstname_index(store.list_slack_users())

    resolved, unresolved, aliases = bf.resolve_value(
        'Mukund', fuzzy_pool=pool, firstname_idx=idx,
    )
    assert resolved == ['U_MUKUND']
    # First-name resolution caches the variant as an alias for next time
    assert aliases == [('U_MUKUND', 'Mukund')]


def test_firstname_ambiguous_does_not_resolve(populated_slack_users):
    """'Rahul' matches both Rahul Chawla and Rahul Kumar → don't guess."""
    pool = bf._build_fuzzy_pool(populated_slack_users.list_slack_users())
    idx = bf._build_firstname_index(populated_slack_users.list_slack_users())
    resolved, unresolved, _ = bf.resolve_value(
        'Rahul', fuzzy_pool=pool, firstname_idx=idx,
    )
    assert resolved == []
    assert unresolved == ['Rahul']


def test_fuzzy_resolves_typo(populated_slack_users):
    """'rahul chawala' (typo) should fuzzy-match Rahul Chawla."""
    pool = bf._build_fuzzy_pool(populated_slack_users.list_slack_users())
    idx = bf._build_firstname_index(populated_slack_users.list_slack_users())
    resolved, _, aliases = bf.resolve_value(
        'rahul chawala', fuzzy_pool=pool, firstname_idx=idx,
    )
    assert resolved == ['U_RAHUL1']
    # Typo gets cached as alias so future lookups skip fuzzy
    assert aliases == [('U_RAHUL1', 'rahul chawala')]


def test_fuzzy_ambiguous_does_not_resolve(populated_slack_users):
    """If the fuzzy threshold matches more than one full name, don't
    pick one. (Both Rahuls could fuzzy-match a borderline value.)"""
    pool = bf._build_fuzzy_pool(populated_slack_users.list_slack_users())
    idx = bf._build_firstname_index(populated_slack_users.list_slack_users())
    resolved, unresolved, _ = bf.resolve_value(
        'rahull', fuzzy_pool=pool, firstname_idx=idx,
    )
    # 'rahull' is too close to both Rahul Chawla AND Rahul Kumar → ambiguous,
    # OR it might match only firstname 'rahul' which is ambiguous in idx.
    # Either way it should not resolve.
    assert resolved == []


def test_non_mdb_label_skipped(populated_slack_users):
    pool = bf._build_fuzzy_pool(populated_slack_users.list_slack_users())
    idx = bf._build_firstname_index(populated_slack_users.list_slack_users())
    for label in ['Renew', 'Company domain renewal', 'advertiser', 'keitaro']:
        resolved, unresolved, _ = bf.resolve_value(
            label, fuzzy_pool=pool, firstname_idx=idx,
        )
        assert resolved == [], f'expected {label!r} to skip but got {resolved}'
        # The label itself is not in unresolved list — it's an explicit skip
        # (resolver returns ('non_mdb_label') which becomes resolved=[],
        # unresolved=[label] via the multi-split single-token path)
        # We allow it in unresolved list — caller filters later.


def test_multi_mdb_splits_and_resolves_each(populated_slack_users):
    """'Anusree Madhu, Rajat Grover, rahul chawala' → 3 Slack IDs."""
    pool = bf._build_fuzzy_pool(populated_slack_users.list_slack_users())
    idx = bf._build_firstname_index(populated_slack_users.list_slack_users())
    resolved, unresolved, _ = bf.resolve_value(
        'Anusree Madhu, Rajat Grover, rahul chawala',
        fuzzy_pool=pool, firstname_idx=idx,
    )
    assert set(resolved) == {'U_ANUS', 'U_RAJAT', 'U_RAHUL1'}
    assert unresolved == []


def test_multi_mdb_partial_resolution(populated_slack_users):
    """Three tokens, two resolve, one doesn't. The two get inserted,
    the one is reported as unresolved."""
    pool = bf._build_fuzzy_pool(populated_slack_users.list_slack_users())
    idx = bf._build_firstname_index(populated_slack_users.list_slack_users())
    resolved, unresolved, _ = bf.resolve_value(
        'Anusree Madhu, Babu, Rajat Grover',
        fuzzy_pool=pool, firstname_idx=idx,
    )
    assert set(resolved) == {'U_ANUS', 'U_RAJAT'}
    assert unresolved == ['Babu']


def test_resolved_dedupes_within_single_value(populated_slack_users):
    """If multi-token string mentions the same person twice, only one
    assignment row gets queued."""
    pool = bf._build_fuzzy_pool(populated_slack_users.list_slack_users())
    idx = bf._build_firstname_index(populated_slack_users.list_slack_users())
    resolved, _, _ = bf.resolve_value(
        'Rajat Grover, rajat grover',     # case variants
        fuzzy_pool=pool, firstname_idx=idx,
    )
    assert resolved == ['U_RAJAT']


def test_empty_value(populated_slack_users):
    pool = bf._build_fuzzy_pool(populated_slack_users.list_slack_users())
    idx = bf._build_firstname_index(populated_slack_users.list_slack_users())
    resolved, unresolved, _ = bf.resolve_value(
        '', fuzzy_pool=pool, firstname_idx=idx,
    )
    assert resolved == []
    assert unresolved == []


# ─── End-to-end with the actual store ─────────────────────────────────────

def test_alias_cache_makes_second_resolution_instant(populated_slack_users):
    """After resolving 'rahul chawala' once (via fuzzy), the alias is
    saved on U_RAHUL1. Next lookup of the same typo should hit the
    alias cache — exact_or_alias kind."""
    pool = bf._build_fuzzy_pool(populated_slack_users.list_slack_users())
    idx = bf._build_firstname_index(populated_slack_users.list_slack_users())

    resolved1, _, aliases = bf.resolve_value(
        'rahul chawala', fuzzy_pool=pool, firstname_idx=idx,
    )
    assert resolved1 == ['U_RAHUL1']
    for uid, alias in aliases:
        store.add_alias_to_slack_user(uid, alias)

    # Now lookup_slack_id_by_alias should find it directly
    assert store.lookup_slack_id_by_alias('rahul chawala') == 'U_RAHUL1'
