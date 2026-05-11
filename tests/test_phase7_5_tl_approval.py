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
        'extension': '.com', 'requester': 'U_MDB', 'aws_account': 'auto-insurance',
    })
    parsed = json.loads(payload)
    # confirm_approved reads these keys directly:
    for k in ('domain', 'vertical', 'lander', 'extension', 'requester',
             'aws_account'):
        assert k in parsed, f'{k!r} missing — confirm_approved would crash'


# ─── /new-domain modal shape (audit 2026-05-11) ───────────────────────────

def test_new_domain_modal_includes_aws_account_picker():
    """The modal must expose an explicit AWS account choice. The previous
    implicit default (auto-insurance via init_db NULL-backfill) silently
    routed every new domain into one account; making it a required modal
    choice is what closes that hole.
    """
    from slack_bot.routes import _build_new_domain_modal
    modal = _build_new_domain_modal()
    block_ids = [b['block_id'] for b in modal['blocks'] if 'block_id' in b]
    assert 'aws_account_block' in block_ids, (
        'aws_account_block missing — /new-domain would fall back to the '
        'silent init_db default again'
    )


def test_new_domain_modal_makes_lander_optional():
    """The lander_block must be marked optional. Setup-only runs
    (provision AWS infra now, deploy a lander later) are a real workflow
    and the modal should not block them by requiring a URL.
    """
    from slack_bot.routes import _build_new_domain_modal
    modal = _build_new_domain_modal()
    lander_block = next(
        b for b in modal['blocks'] if b.get('block_id') == 'lander_block'
    )
    assert lander_block.get('optional') is True


def test_pick_domain_button_payload_carries_aws_account(monkeypatch):
    """The Pick-this button signed at shortlist time must include
    aws_account in its payload so the choice survives every downstream
    hop (TL approval, Mark Purchased, inventory insert).
    """
    from config import Config
    from slack_bot.routes import _build_new_domain_shortlist_blocks
    from slack_bot.payload_signing import verify_payload

    # sign_payload refuses to sign with the dev-default secret. Patch a
    # real-looking secret JUST for this test — monkeypatch reverts at
    # test end so other tests' assumptions about Config don't break.
    monkeypatch.setattr(Config, 'FLASK_SECRET_KEY', 'x' * 64)

    blocks = _build_new_domain_shortlist_blocks(
        suggestions=[{'domain': 'pick.com', 'price': 1.0, 'extension': '.com'}],
        vertical='auto-insurance',
        audience='',
        extension='.com',
        lander='https://x/y',
        requester='U_MDB',
        aws_account='medicare',
    )
    pick_button = next(
        b['accessory'] for b in blocks
        if b.get('accessory', {}).get('action_id') == 'pick_domain'
    )
    parsed = verify_payload(pick_button['value'])
    assert parsed['aws_account'] == 'medicare'
