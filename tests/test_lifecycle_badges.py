"""Tests for lifecycle.badges.

Pure-function module: emoji + label lookup, plus the `:keyword` state
filter helper used by /list-domains.
"""
import pytest

from lifecycle import badges, states as S


# ─── emoji() / label() ────────────────────────────────────────────────────

@pytest.mark.parametrize('state,expected_emoji', [
    (None,                              '⚪'),
    ('',                                '⚪'),     # empty normalised to None
    (S.ACTIVE,                          '🟢'),
    (S.IDLE,                            '💤'),
    (S.INVENTORY,                       '📦'),
    (S.EXPIRING_30,                     '⚠️'),
    (S.EXPIRING_7,                      '🟠'),
    (S.EXPIRING_1,                      '🔴'),
    (S.EXPIRED,                         '❌'),
    (S.RENEWED,                         '✅'),
    (S.EXTENDED_30,                     '🔁'),
    (S.AWAITING_MDB_USAGE_RESPONSE,     '⏰'),
    (S.AWAITING_UTKARSH_RENEW,          '🔧'),
    (S.AWAITING_TL_OVERRIDE_USAGE,      '🚨'),
])
def test_emoji_for_known_states(state, expected_emoji):
    assert badges.emoji(state) == expected_emoji


def test_emoji_unknown_state_returns_dot():
    assert badges.emoji('SOMETHING_NEVER_DEFINED') == '·'


def test_label_returns_human_readable_for_each_state():
    """Every defined state has a readable label — sanity check that the
    emoji table and label table stay in sync as states are added."""
    for state in badges.LIFECYCLE_EMOJI:
        assert badges.label(state) != 'unknown', (
            f'state={state!r} has emoji but no label'
        )


def test_label_handles_empty_string_like_none():
    assert badges.label('') == badges.label(None)


# ─── state_filter() ───────────────────────────────────────────────────────

def test_state_filter_recognised_keyword_returns_predicate():
    pred = badges.state_filter(':expiring')
    assert pred is not None
    assert pred({'lifecycle_state': S.EXPIRING_14}) is True
    assert pred({'lifecycle_state': S.ACTIVE}) is False
    assert pred({'lifecycle_state': None}) is False


def test_state_filter_unknown_keyword_returns_none():
    """`/list-domains :nonsense` should fall back to substring search,
    so the helper must signal that with None."""
    assert badges.state_filter(':nonsense') is None
    assert badges.state_filter('plain-text') is None


def test_state_filter_strips_leading_colon_and_lowercases():
    """`:Expiring`, `expiring`, and `:EXPIRING` should all work."""
    p1 = badges.state_filter(':Expiring')
    p2 = badges.state_filter('EXPIRING')
    p3 = badges.state_filter(':expiring')
    row = {'lifecycle_state': S.EXPIRING_30}
    assert p1 is not None and p1(row)
    assert p2 is not None and p2(row)
    assert p3 is not None and p3(row)


def test_state_filter_awaiting_matches_all_awaiting_substates():
    """`:awaiting` must match every AWAITING_* state, including the new
    AWAITING_TL_OVERRIDE_* ones from Phase C."""
    pred = badges.state_filter(':awaiting')
    for state in S.AWAITING_STATES:
        assert pred({'lifecycle_state': state}) is True, (
            f'AWAITING state {state!r} not matched by :awaiting filter'
        )


def test_state_filter_idle_does_not_match_extended():
    """:idle is the FRESH idle state; EXTENDED_* (snoozed) is different."""
    pred = badges.state_filter(':idle')
    assert pred({'lifecycle_state': S.IDLE}) is True
    assert pred({'lifecycle_state': S.EXTENDED_30}) is False


def test_state_filter_snoozed_matches_extensions():
    pred = badges.state_filter(':snoozed')
    assert pred({'lifecycle_state': S.EXTENDED_30}) is True
    assert pred({'lifecycle_state': S.EXTENDED_15}) is True
    assert pred({'lifecycle_state': S.IDLE}) is False


def test_help_keywords_lists_all_keywords():
    """Help text shown to users when they typo a state filter must
    cover every keyword the filter recognises."""
    text = badges.help_keywords()
    for kw in ('expiring', 'idle', 'active', 'awaiting',
               'inventory', 'expired', 'renewed', 'snoozed'):
        assert f':{kw}' in text, f'help text missing :{kw}'
