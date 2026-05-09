"""Slack-friendly emoji + label for each lifecycle_state.

Used by the /list-domains rendering so each row shows its lifecycle
state at a glance — alongside the existing setup-status emoji
(✅ deployed / ⏳ pending).

Single source of truth: if a state's visual changes, change it here and
both the slash command and the future /domain-history command update.
"""
from __future__ import annotations

from typing import Optional

from lifecycle import states as S


# State → single emoji used as a per-row badge in slack messages.
# We use single emojis (not multi-codepoint sequences) so they render
# consistently across desktop / mobile slack clients.
LIFECYCLE_EMOJI = {
    None:                              '⚪',
    S.ACTIVE:                          '🟢',
    S.IDLE:                            '💤',
    S.INVENTORY:                       '📦',
    S.EXPIRING_30:                     '⚠️',
    S.EXPIRING_14:                     '⚠️',
    S.EXPIRING_7:                      '🟠',
    S.EXPIRING_1:                      '🔴',
    S.EXPIRED:                         '❌',
    S.RENEWED:                         '✅',
    S.EXTENDED_30:                     '🔁',
    S.EXTENDED_15:                     '🔁',
    S.AWAITING_MDB_USAGE_RESPONSE:     '⏰',
    S.AWAITING_MDB_INVENTORY_RESPONSE: '⏰',
    S.AWAITING_UTKARSH_RENEW:          '🔧',
    S.AWAITING_UTKARSH_DISABLE_RENEW:  '🔧',
    S.AWAITING_TL_OVERRIDE_USAGE:      '🚨',
    S.AWAITING_TL_OVERRIDE_INVENTORY:  '🚨',
}

# State → short human-readable label.
LIFECYCLE_LABEL = {
    None:                              'unclassified',
    S.ACTIVE:                          'active',
    S.IDLE:                            'idle',
    S.INVENTORY:                       'inventory pool',
    S.EXPIRING_30:                     'expires <30d',
    S.EXPIRING_14:                     'expires <14d',
    S.EXPIRING_7:                      'expires <7d',
    S.EXPIRING_1:                      'expires <1d',
    S.EXPIRED:                         'expired',
    S.RENEWED:                         'renewed',
    S.EXTENDED_30:                     'snoozed 30d',
    S.EXTENDED_15:                     'snoozed 15d',
    S.AWAITING_MDB_USAGE_RESPONSE:     'awaiting MDB (usage)',
    S.AWAITING_MDB_INVENTORY_RESPONSE: 'awaiting MDB (inventory)',
    S.AWAITING_UTKARSH_RENEW:          'awaiting Utkarsh (renew)',
    S.AWAITING_UTKARSH_DISABLE_RENEW:  'awaiting Utkarsh (disable autorenew)',
    S.AWAITING_TL_OVERRIDE_USAGE:      'awaiting TL (usage override)',
    S.AWAITING_TL_OVERRIDE_INVENTORY:  'awaiting TL (inventory override)',
}


def emoji(state: Optional[str]) -> str:
    """Return the single-glyph emoji for a lifecycle state, or '·' for
    anything we don't recognise. Empty string is normalised to None
    (treats unset === unknown)."""
    if state == '':
        state = None
    return LIFECYCLE_EMOJI.get(state, '·')


def label(state: Optional[str]) -> str:
    """Return the human-readable label, or 'unknown' for unrecognised
    states."""
    if state == '':
        state = None
    return LIFECYCLE_LABEL.get(state, 'unknown')


# State filter keywords accepted by /list-domains as `:keyword`.
# Each maps to a predicate over `state` so we can match groups (e.g.
# `:expiring` matches all four EXPIRING_* states).
_FILTER_KEYWORDS = {
    'awaiting':  lambda s: s in S.AWAITING_STATES,
    'expiring':  lambda s: bool(s) and s.startswith('EXPIRING_'),
    'idle':      lambda s: s == S.IDLE,
    'active':    lambda s: s == S.ACTIVE,
    'inventory': lambda s: s == S.INVENTORY,
    'expired':   lambda s: s == S.EXPIRED,
    'renewed':   lambda s: s == S.RENEWED,
    'snoozed':   lambda s: s in (S.EXTENDED_30, S.EXTENDED_15),
}


def state_filter(keyword: str):
    """Return a predicate(row) that matches rows whose lifecycle_state
    falls into the given keyword group, or None if the keyword isn't
    a recognised state filter (caller falls back to substring search).

    Keyword is case-insensitive and the leading `:` is stripped if present.
    """
    k = keyword.lstrip(':').strip().lower()
    pred = _FILTER_KEYWORDS.get(k)
    if not pred:
        return None
    return lambda row: pred(row.get('lifecycle_state'))


def help_keywords() -> str:
    """Comma-separated list of state-filter keywords for the slash
    command help text."""
    return ', '.join(f'`:{k}`' for k in _FILTER_KEYWORDS)


# ─── Event-type → emoji (for /domain-history timeline) ────────────────────
# Different concern from state badges above: an event_type is what
# happened (verb), a state is where the row IS (noun). We use a separate
# map so the visual cue for the verb stays distinct from the state.
#
# Unknown event types fall through to the default '·' so a future event
# type added without updating this map degrades gracefully — better
# than crashing the slash command.

EVENT_EMOJI = {
    # Initial / classifier events
    'added_via_path_b':            '🛒',
    'classified_active':           '🟢',
    'classified_idle':             '💤',
    'classified_inventory':        '📦',
    'classified_expiring':         '⚠️',
    'expired':                     '❌',
    # Prompts the cron sends
    'prompted_mdb_usage':          '⏰',
    'prompted_mdb_idle':           '⏰',
    # MDB button clicks
    'mdb_said_using_yes':          '✅',
    'mdb_said_using_no':           '❌',
    'mdb_no_but_recent_spend':     '⚠️',  # contradiction guard
    'mdb_extended_30':             '🔁',
    'mdb_extended_15':             '🔁',
    'pushed_to_inventory':         '📦',
    # Utkarsh button clicks
    'renewed':                     '💰',
    'auto_renew_disabled':         '🚫',
    # SLA escalation + TL overrides
    'escalated_to_tl':             '🚨',
    'tl_forced_renew':             '⚖️',
    'tl_forced_disable_renew':     '⚖️',
    'tl_forced_push_inventory':    '⚖️',
    'tl_forced_keep_30':           '⚖️',
    # Manual / admin
    'assigned':                    '👤',
    'unassigned':                  '👤',
}


def event_emoji(event_type: str) -> str:
    """Single-glyph emoji for an event_type. '·' for unrecognised types."""
    return EVENT_EMOJI.get(event_type, '·')
