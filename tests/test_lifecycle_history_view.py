"""Tests for lifecycle.history_view.render_timeline.

Pure renderer — no Slack client, no DB, no fixtures beyond plain dicts.
Each test asserts on the structure / text of the returned blocks. Slack
itself is then a thin layer over this.

Coverage:
  • not-in-inventory (helpful single block, no crash)
  • no events yet (current state shown, friendly message)
  • full timeline rendering (header, summary, divider, event rows, more-line)
  • each metadata flavour (spend dict, recent_cost, days_until_expiry,
    snooze_days, previous_assigned_to)
  • expiry urgency markers (>30d, ≤30d, ≤7d, expired)
  • Slack:UXXX prefix stripping in assigned_to display
  • timestamp coercion from string / datetime
  • event_emoji fallback for unknown event types
  • truncation when events > _MAX_EVENT_ROWS_PER_MESSAGE
"""
import datetime as dt
from typing import Optional

import pytest

from lifecycle import history_view, states as S


TODAY = dt.date(2026, 6, 1)


# ─── Helpers ───────────────────────────────────────────────────────────────

def _row(**overrides) -> dict:
    base = {
        'domain': 'example.com',
        'lifecycle_state': S.ACTIVE,
        'assigned_to': 'U_NEERAJ',
        'vertical': 'Auto Insurance',
        'expire_at': None,
        'last_active_at': None,
        'requested_by': 'Slack:U_NEERAJ',
    }
    base.update(overrides)
    return base


def _event(event_type: str, **overrides) -> dict:
    base = {
        'event_type': event_type,
        'occurred_at': dt.datetime(2026, 5, 20, 14, 30),
        'actor': 'cron',
        'from_state': None,
        'to_state': None,
        'metadata': None,
    }
    base.update(overrides)
    return base


def _all_text(blocks) -> str:
    """Flatten every text fragment in the blocks for substring assertions."""
    out = []
    for b in blocks:
        if b.get('type') == 'header':
            out.append(b['text']['text'])
        elif b.get('type') == 'section':
            out.append(b['text']['text'])
        elif b.get('type') == 'context':
            for el in b.get('elements', []):
                out.append(el.get('text', ''))
    return '\n'.join(out)


# ─── Not-in-inventory ─────────────────────────────────────────────────────

def test_not_in_inventory_renders_helpful_single_block():
    blocks = history_view.render_timeline(
        domain_row=None, events=[],
        requested_domain='ghost.com', today=TODAY,
    )
    assert len(blocks) == 1
    assert blocks[0]['type'] == 'section'
    text = blocks[0]['text']['text']
    assert 'ghost.com' in text
    assert 'not in our inventory' in text


# ─── No-events case ───────────────────────────────────────────────────────

def test_no_events_shows_current_state_and_friendly_message():
    blocks = history_view.render_timeline(
        domain_row=_row(), events=[], today=TODAY,
    )
    text = _all_text(blocks)
    # Header + state
    assert 'Timeline for example.com' in text
    assert 'active' in text  # state label
    # Friendly note
    assert 'No audit events recorded yet' in text


# ─── Header + summary block ───────────────────────────────────────────────

def test_summary_section_shows_assigned_user_with_prefix_stripped():
    """assigned_to='Slack:U_FOO' should render as <@U_FOO>, not <@Slack:U_FOO>."""
    blocks = history_view.render_timeline(
        domain_row=_row(assigned_to='Slack:U_NEERAJ'),
        events=[], today=TODAY,
    )
    text = _all_text(blocks)
    assert '<@U_NEERAJ>' in text
    assert 'Slack:U_NEERAJ' not in text


def test_summary_section_handles_unassigned():
    blocks = history_view.render_timeline(
        domain_row=_row(assigned_to=''), events=[], today=TODAY,
    )
    assert '_unassigned_' in _all_text(blocks)


def test_summary_shows_no_vertical_label_when_missing():
    blocks = history_view.render_timeline(
        domain_row=_row(vertical=None), events=[], today=TODAY,
    )
    assert '_no vertical_' in _all_text(blocks)


# ─── Expiry urgency markers ───────────────────────────────────────────────

@pytest.mark.parametrize('days,expected_marker', [
    (-3,  ':rotating_light: *expired*'),
    (1,   ':rotating_light: *1d left*'),
    (7,   ':rotating_light: *7d left*'),
    (8,   ':warning: 8d left'),
    (30,  ':warning: 30d left'),
    (31,  '(31d away)'),
    (180, '(180d away)'),
])
def test_expiry_urgency_marker_per_band(days, expected_marker):
    expire_date = TODAY + dt.timedelta(days=days)
    blocks = history_view.render_timeline(
        domain_row=_row(expire_at=dt.datetime(
            expire_date.year, expire_date.month, expire_date.day,
        )),
        events=[], today=TODAY,
    )
    text = _all_text(blocks)
    assert expected_marker in text


def test_no_expire_line_when_expire_at_missing():
    blocks = history_view.render_timeline(
        domain_row=_row(expire_at=None), events=[], today=TODAY,
    )
    text = _all_text(blocks)
    assert 'Expires:' not in text


def test_last_active_line_when_present():
    blocks = history_view.render_timeline(
        domain_row=_row(last_active_at=dt.datetime(2026, 5, 25)),
        events=[], today=TODAY,
    )
    text = _all_text(blocks)
    assert 'Last active' in text
    assert '2026-05-25' in text
    assert '7d ago' in text


# ─── Event row formatting ─────────────────────────────────────────────────

def test_event_row_shows_emoji_timestamp_actor_and_transition():
    ev = _event(
        'pushed_to_inventory',
        occurred_at=dt.datetime(2026, 5, 20, 14, 30),
        actor='U_ANUSHREE',
        from_state=S.AWAITING_MDB_INVENTORY_RESPONSE,
        to_state=S.INVENTORY,
    )
    blocks = history_view.render_timeline(
        domain_row=_row(), events=[ev], today=TODAY,
    )
    text = _all_text(blocks)
    assert '📦' in text
    assert '2026-05-20 14:30' in text
    assert 'pushed_to_inventory' in text
    assert '<@U_ANUSHREE>' in text
    assert 'awaiting MDB (inventory)' in text
    assert 'inventory pool' in text


def test_event_row_marks_cron_actor_distinctly():
    ev = _event('classified_active', actor='cron')
    blocks = history_view.render_timeline(
        domain_row=_row(), events=[ev], today=TODAY,
    )
    text = _all_text(blocks)
    assert '_cron_' in text
    assert '<@cron>' not in text  # we don't want it tagged as a user


def test_event_row_handles_missing_actor():
    ev = _event('classified_active', actor=None)
    blocks = history_view.render_timeline(
        domain_row=_row(), events=[ev], today=TODAY,
    )
    assert '_system_' in _all_text(blocks)


def test_event_row_omits_transition_when_no_from_to():
    """Some events don't move states (e.g. metadata-only audit logs).
    Don't render a misleading 'unknown → unknown' transition row."""
    ev = _event('contradiction_alert', from_state=None, to_state=None)
    blocks = history_view.render_timeline(
        domain_row=_row(), events=[ev], today=TODAY,
    )
    text = _all_text(blocks)
    assert '→' not in text  # no transition arrow


def test_unknown_event_type_falls_back_to_dot_emoji():
    """Future event types added without updating the badge map must
    not crash the slash command — render with a neutral '·'."""
    ev = _event('some_future_event_type', actor='cron',
                from_state=S.ACTIVE, to_state=S.IDLE)
    blocks = history_view.render_timeline(
        domain_row=_row(), events=[ev], today=TODAY,
    )
    text = _all_text(blocks)
    assert '·' in text
    assert 'some_future_event_type' in text


# ─── Metadata flavours ────────────────────────────────────────────────────

def test_metadata_spend_dict_renders_cost_and_revenue():
    ev = _event('classified_active', metadata={
        'spend': {'cost': 147.22, 'revenue': 540.0, 'clicks': 1234},
    })
    text = _all_text(history_view.render_timeline(
        domain_row=_row(), events=[ev], today=TODAY,
    ))
    assert 'spend $147.22' in text
    assert 'rev $540.00' in text


def test_metadata_recent_cost_renders_for_contradiction_guard():
    ev = _event('mdb_no_but_recent_spend', metadata={'recent_cost': 250.0})
    text = _all_text(history_view.render_timeline(
        domain_row=_row(), events=[ev], today=TODAY,
    ))
    assert 'recent 7d spend $250.00' in text


def test_metadata_days_until_expiry_renders():
    ev = _event('prompted_mdb_usage', metadata={'days_until_expiry': '14'})
    text = _all_text(history_view.render_timeline(
        domain_row=_row(), events=[ev], today=TODAY,
    ))
    assert '14d to expiry' in text


def test_metadata_snooze_days_renders():
    ev = _event('mdb_extended_30', metadata={'snooze_days': 30})
    text = _all_text(history_view.render_timeline(
        domain_row=_row(), events=[ev], today=TODAY,
    ))
    assert 'snoozed 30d' in text


def test_metadata_previous_assigned_to_renders():
    ev = _event('pushed_to_inventory',
                metadata={'previous_assigned_to': 'U_OLD_OWNER'})
    text = _all_text(history_view.render_timeline(
        domain_row=_row(), events=[ev], today=TODAY,
    ))
    assert 'was: U_OLD_OWNER' in text


def test_metadata_unknown_keys_are_silently_dropped():
    """If an event has metadata we don't know how to render, don't
    crash and don't dump raw JSON into the timeline."""
    ev = _event('classified_active',
                metadata={'unknown_key': {'nested': 'data'}})
    blocks = history_view.render_timeline(
        domain_row=_row(), events=[ev], today=TODAY,
    )
    # Should render WITHOUT including the raw nested dict
    text = _all_text(blocks)
    assert 'nested' not in text


def test_metadata_none_does_not_crash():
    ev = _event('classified_active', metadata=None)
    blocks = history_view.render_timeline(
        domain_row=_row(), events=[ev], today=TODAY,
    )
    # No crash, no metadata line
    assert blocks  # produced output


# ─── Timestamp coercion ───────────────────────────────────────────────────

def test_timestamp_handles_iso_string_with_microseconds():
    """SQLite returns timestamps as 'YYYY-MM-DD HH:MM:SS.ffffff' strings."""
    ev = _event('classified_active',
                occurred_at='2026-05-20 14:30:45.123456')
    text = _all_text(history_view.render_timeline(
        domain_row=_row(), events=[ev], today=TODAY,
    ))
    assert '2026-05-20 14:30' in text


def test_timestamp_handles_datetime_object():
    """Postgres returns datetime objects via psycopg2 RealDictCursor."""
    ev = _event('classified_active',
                occurred_at=dt.datetime(2026, 5, 20, 14, 30, 45))
    text = _all_text(history_view.render_timeline(
        domain_row=_row(), events=[ev], today=TODAY,
    ))
    assert '2026-05-20 14:30' in text


def test_timestamp_handles_garbage_input_gracefully():
    """Defensive: a malformed timestamp should not crash the renderer."""
    ev = _event('classified_active', occurred_at='not-a-date')
    blocks = history_view.render_timeline(
        domain_row=_row(), events=[ev], today=TODAY,
    )
    assert blocks  # didn't crash


# ─── Truncation ───────────────────────────────────────────────────────────

def test_truncates_long_timeline_with_more_count():
    """Only render up to _MAX_EVENT_ROWS_PER_MESSAGE rows + a more-line."""
    n = history_view._MAX_EVENT_ROWS_PER_MESSAGE + 10
    events = [
        _event('classified_active',
               occurred_at=dt.datetime(2026, 5, 20, 14, 30) - dt.timedelta(hours=i))
        for i in range(n)
    ]
    blocks = history_view.render_timeline(
        domain_row=_row(), events=events, today=TODAY,
    )
    text = _all_text(blocks)
    # …and N more event(s) line present
    assert '…and 10 more event(s)' in text
    # Stays under Slack's 50-block hard cap
    assert len(blocks) <= 50


def test_short_timeline_does_not_show_more_line():
    events = [_event('classified_active') for _ in range(3)]
    text = _all_text(history_view.render_timeline(
        domain_row=_row(), events=events, today=TODAY,
    ))
    assert 'more event(s)' not in text


# ─── Smoke test: full realistic timeline ──────────────────────────────────

def test_realistic_full_timeline_renders_cleanly():
    """End-to-end smoke: pretend a domain has gone through the whole
    flow once. Verify the output has all the expected pieces and no
    Python exceptions."""
    row = _row(
        lifecycle_state=S.RENEWED,
        expire_at=dt.datetime(2027, 5, 20),
        last_active_at=dt.datetime(2026, 5, 30),
    )
    events = [
        _event('renewed', occurred_at=dt.datetime(2026, 5, 22, 10, 0),
               actor='U_UTKARSH',
               from_state=S.AWAITING_UTKARSH_RENEW, to_state=S.RENEWED),
        _event('mdb_said_using_yes', occurred_at=dt.datetime(2026, 5, 21, 9, 30),
               actor='U_NEERAJ',
               from_state=S.AWAITING_MDB_USAGE_RESPONSE,
               to_state=S.AWAITING_UTKARSH_RENEW),
        _event('prompted_mdb_usage', occurred_at=dt.datetime(2026, 5, 20, 19, 0),
               actor='cron',
               from_state=S.ACTIVE, to_state=S.AWAITING_MDB_USAGE_RESPONSE,
               metadata={'spend': {'cost': 89.50, 'revenue': 245.0},
                         'days_until_expiry': '7'}),
        _event('classified_active', occurred_at=dt.datetime(2026, 5, 1, 19, 0),
               actor='cron', from_state=None, to_state=S.ACTIVE,
               metadata={'spend': {'cost': 89.50, 'revenue': 245.0}}),
        _event('added_via_path_b', occurred_at=dt.datetime(2026, 4, 20, 14, 0),
               actor='U_NEERAJ', from_state=None, to_state=None),
    ]
    blocks = history_view.render_timeline(
        domain_row=row, events=events, today=TODAY,
    )
    text = _all_text(blocks)
    # Each event present
    assert 'renewed' in text
    assert 'mdb_said_using_yes' in text
    assert 'prompted_mdb_usage' in text
    assert 'classified_active' in text
    assert 'added_via_path_b' in text
    # Right people tagged
    assert '<@U_UTKARSH>' in text
    assert '<@U_NEERAJ>' in text
    # State summary at top
    assert 'renewed' in text  # state label
    # Expiry shows urgency in the >30 days-away band (parens form, no
    # warning emoji). Day count we don't pin precisely since it's
    # date-arithmetic-sensitive across leap years.
    import re
    assert re.search(r'\(\d+d away\)', text), (
        'expected "(Nd away)" parenthetical for far-off expiry'
    )
    # Spend metadata threaded through
    assert 'spend $89.50' in text
