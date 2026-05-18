"""Tracker-domain setup — adds a Route 53 CNAME for a tracker subdomain
+ registers the same hostname with RedTrack.

Background: an MDB runs `/new-tracker {cname} {domain}` in Slack to set
up an additional tracker subdomain beyond the default `track.<domain>`
that ATOM creates during setup-domain. Each MDB / campaign typically
wants its own subdomain to keep tracker data clean (e.g., one wants
`trk.neurobloomone.com`, another wants `cl.neurobloomone.com`).

The 2-step chain (DNS first, then RedTrack — see "order" note below):

  1. Route 53: CREATE CNAME `<cname>.<domain>` → REDTRACK_TRACKER_CNAME_TARGET
     (via ATOM's POST /api/add-cname endpoint).
  2. RedTrack: POST /domains to register `<cname>.<domain>` as one of
     our tracker hostnames (via redtrack_client.add_tracker_domain).

**Why DNS first:** RedTrack provisions a Let's Encrypt cert for the new
hostname (`use_auto_generated_ssl: true`). Let's Encrypt's HTTP-01
challenge needs DNS to resolve correctly BEFORE the registration call,
or SSL provisioning fails and the MDB sees a broken cert.

**Partial-failure semantics:**

  • DNS fails  → nothing to clean up; we never called RedTrack. Return
                 `dns_error` with the underlying reason.
  • DNS ok, RedTrack fails → the R53 record is harmless on its own
                 (RedTrack 404s for unregistered hostnames). We DON'T
                 roll back. Return `dns_done_redtrack_failed` with a
                 clear "re-run to retry just RedTrack" hint — the re-run
                 hits 'skipped_already_correct' on DNS and retries the
                 RedTrack POST. Idempotent end state.

Architecture: pure orchestration logic, no Slack code. Tests can drive
the whole flow with mocked AtomClient + a monkeypatched
add_tracker_domain. Mirrors `orchestrator/pixel_fire.py`'s shape.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

from config import Config
from inventory import store
from orchestrator.atom_client import (
    AtomClient,
    AtomClientError,
    AtomError,
)
from orchestrator.log_setup import log_event
from redtrack_client import add_tracker_domain


logger = logging.getLogger(__name__)


# ─── Validation rules ─────────────────────────────────────────────────────

# DNS-label regex per RFC 1035: lowercase alphanumeric + dashes,
# can't start or end with a dash, max 63 chars. We enforce lowercase
# input to keep CNAMEs predictable.
_DNS_LABEL_RE = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')

# Reserved cname labels — refuse these so MDBs can't accidentally collide
# with infrastructure ATOM manages or break the apex:
#   • 'track' — ATOM owns this; reserves it for the default tracker
#               CNAME setup-domain creates (aws_automation.py:2088).
#   • 'www'   — the apex's www-alias; managed by ATOM's setup pipeline
#               as an A/AAAA record to CloudFront. Adding a CNAME would
#               break the alias.
#   • ''      — empty == apex, not a subdomain.
RESERVED_CNAMES = frozenset({'track', 'www', ''})

# Apex domain regex (basic) — letters, digits, dots, dashes. The
# stricter ATOM-side validation runs as part of the R53 lookup; this
# is just a fast-reject for obvious garbage.
_DOMAIN_RE = re.compile(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?'
                        r'(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$')


# ─── Public API ────────────────────────────────────────────────────────────

@dataclass
class TrackerSetupResult:
    """Outcome of a /new-tracker run. `status` is one of:

      • 'created'                  — both DNS + RedTrack succeeded fresh
      • 'already_present'          — both already in place (idempotent re-run)
      • 'dns_done_redtrack_failed' — partial: DNS landed but RedTrack rejected
                                     (re-run to retry just RedTrack)
      • 'safety_belt'              — DNS exists with a different value;
                                     refused to overwrite
      • 'invalid_input'            — args failed format/reserved validation
      • 'inventory_error'          — domain missing, no aws_account, not deployed
      • 'dns_error'                — ATOM call failed (4xx/5xx/transport)
      • 'redtrack_error'           — RedTrack call failed AND DNS wasn't touched
                                     yet (creds missing, validation pre-step)
      • 'unexpected_error'         — bare except — should be impossible

    `message` is the operator-facing one-line summary (used in the Slack
    DM). `details` is structured context for logging + the audit row.
    """
    status: str
    message: str
    details: dict = field(default_factory=dict)


def add_tracker(
    cname_name: str,
    domain: str,
    *,
    actor: str,
    atom_client: Optional[AtomClient] = None,
) -> TrackerSetupResult:
    """Run the 2-step setup. Never raises — every failure mode maps to a
    distinct status so the Slack handler can render appropriate feedback.

    Args:
      cname_name: the new subdomain label (e.g., 'trk'). Lowercased,
        DNS-label-validated, reserved-label-rejected before any IO.
      domain:     the apex domain (e.g., 'neurobloomone.com'). Must be
        in our inventory with aws_account set AND setup_at populated.
      actor:      Slack user ID of the operator who ran the command.
      atom_client: optional pre-built AtomClient (tests inject a mock).
    """
    started = time.time()

    # Defensive normalisation — strip quotes/whitespace operators sometimes
    # paste in from RedTrack's UI or chat.
    cname_name = (cname_name or '').strip().strip('"\'').lower()
    domain = (domain or '').strip().strip('"\'').lower().rstrip('.')

    log_event(
        'tracker_setup_started', cname_name=cname_name, domain=domain,
        actor=actor,
    )

    # ── 1. Validate inputs ────────────────────────────────────────────────
    if not _DNS_LABEL_RE.match(cname_name):
        return _result(
            status='invalid_input',
            message=(f'Cname `{cname_name or "(empty)"}` is not a valid '
                     'DNS label. Use lowercase letters/digits/dashes, '
                     '1-63 chars, no leading/trailing dash. '
                     'e.g. `trk`, `cl`, `t1`.'),
            details={'reason': 'bad_cname_format', 'value': cname_name},
            actor=actor, started=started, cname_name=cname_name, domain=domain,
        )

    if cname_name in RESERVED_CNAMES:
        return _result(
            status='invalid_input',
            message=(f'Cname `{cname_name}` is reserved. `track` is the '
                     'default that ATOM creates during setup-domain; '
                     '`www` is the apex alias. Pick a different label '
                     '(e.g. `trk`, `cl`).'),
            details={'reason': 'reserved_cname', 'value': cname_name},
            actor=actor, started=started, cname_name=cname_name, domain=domain,
        )

    if not _DOMAIN_RE.match(domain):
        return _result(
            status='invalid_input',
            message=(f'Domain `{domain or "(empty)"}` is not a valid apex '
                     'domain. Use the apex form (e.g. `neurobloomone.com`, '
                     'not `https://www.neurobloomone.com/`).'),
            details={'reason': 'bad_domain_format', 'value': domain},
            actor=actor, started=started, cname_name=cname_name, domain=domain,
        )

    # ── 2. Resolve inventory + AWS account ────────────────────────────────
    try:
        row = store.get_domain(domain)
    except Exception as e:
        logger.exception('inventory lookup failed for %s', domain)
        return _result(
            status='inventory_error',
            message=(f'Inventory store unreachable while looking up '
                     f'`{domain}`: {type(e).__name__}.'),
            details={'reason': 'store_exception',
                     'error': f'{type(e).__name__}: {e}'},
            actor=actor, started=started, cname_name=cname_name, domain=domain,
        )

    if not row:
        return _result(
            status='inventory_error',
            message=(f'`{domain}` is not in our inventory. Add it via '
                     '`/new-domain` first (and let setup finish) before '
                     'adding tracker CNAMEs.'),
            details={'reason': 'domain_missing'},
            actor=actor, started=started, cname_name=cname_name, domain=domain,
        )

    aws_account = row.get('aws_account')
    if not aws_account:
        return _result(
            status='inventory_error',
            message=(f'`{domain}` has no `aws_account` set in inventory. '
                     'Set it via SQL or re-import the row before running '
                     '/new-tracker.'),
            details={'reason': 'aws_account_missing'},
            actor=actor, started=started, cname_name=cname_name, domain=domain,
        )

    # No setup_at pre-check: it's a metadata proxy that can drift from
    # AWS reality (a domain may have been set up directly via ATOM's UI
    # or another path that didn't write setup_at). ATOM's POST
    # /api/add-cname is the authoritative source — it returns 404
    # `no_hosted_zone_for_<domain>` when there's actually no R53 zone,
    # and that's surfaced with a precise message in the ATOM error
    # handling below. Skipping the metadata check unblocks legacy
    # domains whose inventory state is stale but whose AWS state is
    # fine (production case 2026-05-18 on diywithryan.com).

    # ── 3. Build target + tracker URL ─────────────────────────────────────
    tracker_url = f'{cname_name}.{domain}'
    cname_target = Config.REDTRACK_TRACKER_CNAME_TARGET
    if not cname_target:
        return _result(
            status='dns_error',
            message=('REDTRACK_TRACKER_CNAME_TARGET is not configured. '
                     'Set the env var before running /new-tracker.'),
            details={'reason': 'tracker_target_unconfigured'},
            actor=actor, started=started, cname_name=cname_name, domain=domain,
        )

    # ── 4. ATOM client (login if we built it ourselves) ──────────────────
    owns_client = atom_client is None
    if owns_client:
        atom_client = AtomClient()
        try:
            atom_client.login(Config.ATOM_USERNAME, Config.ATOM_PASSWORD)
        except Exception as e:
            logger.exception('ATOM login failed for /new-tracker')
            return _result(
                status='dns_error',
                message=f'Could not log in to ATOM: {type(e).__name__}.',
                details={'reason': 'atom_login_failed',
                         'error': f'{type(e).__name__}: {e}'},
                actor=actor, started=started,
                cname_name=cname_name, domain=domain,
            )

    # ── 5. DNS step (ATOM POST /api/add-cname) ────────────────────────────
    try:
        dns_resp = atom_client.add_cname(
            account_key=aws_account, domain=domain,
            cname_name=cname_name, value=cname_target,
        )
    except AtomClientError as e:
        msg = str(e).lower()
        # Distinguish two flavours of 404:
        #   • JSON body containing `no_hosted_zone_for_<domain>` — real
        #     R53 zone missing. Operator should check setup-domain.
        #   • HTML body ("not found", default Flask 404 page) — the
        #     /api/add-cname route itself doesn't exist on the running
        #     ATOM service (PR not merged OR Render hasn't redeployed
        #     since merge). Operator should redeploy ATOM, not retry.
        # The HTML-404 case bit us 2026-05-18: PR merged 3h prior but
        # Render's ATOM service didn't auto-redeploy from main.
        if 'no_hosted_zone' in msg:
            return _result(
                status='dns_error',
                message=(f'ATOM has no R53 zone for `{domain}` in account '
                         f'`{aws_account}`. Has the setup-domain pipeline '
                         f'finished for this domain? Check '
                         f'`/domain-history {domain}` — and verify the '
                         '`aws_account` column matches where the zone '
                         'actually lives.'),
                details={'reason': 'no_hosted_zone',
                         'aws_account': aws_account,
                         'error': f'{type(e).__name__}: {e}'},
                actor=actor, started=started,
                cname_name=cname_name, domain=domain,
            )
        if ('http 404' in msg
                and ('not found</' in msg or '<html' in msg or 'doctype' in msg)):
            return _result(
                status='dns_error',
                message=('ATOM returned a generic 404 (route not found). '
                         'The `/api/add-cname` endpoint is not deployed on '
                         'the ATOM service yet — check Render dashboard, '
                         'redeploy aws-automation from latest main, then '
                         'retry. (PR may have merged but Render did not '
                         'auto-redeploy.)'),
                details={'reason': 'atom_endpoint_missing',
                         'error': f'{type(e).__name__}: {e}'},
                actor=actor, started=started,
                cname_name=cname_name, domain=domain,
            )
        if 'exists_with_different_value' in msg or 'http 409' in msg:
            return _result(
                status='safety_belt',
                message=(f'CNAME `{tracker_url}` already exists in Route 53 '
                         f'with a different value (expected `{cname_target}`). '
                         'Refusing to overwrite. Investigate the existing '
                         'record manually before retrying.'),
                details={'reason': 'cname_value_conflict',
                         'requested_target': cname_target,
                         'error': f'{type(e).__name__}: {e}'},
                actor=actor, started=started,
                cname_name=cname_name, domain=domain,
            )
        logger.exception('ATOM 4xx on add-cname for %s', tracker_url)
        return _result(
            status='dns_error',
            message=f'ATOM rejected the CNAME create: {type(e).__name__}.',
            details={'reason': 'atom_4xx',
                     'error': f'{type(e).__name__}: {e}'},
            actor=actor, started=started,
            cname_name=cname_name, domain=domain,
        )
    except AtomError as e:
        logger.exception('ATOM error on add-cname for %s', tracker_url)
        return _result(
            status='dns_error',
            message=(f'Could not create CNAME via ATOM: '
                     f'{type(e).__name__}. Try again in a minute.'),
            details={'reason': 'atom_failed',
                     'error': f'{type(e).__name__}: {e}'},
            actor=actor, started=started,
            cname_name=cname_name, domain=domain,
        )

    dns_action = dns_resp.get('action')  # 'created' | 'skipped_already_correct'
    dns_was_already_correct = dns_action == 'skipped_already_correct'

    # ── 6. RedTrack step (POST /domains) ──────────────────────────────────
    try:
        rt_resp = add_tracker_domain(tracker_url)
    except RuntimeError as e:
        # add_tracker_domain raises RuntimeError when REDTRACK_API_KEY or
        # WORKSPACE_ID is missing — that's a config error the operator
        # should fix, not a transient issue. DNS already landed.
        return _result(
            status='dns_done_redtrack_failed',
            message=(f'DNS done. RedTrack registration could not start: '
                     f'{e}. The CNAME is in place — once env is fixed, '
                     're-run /new-tracker to retry just the RedTrack step.'),
            details={
                'reason': 'redtrack_config_missing',
                'dns_action': dns_action, 'tracker_url': tracker_url,
                'cname_target': cname_target,
                'error': f'{type(e).__name__}: {e}',
            },
            actor=actor, started=started,
            cname_name=cname_name, domain=domain,
        )
    except requests.RequestException as e:
        logger.exception('RedTrack add-tracker-domain failed for %s',
                         tracker_url)
        # Bump the truncation to 400 chars so RedTrack's response body
        # (included in the HTTPError message by add_tracker_domain) is
        # visible in the Slack DM, not just Render logs.
        return _result(
            status='dns_done_redtrack_failed',
            message=(f'DNS done. RedTrack rejected the domain: '
                     f'`{type(e).__name__}: {str(e)[:400]}`. The CNAME is '
                     'in place — once we fix the body shape, re-run '
                     '/new-tracker (DNS step skips, only RedTrack retries).'),
            details={
                'reason': 'redtrack_request_failed',
                'dns_action': dns_action, 'tracker_url': tracker_url,
                'cname_target': cname_target,
                'error': f'{type(e).__name__}: {e}',
            },
            actor=actor, started=started,
            cname_name=cname_name, domain=domain,
        )
    except Exception as e:
        logger.exception('RedTrack add-tracker-domain unexpected crash')
        return _result(
            status='dns_done_redtrack_failed',
            message=(f'DNS done. RedTrack call crashed unexpectedly: '
                     f'{type(e).__name__}. Re-run to retry RedTrack only.'),
            details={'reason': 'redtrack_unexpected_error',
                     'dns_action': dns_action, 'tracker_url': tracker_url,
                     'error': f'{type(e).__name__}: {e}'},
            actor=actor, started=started,
            cname_name=cname_name, domain=domain,
        )

    redtrack_already_existed = bool(rt_resp.get('_already_exists'))
    redtrack_id = rt_resp.get('id')

    # ── 7. Map combined outcome to final status ──────────────────────────
    details = {
        'tracker_url': tracker_url,
        'cname_target': cname_target,
        'aws_account': aws_account,
        'dns_action': dns_action,
        'redtrack_id': redtrack_id,
        'redtrack_already_existed': redtrack_already_existed,
        'duration_sec': round(time.time() - started, 2),
    }

    if dns_was_already_correct and redtrack_already_existed:
        return _result(
            status='already_present',
            message=(f'Tracker `{tracker_url}` was already set up — DNS '
                     'CNAME correct and RedTrack has the domain registered. '
                     'No changes needed.'),
            details=details,
            actor=actor, started=started,
            cname_name=cname_name, domain=domain,
        )

    return _result(
        status='created',
        message=(f'Tracker `{tracker_url}` is set up. DNS '
                 f'{"already correct" if dns_was_already_correct else "created"}; '
                 f'RedTrack '
                 f'{"already had it" if redtrack_already_existed else "registered fresh"}.'),
        details=details,
        actor=actor, started=started,
        cname_name=cname_name, domain=domain,
    )


# ─── Internals ─────────────────────────────────────────────────────────────

def _result(*, status: str, message: str, details: dict,
            actor: str, started: float,
            cname_name: str = '', domain: str = '') -> TrackerSetupResult:
    """Build the result, write the audit row, and emit a structured log
    event. Centralised so every return path goes through the same
    observability pipe."""
    duration_sec = round(time.time() - started, 2)
    log_fields = {
        'cname_name': cname_name, 'domain': domain,
        'actor': actor, 'status': status,
        'duration_sec': duration_sec,
        **details,
    }
    if status in ('created', 'already_present'):
        log_event('tracker_setup_completed', **log_fields)
    elif status == 'dns_done_redtrack_failed':
        log_event('tracker_setup_partial', level=logging.ERROR, **log_fields)
    else:
        log_event('tracker_setup_failed', level=logging.ERROR, **log_fields)

    # Audit row: only for outcomes that touched (or would have touched)
    # state. Validation errors never get a row — they didn't act.
    if status in ('created', 'already_present', 'safety_belt',
                  'dns_done_redtrack_failed'):
        event_type_map = {
            'created': 'tracker_added',
            'already_present': 'tracker_already_present',
            'safety_belt': 'tracker_safety_belt',
            'dns_done_redtrack_failed': 'tracker_partial_dns_only',
        }
        try:
            store.record_event(
                domain or '<unknown>',
                event_type=event_type_map[status],
                actor=actor,
                metadata={**details, 'cname_name': cname_name,
                          'message': message[:300]},
            )
        except Exception:
            logger.exception(
                'tracker_setup: failed to write domain_events audit row',
            )

    return TrackerSetupResult(status=status, message=message, details=details)
