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
