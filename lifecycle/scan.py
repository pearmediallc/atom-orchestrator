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
    # Phase E: pre-load all current assignments so the classifier
    # doesn't have to issue 744 lookups. One SELECT covers the lot.
    try:
        bulk_assignments = store.bulk_current_assignments()
    except Exception:
        logger.exception('bulk_current_assignments failed — falling back '
                         'to legacy domains.assigned_to for this run')
        bulk_assignments = {}

    logger.info(
        'lifecycle scan starting — %d domains to classify, %d hosts in spend data, '
        '%d domains with active assignments, dry_run=%s',
        len(rows), len(spend_by_host), len(bulk_assignments),
        Config.LIFECYCLE_DRY_RUN,
    )

    counters = {
        'classified': 0, 'prompted': 0, 'unchanged': 0,
        'skipped': 0, 'errors': 0,
    }

    for row in rows:
        try:
            outcome = _process_row(
                slack_client, row, spend_by_host, today,
                bulk_assignments=bulk_assignments,
            )
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


def _process_row(slack_client, row: dict, spend_by_host, today,
                 *, bulk_assignments=None) -> str:
    """Decide + dispatch for one domain. Returns one of:
        'classified' | 'prompted' | 'unchanged' | 'skipped' | 'errors'
    so the caller can tally counters.

    `bulk_assignments` is the pre-loaded {domain: [slack_user_id, ...]}
    map from store.bulk_current_assignments(). When provided, the
    classifier uses it as the authoritative is-assigned signal.
    """
    domain = row['domain']
    current = row.get('lifecycle_state')

    spend = spend_by_host.get(domain.lower(), {})
    assignees = (bulk_assignments or {}).get(domain, []) if bulk_assignments is not None else None
    new_state = classify_domain(row, spend, today=today,
                                current_assignees=assignees)

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
    # Dry run is observe-only — see the note in _prompt_mdb_idle. Setting
    # EXPIRED here means the next run sees it as 'unchanged' and the real
    # TL alert never fires once we flip live.
    if Config.LIFECYCLE_DRY_RUN:
        return 'classified'
    store.set_lifecycle_state(domain, S.EXPIRED)
    store.record_event(
        domain, 'expired', actor='cron',
        from_state=from_state, to_state=S.EXPIRED,
        metadata={'spend': spend, 'expire_at': str(row.get('expire_at'))},
    )
    return 'classified'


def _build_recipient_list(assignees):
    """Phase F — fan-out targets for a prompt: every assigned MDB PLUS
    the TL. Returns [(slack_user_id, is_tl), ...], deduped.

    The TL is a full interactive recipient (same card, same buttons) so
    he can resolve a prompt himself if the MDBs are slow — strict
    first-click-wins, the existing 48h SLA escalator stays as the
    separate "nobody answered" path. If the TL is also an assigned MDB
    they appear once (as the MDB — the is_tl flag is display-only).
    """
    tl_norm = _dm.normalise_slack_id(Config.TL_SLACK_USER_ID)
    out = []
    seen = set()
    for uid in assignees:
        n = _dm.normalise_slack_id(uid)
        if n and n not in seen:
            seen.add(n)
            out.append((uid, False))
    if tl_norm and tl_norm not in seen:
        out.append((Config.TL_SLACK_USER_ID, True))
    return out


def _extract_message_coords(resp, recipient_uid, is_tl):
    """Pull {channel, ts} out of a successful chat_postMessage response
    so we can record the recipient in the fan-out ledger.

    Returns the ledger dict, or None for: dry-run sentinels (no real
    message), unresolved recipients (_dm.dm returned None), or responses
    missing channel/ts. A None recipient is simply omitted from the
    ledger — there's no card to sync for them.

    Bug fix 2026-05-15: previously checked `isinstance(resp, dict)` but
    slack_sdk's SlackResponse is NOT a dict subclass — it just exposes
    .get(). The isinstance check rejected every real send as "failed",
    nothing landed in the ledger, every fan-out then fell into the
    "every DM failed" escalation. Now we duck-type on .get() instead.
    """
    if not resp:
        return None
    try:
        if resp.get('dry_run'):
            return None
        channel = resp.get('channel')
        ts = resp.get('ts')
    except (AttributeError, TypeError):
        return None
    if not channel or not ts:
        return None
    return {
        'recipient_slack_id': recipient_uid,
        'channel_id': channel,
        'message_ts': ts,
        'is_tl': is_tl,
    }


def _prompt_mdb_idle(slack_client, row, from_state, spend) -> str:
    """TL Flow 2 — MDB hasn't run any spend in 30 days. Ask whether to
    keep the domain (30/15-day snooze) or push it to the inventory pool.

    Phase F: fans out to every assigned MDB AND the TL. Records each
    recipient's Slack message coords in domain_prompt_recipients so the
    button handlers can sync every sibling card on resolution. First
    click wins (handler uses an atomic state transition)."""
    domain = row['domain']
    assignees = _dm.get_mdb_slack_ids_for_domain(domain, row=row)

    if not assignees:
        # No resolvable MDB at all → escalate straight to TL
        _dm.dm(
            slack_client, real_recipient=Config.TL_SLACK_USER_ID,
            text=(f':warning: `{domain}` is idle but no resolvable MDB '
                  '(assigned_to empty or unresolved). Reassign or push to inventory.'),
            dry_run_label=f'idle_no_mdb:{domain}',
        )
        return 'skipped'

    if not _dedup_ok(row):
        return 'skipped'

    recipients = _build_recipient_list(assignees)
    sent_records = []
    for uid, is_tl in recipients:
        blocks = _idle_card(domain, uid, spend)
        resp = _dm.dm(
            slack_client, real_recipient=uid,
            text=(f'Heads up — `{domain}` had no spend in the last 30 days. '
                  'keep it, or push to inventory?'),
            blocks=blocks,
            dry_run_label=f'idle_prompt:{domain}:{uid}',
        )
        rec = _extract_message_coords(resp, uid, is_tl)
        if rec:
            sent_records.append(rec)

    if not sent_records and not Config.LIFECYCLE_DRY_RUN:
        _dm.dm(
            slack_client, real_recipient=Config.TL_SLACK_USER_ID,
            text=(f':warning: `{domain}` is idle but every DM attempt '
                  f'failed (recipients: {[u for u, _ in recipients]}). '
                  'Reassign or push to inventory.'),
            dry_run_label=f'idle_dm_failed:{domain}',
        )
        return 'skipped'

    # Dry run is observe-only. The "would DM" lines are already logged
    # by _dm.dm above — do NOT advance the state machine. If we set
    # AWAITING_* here, the classifier skips the row on every later run
    # (rule 1), so once we flip live the REAL DM never goes out. The
    # row would be stuck waiting on a click for a DM that was never sent.
    if Config.LIFECYCLE_DRY_RUN:
        return 'prompted'

    store.set_lifecycle_state(domain, S.AWAITING_MDB_INVENTORY_RESPONSE)
    store.bump_last_prompted_at(domain)
    store.record_prompt_recipients(domain, sent_records)
    store.record_event(
        domain, 'prompted_mdb_idle', actor='cron',
        from_state=from_state, to_state=S.AWAITING_MDB_INVENTORY_RESPONSE,
        metadata={'spend': spend,
                  'recipients': [r['recipient_slack_id'] for r in sent_records]},
    )
    return 'prompted'


def _prompt_mdb_expiring(slack_client, row, from_state, new_state, spend) -> str:
    """TL Flow 1 — domain is actively spending AND expiring soon.
    Ask MDB if they're still using it. The 4-stage cascade
    (EXPIRING_30/14/7/1) all enter through here; urgency escalates
    via the days_left label in the DM copy.

    Phase F: same fan-out pattern as _prompt_mdb_idle — DM every
    assigned MDB AND the TL, record the ledger, first click wins."""
    domain = row['domain']
    assignees = _dm.get_mdb_slack_ids_for_domain(domain, row=row)
    days_left_label = new_state.removeprefix('EXPIRING_')

    if not assignees:
        _dm.dm(
            slack_client, real_recipient=Config.TL_SLACK_USER_ID,
            text=(f':warning: `{domain}` expires in ~{days_left_label} day(s) '
                  'but no resolvable MDB. Decide on renew vs lapse manually.'),
            dry_run_label=f'expiring_no_mdb:{domain}',
        )
        return 'skipped'

    if not _dedup_ok(row):
        return 'skipped'

    recipients = _build_recipient_list(assignees)
    sent_records = []
    for uid, is_tl in recipients:
        blocks = _expiring_card(domain, uid, days_left_label, new_state, spend)
        resp = _dm.dm(
            slack_client, real_recipient=uid,
            text=(f'`{domain}` expires in ~{days_left_label} day(s). '
                  'are you still using it?'),
            blocks=blocks,
            dry_run_label=f'expiring_{days_left_label}:{domain}:{uid}',
        )
        rec = _extract_message_coords(resp, uid, is_tl)
        if rec:
            sent_records.append(rec)

    if not sent_records and not Config.LIFECYCLE_DRY_RUN:
        _dm.dm(
            slack_client, real_recipient=Config.TL_SLACK_USER_ID,
            text=(f':warning: `{domain}` expires in ~{days_left_label} day(s); '
                  f'every DM attempt failed (recipients: '
                  f'{[u for u, _ in recipients]}). Decide manually.'),
            dry_run_label=f'expiring_dm_failed:{domain}',
        )
        return 'skipped'

    # Dry run is observe-only — see the note in _prompt_mdb_idle. Don't
    # advance to AWAITING_* or the real expiry DM never fires once live.
    if Config.LIFECYCLE_DRY_RUN:
        return 'prompted'

    store.set_lifecycle_state(domain, S.AWAITING_MDB_USAGE_RESPONSE)
    store.bump_last_prompted_at(domain)
    store.record_prompt_recipients(domain, sent_records)
    store.record_event(
        domain, 'prompted_mdb_usage', actor='cron',
        from_state=from_state, to_state=S.AWAITING_MDB_USAGE_RESPONSE,
        metadata={
            'spend': spend,
            'recipients': [r['recipient_slack_id'] for r in sent_records],
            'days_until_expiry': days_left_label,
            'expiring_state': new_state,
        },
    )
    return 'prompted'


def _dedup_ok(row) -> bool:
    """Defensive 23h dedup. The state-machine filter
    (exclude_states=AWAITING_*) already prevents most duplicate prompts;
    this is a belt-and-braces guard for cases where state was manually
    cleared but the prompt was sent recently.

    Must compare at DATETIME precision, not date — coercing to a date
    measures the window from midnight, which makes the guard pass or
    fail depending on the time of day (a domain prompted 5 minutes ago
    could read as "23h ago" if it's late evening). Bug caught 2026-05-14.
    """
    import datetime as _dt
    last = row.get('last_prompted_at')
    if last is None:
        return True
    # Postgres hands us a datetime (tz-aware); SQLite an ISO string.
    if isinstance(last, _dt.datetime):
        last_dt = last
    elif isinstance(last, str):
        try:
            last_dt = _dt.datetime.fromisoformat(last.strip())
        except ValueError:
            return True  # unparseable → don't block the prompt
    else:
        return True
    # datetime.now() is naive; strip tzinfo so the subtraction is valid.
    if last_dt.tzinfo is not None:
        last_dt = last_dt.replace(tzinfo=None)
    age_h = (_dt.datetime.now() - last_dt).total_seconds() / 3600
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


# ─── SLA escalator ────────────────────────────────────────────────────────
# Second cron pass — scans rows in AWAITING_MDB_* whose last_prompted_at is
# older than LIFECYCLE_MDB_RESPONSE_SLA_HOURS, and DMs TL with override
# buttons. After escalation the row's state moves to AWAITING_TL_OVERRIDE_*
# so we don't re-escalate on the next run.

def run_sla_escalation(slack_client=None) -> Dict[str, int]:
    """Walk every MDB-side AWAITING row past SLA and DM TL with the
    override card. Returns counters for the cron's stdout log."""
    if slack_client is None:
        slack_client = _make_slack_client()

    sla_h = Config.LIFECYCLE_MDB_RESPONSE_SLA_HOURS
    rows = store.get_awaiting_domains_past_sla(
        awaiting_states=S.AWAITING_MDB_STATES, hours_ago=sla_h,
    )
    logger.info(
        'sla escalator starting — %d rows past SLA (%dh), dry_run=%s',
        len(rows), sla_h, Config.LIFECYCLE_DRY_RUN,
    )

    counters = {'escalated': 0, 'errors': 0}
    for row in rows:
        try:
            _escalate_to_tl(slack_client, row)
            counters['escalated'] += 1
        except Exception:
            logger.exception(
                'sla escalation failed for domain=%s', row.get('domain'),
            )
            counters['errors'] += 1

    logger.info('sla escalator finished — %s', counters)
    return counters


def _escalate_to_tl(slack_client, row) -> None:
    """Post the matching TL override card. Branches on the current
    AWAITING_MDB_* state to know which 2 buttons to offer."""
    domain = row['domain']
    current = row.get('lifecycle_state')

    if current == S.AWAITING_MDB_USAGE_RESPONSE:
        new_state = S.AWAITING_TL_OVERRIDE_USAGE
        blocks = _tl_override_usage_card(row)
    elif current == S.AWAITING_MDB_INVENTORY_RESPONSE:
        new_state = S.AWAITING_TL_OVERRIDE_INVENTORY
        blocks = _tl_override_inventory_card(row)
    else:
        logger.warning(
            'sla escalator saw domain=%s in unexpected state=%s — skipping',
            domain, current,
        )
        return

    sent = _dm.dm(
        slack_client, real_recipient=Config.TL_SLACK_USER_ID,
        text=(f':alarm_clock: MDB has not responded about `{domain}` in '
              f'{Config.LIFECYCLE_MDB_RESPONSE_SLA_HOURS}h. your call.'),
        blocks=blocks,
        dry_run_label=f'tl_escalation:{domain}',
    )
    if sent is None:
        logger.warning(
            'sla escalator could not DM TL for %s (no TL_SLACK_USER_ID?)',
            domain,
        )
        return

    store.set_lifecycle_state(domain, new_state)
    store.bump_last_prompted_at(domain)
    store.record_event(
        domain, 'escalated_to_tl', actor='cron',
        from_state=current, to_state=new_state,
        metadata={
            'sla_hours': Config.LIFECYCLE_MDB_RESPONSE_SLA_HOURS,
            'previous_assigned_to': row.get('assigned_to'),
        },
    )


def _tl_override_usage_card(row) -> list:
    """Card posted when an MDB ghosts an EXPIRING DM. TL picks renew or
    let-it-lapse."""
    domain = row['domain']
    payload = json.dumps({
        'domain': domain,
        'assigned_to': row.get('assigned_to') or '',
    })
    expire_at = row.get('expire_at')
    return [
        {'type': 'section', 'text': {
            'type': 'mrkdwn',
            'text': (
                f':alarm_clock: *MDB ghosted: `{domain}`*\n'
                f'• Was assigned to: <@{_dm.normalise_slack_id(row.get("assigned_to")) or "unknown"}>\n'
                f'• Expires: `{expire_at}`\n\n'
                'They had 48h to confirm. Decide for them:'
            ),
        }},
        {'type': 'actions', 'elements': [
            {'type': 'button', 'action_id': 'lifecycle_tl_force_renew',
             'text': {'type': 'plain_text', 'text': ':moneybag: Tell Utkarsh to renew'},
             'style': 'primary', 'value': payload},
            {'type': 'button', 'action_id': 'lifecycle_tl_force_disable_renew',
             'text': {'type': 'plain_text', 'text': ':no_entry_sign: Let it lapse'},
             'style': 'danger', 'value': payload},
        ]},
    ]


def _tl_override_inventory_card(row) -> list:
    """Card posted when an MDB ghosts an IDLE DM. TL picks push or
    snooze 30."""
    domain = row['domain']
    payload = json.dumps({
        'domain': domain,
        'assigned_to': row.get('assigned_to') or '',
    })
    return [
        {'type': 'section', 'text': {
            'type': 'mrkdwn',
            'text': (
                f':alarm_clock: *MDB ghosted: `{domain}`*\n'
                f'• Was assigned to: <@{_dm.normalise_slack_id(row.get("assigned_to")) or "unknown"}>\n'
                '• Has had no spend in last 30d.\n\n'
                'They had 48h to respond. Decide for them:'
            ),
        }},
        {'type': 'actions', 'elements': [
            {'type': 'button', 'action_id': 'lifecycle_tl_force_push',
             'text': {'type': 'plain_text', 'text': ':package: Push to inventory'},
             'style': 'primary', 'value': payload},
            {'type': 'button', 'action_id': 'lifecycle_tl_force_keep_30',
             'text': {'type': 'plain_text', 'text': ':zzz: Force keep 30 days'},
             'value': payload},
        ]},
    ]
