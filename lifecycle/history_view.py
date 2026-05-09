"""Slack Block Kit renderer for `/domain-history <domain>`.

Pure function: takes a domain row + an event list and returns a list of
blocks. No I/O (no Slack client, no DB). The slash-command handler
fetches the data; this module decides what the output looks like.

Why a separate module:
  • Block-Kit rendering logic gets noisy fast — keeping it out of
    slack_bot/routes.py prevents that file from bloating further.
  • Pure render functions are trivial to unit-test (just compare
    output blocks to expected shapes).
  • Future commands (e.g. `/lifecycle-report`) can reuse formatting
    helpers like _fmt_event() and the date-coercion logic.
"""
from __future__ import annotations

import datetime as _dt
from typing import Dict, List, Optional

from lifecycle import badges as _badges
from lifecycle.classifier import _coerce_date


# Slack caps a single message at 50 blocks. Reserve 5-7 for header +
# status section + dividers; the rest is event rows.
_MAX_EVENT_ROWS_PER_MESSAGE = 25


def render_timeline(
    domain_row: Optional[Dict],
    events: List[Dict],
    *,
    requested_domain: str = '',
    today: Optional[_dt.date] = None,
) -> List[Dict]:
    """Build the full block list for /domain-history.

    `domain_row` is the inventory row (output of store.get_domain), or
    None when the user asked about a domain we don't track. The
    handler still calls render_timeline so the not-found UX lives here.

    `events` is the list from store.list_domain_events, newest-first.

    `today` overrides date.today() for tests.
    """
    today = today or _dt.date.today()

    # ── Not-in-inventory case — single helpful block ─────────────────────
    if domain_row is None:
        return [_section(
            f":mag: *`{requested_domain}` is not in our inventory.*\n"
            'It might be a typo, or a domain that was never added through '
            "atom-orchestrator. Try `/list-domains` to find what's tracked.",
        )]

    domain = domain_row['domain']

    blocks: List[Dict] = [
        {
            'type': 'header',
            'text': {'type': 'plain_text',
                     'text': f'Timeline for {domain}'},
        },
        _summary_section(domain_row, today),
        {'type': 'divider'},
    ]

    # ── No-events case — current state only, with prompt ─────────────────
    if not events:
        blocks.append(_section(
            ":information_source: No audit events recorded yet for this "
            "domain. Once the lifecycle cron starts running and the MDB "
            "interacts with bot prompts, events will accumulate here."
        ))
        return blocks

    # ── Event rows ───────────────────────────────────────────────────────
    blocks.append({
        'type': 'context',
        'elements': [{
            'type': 'mrkdwn',
            'text': (
                f'_Recent events (newest first, showing '
                f'{min(_MAX_EVENT_ROWS_PER_MESSAGE, len(events))} '
                f'of {len(events)} total):_'
            ),
        }],
    })

    for ev in events[:_MAX_EVENT_ROWS_PER_MESSAGE]:
        blocks.append(_section(_fmt_event(ev)))

    if len(events) > _MAX_EVENT_ROWS_PER_MESSAGE:
        remaining = len(events) - _MAX_EVENT_ROWS_PER_MESSAGE
        blocks.append({'type': 'context', 'elements': [{
            'type': 'mrkdwn',
            'text': (f'_…and {remaining} more event(s). '
                     'Query the `domain_events` table directly for the '
                     'full history._'),
        }]})

    return blocks


# ─── Summary section (current state of the domain) ────────────────────────

def _summary_section(row: Dict, today: _dt.date) -> Dict:
    """Top-of-message block: current state + key inventory facts."""
    state = row.get('lifecycle_state')
    state_emoji = _badges.emoji(state)
    state_label = _badges.label(state)

    assigned_to = (row.get('assigned_to') or '').strip()
    if assigned_to.startswith('Slack:'):
        assigned_to = assigned_to[6:]
    assigned_display = f'<@{assigned_to}>' if assigned_to else '_unassigned_'

    vertical = row.get('vertical') or '_no vertical_'

    expire_line = ''
    expire = _coerce_date(row.get('expire_at'))
    if expire:
        days = (expire - today).days
        urgency = ''
        if days < 0:
            urgency = ' :rotating_light: *expired*'
        elif days <= 7:
            urgency = f' :rotating_light: *{days}d left*'
        elif days <= 30:
            urgency = f' :warning: {days}d left'
        else:
            urgency = f' ({days}d away)'
        expire_line = f'\n*Expires:* `{expire.isoformat()}`{urgency}'

    last_active = _coerce_date(row.get('last_active_at'))
    last_active_line = (
        f'\n*Last active:* `{last_active.isoformat()}` '
        f'({(today - last_active).days}d ago)'
    ) if last_active else ''

    return _section(
        f'*Current state:*  {state_emoji}  _{state_label}_\n'
        f'*Assigned to:*  {assigned_display}\n'
        f'*Vertical:*  _{vertical}_'
        f'{expire_line}'
        f'{last_active_line}'
    )


# ─── Event row formatter ───────────────────────────────────────────────────

def _fmt_event(event: Dict) -> str:
    """Markdown for one event row. Stable layout so users can scan
    a long timeline visually."""
    emoji = _badges.event_emoji(event.get('event_type', ''))
    ts = _fmt_timestamp(event.get('occurred_at'))
    actor_raw = (event.get('actor') or '').strip()
    if actor_raw == 'cron' or not actor_raw:
        actor = '_cron_' if actor_raw else '_system_'
    else:
        actor = f'<@{actor_raw}>'
    event_type = event.get('event_type') or 'unknown'

    transition = _fmt_transition(event)
    metadata_summary = _fmt_metadata(event.get('metadata'))

    parts = [
        f'{emoji}  `{ts}`  `{event_type}`  {actor}',
    ]
    if transition:
        parts.append(transition)
    if metadata_summary:
        parts.append(metadata_summary)
    return '\n'.join(parts)


def _fmt_transition(event: Dict) -> str:
    """Render the `from → to` state transition as a single context line.
    Returns '' when neither side is set (most non-state-changing events
    e.g. legacy `assigned`)."""
    fr = event.get('from_state')
    to = event.get('to_state')
    if not fr and not to:
        return ''
    fr_label = _badges.label(fr) if fr else '_(none)_'
    to_label = _badges.label(to) if to else '_(none)_'
    return f'_{fr_label}_  →  _{to_label}_'


def _fmt_metadata(metadata) -> str:
    """One-liner summary of the metadata blob.

    Real-world metadata can be large (full ATOM error dumps, RedTrack
    spend dicts). We only surface the keys most useful in a timeline:
    spend amounts, days_until_expiry, recent_cost (contradiction guard).
    Everything else is truncated.

    Pure: never mutates the input.
    """
    if not isinstance(metadata, dict) or not metadata:
        return ''

    parts = []
    spend = metadata.get('spend')
    if isinstance(spend, dict):
        cost = spend.get('cost')
        rev = spend.get('revenue')
        if cost is not None or rev is not None:
            parts.append(
                f'spend ${float(cost or 0):.2f} / rev ${float(rev or 0):.2f}'
            )

    if 'recent_cost' in metadata:
        parts.append(f'recent 7d spend ${float(metadata["recent_cost"]):.2f}')

    if 'days_until_expiry' in metadata:
        parts.append(f'{metadata["days_until_expiry"]}d to expiry')

    if 'snooze_days' in metadata:
        parts.append(f'snoozed {metadata["snooze_days"]}d')

    if 'previous_assigned_to' in metadata and metadata['previous_assigned_to']:
        parts.append(f'was: {metadata["previous_assigned_to"]}')

    if not parts:
        return ''
    return '_' + ' · '.join(parts) + '_'


def _fmt_timestamp(ts) -> str:
    """Compact timestamp for the event row. Accepts datetime, date,
    or ISO string (SQLite returns strings)."""
    if ts is None:
        return '?'
    if isinstance(ts, _dt.datetime):
        return ts.strftime('%Y-%m-%d %H:%M')
    if isinstance(ts, _dt.date):
        return ts.isoformat()
    if isinstance(ts, str):
        # Strip microseconds + tz if present, return YYYY-MM-DD HH:MM.
        try:
            parsed = _dt.datetime.fromisoformat(ts.replace('Z', ''))
            return parsed.strftime('%Y-%m-%d %H:%M')
        except ValueError:
            return ts[:16]
    return str(ts)


def _section(text: str) -> Dict:
    return {'type': 'section',
            'text': {'type': 'mrkdwn', 'text': text}}
