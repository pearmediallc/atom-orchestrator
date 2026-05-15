"""Slack DM wrapper for the lifecycle bot.

Centralises three concerns every lifecycle DM has to honour:

  • LIFECYCLE_DRY_RUN — when True, no DMs go out; we log intent only.
    First 48h of any deploy runs in this mode so we can verify the
    classifier's decisions on real data without spamming MDBs.

  • DEV_REROUTE_DMS_TO — when set, all TL/Utkarsh/MDB DMs land in the
    dev's inbox instead of going to the real recipient. Same seam the
    Phase 7 worker uses (see Config.route_recipient).

  • Slack ID normalisation — assigned_to values can be either a bare
    user ID ('U_ABCDEF') or 'Slack:U_ABCDEF' (legacy from Path B).
    Strip the prefix at DM-send time so we keep `requested_by` /
    `assigned_to` as the single source of truth in the DB.

Every lifecycle handler + the scan orchestrator goes through dm() —
that's how we guarantee DRY_RUN is honoured everywhere.
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional

from config import Config

logger = logging.getLogger(__name__)

# How many times to retry a 'ratelimited' response before giving up. Two
# retries plus the initial attempt = up to three sends per DM. Beyond
# that the burst is too sustained to handle from here — let the row's
# per-row exception handler log it and the next cron run pick it up.
_RATELIMIT_MAX_RETRIES = 2


def normalise_slack_id(value: Optional[str]) -> Optional[str]:
    """Convert a stored assigned_to / requested_by value to a Slack ID
    we can pass to chat_postMessage(channel=…). None for empty inputs.

    Strips the 'Slack:' prefix that confirm_purchased writes into
    requested_by. Keeps the DB value as a single source of truth (the
    prefix is meaningful — tells us the domain came in via the bot —
    so we don't strip it at write time)."""
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    if value.startswith('Slack:'):
        value = value[6:].strip()
    return value or None


def get_mdb_slack_ids_for_domain(
    domain: str, *, row: Optional[dict] = None,
) -> List[str]:
    """Return all Slack user IDs to DM for this domain's MDB(s).

    Authoritative source: domain_assignments (Phase E). The legacy
    `domains.assigned_to` is read only as a fallback for rows the
    backfill hasn't reached yet — once backfill_assignments has run on
    prod, branch 1 handles every resolvable row.

    Order of precedence:
      1. Active rows in domain_assignments (excludes deleted users)
      2. Legacy domains.assigned_to if it's already a Slack ID
      3. Legacy domains.assigned_to resolved via slack_users name match
      4. Empty list (domain is unassigned → goes to inventory pool)
    """
    from inventory import store as _store

    current = _store.current_assignments_for_domain(domain)
    if current:
        return [a['slack_user_id'] for a in current
                if not (a.get('deleted') in (1, True))]

    if row is None:
        row = _store.get_domain(domain)
    if not row:
        return []
    legacy = (row.get('assigned_to') or '').strip()
    if not legacy:
        return []
    if legacy.startswith('Slack:'):
        legacy = legacy[6:].strip()
    # Slack-ID heuristic: starts with U or W, only alphanumerics +
    # underscores, no spaces. Real Slack IDs match; test fixtures like
    # 'U_NEERAJ' also match. Free-text names ('Neeraj Nitin Tanish')
    # don't (spaces) and fall through to alias resolution.
    if (legacy and legacy[0] in ('U', 'W') and len(legacy) >= 2
            and all(c.isalnum() or c == '_' for c in legacy)):
        return [legacy]
    uid = _store.lookup_slack_id_by_alias(legacy)
    return [uid] if uid else []


def dm(
    client,
    *,
    real_recipient: Optional[str],
    text: str,
    blocks: Optional[List[dict]] = None,
    dry_run_label: str = '',
) -> Optional[dict]:
    """Send a DM to `real_recipient`, with reroute + dry-run applied.

    Returns the chat_postMessage response dict on a real send, a
    dry-run sentinel dict on dry-run, or None if no recipient could be
    resolved.
    """
    recipient = normalise_slack_id(real_recipient)
    if not recipient:
        logger.warning(
            'lifecycle.dm: cannot send (no recipient resolved) — text=%r',
            text[:80],
        )
        return None

    routed = Config.route_recipient(recipient)

    if Config.LIFECYCLE_DRY_RUN:
        logger.info(
            'lifecycle.dm DRY_RUN[%s]: would DM %s (real=%s) — text=%r',
            dry_run_label or 'unknown', routed, recipient, text[:160],
        )
        return {'dry_run': True, 'recipient': routed,
                'real_recipient': recipient,
                'label': dry_run_label}

    # Retry on Slack's 'ratelimited' response, respecting the Retry-After
    # header. Without this the lifecycle catch-up wave (~700 sends to
    # the TL's channel after the first live flip) burns through the
    # ~1/sec per-channel limit and every send after the first dozen
    # cascade-fails. With retry + the post-send pacing below, the burst
    # paces itself naturally.
    for attempt in range(_RATELIMIT_MAX_RETRIES + 1):
        try:
            response = client.chat_postMessage(
                channel=routed,
                text=text,
                blocks=blocks,
            )
            # Pace under Slack's ~1/sec per-channel cap so a sequence of
            # sends (e.g. the cron's idle/expiring loop) doesn't trip the
            # limit on the next iteration. Skipped on dry-run above.
            if Config.LIFECYCLE_DM_PACE_SECONDS > 0:
                time.sleep(Config.LIFECYCLE_DM_PACE_SECONDS)
            return response
        except Exception as e:  # noqa: BLE001 — slack_sdk's SlackApiError
            err = None
            resp = getattr(e, 'response', None)
            if resp is not None:
                try:
                    err = resp.get('error')
                except Exception:
                    err = None
            if err == 'ratelimited' and attempt < _RATELIMIT_MAX_RETRIES:
                retry_after = Config.LIFECYCLE_DM_RETRY_SLEEP_SECONDS
                try:
                    headers = getattr(resp, 'headers', {}) or {}
                    retry_after = float(headers.get('Retry-After',
                                                    retry_after))
                except (TypeError, ValueError):
                    pass
                logger.warning(
                    'lifecycle.dm: ratelimited posting to %s (attempt '
                    '%d/%d), sleeping %ss then retrying',
                    routed, attempt + 1, _RATELIMIT_MAX_RETRIES + 1,
                    retry_after,
                )
                time.sleep(retry_after)
                continue
            raise
    # Loop exhausted — should be unreachable (last iteration either
    # returns or raises) but defend against it anyway.
    return None
