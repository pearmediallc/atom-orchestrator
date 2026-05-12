"""Pure classification logic for the daily lifecycle scan.

Given one domain row + its RedTrack spend data, decide what state the
domain should be in. No I/O — the classifier is fully unit-testable
and the orchestrator (lifecycle/scan.py, Phase B-next) handles all
the side effects (DMs, DB writes, event logging).

Decision tree (matches TL's spec + the design doc):

  current state in AWAITING_*?
    yes → return None  (cron must NOT touch domains waiting on a click)

  unassigned?
    yes → INVENTORY  (rotation pool — let it sit until someone claims it)

  has spend in last 30d (cost ≥ LIFECYCLE_ACTIVE_SPEND_USD)?
    yes:
      expire_at ≤ today  → EXPIRED
      expire_at within 1 day  → EXPIRING_1
      expire_at within 7 days → EXPIRING_7
      expire_at within 14 days → EXPIRING_14
      expire_at within 30 days → EXPIRING_30
      otherwise              → ACTIVE
    no:
      reference_date = max(last_active_at, purchased_at)
      days_since_active >= LIFECYCLE_ASSIGNMENT_GRACE_DAYS  → IDLE
      within grace period                                  → None  (skip)
"""
from __future__ import annotations

import datetime as _dt
from typing import Dict, Optional

from config import Config
from lifecycle import states as S


def classify_domain(
    row: Dict,
    spend_data: Optional[Dict[str, float]] = None,
    *,
    today: Optional[_dt.date] = None,
    current_assignees: Optional[list] = None,
) -> Optional[str]:
    """Decide what lifecycle_state `row` should be in.

    Returns the new state string, OR None if the cron should leave the
    row's lifecycle_state alone (currently waiting on human action OR
    within the post-assignment grace window).

    Args:
      row: a `domains` row as a dict.
      spend_data: `{cost, revenue, …}` for this domain from RedTrack.
      today: override for tests. Defaults to date.today().
      current_assignees: optional list of Slack user IDs from
        domain_assignments (Phase E). When None, classifier falls back
        to the legacy `row['assigned_to']` field. Pre-loading this in
        bulk via store.bulk_current_assignments() and passing it in
        avoids N+1 lookups when classifying 744 rows.
    """
    spend = spend_data or {}
    today = today or _dt.date.today()

    # 1. Awaiting human action — cron stays out.
    if row.get('lifecycle_state') in S.AWAITING_STATES:
        return None

    # 2. No assigned MDB → goes to inventory.
    # UNION semantics during migration: assigned if EITHER the new
    # domain_assignments table OR the legacy assigned_to column says so.
    # This way pre-backfill rows (where current_assignees is [] but
    # legacy column has a name) don't get incorrectly pushed to INVENTORY.
    has_new = bool(current_assignees)
    has_legacy = bool((row.get('assigned_to') or '').strip())
    is_assigned = has_new or has_legacy

    if not is_assigned:
        if row.get('lifecycle_state') in (
            S.EXTENDED_30, S.EXTENDED_15,
        ):
            return None
        return S.INVENTORY

    cost = float(spend.get('cost') or 0)
    has_spend = cost >= Config.LIFECYCLE_ACTIVE_SPEND_USD

    # 3. Active + maybe-expiring path.
    if has_spend:
        expire = _coerce_date(row.get('expire_at'))
        if expire is not None:
            days = (expire - today).days
            if days <= 0:
                return S.EXPIRED
            # Iterate cascade ascending — pick the SMALLEST bucket the
            # remaining days fit into. With cascade [1, 7, 14, 30]:
            #   days=14 → EXPIRING_14 (not EXPIRING_30)
            #   days=15 → EXPIRING_30 (still in the 30-day band)
            for cascade_day in sorted(Config.LIFECYCLE_EXPIRY_CASCADE_DAYS):
                if days <= cascade_day:
                    return f'EXPIRING_{cascade_day}'
        return S.ACTIVE

    # 4. No spend → idle eligibility check.
    # Don't flag a freshly-assigned domain as idle just because it
    # hasn't spent yet — give it the grace window first.
    reference = (
        _coerce_date(row.get('last_active_at'))
        or _coerce_date(row.get('purchased_at'))
    )
    if reference is None:
        # No reference timestamp at all — be conservative, leave alone.
        return None

    days_since = (today - reference).days
    if days_since >= Config.LIFECYCLE_ASSIGNMENT_GRACE_DAYS:
        return S.IDLE

    # Within grace window — let the next pass try again once enough
    # days have passed.
    return None


def _coerce_date(value) -> Optional[_dt.date]:
    """Return a date object regardless of whether the DB row gave us a
    datetime, a date, or a string (SQLite returns ISO-formatted strings
    with or without microseconds; Postgres returns datetime objects).
    None if unparseable."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        # fromisoformat accepts 'YYYY-MM-DD', 'YYYY-MM-DDTHH:MM:SS[.ffffff]',
        # and 'YYYY-MM-DD HH:MM:SS[.ffffff]' on Python 3.7+. Covers every
        # shape SQLite or Postgres could hand us.
        try:
            return _dt.datetime.fromisoformat(v).date()
        except ValueError:
            pass
        # Last-ditch: bare YYYY-MM-DD prefix.
        try:
            return _dt.datetime.strptime(v[:10], '%Y-%m-%d').date()
        except ValueError:
            return None
    return None
