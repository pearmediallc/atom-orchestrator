"""HMAC signing for Slack interactive button payloads (audit #14 fix).

Threat model — what this defends against:

  Slack's request signing already authenticates that an interactive
  request came from Slack's servers (Slack-Signing-Secret header).
  But it does NOT authenticate the *content* of `block.value` JSON
  blobs in messages. A malicious user with channel-write permission
  can post a Block Kit message whose Pick this / Mark Purchased /
  Mark Deployed button carries a tampered payload (e.g. domain
  swapped, requester impersonated). Another user clicking it would
  trigger our action with the forged values.

  This module appends an HMAC-SHA256 signature to every button
  payload we emit and verifies it on every button click. Forged
  payloads (or genuine payloads modified after the bot signed them)
  fail verification and are rejected at the handler boundary.

Backward compatibility — what this doesn't break:

  Buttons posted to Slack BEFORE this code lands have no `_sig`
  field. ``verify_payload`` treats a missing signature as a legacy
  passthrough — returns the data dict, lets the handler proceed.
  This avoids breaking pending workflow buttons (Mark Purchased
  cards already in someone's DM, etc.) on the deploy.

  Tampered legacy payloads are NOT detectable by this scheme — but
  the only thing we can do better is force-strict mode after a
  transition window once all old buttons have aged out.

Key material:

  Uses Config.FLASK_SECRET_KEY (already required for Flask sessions).
  The default fallback 'dev-only-not-for-prod' is detected and refused
  at sign-time so a misconfigured production environment fails loud
  rather than producing predictable-secret signatures.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Mapping

from config import Config


logger = logging.getLogger(__name__)


_SIG_FIELD = '_sig'

# Algorithm identifier in case we ever need to rotate (e.g. switch to
# SHA-3). Embedded in the signed envelope as `_alg` so old + new
# signatures can coexist during a rotation. Today only sha256-v1
# exists; verify_payload accepts only this version.
_SIG_ALG = 'sha256-v1'

_DEFAULT_SECRET = 'dev-only-not-for-prod'


class BadSignature(Exception):
    """Raised when a button payload arrives with a present-but-invalid
    signature. The caller should treat this as 'tampered click; ignore'
    and emit a structured log event so operators can spot the attempt.
    """


class WeakSecretError(RuntimeError):
    """Raised when sign_payload is asked to sign with the placeholder
    Flask secret. Production must set a real FLASK_SECRET_KEY."""


def _canonical(data: Mapping[str, Any]) -> str:
    """Stable, deterministic JSON serialisation for HMAC input.

    sort_keys ensures dict insertion order doesn't change the
    signature; separators removes the optional whitespace the
    default json module inserts (so the bytes hashed match what
    Slack returns to us verbatim).
    """
    return json.dumps(data, sort_keys=True, separators=(',', ':'))


def _hmac_hex(canonical: str) -> str:
    secret = Config.FLASK_SECRET_KEY or ''
    if not secret or secret == _DEFAULT_SECRET:
        raise WeakSecretError(
            'Cannot sign button payloads — FLASK_SECRET_KEY is unset or '
            'the placeholder default. Set a strong random value in the '
            'production environment and redeploy.'
        )
    return hmac.new(
        secret.encode('utf-8'),
        canonical.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()


def sign_payload(data: Mapping[str, Any]) -> str:
    """Return JSON-encoded ``button.value`` with HMAC sig + alg field.

    The wrapped data is the original dict + two reserved keys:
      _alg  — signature algorithm identifier (sha256-v1)
      _sig  — hex-encoded HMAC over the canonical JSON of all OTHER
              fields (so future schema changes only break verification
              for tampered payloads, not legitimate ones).

    Raises WeakSecretError when FLASK_SECRET_KEY isn't strong enough
    to actually defend anything.
    """
    if _SIG_FIELD in data or '_alg' in data:
        raise ValueError(
            'Reserved keys (_sig, _alg) cannot be used as payload fields'
        )
    # Build the body that gets HMAC'd. _alg is included so an attacker
    # can't downgrade the algorithm without invalidating the signature.
    # _sig itself is NOT in the body — that would be signing our own
    # output. Instead we compute the signature over (data + _alg) and
    # then append _sig to the final wire format.
    body = {**data, '_alg': _SIG_ALG}
    sig = _hmac_hex(_canonical(body))
    return json.dumps({**body, _SIG_FIELD: sig})


def verify_payload(button_value: str) -> dict:
    """Parse + verify a Slack ``button.value`` string.

    Returns the data dict (with reserved sig/alg fields stripped) on:
      • valid signature
      • LEGACY payload that has no _sig — backward compatibility for
        in-flight Slack messages from before this module shipped.

    Raises BadSignature when:
      • _sig is present but doesn't match
      • _alg is present but unrecognised (defensive against future
        algorithm-rotation attempts being downgraded back)

    Raises ValueError on JSON parse failure (caller should treat as
    'malformed click; ignore', same as before this module existed).
    """
    parsed = json.loads(button_value)
    if not isinstance(parsed, dict):
        raise ValueError('Button value must be a JSON object')

    sig = parsed.pop(_SIG_FIELD, None)
    alg = parsed.pop('_alg', None)

    if sig is None and alg is None:
        # Legacy unsigned payload — pass through. Recorded as a
        # warning so operators can see when the last unsigned
        # button is consumed and consider tightening to strict mode.
        logger.warning(
            'Received unsigned button payload (legacy compat): keys=%s',
            sorted(parsed.keys()),
        )
        return parsed

    if alg != _SIG_ALG:
        raise BadSignature(
            f'Unsupported signature algorithm: {alg!r}'
        )

    expected = _hmac_hex(_canonical({**parsed, '_alg': alg}))
    # hmac.compare_digest is constant-time — protects against
    # timing-attack reasoning even though our threat model doesn't
    # really need it.
    if not hmac.compare_digest(sig or '', expected):
        raise BadSignature(
            'Button payload signature does not match — payload was '
            'either tampered with or signed with a different secret'
        )

    return parsed
