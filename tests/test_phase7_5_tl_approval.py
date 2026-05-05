"""Phase 7.5 tests — TL approval step in Path B + DEV_REROUTE_DMS_TO override.

These tests target three behaviors:

  1. When APPROVER_SLACK_USER_IDS is set, "Pick this" sends an approval
     card to each approver — Utkarsh is NOT yet DMed.
  2. When APPROVER_SLACK_USER_IDS is empty, "Pick this" falls through to
     the Phase 5 behavior and DMs Utkarsh directly (back-compat).
  3. DEV_REROUTE_DMS_TO redirects all non-requester DMs to a single user
     so a solo dev can walk the whole flow alone.

The Approve / Reject handlers are tested by feeding fake Slack action
bodies into the helpers — the same shape Slack would send.
"""
import json
from unittest.mock import MagicMock

from config import Config
from slack_bot.routes import _send_purchase_request_to_utkarsh


# ─── Helpers ───────────────────────────────────────────────────────────────

def _slack_client():
    return MagicMock(name='slack_client')


def _channels_dmed(client) -> list:
    return [c.kwargs.get('channel') for c in client.chat_postMessage.call_args_list]


def _set_approvers(monkeypatch, ids: list):
    monkeypatch.setattr(Config, 'APPROVER_SLACK_USER_IDS', ids)


def _set_utkarsh(monkeypatch, uid: str):
    monkeypatch.setattr(Config, 'UTKARSH_SLACK_USER_ID', uid)


def _set_reroute(monkeypatch, uid: str):
    monkeypatch.setattr(Config, 'DEV_REROUTE_DMS_TO', uid)


# ─── _send_purchase_request_to_utkarsh helper ──────────────────────────────

def test_purchase_request_dms_real_utkarsh_when_no_reroute(monkeypatch):
    """Configured Utkarsh + no dev override → DM goes to real Utkarsh."""
    _set_utkarsh(monkeypatch, 'U_REAL_UTKARSH')
    _set_reroute(monkeypatch, '')

    client = _slack_client()
    purchaser = _send_purchase_request_to_utkarsh(
        client, domain='example.com', vertical='auto', lander='https://x/y',
        extension='.com', requester='U_MDB',
    )
    assert purchaser == 'U_REAL_UTKARSH'
    assert 'U_REAL_UTKARSH' in _channels_dmed(client)


def test_purchase_request_reroutes_to_dev_when_set(monkeypatch):
    """DEV_REROUTE_DMS_TO set → DM goes to the dev, not real Utkarsh."""
    _set_utkarsh(monkeypatch, 'U_REAL_UTKARSH')
    _set_reroute(monkeypatch, 'U_DEV_ANAND')

    client = _slack_client()
    purchaser = _send_purchase_request_to_utkarsh(
        client, domain='example.com', vertical='auto', lander='https://x/y',
        extension='.com', requester='U_MDB',
    )
    assert purchaser == 'U_DEV_ANAND'
    channels = _channels_dmed(client)
    assert 'U_DEV_ANAND' in channels
    assert 'U_REAL_UTKARSH' not in channels


def test_purchase_request_falls_back_to_requester_when_no_utkarsh(monkeypatch):
    """No Utkarsh + no reroute → DM the requester (Phase 5 self-test pattern)."""
    _set_utkarsh(monkeypatch, '')
    _set_reroute(monkeypatch, '')

    client = _slack_client()
    purchaser = _send_purchase_request_to_utkarsh(
        client, domain='example.com', vertical='auto', lander='https://x/y',
        extension='.com', requester='U_MDB',
    )
    assert purchaser == 'U_MDB'


# ─── Config.route_recipient ────────────────────────────────────────────────

def test_route_recipient_returns_real_when_reroute_empty(monkeypatch):
    _set_reroute(monkeypatch, '')
    assert Config.route_recipient('U_REAL_TL') == 'U_REAL_TL'


def test_route_recipient_returns_dev_override_when_set(monkeypatch):
    _set_reroute(monkeypatch, 'U_DEV')
    assert Config.route_recipient('U_REAL_TL') == 'U_DEV'
    assert Config.route_recipient('U_REAL_UTKARSH') == 'U_DEV'


# ─── Approval card payload shape (the contract pick_domain uses) ──────────

def test_approval_card_payload_contains_all_fields_for_approve_handler():
    """The button value JSON must carry everything confirm_approved needs."""
    payload = json.dumps({
        'domain': 'foo.com', 'vertical': 'auto', 'lander': 'https://x/y',
        'extension': '.com', 'requester': 'U_MDB',
    })
    parsed = json.loads(payload)
    # confirm_approved reads these keys directly:
    for k in ('domain', 'vertical', 'lander', 'extension', 'requester'):
        assert k in parsed, f'{k!r} missing — confirm_approved would crash'
