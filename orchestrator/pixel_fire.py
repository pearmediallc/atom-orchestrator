"""Pixel-fire — swap a Meta Pixel ID + event on a lander HTML in S3.

Background: an MDB runs `/pixel-fire {event} {id}` in Slack to point a
specific lander at their own Meta (Facebook) pixel and conversion event,
without hand-editing the HTML in ATOM's file editor.

Scope (v1, hardcoded):
  • One specific domain  — `get-usa-help.com`
  • One specific file    — `pixel-fire/index.html` in that bucket
  • Meta Pixel only      — `fbq('init', '<id>')` + `fbq('track', '<event>')`
                            and the noscript `facebook.com/tr?id=<id>` URL.

The lander template has the pixel ID in 2 spots and the conversion event
in 3 spots; this module asserts those exact counts before saving (the
"safety belt" — if the template ever changes shape we abort with a loud
error rather than silently corrupt the lander).

Architecture: this module is pure orchestration logic. It calls ATOM via
AtomClient and reads/writes the inventory store. No Slack code lives
here — that's `slack_bot/routes.py`'s job. Tests can drive the whole
flow with a mock AtomClient.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from config import Config
from inventory import store
from orchestrator.atom_client import (
    AtomClient,
    AtomError,
    AtomClientError,
)
from orchestrator.log_setup import log_event


logger = logging.getLogger(__name__)


# ─── Hardcoded targets (v1) ────────────────────────────────────────────────
# When the TL extends this to other landers, lift these to args and add a
# `domain` field to the slash command. The handler + tests stay otherwise
# unchanged.

PIXEL_FIRE_DOMAIN = 'get-usa-help.com'
PIXEL_FIRE_FILE_KEY = 'pixel-fire/index.html'

# The user-facing live URL for the proof DM. Trailing slash because the
# folder is what actually serves index.html via S3 website hosting.
PIXEL_FIRE_LIVE_URL = (
    f'https://{PIXEL_FIRE_DOMAIN}/{PIXEL_FIRE_FILE_KEY.rsplit("/", 1)[0]}/'
)

# Expected match counts on the v1 lander. If the template ever changes
# (someone adds another `fbq('track', ...)` call, or removes one) the
# safety belt aborts with a clear "expected X/Y, found A/B" message
# instead of silently producing a broken lander.
EXPECTED_ID_MATCHES = 2
EXPECTED_EVENT_MATCHES = 3

# Sanity limits: lander HTML is ~50KB in practice; reject anything wildly
# larger to avoid trying to regex through a 100MB binary if something
# upstream goes wrong with the file key.
MAX_LANDER_BYTES = 1 * 1024 * 1024  # 1 MB

# Meta Pixel IDs are 15-16 digit numerics. We accept up to 17 to be safe
# against future format changes; 14 catches obvious typos.
PIXEL_ID_RE = re.compile(r'^\d{15,17}$')

# Event names: alphanumeric + underscore only. Meta's standard events
# (Lead, Purchase, CompleteRegistration, ViewContent, AddToCart,
# InitiateCheckout, Subscribe, Contact, …) all fit this. Custom events
# are allowed by Meta with the same charset.
EVENT_NAME_RE = re.compile(r'^[A-Za-z0-9_]{1,40}$')


# ─── Replacement regexes ───────────────────────────────────────────────────
# Captured groups identify the OLD value at each spot; we rebuild each
# match keeping the surrounding text intact and substituting just the
# captured group with the new value. Compiled once, used per request.

# fbq('init', '2714057732308829')   — single OR double quotes accepted
_FBQ_INIT_RE = re.compile(
    r"""(fbq\(\s*['"]init['"]\s*,\s*['"])(\d+)(['"]\s*\))"""
)

# https://www.facebook.com/tr?id=2714057732308829&ev=PageView&...
# Match scheme-optional + with or without www. for safety against template
# variation; the captured group is the pixel ID only.
_NOSCRIPT_TR_RE = re.compile(
    r'(facebook\.com/tr\?id=)(\d+)'
)

# fbq('track', 'Lead')   — captures the event name regardless of arity
# of subsequent arguments (some templates pass an options object).
_FBQ_TRACK_RE = re.compile(
    r"""(fbq\(\s*['"]track['"]\s*,\s*['"])([A-Za-z0-9_]+)(['"])"""
)


# ─── Public API ────────────────────────────────────────────────────────────

@dataclass
class PixelFireResult:
    """Outcome of a /pixel-fire run.

    `status` is one of:
      • 'updated'          — file was edited and saved
      • 'no_change'        — file already had the requested values
      • 'safety_belt'      — abort, regex match counts didn't match expected
      • 'invalid_input'    — args failed validation
      • 'inventory_error'  — domain row missing or malformed
      • 'atom_error'       — ATOM read/write failed (5xx, 404, transport)
      • 'unexpected_error' — anything else; full traceback in logs

    `message` is the operator-facing one-line summary (used in the Slack DM).
    `details` is structured context for logging + the audit row.
    """
    status: str
    message: str
    details: dict = field(default_factory=dict)


def update_pixel_on_lander(
    event: str,
    pixel_id: str,
    *,
    actor: str,
    atom_client: Optional[AtomClient] = None,
) -> PixelFireResult:
    """Replace the Meta Pixel ID + event on the v1 lander.

    Args:
      event:      the new conversion event name (e.g. 'Lead', 'Purchase').
      pixel_id:   the new Meta Pixel ID (15-17 digit numeric string).
      actor:      Slack user ID of the operator who ran the command. Used
                  for audit log + structured logging.
      atom_client: optional pre-built AtomClient (tests inject a mock).
                   When None we build one and login with Config creds.

    Returns: PixelFireResult. Never raises — every failure mode maps to a
    distinct status so the caller (Slack handler) can render appropriate
    user feedback.
    """
    started = time.time()
    log_event(
        'pixel_fire_started', domain=PIXEL_FIRE_DOMAIN,
        file_key=PIXEL_FIRE_FILE_KEY, actor=actor,
        new_event=event, new_pixel_id=pixel_id,
    )

    # ── 1. Validate inputs ────────────────────────────────────────────────
    pixel_id = (pixel_id or '').strip().strip('"\'').replace(',', '').replace(' ', '')
    event = (event or '').strip().strip('"\'')

    if not PIXEL_ID_RE.match(pixel_id):
        return _result(
            status='invalid_input',
            message=(f'Pixel ID must be 15-17 digits (Meta format), '
                     f'got `{pixel_id or "(empty)"}`.'),
            details={'reason': 'bad_pixel_id', 'value': pixel_id},
            actor=actor, started=started,
        )

    if not EVENT_NAME_RE.match(event):
        return _result(
            status='invalid_input',
            message=(f'Event name must be alphanumeric/underscore, '
                     f'1-40 chars (e.g. `Lead`, `Purchase`, '
                     f'`CompleteRegistration`). Got `{event or "(empty)"}`.'),
            details={'reason': 'bad_event', 'value': event},
            actor=actor, started=started,
        )

    # ── 2. Resolve the AWS account from inventory ─────────────────────────
    try:
        row = store.get_domain(PIXEL_FIRE_DOMAIN)
    except Exception as e:
        logger.exception('inventory lookup failed for %s', PIXEL_FIRE_DOMAIN)
        return _result(
            status='inventory_error',
            message=(f'Inventory store unreachable while looking up '
                     f'`{PIXEL_FIRE_DOMAIN}`: {type(e).__name__}.'),
            details={'reason': 'store_exception',
                     'error': f'{type(e).__name__}: {e}'},
            actor=actor, started=started,
        )

    if not row:
        return _result(
            status='inventory_error',
            message=(f'`{PIXEL_FIRE_DOMAIN}` is not in our inventory. '
                     f'Add it first or contact TL.'),
            details={'reason': 'domain_missing'},
            actor=actor, started=started,
        )

    aws_account = row.get('aws_account')
    if not aws_account:
        return _result(
            status='inventory_error',
            message=(f'`{PIXEL_FIRE_DOMAIN}` has no aws_account set in '
                     'inventory. Set it via SQL before running /pixel-fire.'),
            details={'reason': 'aws_account_missing'},
            actor=actor, started=started,
        )

    # ── 3. ATOM client (login if we built it ourselves) ──────────────────
    owns_client = atom_client is None
    if owns_client:
        atom_client = AtomClient()
        try:
            atom_client.login(Config.ATOM_USERNAME, Config.ATOM_PASSWORD)
        except Exception as e:
            logger.exception('ATOM login failed for /pixel-fire')
            return _result(
                status='atom_error',
                message=(f'Could not log in to ATOM: '
                         f'{type(e).__name__}.'),
                details={'reason': 'atom_login_failed',
                         'error': f'{type(e).__name__}: {e}'},
                actor=actor, started=started,
            )

    # ── 4. Read the lander ───────────────────────────────────────────────
    try:
        content = atom_client.get_file_content(
            aws_account, PIXEL_FIRE_DOMAIN, PIXEL_FIRE_FILE_KEY,
        )
    except AtomClientError as e:
        # 404 lives here — distinguish "file moved" from a generic ATOM error.
        msg = str(e).lower()
        if 'http 404' in msg or 'does not exist' in msg:
            return _result(
                status='atom_error',
                message=(f'Lander file `{PIXEL_FIRE_FILE_KEY}` not found in '
                         f'bucket `{PIXEL_FIRE_DOMAIN}`. Has someone moved '
                         'or deleted it?'),
                details={'reason': 'file_not_found',
                         'bucket': PIXEL_FIRE_DOMAIN,
                         'file_key': PIXEL_FIRE_FILE_KEY},
                actor=actor, started=started,
            )
        logger.exception('ATOM client error reading lander')
        return _result(
            status='atom_error',
            message=f'ATOM rejected the read: {type(e).__name__}.',
            details={'reason': 'atom_read_4xx',
                     'error': f'{type(e).__name__}: {e}'},
            actor=actor, started=started,
        )
    except AtomError as e:
        logger.exception('ATOM error reading lander')
        return _result(
            status='atom_error',
            message=(f'Could not read lander from S3 via ATOM: '
                     f'{type(e).__name__}. Try again in a minute.'),
            details={'reason': 'atom_read_failed',
                     'error': f'{type(e).__name__}: {e}'},
            actor=actor, started=started,
        )

    # ── 5. Sanity-check the file ─────────────────────────────────────────
    if len(content.encode('utf-8')) > MAX_LANDER_BYTES:
        return _result(
            status='atom_error',
            message=(f'Lander file is unexpectedly large '
                     f'({len(content):,} chars > {MAX_LANDER_BYTES:,} byte cap). '
                     'Refusing to edit — likely not the file we expected.'),
            details={'reason': 'file_too_large', 'size': len(content)},
            actor=actor, started=started,
        )
    if '<html' not in content.lower() and '<!doctype' not in content.lower():
        return _result(
            status='atom_error',
            message=('Lander file does not look like HTML (no `<html>` or '
                     '`<!doctype>` tag). Refusing to edit.'),
            details={'reason': 'not_html'},
            actor=actor, started=started,
        )

    # ── 6. Snapshot OLD values BEFORE the regex passes (for audit + DM) ──
    old_id_match = _FBQ_INIT_RE.search(content)
    old_event_match = _FBQ_TRACK_RE.search(content)
    old_pixel_id = old_id_match.group(2) if old_id_match else None
    old_event = old_event_match.group(2) if old_event_match else None

    # ── 7. Replace and count substitutions in the same pass ──────────────
    new_content, id_count = _replace_pixel_id(content, pixel_id)
    new_content, event_count = _replace_event(new_content, event)

    # ── 8. Safety belt — abort if the template's shape doesn't match ─────
    if id_count != EXPECTED_ID_MATCHES or event_count != EXPECTED_EVENT_MATCHES:
        return _result(
            status='safety_belt',
            message=(f'Lander structure changed — expected '
                     f'{EXPECTED_ID_MATCHES} pixel-ID spots and '
                     f'{EXPECTED_EVENT_MATCHES} event spots, found '
                     f'{id_count} and {event_count}. Aborted without '
                     'saving. Flag to TL.'),
            details={
                'reason': 'safety_belt_tripped',
                'expected_id_count': EXPECTED_ID_MATCHES,
                'expected_event_count': EXPECTED_EVENT_MATCHES,
                'actual_id_count': id_count,
                'actual_event_count': event_count,
            },
            actor=actor, started=started,
        )

    # ── 9. No-change case (idempotent re-run) ────────────────────────────
    if new_content == content:
        return _result(
            status='no_change',
            message=(f'Pixel was already set to `{event}` / `{pixel_id}` on '
                     f'`{PIXEL_FIRE_DOMAIN}/{PIXEL_FIRE_FILE_KEY}`. '
                     'No change needed.'),
            details={
                'old_pixel_id': old_pixel_id, 'old_event': old_event,
                'new_pixel_id': pixel_id, 'new_event': event,
                'id_count': id_count, 'event_count': event_count,
            },
            actor=actor, started=started,
        )

    # ── 10. Write back to S3 via ATOM ────────────────────────────────────
    try:
        atom_client.save_file_content(
            aws_account, PIXEL_FIRE_DOMAIN, PIXEL_FIRE_FILE_KEY, new_content,
        )
    except AtomError as e:
        logger.exception('ATOM error saving lander')
        return _result(
            status='atom_error',
            message=(f'Could not save lander to S3 via ATOM: '
                     f'{type(e).__name__}. The file was NOT modified.'),
            details={'reason': 'atom_write_failed',
                     'error': f'{type(e).__name__}: {e}'},
            actor=actor, started=started,
        )

    # ── 11. Success — record audit row + return ──────────────────────────
    duration_sec = round(time.time() - started, 2)
    details = {
        'old_pixel_id': old_pixel_id, 'old_event': old_event,
        'new_pixel_id': pixel_id, 'new_event': event,
        'id_count': id_count, 'event_count': event_count,
        'aws_account': aws_account,
        'file_key': PIXEL_FIRE_FILE_KEY,
        'live_url': PIXEL_FIRE_LIVE_URL,
        'duration_sec': duration_sec,
    }
    return _result(
        status='updated',
        message=(f'Pixel updated on `{PIXEL_FIRE_DOMAIN}/{PIXEL_FIRE_FILE_KEY}`. '
                 f'ID `{old_pixel_id}` → `{pixel_id}` (2 spots), '
                 f'event `{old_event}` → `{event}` (3 spots).'),
        details=details, actor=actor, started=started,
    )


# ─── Internals ─────────────────────────────────────────────────────────────

def _replace_pixel_id(content: str, new_id: str) -> tuple:
    """Run both ID regex passes, return (new_content, total_matches)."""
    new_content, init_count = _FBQ_INIT_RE.subn(
        lambda m: f'{m.group(1)}{new_id}{m.group(3)}', content,
    )
    new_content, tr_count = _NOSCRIPT_TR_RE.subn(
        lambda m: f'{m.group(1)}{new_id}', new_content,
    )
    return new_content, init_count + tr_count


def _replace_event(content: str, new_event: str) -> tuple:
    """Run the event regex pass, return (new_content, total_matches)."""
    return _FBQ_TRACK_RE.subn(
        lambda m: f'{m.group(1)}{new_event}{m.group(3)}', content,
    )


def _result(*, status: str, message: str, details: dict,
            actor: str, started: float) -> PixelFireResult:
    """Build the result dataclass, write the audit row, and emit the
    structured log event. Centralised so every return path goes through
    the same observability pipe."""
    duration_sec = round(time.time() - started, 2)
    log_fields = {
        'domain': PIXEL_FIRE_DOMAIN,
        'file_key': PIXEL_FIRE_FILE_KEY,
        'actor': actor,
        'status': status,
        'duration_sec': duration_sec,
        **details,
    }
    if status == 'updated':
        log_event('pixel_fire_completed', **log_fields)
    elif status == 'no_change':
        log_event('pixel_fire_no_change', **log_fields)
    else:
        log_event('pixel_fire_failed', level=logging.ERROR, **log_fields)

    # Audit row: only write for outcomes the operator should be able to
    # see in /domain-history. Validation errors don't get a row (they
    # never touched the file).
    if status in ('updated', 'no_change', 'safety_belt'):
        try:
            store.record_event(
                PIXEL_FIRE_DOMAIN,
                event_type='pixel_updated' if status == 'updated'
                           else f'pixel_{status}',
                actor=actor,
                metadata={**details, 'message': message[:300]},
            )
        except Exception:
            # Audit failure must NOT mask the user-facing outcome — the
            # file edit (if any) already succeeded or didn't.
            logger.exception(
                'pixel_fire: failed to write domain_events audit row',
            )

    return PixelFireResult(status=status, message=message, details=details)
