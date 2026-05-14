"""Unit tests for the slack_bot.routes shared helpers (audit #13 fix).

Covers:
  • _verify_button_click — happy path, tampered-sig path, malformed
    JSON path, missing-actions-key path. Replaces the seven copy-
    pasted try/except blocks before this batch.
  • _build_confirmed_card — block kit shape, action label injection,
    extra_context appending, mrkdwn confirmer mention.
  • orchestrator.tasks_runner.enqueue_phase7 vs the legacy
    enqueue_path_a / enqueue_path_b shims — both paths must
    produce a task row of the right kind.
"""
import json

import pytest

from config import Config
from slack_bot.payload_signing import sign_payload
from slack_bot.routes import (
    _build_confirmed_card,
    _verify_button_click,
)


@pytest.fixture(autouse=True)
def strong_secret(monkeypatch):
    monkeypatch.setattr(
        Config, 'FLASK_SECRET_KEY',
        'test-strong-secret-do-not-use-in-prod',
    )


# ─── _verify_button_click ─────────────────────────────────────────────────

def _body(value: str, action_id: str = 'confirm_deployed',
          user_id: str = 'U_TEST') -> dict:
    """Minimal Slack interactive-button body shape for the helper."""
    return {
        'actions': [{'value': value, 'action_id': action_id}],
        'user': {'id': user_id},
    }


def test_verify_button_click_returns_payload_on_valid_sig():
    payload = {'domain': 'x.com', 'requester': 'U_REQ'}
    body = _body(sign_payload(payload))
    out = _verify_button_click(body)
    assert out == payload


def test_verify_button_click_returns_None_on_tampered_sig():
    payload = {'domain': 'good.com'}
    signed = sign_payload(payload)
    parsed = json.loads(signed)
    parsed['domain'] = 'attacker.com'
    body = _body(json.dumps(parsed))

    assert _verify_button_click(body) is None


def test_verify_button_click_returns_None_on_malformed_json():
    body = _body('not even json')
    assert _verify_button_click(body) is None


def test_verify_button_click_returns_None_on_missing_actions():
    """Slack sometimes delivers bodies that don't have the expected
    shape (rare; usually a developer error). Helper must not crash."""
    assert _verify_button_click({}) is None
    assert _verify_button_click({'actions': []}) is None


def test_verify_button_click_accepts_legacy_unsigned_payload():
    """In-flight buttons from before HMAC was deployed don't have
    _sig — they pass through (a warning is logged inside
    verify_payload)."""
    legacy = json.dumps({'domain': 'legacy.com', 'requester': 'U_OLD'})
    body = _body(legacy)
    out = _verify_button_click(body)
    assert out == {'domain': 'legacy.com', 'requester': 'U_OLD'}


# ─── _build_confirmed_card ───────────────────────────────────────────────

def test_build_confirmed_card_minimal():
    blocks = _build_confirmed_card(
        action_label='Deployed',
        target='example.com',
        confirmer_id='U_OPS',
    )
    assert len(blocks) == 2
    assert blocks[0]['type'] == 'header'
    assert ':white_check_mark: Deployed: example.com' in blocks[0]['text']['text']
    assert blocks[1]['type'] == 'context'
    assert blocks[1]['elements'][0]['text'] == 'Confirmed by <@U_OPS>.'


def test_build_confirmed_card_appends_extra_context():
    blocks = _build_confirmed_card(
        action_label='Purchased',
        target='example.com',
        confirmer_id='U_OPS',
        extra_context='Added to inventory. Triggering ATOM setup.',
    )
    ctx = blocks[1]['elements'][0]['text']
    # Standard prefix preserved.
    assert ctx.startswith('Confirmed by <@U_OPS>.')
    # Extra context appended after a separator.
    assert 'Added to inventory.' in ctx
    assert 'Triggering ATOM setup.' in ctx


def test_build_confirmed_card_renders_action_label_in_header():
    """Same helper, different action labels — the audit complaint was
    that drift between Path A's 'Deployed' and Path B's 'Purchased'
    cards was easy to introduce. Helper makes them structurally
    identical, varying only the label text."""
    deployed = _build_confirmed_card(
        action_label='Deployed', target='x.com', confirmer_id='U',
    )
    purchased = _build_confirmed_card(
        action_label='Purchased', target='x.com', confirmer_id='U',
    )
    assert deployed[0]['type'] == purchased[0]['type'] == 'header'
    assert ':white_check_mark: Deployed:' in deployed[0]['text']['text']
    assert ':white_check_mark: Purchased:' in purchased[0]['text']['text']


# ─── tasks_runner unification ────────────────────────────────────────────

def test_enqueue_phase7_path_a_creates_path_a_task(tmp_inventory):
    """Audit #13: enqueue_phase7(kind=PATH_A) replaces the legacy
    enqueue_path_a function. Both must result in a row with the
    right kind constant on the task table."""
    from orchestrator import tasks
    from orchestrator.tasks_runner import enqueue_phase7

    # Stub out dispatch_worker_for so the test doesn't actually spawn
    # a thread that would try to log into ATOM.
    import orchestrator.tasks_runner as runner_mod
    original = runner_mod.dispatch_worker_for
    runner_mod.dispatch_worker_for = lambda tid: None
    try:
        task_id = enqueue_phase7(
            kind=tasks.TASK_KIND_PATH_A,
            channel='C1', message_ts='1.0', target_domain='x.com',
            vertical='auto-insurance', requester='U_R',
            lander_url='https://src.com/',
        )
    finally:
        runner_mod.dispatch_worker_for = original

    from inventory import store
    with store._conn() as c:
        cur = store._execute(
            c, 'SELECT kind FROM phase7_tasks WHERE id = ?', (task_id,),
        )
        row = cur.fetchone()
    assert row['kind'] == tasks.TASK_KIND_PATH_A


def test_legacy_enqueue_path_b_shim_still_creates_path_b_task(tmp_inventory):
    """The backward-compat shim enqueue_path_b(...) routes through
    enqueue_phase7(kind=PATH_B). Test ensures the shim wasn't broken
    by the rename."""
    from orchestrator import tasks
    from orchestrator.tasks_runner import enqueue_path_b
    import orchestrator.tasks_runner as runner_mod

    original = runner_mod.dispatch_worker_for
    runner_mod.dispatch_worker_for = lambda tid: None
    try:
        task_id = enqueue_path_b(
            channel='C1', message_ts='1.0', target_domain='x.com',
            vertical='auto-insurance', requester='U_R',
            lander_url='https://src.com/',
        )
    finally:
        runner_mod.dispatch_worker_for = original

    from inventory import store
    with store._conn() as c:
        cur = store._execute(
            c, 'SELECT kind FROM phase7_tasks WHERE id = ?', (task_id,),
        )
        row = cur.fetchone()
    assert row['kind'] == tasks.TASK_KIND_PATH_B


# ─── Phase: external /new-domain requester threading ──────────────────────

from slack_bot.routes import _build_new_domain_shortlist_blocks
from slack_bot.payload_signing import verify_payload


def _pick_and_refresh_payloads(blocks):
    """Pull the signed pick_domain + refresh payloads back out of a
    shortlist block list."""
    pick, refresh = None, None
    for b in blocks:
        acc = b.get('accessory')
        if acc and acc.get('action_id') == 'pick_domain':
            pick = verify_payload(acc['value'])
        if b.get('type') == 'actions':
            for el in b['elements']:
                if el.get('action_id') == 'refresh_domain_suggestions':
                    refresh = verify_payload(el['value'])
    return pick, refresh


def test_shortlist_threads_external_requester_into_payloads():
    """external_requester must ride through both the pick_domain and the
    'Show 5 more' refresh payloads so it survives every hop down to the
    inventory write."""
    blocks = _build_new_domain_shortlist_blocks(
        suggestions=[{'domain': 'acmecorp-quote.com', 'price': 9.99,
                      'extension': '.com'}],
        vertical='auto-insurance', audience='', extension='.com',
        lander='', requester='U_OPERATOR', aws_account='auto-insurance',
        external_requester='John from AcmeCorp',
    )
    pick, refresh = _pick_and_refresh_payloads(blocks)
    assert pick is not None and refresh is not None
    assert pick['external_requester'] == 'John from AcmeCorp'
    assert refresh['external_requester'] == 'John from AcmeCorp'
    # The operator stays the requester/owner — external is just a name.
    assert pick['requester'] == 'U_OPERATOR'


def test_shortlist_external_requester_defaults_empty_for_internal():
    """Internal requests (no external name) carry an empty string, not a
    missing key — downstream readers use .get() but consistency helps."""
    blocks = _build_new_domain_shortlist_blocks(
        suggestions=[{'domain': 'x.com', 'price': 9.99, 'extension': '.com'}],
        vertical='v', audience='', extension='.com', lander='',
        requester='U_MDB', aws_account='acct',
    )
    pick, refresh = _pick_and_refresh_payloads(blocks)
    assert pick['external_requester'] == ''
    assert refresh['external_requester'] == ''


# ─── /new-domain-external modal ──────────────────────────────────────────

def test_external_modal_has_required_name_field_and_no_mdb_picker():
    """The external modal swaps the optional MDB picker for a REQUIRED
    external-requester name field, and shares the rest with the internal
    modal via the same callback_id + common blocks."""
    from slack_bot.routes import (
        _build_new_domain_external_modal, _build_new_domain_modal,
    )
    ext = _build_new_domain_external_modal()
    internal = _build_new_domain_modal()
    ext_ids = {b.get('block_id') for b in ext['blocks']}
    int_ids = {b.get('block_id') for b in internal['blocks']}

    # External: required name field, no MDB picker.
    assert 'external_requester_block' in ext_ids
    assert 'mdb_block' not in ext_ids
    ext_block = next(b for b in ext['blocks']
                     if b.get('block_id') == 'external_requester_block')
    assert ext_block['optional'] is False

    # Internal: MDB picker, no external field.
    assert 'mdb_block' in int_ids
    assert 'external_requester_block' not in int_ids

    # Both carry the same downstream blocks + share the submit handler.
    for shared in ('vertical_block', 'aws_account_block', 'extension_block'):
        assert shared in ext_ids and shared in int_ids
    assert ext['callback_id'] == internal['callback_id'] == 'new_domain_modal'
