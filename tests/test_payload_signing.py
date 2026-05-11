"""Unit tests for slack_bot.payload_signing (audit #14 fix).

Covers the contract:
  • sign_payload → verify_payload round-trips arbitrary dicts
  • a tampered payload (any field modified after signing) is rejected
    via BadSignature
  • legacy unsigned payloads (no _sig field) pass through verify_payload
    unchanged — this is the deliberate backward-compat path so in-flight
    Slack messages from before this module shipped don't break
  • a signature from a DIFFERENT secret fails verification
  • reserved fields (_sig, _alg) raise on sign-time
  • signing with the placeholder default secret raises (production
    must set FLASK_SECRET_KEY explicitly)
  • _alg downgrade attempts are rejected
"""
import json

import pytest

from config import Config
from slack_bot.payload_signing import (
    BadSignature,
    WeakSecretError,
    sign_payload,
    verify_payload,
)


@pytest.fixture(autouse=True)
def strong_secret(monkeypatch):
    """Every test runs with a non-default FLASK_SECRET_KEY so signing
    actually exercises the HMAC path. Tests that exercise the
    weak-secret guardrail explicitly override this."""
    monkeypatch.setattr(
        Config, 'FLASK_SECRET_KEY',
        'test-strong-secret-do-not-use-in-prod',
    )


# ─── Round-trip ───────────────────────────────────────────────────────────

def test_sign_then_verify_returns_original_data():
    payload = {
        'domain': 'example.com',
        'vertical': 'auto-insurance',
        'requester': 'U06ABC',
        'lander': 'https://src.com/folder/',
    }
    signed = sign_payload(payload)
    out = verify_payload(signed)
    assert out == payload


def test_sign_then_verify_strips_reserved_fields():
    """The dict returned to handlers must NOT carry _sig or _alg."""
    signed = sign_payload({'domain': 'x.com'})
    out = verify_payload(signed)
    assert '_sig' not in out
    assert '_alg' not in out


def test_signed_value_is_valid_json_string():
    signed = sign_payload({'domain': 'x.com'})
    parsed = json.loads(signed)
    # Both reserved fields must be present in the wire format.
    assert parsed['domain'] == 'x.com'
    assert '_sig' in parsed
    assert '_alg' in parsed


# ─── Tamper detection ─────────────────────────────────────────────────────

def test_tampered_field_rejected():
    """An attacker who edits the JSON to swap domain after signing
    must be caught — the whole point of this batch."""
    signed = sign_payload({
        'domain': 'good.com',
        'requester': 'U_GOOD',
    })
    parsed = json.loads(signed)
    parsed['domain'] = 'attacker.com'
    tampered = json.dumps(parsed)

    with pytest.raises(BadSignature):
        verify_payload(tampered)


def test_tampered_signature_rejected():
    signed = sign_payload({'domain': 'good.com'})
    parsed = json.loads(signed)
    # Flip one hex digit.
    parsed['_sig'] = ('0' if parsed['_sig'][0] != '0' else '1') + parsed['_sig'][1:]
    tampered = json.dumps(parsed)

    with pytest.raises(BadSignature):
        verify_payload(tampered)


def test_signature_from_different_secret_rejected(monkeypatch):
    """Signing under one secret then verifying under another fails —
    e.g. a misconfigured deploy that uses the wrong env var won't
    silently accept its own signatures and someone else's."""
    signed = sign_payload({'domain': 'x.com'})
    monkeypatch.setattr(
        Config, 'FLASK_SECRET_KEY', 'different-secret-after-rotation',
    )
    with pytest.raises(BadSignature):
        verify_payload(signed)


# ─── Backward compatibility ───────────────────────────────────────────────

def test_legacy_unsigned_payload_passes_through(caplog):
    """In-flight Slack messages from before this module shipped have
    no _sig field. verify_payload must accept them so pending
    workflow buttons keep working through the deploy."""
    legacy = json.dumps({'domain': 'legacy.com', 'requester': 'U_OLD'})
    out = verify_payload(legacy)
    assert out == {'domain': 'legacy.com', 'requester': 'U_OLD'}


def test_legacy_payload_logs_warning_so_we_can_track_phase_out():
    import logging
    legacy = json.dumps({'domain': 'legacy.com'})

    # Capture only at the right logger to avoid ambient noise.
    log_records = []

    class _Cap(logging.Handler):
        def emit(self, record):
            log_records.append(record)

    handler = _Cap(level=logging.WARNING)
    target = logging.getLogger('slack_bot.payload_signing')
    target.addHandler(handler)
    target.setLevel(logging.WARNING)
    try:
        verify_payload(legacy)
    finally:
        target.removeHandler(handler)

    assert any(
        'unsigned button payload' in r.getMessage().lower()
        for r in log_records
    )


# ─── Algorithm rotation defence ──────────────────────────────────────────

def test_unknown_algorithm_rejected():
    """An attacker can't strip _sig and supply a fake _alg to bypass —
    any _alg other than 'sha256-v1' is rejected."""
    payload = {'domain': 'x.com', '_alg': 'md5-v0', '_sig': 'whatever'}
    with pytest.raises(BadSignature):
        verify_payload(json.dumps(payload))


# ─── Reserved-field guard at sign time ────────────────────────────────────

def test_reserved_fields_raise_on_sign():
    with pytest.raises(ValueError):
        sign_payload({'_sig': 'attacker-supplied'})
    with pytest.raises(ValueError):
        sign_payload({'_alg': 'sha256-v1'})


# ─── Weak-secret guard ────────────────────────────────────────────────────

def test_default_placeholder_secret_refuses_to_sign(monkeypatch):
    """Production must set FLASK_SECRET_KEY explicitly. Refuse to
    create signatures with the dev placeholder so a misconfigured
    deploy can't ship predictable-secret signatures."""
    monkeypatch.setattr(
        Config, 'FLASK_SECRET_KEY', 'dev-only-not-for-prod',
    )
    with pytest.raises(WeakSecretError):
        sign_payload({'domain': 'x.com'})


def test_empty_secret_refuses_to_sign(monkeypatch):
    monkeypatch.setattr(Config, 'FLASK_SECRET_KEY', '')
    with pytest.raises(WeakSecretError):
        sign_payload({'domain': 'x.com'})


# ─── Malformed input ──────────────────────────────────────────────────────

def test_non_object_payload_raises():
    with pytest.raises(ValueError):
        verify_payload('"just a string"')
    with pytest.raises(ValueError):
        verify_payload('[1, 2, 3]')


def test_invalid_json_raises():
    with pytest.raises(ValueError):
        verify_payload('not json at all')
