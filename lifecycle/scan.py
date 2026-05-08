"""Daily lifecycle scan — the orchestration layer.

Pulls last-30d spend from RedTrack, classifies every domain, and
dispatches the right Slack action for each transition. This is the
function the Render Cron Job runs once a day at 7 PM IST.

Architecture:
  classifier.py decides WHAT each domain's state should be (pure logic).
  scan.py decides WHAT TO DO when a state changes (DM, store update,
  event log).

Action map per classifier output:

  ACTIVE      → save state + stamp last_active_at, no DM
  EXPIRING_*  → DM MDB "still using?" buttons, save AWAITING_MDB_USAGE
  IDLE        → DM MDB "keep or push?" buttons, save AWAITING_MDB_INVENTORY
  EXPIRED     → DM TL "domain expired", save state, no MDB DM
  INVENTORY   → save state, no DM
  None        → skip (within grace window, awaiting human, etc.)
"""
from __future__ import annotations

import json
import logging
import os
from typing import Dict, Optional

from config import Config
from inventory import store
from lifecycle import dm as _dm, states as S
from lifecycle.classifier import classify_domain
from redtrack_client import get_domain_spend_revenue_30d

logger = logging.getLogger(__name__)


# ─── Public entry point ───────────────────────────────────────────────────

def run_scan(
    slack_client=None,
    *,
    today=None,
    spend_by_host: Optional[Dict[str, Dict[str, float]]] = None,
) -> Dict[str, int]:
    """Run one classifier pass over every domain.

    Args:
      slack_client: a slack_sdk.WebClient. When None, the function
        instantiates one from Config.SLACK_BOT_TOKEN. Tests pass a mock.
      today: override for tests; otherwise date.today() in classifier.
      spend_by_host: override for tests so we don't hit RedTrack live.

    Returns counters: {'classified', 'prompted', 'unchanged', 'skipped',
                       'errors'}. Useful for the cron's stdout log.
    """
    if slack_client is None:
        slack_client = _make_slack_client()

    if spend_by_host is None:
        try:
            spend_by_host = get_domain_spend_revenue_30d()
        except Exception:
            logger.exception(
                'redtrack fetch crashed — running with empty spend data'
            )
            spend_by_host = {}

    rows = store.list_domains_for_lifecycle(
        exclude_states=S.AWAITING_STATES,
    )
    logger.info(
        'lifecycle scan starting — %d domains to classify, %d hosts in spend data, dry_run=%s',
        len(rows), len(spend_by_host), Config.LIFECYCLE_DRY_RUN,
    )

    counters = {
        'classified': 0, 'prompted': 0, 'unchanged': 0,
        'skipped': 0, 'errors': 0,
    }

    for row in rows:
        try:
            outcome = _process_row(slack_client, row, spend_by_host, today)
            counters[outcome] = counters.get(outcome, 0) + 1
        except Exception:
            logger.exception(
                'classifier failed on domain=%s — continuing',
                row.get('domain'),
            )
            counters['errors'] += 1

    logger.info('lifecycle scan finished — %s', counters)
    return counters


# ─── Internals ────────────────────────────────────────────────────────────

def _make_slack_client():
    """Lazy import so unit tests can run without slack_sdk installed."""
    from slack_sdk import WebClient
    return WebClient(token=Config.SLACK_BOT_TOKEN)


def _process_row(slack_client, row: dict, spend_by_host, today) -> str:
    """Decide + dispatch for one domain. Returns one of:
        'classified' | 'prompted' | 'unchanged' | 'skipped' | 'errors'
    so the caller can tally counters.
    """
    domain = row['domain']
    current = row.get('lifecycle_state')

    spend = spend_by_host.get(domain.lower(), {})
    new_state = classify_domain(row, spend, today=today)

    if new_state is None:
        return 'skipped'

    # Stamp last_active_at whenever the data shows real spend, even if
    # state itself is unchanged. last_active_at drives idle/grace later.
    if new_state == S.ACTIVE:
        store.mark_active(domain)

    # No-op when the new state matches what's already saved.
    if new_state == current:
        return 'unchanged'

    # Quiet transitions — store + log, no DMs.
    if new_state in (S.ACTIVE, S.INVENTORY):
        store.set_lifecycle_state(domain, new_state)
        store.record_event(
            domain, f'classified_{new_state.lower()}',
            actor='cron', from_state=current, to_state=new_state,
            metadata={'spend': spend} if spend else None,
        )
        return 'classified'

    if new_state == S.EXPIRED:
        return _handle_expired(slack_client, row, current, spend)

    if new_state == S.IDLE:
        return _prompt_mdb_idle(slack_client, row, current, spend)

    if new_state.startswith('EXPIRING_'):
        return _prompt_mdb_expiring(
            slack_client, row, current, new_state, spend,
        )

    # Unknown state value (shouldn't happen — classifier vocabulary is
    # closed) — log + skip rather than crash the whole scan.
    logger.warning(
        'classifier returned unrecognised state=%s for domain=%s',
        new_state, domain,
    )
    return 'skipped'


# ─── Action handlers ──────────────────────────────────────────────────────

def _handle_expired(slack_client, row, from_state, spend) -> str:
    """Domain went past expire_at without renewal. Save state + DM TL.
    No MDB DM — at this point the domain is dead and the conversation
    is between TL and Utkarsh."""
    domain = row['domain']
    store.set_lifecycle_state(domain, S.EXPIRED)
    store.record_event(
        domain, 'expired', actor='cron',
        from_state=from_state, to_state=S.EXPIRED,
        metadata={'spend': spend, 'expire_at': str(row.get('expire_at'))},
    )
    _dm.dm(
        slack_client,
        real_recipient=Config.TL_SLACK_USER_ID,
        text=(f':rotating_light: `{domain}` *expired without renewal*. '
              f'It past its expire date today. Last 30d spend was '
              f"${float(spend.get('cost') or 0):.2f}, revenue "
              f"${float(spend.get('revenue') or 0):.2f}. Coordinate with "
              f'Utkarsh on whether to re-buy or write it off.'),
        dry_run_label=f'expired:{domain}',
    )
    return 'classified'


def _prompt_mdb_idle(slack_client, row, from_state, spend) -> str:
    """TL Flow 2 — MDB hasn't run any spend in 30 days. Ask whether to
    keep the domain (30/15-day snooze) or push it to the inventory pool.
    """
    domain = row['domain']
    assigned = row.get('assigned_to')

    if not _dedup_ok(row):
        return 'skipped'

    blocks = _idle_card(domain, assigned, spend)
    sent = _dm.dm(
        slack_client, real_recipient=assigned,
        text=(f'Heads up — `{domain}` had no spend in the last 30 days. '
              'keep it, or push to inventory?'),
        blocks=blocks,
        dry_run_label=f'idle_prompt:{domain}',
    )
    if sent is None:
        # No recipient → escalate straight to TL so the domain isn't
        # stuck in limbo waiting for someone who can't be DM'd.
        _dm.dm(
            slack_client, real_recipient=Config.TL_SLACK_USER_ID,
            text=(f':warning: `{domain}` is idle but has no MDB to DM '
                  '(assigned_to is empty). Reassign or push to inventory.'),
            dry_run_label=f'idle_no_mdb:{domain}',
        )
        return 'skipped'

    store.set_lifecycle_state(domain, S.AWAITING_MDB_INVENTORY_RESPONSE)
    store.bump_last_prompted_at(domain)
    store.record_event(
        domain, 'prompted_mdb_idle', actor='cron',
        from_state=from_state, to_state=S.AWAITING_MDB_INVENTORY_RESPONSE,
        metadata={'spend': spend, 'assigned_to': assigned},
    )
    return 'prompted'


def _prompt_mdb_expiring(slack_client, row, from_state, new_state, spend) -> str:
    """TL Flow 1 — domain is actively spending AND expiring soon.
    Ask MDB if they're still using it. The 4-stage cascade
    (EXPIRING_30/14/7/1) all enter through here; the urgency in the
    DM copy escalates by stage."""
    domain = row['domain']
    assigned = row.get('assigned_to')
    days_left_label = new_state.removeprefix('EXPIRING_')

    if not _dedup_ok(row):
        return 'skipped'

    blocks = _expiring_card(domain, assigned, days_left_label, new_state, spend)
    sent = _dm.dm(
        slack_client, real_recipient=assigned,
        text=(f'`{domain}` expires in ~{days_left_label} day(s). '
              'are you still using it?'),
        blocks=blocks,
        dry_run_label=f'expiring_{days_left_label}:{domain}',
    )
    if sent is None:
        _dm.dm(
            slack_client, real_recipient=Config.TL_SLACK_USER_ID,
            text=(f':warning: `{domain}` expires in ~{days_left_label} day(s) '
                  'but has no MDB to DM (assigned_to is empty). Decide on '
                  'renew vs lapse manually.'),
            dry_run_label=f'expiring_no_mdb:{domain}',
        )
        return 'skipped'

    store.set_lifecycle_state(domain, S.AWAITING_MDB_USAGE_RESPONSE)
    store.bump_last_prompted_at(domain)
    store.record_event(
        domain, 'prompted_mdb_usage', actor='cron',
        from_state=from_state, to_state=S.AWAITING_MDB_USAGE_RESPONSE,
        metadata={
            'spend': spend, 'assigned_to': assigned,
            'days_until_expiry': days_left_label,
            'expiring_state': new_state,
        },
    )
    return 'prompted'


def _dedup_ok(row) -> bool:
    """Defensive 23h dedup. The state-machine filter
    (exclude_states=AWAITING_*) already prevents most duplicate prompts;
    this is a belt-and-braces guard for cases where state was manually
    cleared but the prompt was sent recently."""
    last = row.get('last_prompted_at')
    if last is None:
        return True
    # `last` may be a datetime (Postgres) or ISO string (SQLite). Defer
    # to the classifier's coercion helper to keep one date-parsing seam.
    from lifecycle.classifier import _coerce_date
    last_date = _coerce_date(last)
    if last_date is None:
        return True
    import datetime as _dt
    age_h = (_dt.datetime.now() - _dt.datetime(
        last_date.year, last_date.month, last_date.day,
    )).total_seconds() / 3600
    return age_h >= Config.LIFECYCLE_PROMPT_DEDUP_HOURS


# ─── Block Kit cards ──────────────────────────────────────────────────────
# Kept here so the buttons + DM text stay co-located. Each card's button
# `value` carries the full payload the handler needs — domain, MDB,
# whatever the next-step DM needs. JSON-encoded.

def _idle_card(domain: str, assigned: Optional[str],
               spend: Dict) -> list:
    payload = json.dumps({
        'domain': domain, 'assigned_to': assigned or '',
    })
    cost = float(spend.get('cost') or 0)
    revenue = float(spend.get('revenue') or 0)
    return [
        {
            'type': 'section',
            'text': {
                'type': 'mrkdwn',
                'text': (
                    f':zzz: *`{domain}` has gone quiet*\n'
                    f'• Spend last 30 days: ${cost:.2f}\n'
                    f'• Revenue last 30 days: ${revenue:.2f}\n\n'
                    'Push it to inventory, or extend on a snooze?'
                ),
            },
        },
        {'type': 'actions', 'elements': [
            {'type': 'button', 'action_id': 'lifecycle_keep_30',
             'text': {'type': 'plain_text', 'text': 'Keep 30 more days'},
             'value': payload},
            {'type': 'button', 'action_id': 'lifecycle_keep_15',
             'text': {'type': 'plain_text', 'text': 'Keep 15 more days'},
             'value': payload},
            {'type': 'button', 'action_id': 'lifecycle_push_inventory',
             'text': {'type': 'plain_text', 'text': 'Push to inventory'},
             'style': 'danger', 'value': payload},
        ]},
    ]


def _expiring_card(domain: str, assigned: Optional[str],
                   days_left: str, new_state: str,
                   spend: Dict) -> list:
    payload = json.dumps({
        'domain': domain, 'assigned_to': assigned or '',
        'days_left': days_left, 'expiring_state': new_state,
    })
    cost = float(spend.get('cost') or 0)
    return [
        {
            'type': 'section',
            'text': {
                'type': 'mrkdwn',
                'text': (
                    f':bell: *`{domain}` expires in ~{days_left} day(s)*\n'
                    f'• Spend last 30 days: ${cost:.2f}\n\n'
                    'Are you still using this domain?'
                ),
            },
        },
        {'type': 'actions', 'elements': [
            {'type': 'button', 'action_id': 'lifecycle_using_yes',
             'text': {'type': 'plain_text', 'text': ':white_check_mark: Yes, using it'},
             'style': 'primary', 'value': payload},
            {'type': 'button', 'action_id': 'lifecycle_using_no',
             'text': {'type': 'plain_text', 'text': ':x: No, not using'},
             'style': 'danger', 'value': payload},
        ]},
    ]
