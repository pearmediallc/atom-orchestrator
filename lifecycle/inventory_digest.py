"""Daily 'domains available for grabs' digest posted to a public channel.

Once-per-day Slack message listing every domain in our DB whose
assigned_to is NULL — i.e. the rotation pool. Lets MDBs self-serve
("oh I can claim that one") instead of DMing Utkarsh to ask what's
free.

Architecture mirrors the rest of lifecycle/:
  • inventory/store.list_unassigned_domains — DB layer (SQL).
  • render_digest_blocks(...)                — pure block renderer.
  • run_inventory_digest(slack_client)        — orchestrator: fetch,
    render, post. Honours LIFECYCLE_DRY_RUN. Skips post when the pool
    is empty (no point spamming the channel with "0 available").

Wired into the daily `python -m lifecycle` cron alongside the
classifier and SLA escalator. DEVELOPERS_CHANNEL_ID env var is the
clean off-switch — empty disables the digest entirely.
"""
from __future__ import annotations

import datetime as _dt
import logging
from collections import OrderedDict
from typing import Dict, List, Optional

from config import Config
from inventory import store
from lifecycle.classifier import _coerce_date

logger = logging.getLogger(__name__)

# Slack 50-block-per-message ceiling. Header + footer + dividers eat
# ~6 blocks, then each vertical group adds 1 sub-header + N domain
# rows. 30 domain rows leaves room for ~10 vertical groups.
_MAX_DOMAINS_PER_MESSAGE = 30


# ─── Public API ───────────────────────────────────────────────────────────

def run_inventory_digest(
    slack_client=None, *, today: Optional[_dt.date] = None,
) -> Dict[str, int]:
    """Pull the inventory pool, render, post. Returns counters for the
    cron's stdout log.

    Skips entirely when:
      • DEVELOPERS_CHANNEL_ID is unset (digest is opt-in)
      • the pool is empty (don't spam the channel)
    """
    counters = {'unassigned': 0, 'posted': 0, 'skipped': 0}

    channel = Config.DEVELOPERS_CHANNEL_ID
    if not channel:
        logger.info(
            'inventory digest skipped — DEVELOPERS_CHANNEL_ID not set',
        )
        counters['skipped'] = 1
        return counters

    rows = store.list_unassigned_domains(limit=_MAX_DOMAINS_PER_MESSAGE * 4)
    counters['unassigned'] = len(rows)
    if not rows:
        logger.info('inventory digest skipped — 0 unassigned domains')
        counters['skipped'] = 1
        return counters

    blocks = render_digest_blocks(rows, today=today or _dt.date.today())
    fallback = (
        f'Inventory pool: {len(rows)} unassigned domain(s) '
        'available — see message for details.'
    )

    if Config.LIFECYCLE_DRY_RUN:
        logger.info(
            'inventory digest DRY_RUN — would post to %s: %d domains',
            channel, len(rows),
        )
        return counters

    if slack_client is None:
        slack_client = _make_slack_client()

    try:
        slack_client.chat_postMessage(
            channel=channel, text=fallback, blocks=blocks,
        )
        counters['posted'] = 1
        logger.info(
            'inventory digest posted to %s — %d domains', channel, len(rows),
        )
    except Exception:
        logger.exception('inventory digest post failed')
        counters['skipped'] = 1

    return counters


# ─── Pure renderer (unit-testable) ────────────────────────────────────────

def render_digest_blocks(
    rows: List[Dict], *, today: _dt.date,
) -> List[Dict]:
    """Build the Block Kit blocks for one day's digest.

    Layout:
      Header
      Context (count + how to claim)
      Divider
      For each vertical (sorted A-Z):
        Vertical sub-header
        Up to N domain rows (sorted by expire_at ASC, NULLs last)
      Divider
      Footer (total count, link to /list-domains :inventory)
    """
    total = len(rows)
    shown = rows[:_MAX_DOMAINS_PER_MESSAGE]
    truncated = total - len(shown)

    by_vertical = _group_by_vertical(shown)

    blocks: List[Dict] = [
        {'type': 'header', 'text': {
            'type': 'plain_text',
            'text': f'Inventory pool — {total} available',
        }},
        {'type': 'context', 'elements': [{
            'type': 'mrkdwn',
            'text': (
                'Domains below have *no assigned MDB* — claim one by '
                'pinging Utkarsh, or use it as the target in '
                '`/list-domains` → *Deploy lander*. Listed by vertical, '
                'closest expiry first within each group.'
            ),
        }]},
        {'type': 'divider'},
    ]

    for vertical, vertical_rows in by_vertical.items():
        blocks.append({
            'type': 'section',
            'text': {
                'type': 'mrkdwn',
                'text': f'*{vertical}* — {len(vertical_rows)} domain(s)',
            },
        })
        for row in vertical_rows:
            blocks.append(_section(_fmt_row(row, today)))

    if truncated > 0:
        blocks.append({'type': 'divider'})
        blocks.append({'type': 'context', 'elements': [{
            'type': 'mrkdwn',
            'text': (
                f'_…and {truncated} more available. '
                'Run `/list-domains :inventory` for the full list._'
            ),
        }]})

    return blocks


# ─── Helpers ───────────────────────────────────────────────────────────────

_NO_VERTICAL_KEY = '_no vertical_'


def _group_by_vertical(rows: List[Dict]) -> 'OrderedDict[str, List[Dict]]':
    """Bucket rows by vertical, alphabetised. Rows with empty/null
    vertical bucket into '_no vertical_' and float to the end (since
    they're less actionable for MDBs scanning their own area)."""
    grouped: Dict[str, List[Dict]] = {}
    for r in rows:
        key = (r.get('vertical') or '').strip() or _NO_VERTICAL_KEY
        grouped.setdefault(key, []).append(r)
    # Sort tuple: (is_no_vertical, lowercased key). The first element
    # forces _no vertical_ to the bottom regardless of ASCII ordering
    # (underscore actually sorts BEFORE letters in raw ASCII — 0x5F).
    return OrderedDict(
        sorted(
            grouped.items(),
            key=lambda kv: (
                1 if kv[0] == _NO_VERTICAL_KEY else 0,
                kv[0].lower(),
            ),
        )
    )


def _fmt_row(row: Dict, today: _dt.date) -> str:
    """Single domain line. Format:
        `domain.com`  (expires 2026-08-12, 95d)   _vertical_  _was: U_OWNER_
    Falls back gracefully when fields are missing.
    """
    domain = row['domain']
    parts = [f'`{domain}`']

    expire = _coerce_date(row.get('expire_at'))
    if expire:
        days = (expire - today).days
        if days < 0:
            parts.append(f'(expired {abs(days)}d ago)')
        elif days == 0:
            parts.append('(*expires today*)')
        elif days <= 30:
            parts.append(f'(*{days}d to expire*)')
        else:
            parts.append(f'(expires {expire.isoformat()}, {days}d)')
    else:
        parts.append('(_expire date unknown — may be in another account_)')

    # Surface the previous owner if we have one (the row was just pushed
    # to inventory) — gives MDBs context on whether the campaign worked.
    notes = (row.get('notes') or '').strip()
    if notes:
        # Keep a short snippet only — notes can be long.
        parts.append(f'_{notes[:80]}_')

    return '  '.join(parts)


def _section(text: str) -> Dict:
    return {'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}


def _make_slack_client():
    """Lazy import — keeps unit tests clean of slack_sdk."""
    from slack_sdk import WebClient
    return WebClient(token=Config.SLACK_BOT_TOKEN)
