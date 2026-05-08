"""Tests for lifecycle.classifier.classify_domain.

Pure-function tests — no DB, no HTTP, no Slack mocks needed. Each test
constructs a fake domain row + spend dict and asserts the resulting
state. Covers TL's two flows + every transition in the design doc.
"""
import datetime as dt

import pytest

from config import Config
from lifecycle import states as S
from lifecycle.classifier import classify_domain


# ─── Helpers ───────────────────────────────────────────────────────────────

TODAY = dt.date(2026, 6, 1)


def _row(**overrides) -> dict:
    """Build a domain row with sensible defaults for each test case."""
    base = {
        'domain': 'example.com',
        'lifecycle_state': None,
        'assigned_to': 'U_MDB',
        'expire_at': None,
        'last_active_at': None,
        'purchased_at': dt.datetime(2025, 1, 1),  # well past grace
    }
    base.update(overrides)
    return base


# ─── AWAITING_* protection ────────────────────────────────────────────────

@pytest.mark.parametrize('state', [
    S.AWAITING_MDB_USAGE_RESPONSE,
    S.AWAITING_MDB_INVENTORY_RESPONSE,
    S.AWAITING_UTKARSH_RENEW,
    S.AWAITING_UTKARSH_DISABLE_RENEW,
])
def test_classifier_does_not_touch_awaiting_states(state):
    """Cron MUST NOT re-classify domains waiting on a human click —
    that's how race conditions / re-prompts happen."""
    row = _row(lifecycle_state=state)
    spend = {'cost': 9999.0, 'revenue': 9999.0}  # even big spend is ignored
    assert classify_domain(row, spend, today=TODAY) is None


# ─── INVENTORY / unassigned ───────────────────────────────────────────────

def test_unassigned_domain_goes_to_inventory():
    row = _row(assigned_to=None, lifecycle_state=None)
    assert classify_domain(row, {}, today=TODAY) == S.INVENTORY


def test_unassigned_with_empty_string_assigned_to_treated_as_inventory():
    row = _row(assigned_to='', lifecycle_state=None)
    assert classify_domain(row, {}, today=TODAY) == S.INVENTORY


def test_unassigned_in_extended_state_left_alone():
    """A domain on a 30/15-day snooze must not get yanked back to
    INVENTORY just because assigned_to was cleared — the snooze
    must run its course first."""
    row = _row(assigned_to=None, lifecycle_state=S.EXTENDED_30)
    assert classify_domain(row, {}, today=TODAY) is None


# ─── ACTIVE / IDLE — TL Flow 2 trigger ────────────────────────────────────

def test_active_when_spend_above_threshold():
    row = _row()
    spend = {'cost': 50.0}
    assert classify_domain(row, spend, today=TODAY) == S.ACTIVE


def test_idle_when_no_spend_and_past_grace():
    row = _row(
        last_active_at=None,
        purchased_at=dt.datetime(2025, 1, 1),  # 17 months ago, well past grace
    )
    assert classify_domain(row, {}, today=TODAY) == S.IDLE


def test_idle_threshold_uses_active_spend_usd_config(monkeypatch):
    """Below LIFECYCLE_ACTIVE_SPEND_USD = idle. At-or-above = active."""
    monkeypatch.setattr(Config, 'LIFECYCLE_ACTIVE_SPEND_USD', 5.0)
    row = _row()
    assert classify_domain(row, {'cost': 4.99}, today=TODAY) == S.IDLE
    assert classify_domain(row, {'cost': 5.00}, today=TODAY) == S.ACTIVE


def test_within_grace_returns_none(monkeypatch):
    """Freshly assigned domain with $0 spend should NOT be flagged idle —
    it might just be mid-setup. Classifier returns None (skip)."""
    monkeypatch.setattr(Config, 'LIFECYCLE_ASSIGNMENT_GRACE_DAYS', 14)
    fresh_purchase = TODAY - dt.timedelta(days=5)
    row = _row(
        last_active_at=None,
        purchased_at=dt.datetime(fresh_purchase.year, fresh_purchase.month,
                                 fresh_purchase.day),
    )
    assert classify_domain(row, {}, today=TODAY) is None


def test_grace_uses_last_active_at_when_present():
    """If a domain was active recently, that beats the original purchase
    date — restarts the grace window, so a once-busy domain that just
    went quiet doesn't immediately get IDLE-flagged."""
    row = _row(
        purchased_at=dt.datetime(2024, 1, 1),  # ancient
        last_active_at=dt.datetime(2026, 5, 30),  # 2 days ago
    )
    assert classify_domain(row, {}, today=TODAY) is None


def test_no_reference_timestamp_skips_classification():
    """No purchased_at AND no last_active_at — classifier can't decide
    grace, so leaves the row alone. Real prod data shouldn't hit this
    (every row has purchased_at) but defensive coding."""
    row = _row(purchased_at=None, last_active_at=None)
    assert classify_domain(row, {}, today=TODAY) is None


# ─── EXPIRING_30 / 14 / 7 / 1 — TL Flow 1 trigger ─────────────────────────

@pytest.mark.parametrize('days_until_expiry,expected_state', [
    (45, S.ACTIVE),         # outside cascade entirely
    (30, S.EXPIRING_30),    # boundary of 30-day band
    (29, S.EXPIRING_30),    # inside 30-day band
    (15, S.EXPIRING_30),    # still in 30-day band (>14)
    (14, S.EXPIRING_14),    # boundary of 14-day band
    (8,  S.EXPIRING_14),    # inside 14-day band
    (7,  S.EXPIRING_7),     # boundary of 7-day band
    (2,  S.EXPIRING_7),     # inside 7-day band
    (1,  S.EXPIRING_1),     # boundary — last warning
    (0,  'EXPIRED'),        # today is the day → past
    (-5, 'EXPIRED'),        # past expiry
])
def test_expiry_cascade_picks_correct_bucket(days_until_expiry, expected_state):
    expire = TODAY + dt.timedelta(days=days_until_expiry)
    row = _row(
        expire_at=dt.datetime(expire.year, expire.month, expire.day),
    )
    spend = {'cost': 100.0}  # has spend → expiry path applies
    assert classify_domain(row, spend, today=TODAY) == expected_state


def test_expiring_skipped_when_no_spend():
    """TL spec: expiry warnings only fire when domain is actively
    spending. A 0-spend domain that's expiring takes the IDLE path
    instead — MDB will be asked about inventory, not renewal."""
    expire = TODAY + dt.timedelta(days=10)
    row = _row(
        expire_at=dt.datetime(expire.year, expire.month, expire.day),
        purchased_at=dt.datetime(2024, 1, 1),  # past grace
    )
    assert classify_domain(row, {}, today=TODAY) == S.IDLE


def test_active_when_expiry_is_far_off():
    """Spend present + expiry > 30 days = plain ACTIVE, no cascade."""
    far_off = TODAY + dt.timedelta(days=200)
    row = _row(
        expire_at=dt.datetime(far_off.year, far_off.month, far_off.day),
    )
    assert classify_domain(row, {'cost': 50.0}, today=TODAY) == S.ACTIVE


def test_active_when_expiry_unknown():
    """No expire_at on the row + spend present → ACTIVE (don't speculate)."""
    row = _row(expire_at=None)
    assert classify_domain(row, {'cost': 50.0}, today=TODAY) == S.ACTIVE


# ─── Date coercion (DB returns strings on SQLite, datetime on PG) ─────────

def test_classifier_handles_string_expire_at_from_sqlite():
    """SQLite returns timestamps as ISO strings — classifier must parse."""
    expire = TODAY + dt.timedelta(days=10)
    row = _row(expire_at=f'{expire.isoformat()} 00:00:00')
    assert classify_domain(row, {'cost': 50.0}, today=TODAY) == S.EXPIRING_14


def test_classifier_handles_string_purchased_at_from_sqlite():
    fresh = TODAY - dt.timedelta(days=5)
    row = _row(
        purchased_at=f'{fresh.isoformat()} 00:00:00',
        last_active_at=None,
    )
    # Within grace window → None.
    assert classify_domain(row, {}, today=TODAY) is None


# ─── Cascade configurability ───────────────────────────────────────────────

def test_classifier_respects_custom_cascade_days(monkeypatch):
    """If TL changes LIFECYCLE_EXPIRY_CASCADE_DAYS, classifier emits
    state names matching the new cascade."""
    monkeypatch.setattr(
        Config, 'LIFECYCLE_EXPIRY_CASCADE_DAYS', [60, 30, 5],
    )
    in_60 = TODAY + dt.timedelta(days=50)
    row = _row(
        expire_at=dt.datetime(in_60.year, in_60.month, in_60.day),
    )
    assert classify_domain(row, {'cost': 50.0}, today=TODAY) == 'EXPIRING_60'
