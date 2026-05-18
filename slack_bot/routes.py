"""Slack endpoints — Phase 2 (real handlers via slack_bolt).

This module wires Slack to our orchestration logic. Two slash commands:
  • /list-domains  — replies with inventory contents
  • /new-domain    — opens a modal collecting vertical/examples/lander/extension

Plus interactive button handlers:
  • pick_domain    — MDB clicks "Pick this" on a suggested domain → bot DMs
                     Utkarsh asking him to buy it on Namecheap manually
                     (per TL's decision: manual purchase, no Namecheap purchase API)

Every incoming Slack request is verified against the signing secret automatically
by slack_bolt (refuses requests not actually from Slack).

Architecture note: Anand registered THREE separate Request URLs in the Slack
app config (one per slash command, plus interactivity). Rather than reconfigure
Slack, each Flask route below forwards to the SAME SlackRequestHandler — bolt
internally dispatches based on the request body.
"""
import json
import re
import logging
import threading
from urllib.parse import urlparse
from flask import Blueprint, jsonify, request

from config import Config
from domain_assistant import namecheap_check
from inventory import store as inventory_store
from orchestrator.log_setup import log_event
from orchestrator.pixel_fire import (
    PIXEL_FIRE_DOMAIN,
    PIXEL_FIRE_FILE_KEY,
    PIXEL_FIRE_LIVE_URL,
    update_pixel_on_lander,
)
from orchestrator.tracker_setup import add_tracker
from orchestrator.workflow import (
    ExistingDomainRequest,
    run_existing_domain_workflow,
    suggest_new_domains,
)
from slack_bot.payload_signing import (
    BadSignature,
    sign_payload,
    verify_payload,
)

logger = logging.getLogger(__name__)


def _parse_lander_url(url: str):
    """Parse 'https://domain.com/folder/' into (bucket, folders, error_message).

    The lander URL is the user's "where do I want this lander copied FROM"
    — the host part is treated as the source S3 bucket name (matches ATOM's
    convention that bucket name == domain), and the path is the folder
    inside that bucket.

    Returns:
      (bucket, [folder], None)         on success — folder always ends with '/'
      ('', [], 'reason')               on failure — caller surfaces the reason

    Examples:
      'https://safetyfirstauto.pro/h-insure-c/'  → ('safetyfirstauto.pro', ['h-insure-c/'], None)
      'https://safetyfirstauto.pro/h-insure-c'   → ('safetyfirstauto.pro', ['h-insure-c/'], None)
      'https://safetyfirstauto.pro/'             → ('', [], 'URL is missing a folder path…')
      'safetyfirstauto.pro/lander/'              → ('', [], 'URL must start with https://')
    """
    if not url:
        return '', [], 'URL is empty'
    parsed = urlparse(url.strip())
    if parsed.scheme not in ('http', 'https'):
        return '', [], 'URL must start with https:// (or http://)'
    if not parsed.netloc:
        return '', [], 'URL is missing a domain'
    folder = parsed.path.strip('/')
    if not folder:
        return '', [], (
            'URL is missing a folder path. Use the form '
            'https://<bucket>/<folder>/ — e.g. https://safetyfirstauto.pro/h-insure-c/'
        )
    return parsed.netloc, [folder + '/'], None

slack_bp = Blueprint('slack', __name__)


# ─── slack_bolt setup ──────────────────────────────────────────────────────
# Skip bolt initialisation when tokens aren't present (lets the app boot for
# local dev / unit tests without Slack creds; only the /slack/health route
# stays usable in that mode).

_bolt_app = None
_handler = None

if Config.SLACK_BOT_TOKEN and Config.SLACK_SIGNING_SECRET:
    from slack_bolt import App
    from slack_bolt.adapter.flask import SlackRequestHandler

    _bolt_app = App(
        token=Config.SLACK_BOT_TOKEN,
        signing_secret=Config.SLACK_SIGNING_SECRET,
        # Suppress bolt's own request-logging noise; Flask's default access log
        # is enough.
        request_verification_enabled=True,
    )
    _handler = SlackRequestHandler(_bolt_app)

    # Phase A — register lifecycle bot's button handlers onto the same
    # bolt app. Kept in lifecycle/handlers.py so this file doesn't grow
    # another 500 lines.
    from lifecycle import handlers as _lifecycle_handlers
    _lifecycle_handlers.register(_bolt_app)


# ─── Shared helpers for interactive button click handlers ─────────────────


def _verify_button_click(body: dict):
    """Verify HMAC on a Slack interactive button click.

    Centralises the seven-place repeat of the same sign-check
    boilerplate (parse + verify + log on tamper + drop on malformed).

    Returns:
      • parsed payload dict on success
      • None when the click should be silently dropped (malformed
        JSON, missing fields, OR tampered signature). Tampered
        clicks are logged as `button_signature_invalid`; drops
        from malformed input are silent (matches pre-batch-5
        behaviour).
    """
    try:
        return verify_payload(body['actions'][0]['value'])
    except BadSignature as e:
        # Tampered or wrong-secret signature → log + drop the click.
        # Audit #14: prevents an in-workspace user forging a
        # button.value to redirect a legitimate action to a
        # different domain / requester.
        log_event(
            'button_signature_invalid', level=logging.WARNING,
            action_id=body.get('actions', [{}])[0].get('action_id'),
            user=body.get('user', {}).get('id'),
            error=str(e),
        )
        return None
    except (KeyError, IndexError, ValueError):
        return None


def _build_confirmed_card(*, action_label: str, target: str,
                          confirmer_id: str,
                          extra_context: str = '') -> list:
    """Block Kit blocks for a 'Confirmed X: target' chat_update.

    Used by Path A's Mark Deployed click and Path B's Mark Purchased
    click — both rendered the same header + context blocks before
    this refactor (audit #13 cleanup). Centralising the layout
    prevents the two Slack cards from drifting apart when a future
    change touches one of them.

    Args:
      action_label: post-action verb shown in the green-tick header,
                    e.g. 'Deployed' / 'Purchased' / 'Approved'.
      target: domain name (or whatever else identifies what was
              acted on) shown after the action label.
      confirmer_id: Slack user id of the operator who clicked the
                    button. Used in the @mention.
      extra_context: optional sentence appended to the standard
                     'Confirmed by <@user>.' context line.
    """
    context_text = f'Confirmed by <@{confirmer_id}>.'
    if extra_context:
        context_text += ' ' + extra_context
    return [
        {'type': 'header', 'text': {
            'type': 'plain_text',
            'text': f':white_check_mark: {action_label}: {target}',
        }},
        {'type': 'context', 'elements': [{
            'type': 'mrkdwn', 'text': context_text,
        }]},
    ]


# ─── /new-domain shortlist builder (shared by modal submission + refresh) ─

def _build_new_domain_shortlist_blocks(*, suggestions, vertical, audience,
                                       extension, lander, requester,
                                       examples=None, aws_account='',
                                       external_requester=''):
    """Render the /new-domain shortlist as Slack Block Kit blocks.

    Used by both the initial modal submission and the "Show 5 more"
    refresh handler. Each domain row gets a Pick this button; the
    bottom of the message gets a Show-5-more button that re-runs
    the same query with fresh LLM output.

    `examples` is the user-supplied list of stylistic anchor domains
    from the modal. Carried through the Show-5-more button so the
    refresh worker can produce names in the same family.

    `aws_account` is the AWS account the new domain will be provisioned
    in (R53 zone, ACM cert, S3 bucket, CloudFront). Threaded into every
    Pick-this button payload so the choice survives all hops down to
    the inventory write at Mark Purchased.

    `lander` may be empty — when so, the Pick payload carries empty
    string and the downstream deploy worker will skip the file-copy
    step and only run ATOM setup.
    """
    examples = examples or []
    if extension == 'any':
        cap_label = (
            'Mixed extensions — sorted cheapest first. '
            '.com priced under $15, other extensions ≤$5.'
        )
        ext_display = 'Any (cheapest first)'
    else:
        cap_usd = Config.price_cap_for(extension)
        cap_label = (
            f'All shown are available on Namecheap and '
            f'priced at-or-below ${cap_usd:.2f}/yr.'
        )
        ext_display = extension

    audience_line = f'  ·  Audience: _{audience}_' if audience else ''
    lander_line = f'  ·  Lander: {lander}' if lander else '  ·  Lander: _none (setup-only)_'
    aws_line = f'  ·  AWS: `{aws_account}`' if aws_account else ''

    blocks = [
        {
            'type': 'header',
            'text': {
                'type': 'plain_text',
                'text': f'{len(suggestions)} available — pick one to continue',
            },
        },
        {
            'type': 'context',
            'elements': [{
                'type': 'mrkdwn',
                'text': (f'Vertical: *{vertical}*  ·  Extension: `{ext_display}`'
                         f'{aws_line}{audience_line}{lander_line}\n_{cap_label}_'),
            }],
        },
        {'type': 'divider'},
    ]

    for s in suggestions:
        price = s.get('price')
        price_label = f'  ·  ${price:.2f}/yr' if price is not None else ''
        # When extension='any', each domain has its own real TLD on s['extension'].
        # Pin that into the per-domain button payload so downstream Path B
        # logic (TL approval card, Utkarsh purchase request, etc.) sees the
        # actual TLD, not the literal 'any'.
        per_domain_extension = s.get('extension') or extension
        blocks.append({
            'type': 'section',
            'text': {
                'type': 'mrkdwn',
                'text': f'`{s["domain"]}`{price_label}',
            },
            'accessory': {
                'type': 'button',
                'action_id': 'pick_domain',
                'text': {'type': 'plain_text', 'text': 'Pick this'},
                'style': 'primary',
                'value': sign_payload({
                    'domain': s['domain'],
                    'vertical': vertical,
                    'lander': lander,
                    'extension': per_domain_extension,
                    'requester': requester,
                    'aws_account': aws_account,
                    # External requester name (empty for internal
                    # requests) — threaded through every payload hop so
                    # it survives down to the inventory write.
                    'external_requester': external_requester,
                    # Price came from the Namecheap check in
                    # workflow.suggest_new_domains; threading it
                    # through the signed payload chain so the TL
                    # approval card + Utkarsh purchase card both
                    # display the actual annual cost.
                    'price': s.get('price'),
                }),
            },
        })

    blocks.append({'type': 'divider'})
    blocks.append({
        'type': 'actions',
        'elements': [{
            'type': 'button',
            'action_id': 'refresh_domain_suggestions',
            'text': {'type': 'plain_text', 'text': ':arrows_counterclockwise: Show 5 more'},
            'value': sign_payload({
                'vertical': vertical,
                'audience': audience,
                'extension': extension,
                'lander': lander,
                'requester': requester,
                'examples': examples,
                'aws_account': aws_account,
                'external_requester': external_requester,
            }),
        }],
    })
    return blocks


def _phase8_refresh_suggestions(client, channel, placeholder_ts, *,
                                vertical, audience, extension,
                                lander, requester, examples=None,
                                aws_account='', external_requester=''):
    """Worker that regenerates the /new-domain shortlist. Runs in a
    daemon thread so the original Slack action click can ack within 3s
    while the LLM + Namecheap calls take 15–30s.

    Edits the placeholder message in-place to either show the new
    shortlist or surface a clear failure reason.
    """
    examples = examples or []
    try:
        suggestions = suggest_new_domains(
            vertical=vertical,
            audience=audience,
            extension=extension,
            count=5,
            examples=examples,
        )
    except Exception as e:
        logger.exception('Phase 8 refresh failed for vertical=%s', vertical)
        client.chat_update(
            channel=channel, ts=placeholder_ts,
            text=(f':warning: Could not regenerate suggestions: '
                  f'`{type(e).__name__}: {e}`'),
        )
        return

    if not suggestions:
        client.chat_update(
            channel=channel, ts=placeholder_ts,
            text=(':no_entry: No new available + price-capped domains found. '
                  'Try a different extension or audience via `/new-domain`.'),
        )
        return

    new_blocks = _build_new_domain_shortlist_blocks(
        suggestions=suggestions,
        vertical=vertical,
        audience=audience,
        extension=extension,
        lander=lander,
        requester=requester,
        examples=examples,
        aws_account=aws_account,
        external_requester=external_requester,
    )
    client.chat_update(
        channel=channel,
        ts=placeholder_ts,
        blocks=new_blocks,
        text=f'{len(suggestions)} fresh domains for {vertical}',
    )


# ─── ATOM setup live-progress reporter ────────────────────────────────────
#
# ATOM's setup_domain runs 9 sequential steps server-side. While they
# execute we poll ATOM's /api/status/{task_id} every 5 seconds and edit
# the original progress message in-place with a Slack checklist so the
# requester can SEE the deploy progressing rather than staring at silence
# for 5-20 minutes. Step keys + display labels mirror aws_automation's
# canonical order in aws_automation.py (~line 982 onward).

_ATOM_STEP_ORDER = [
    ('initialization',          'Initializing setup'),
    ('certificate',             'Requesting SSL certificate'),
    ('namecheap_cname',         'Adding CNAME validation records to Namecheap'),
    ('route53_zone',            'Creating Route 53 hosted zone'),
    ('s3_buckets',              'Creating S3 buckets'),
    ('certificate_validation',  'Waiting for SSL certificate validation'),
    ('cloudfront',              'Creating CloudFront distribution'),
    ('route53_records',         'Creating Route 53 alias records'),
    ('nameserver_update',       'Updating Namecheap nameservers'),
]


def _render_progress_checklist(steps: dict) -> str:
    """Render ATOM's per-step status map as a Slack-friendly checklist.

    ``steps`` is the ``steps`` field from ATOM's status response — a dict
    keyed by step_key with values like ``{'status': 'completed'}``. Empty
    dict (pre-first-poll) renders all rows as pending so the requester
    sees the full 9-row outline immediately.

    Implicit-completion: when a later step has any state but an earlier
    step has none, the earlier step is rendered as ✅. ATOM's progress
    callback only fires when a step does NEW work — on retries it
    short-circuits steps whose resources already exist (cert reused,
    R53 zone already exists, etc.) and just skips ahead. Without this
    logic those skipped steps render as ⬜ "pending" alongside the
    later ✅ steps, which looks broken even though the deploy succeeded
    (audit 2026-05-13 retry on naturalfitnessguide.com — CNAME
    validation step never re-emitted because the cert was already
    valid from the first attempt).
    """
    glyphs = {
        'completed':   ':white_check_mark:',
        'in_progress': ':hourglass_flowing_sand:',
        'failed':      ':x:',
    }
    # Find the highest-index step that has any state. Anything below it
    # with no state is implicitly done — ATOM moved past it.
    last_active_idx = -1
    for i, (key, _label) in enumerate(_ATOM_STEP_ORDER):
        step = steps.get(key) or {}
        if step.get('status'):
            last_active_idx = i

    lines = ['*Setup progress:*']
    for i, (key, label) in enumerate(_ATOM_STEP_ORDER):
        step = steps.get(key) or {}
        state = (step.get('status') or '').lower()
        if state in glyphs:
            glyph = glyphs[state]
        elif i < last_active_idx:
            # No explicit state, but a later step IS active — this one
            # was either skipped (resource reused) or finished silently.
            # Either way, it's done from the requester's perspective.
            glyph = ':white_check_mark:'
        else:
            glyph = ':white_large_square:'
        lines.append(f'{glyph} {label}')
    return '\n'.join(lines)


def _make_atom_progress_callback(*, client, channel, message_ts,
                                 header_text, target_domain):
    """Build the callback ``wait_for_setup`` invokes on each poll.

    Closure state: the last rendered checklist string. We only call
    chat_update when the rendered output actually changes — Slack
    rate-limits message edits, and unchanged updates would waste both
    quota and the requester's "new activity" indicators.

    Resilient by design: if message_ts is missing (the initial
    chat_postMessage failed) or chat_update raises, we log + swallow.
    Progress reporting is decorative; it must never derail the real
    workflow.
    """
    state = {'last_rendered': _render_progress_checklist({})}

    def on_progress(status: dict) -> None:
        if not message_ts:
            return
        steps = status.get('steps') or {}
        rendered = _render_progress_checklist(steps)
        if rendered == state['last_rendered']:
            return
        state['last_rendered'] = rendered
        try:
            client.chat_update(
                channel=channel,
                ts=message_ts,
                text=header_text + '\n\n' + rendered,
            )
        except Exception:
            logger.exception(
                'progress chat_update failed for %s (ts=%s); '
                'continuing the wait',
                target_domain, message_ts,
            )

    return on_progress


def _build_retry_setup_blocks(*, heading, target_domain, vertical,
                              requester, lander_url,
                              original_channel, original_message_ts):
    """Build Block Kit blocks for a Phase 7 failure message that
    includes a Retry-setup button.

    ATOM's setup_domain is idempotent for already-existing AWS
    resources (cert, Route 53 zone, S3 bucket, S3 bucket policy) — so
    re-running effectively *resumes* from the failed step rather than
    re-creating what worked. CloudFront creation in particular has a
    non-deterministic failure mode (cert not yet replicated to the
    edge, transient AWS API blip) that often succeeds on retry.

    The button preserves the original ``lander_url`` so a blank-on-
    submission setup-only run retries as setup-only, and a real-lander
    run retries with the same source. The original channel + message
    timestamp are also preserved so the retry's progress updates land
    in the same Slack thread.
    """
    retry_payload = sign_payload({
        'target_domain': target_domain,
        'vertical': vertical,
        'requester': requester,
        'lander_url': lander_url or '',
        'original_channel': original_channel,
        'original_message_ts': original_message_ts,
    })
    return [
        {'type': 'section',
         'text': {'type': 'mrkdwn', 'text': heading}},
        {'type': 'actions', 'elements': [{
            'type': 'button',
            'action_id': 'retry_atom_setup',
            'text': {'type': 'plain_text',
                     'text': ':arrows_counterclockwise: Retry setup'},
            'style': 'primary',
            'value': retry_payload,
        }]},
        {'type': 'context', 'elements': [{
            'type': 'mrkdwn',
            'text': ('_Retry re-runs ATOM\'s 9-step setup. '
                     'Already-created AWS resources (cert / R53 zone / '
                     'S3 bucket) are reused; the failed step gets '
                     'retried. CloudFront errors are most often '
                     'transient — try once before digging into AWS._'),
        }]},
    ]


# ─── Phase 7 worker (module-level so tests can import it) ──────────────────

def _phase7_run_atom_setup(client, channel, message_ts, target_domain,
                           vertical, requester, lander_url=''):
    """Worker: call run_existing_domain_workflow + post progress to Slack.

    Runs in a daemon thread (so the Slack handler can ack quickly).
    Posts thread replies on the original Mark Done message; DMs the
    requester at completion or failure.

    The source bucket + folder come from the user-supplied lander_url
    (parsed via _parse_lander_url) — the URL itself names the source.
    Falls back to Config.phase7_defaults_for(vertical) when no URL is
    available (e.g. legacy buttons that pre-date URL parsing).

    Thread-safety: inventory_store opens a fresh sqlite connection per
    call, and the Slack WebClient is stateless wrt the auth token.
    """
    # Three intentional cases:
    #   1. Empty lander_url  → setup-only run. Pass empty source to the
    #      workflow; run_existing_domain_workflow has a dedicated branch
    #      that runs ATOM setup but skips copy_files (added 2026-05-11
    #      when the modal made lander URL optional).
    #   2. Parseable URL     → standard deploy. Source is URL-derived.
    #   3. Unparseable URL   → fall back to per-vertical defaults, then
    #      warn if even defaults don't have a source.
    #
    # Before this branching the worker treated case 1 as "no source
    # configured" and short-circuited with the legacy "URL is empty"
    # warning — which contradicted the modal's "lander is optional"
    # contract and left freshly-purchased setup-only domains stuck at
    # status=PENDING with setup_at=NULL forever (audit 2026-05-12 in
    # prod).
    lander_url = (lander_url or '').strip()

    if not lander_url:
        source_bucket = ''
        source_folders = []
        source_files = []
        # Source account is still meaningful even for setup-only —
        # workflow may use it for cross-account permissions or future
        # copy operations triggered by a later /list-domains redeploy.
        source_account = Config.phase7_defaults_for(vertical)['source_account']
        source_origin = (
            'setup-only run (no lander URL provided — AWS infrastructure '
            'will be provisioned, no file copy)'
        )
    else:
        url_bucket, url_folders, url_err = _parse_lander_url(lander_url)
        if url_bucket:
            source_bucket = url_bucket
            source_folders = url_folders
            source_files = []
            # Account still comes from per-vertical config — the URL only
            # tells us WHERE the files are, not which AWS creds can read them.
            source_account = Config.phase7_defaults_for(vertical)['source_account']
            source_origin = f'parsed from lander URL `{lander_url}`'
        else:
            defaults = Config.phase7_defaults_for(vertical)
            source_bucket = defaults['source_bucket']
            source_folders = defaults['source_folders']
            source_files = defaults['source_files']
            source_account = defaults['source_account']
            source_origin = (
                f'config defaults (lander URL `{lander_url}` failed to '
                f'parse: {url_err})'
            )

    # Genuine user-error short-circuit: a URL was provided but didn't
    # parse AND no per-vertical fallback exists. Distinct from the
    # setup-only path above (which intentionally has empty source).
    if not source_bucket and lander_url:
        client.chat_postMessage(
            channel=channel, thread_ts=message_ts,
            text=(f':warning: *Phase 7 cannot deploy* — '
                  f'lander URL `{lander_url}` did not parse and no '
                  'fallback source is configured for this vertical.\n'
                  'Inventory was updated but the lander was NOT '
                  'actually deployed.\n'
                  '_Tip: use the form `https://<bucket>/<folder>/` in '
                  'the lander URL field next time and the bot will '
                  'figure out the rest._'),
        )
        try:
            inventory_store.record_event(
                target_domain, 'phase7_skipped', actor='cron',
                metadata={'reason': 'unparseable_lander_url_no_fallback',
                          'lander_url': lander_url},
            )
        except Exception:
            logger.exception('record_event(phase7_skipped) failed')
        return

    # Phase 7 audit: started. Captures the resolved source so an operator
    # debugging a failed deploy via /domain-history sees the inputs ATOM
    # was handed.
    try:
        inventory_store.record_event(
            target_domain, 'phase7_started', actor='cron',
            metadata={
                'source_bucket': source_bucket,
                'source_folders': source_folders,
                'source_account': source_account,
                'source_origin': source_origin,
                'requester': requester,
            },
        )
    except Exception:
        logger.exception('record_event(phase7_started) failed')

    # Decide what message to post BEFORE the workflow runs. The
    # 9-step "Setup progress" checklist only makes sense on a fresh
    # domain — when `setup_at` is already populated the workflow's
    # skip-setup branch goes straight to copy_files and the checklist
    # would stay all-pending forever, confusing the requester (audit
    # 2026-05-11). Look up the row here so the header reflects what
    # will actually happen.
    try:
        record_now = inventory_store.get_domain(target_domain) or {}
    except Exception:
        record_now = {}
    already_setup = bool(record_now.get('setup_at'))

    # Header line for source — render `—` (em-dash) for the
    # setup-only path so we don't show ugly empty backticks "``" in
    # Slack.
    source_bucket_line = (
        f'• source bucket: `{source_bucket}`' if source_bucket
        else '• source: _none (setup-only)_'
    )
    source_folders_line = (
        f'• source folders: `{source_folders}`' if source_folders
        else ''
    )

    if already_setup:
        header_text = (
            f':package: *Deploying lander to* `{target_domain}`\n'
            f'{source_bucket_line}\n'
            + (f'{source_folders_line}\n' if source_folders_line else '')
            + f'• source resolved from: _{source_origin}_\n'
            f'• ATOM setup: _already complete '
            f'(skipping cert / R53 / CloudFront — only copying files)_\n'
            '_This usually takes 1–2 minutes — file copy only._'
        )
        progress_msg = client.chat_postMessage(
            channel=channel, thread_ts=message_ts,
            text=header_text,
        )
        # No live checklist on the skip-setup path. The workflow's
        # wait_for_setup is never called, so the callback would never
        # fire — leaving the checklist stuck at all-pending and
        # making the requester think ATOM hung.
        progress_callback = None
    else:
        header_text = (
            f':rocket: *Triggering ATOM setup* for `{target_domain}`\n'
            f'{source_bucket_line}\n'
            + (f'{source_folders_line}\n' if source_folders_line else '')
            + f'• source resolved from: _{source_origin}_\n'
            '_This usually takes 5–20 minutes (cert validation + '
            'CloudFront)._'
        )
        # Post the progress message as a thread reply and remember its
        # ts — the progress callback edits THIS message in-place
        # rather than spamming the channel with one update per step.
        progress_msg = client.chat_postMessage(
            channel=channel, thread_ts=message_ts,
            text=header_text + '\n\n' + _render_progress_checklist({}),
        )
        progress_ts = progress_msg.get('ts')
        progress_callback = _make_atom_progress_callback(
            client=client, channel=channel, message_ts=progress_ts,
            header_text=header_text, target_domain=target_domain,
        )

    req = ExistingDomainRequest(
        target_domain=target_domain,
        source_account=source_account,
        source_bucket=source_bucket,
        source_folders=source_folders,
        source_files=source_files,
        requested_by=f'Slack:{requester}',
    )

    try:
        result = run_existing_domain_workflow(
            req, progress_callback=progress_callback,
        )
    except Exception as e:
        logger.exception('Phase 7 worker crashed for %s', target_domain)
        try:
            inventory_store.record_event(
                target_domain, 'phase7_crashed', actor='cron',
                metadata={'exception': type(e).__name__, 'message': str(e)[:500]},
            )
        except Exception:
            pass
        retry_blocks = _build_retry_setup_blocks(
            heading=f':x: *ATOM workflow crashed:* '
                    f'`{type(e).__name__}: {e}`',
            target_domain=target_domain, vertical=vertical,
            requester=requester, lander_url=lander_url,
            original_channel=channel, original_message_ts=message_ts,
        )
        client.chat_postMessage(
            channel=channel, thread_ts=message_ts,
            text=f':x: *ATOM workflow crashed:* `{type(e).__name__}: {e}`',
            blocks=retry_blocks,
        )
        client.chat_postMessage(
            channel=requester,
            text=(f':x: Sorry — `{target_domain}` deploy hit an error: '
                  f'`{type(e).__name__}: {e}`. See Slack thread for details.'),
        )
        return

    if result.status == 'completed':
        live = result.details.get('live_url') or f'https://{target_domain}'
        try:
            inventory_store.record_event(
                target_domain, 'phase7_succeeded', actor='cron',
                metadata={'live_url': live, 'message': result.message[:500]},
            )
        except Exception:
            logger.exception('record_event(phase7_succeeded) failed')
        client.chat_postMessage(
            channel=channel, thread_ts=message_ts,
            text=(f':white_check_mark: *ATOM finished.* {result.message}\n'
                  f'Live at: {live}'),
        )
        client.chat_postMessage(
            channel=requester,
            text=(f':tada: `{target_domain}` is fully deployed. '
                  f'Live at: {live}'),
        )
    else:
        setup_result_dict = (result.details.get('setup_result') or {})
        failed_step = setup_result_dict.get('failed_at_step', '?')
        # When ATOM classified the error (CNAMEAlreadyExists,
        # InvalidViewerCertificate, NoSuchHostedZone, etc.), pull the
        # suggested_action so we can render it as a separate block on
        # the Slack thread — operator sees the fix path inline instead
        # of having to hunt for it.
        err_obj = setup_result_dict.get('error') or {}
        if not isinstance(err_obj, dict):
            err_obj = {}
        diagnosis = err_obj.get('diagnosis') or {}
        if not isinstance(diagnosis, dict):
            diagnosis = {}
        diagnosis_action = diagnosis.get('suggested_action') if isinstance(
            diagnosis.get('suggested_action'), str
        ) else None
        diagnosis_root_cause = diagnosis.get('root_cause') if isinstance(
            diagnosis.get('root_cause'), str
        ) else None

        try:
            inventory_store.record_event(
                target_domain, 'phase7_failed', actor='cron',
                metadata={
                    'failed_at_step': failed_step,
                    'reason': (result.details or {}).get('reason'),
                    'message': result.message[:500],
                    'diagnosis_root_cause': diagnosis_root_cause,
                },
            )
        except Exception:
            logger.exception('record_event(phase7_failed) failed')

        retry_blocks = _build_retry_setup_blocks(
            heading=(f':x: *ATOM workflow failed* at step `{failed_step}`.\n'
                     f'Reason: {result.message}'),
            target_domain=target_domain, vertical=vertical,
            requester=requester, lander_url=lander_url,
            original_channel=channel, original_message_ts=message_ts,
        )
        # Inject ATOM's diagnosis (if any) as a separate section block
        # between the failure heading and the Retry button. Multi-line
        # text rendered as mrkdwn — preserves the URLs + numbered steps
        # ATOM emits in suggested_action. Slack section text caps at
        # 3000 chars; we trim to be safe.
        if diagnosis_action:
            diagnosis_blocks = [
                {'type': 'divider'},
                {'type': 'section', 'text': {
                    'type': 'mrkdwn',
                    'text': (
                        f':mag: *Root cause:* `{diagnosis_root_cause}`\n\n'
                        + diagnosis_action[:2800]
                    ),
                }},
                {'type': 'divider'},
            ]
            # Insert just before the actions block (the Retry button)
            # so the suggested_action sits between the heading and the
            # button — natural reading order.
            insert_at = next(
                (i for i, b in enumerate(retry_blocks)
                 if b.get('type') == 'actions'),
                len(retry_blocks),
            )
            retry_blocks = (
                retry_blocks[:insert_at]
                + diagnosis_blocks
                + retry_blocks[insert_at:]
            )

        client.chat_postMessage(
            channel=channel, thread_ts=message_ts,
            text=(f':x: *ATOM workflow failed* at step `{failed_step}`.\n'
                  f'Reason: {result.message}'),
            blocks=retry_blocks,
        )
        client.chat_postMessage(
            channel=requester,
            text=(f':x: `{target_domain}` deploy did not complete. '
                  f'Step `{failed_step}` failed: {result.message}'),
        )


# ─── /pixel-fire worker ────────────────────────────────────────────────────
# Runs in a daemon thread so the slash command can ack within Slack's 3s
# window. Calls the pure-logic pixel_fire module, then DMs the operator
# with a structured proof block (or the failure reason).

def _pixel_fire_worker(client, actor: str, response_channel: str,
                        event: str, pixel_id: str) -> None:
    """Background worker: run the pixel update + DM the operator.

    `actor` is the Slack user ID of whoever ran /pixel-fire — used both
    as the audit-log actor and as the DM recipient.
    `response_channel` is where to fall back to (ephemeral) if the DM
    fails (rare — happens when the user has DMs disabled with the bot).
    """
    try:
        result = update_pixel_on_lander(event, pixel_id, actor=actor)
    except Exception as e:
        # update_pixel_on_lander is designed to never raise — a bare
        # except here only catches genuine bugs (import failure, etc).
        # Log + notify rather than letting the daemon thread die silently.
        logger.exception('pixel_fire worker crashed (should never happen)')
        _pixel_fire_notify(
            client, actor, response_channel,
            text=(f':x: `/pixel-fire` crashed unexpectedly: '
                  f'{type(e).__name__}. Flag to TL — full traceback in '
                  'Render logs.'),
        )
        return

    # Success / no-change: post a structured proof block. Failure: post
    # the operator-facing message verbatim (already explains why).
    if result.status == 'updated':
        _pixel_fire_notify(
            client, actor, response_channel,
            blocks=_pixel_fire_proof_blocks(result),
            text=result.message,
        )
    elif result.status == 'no_change':
        _pixel_fire_notify(
            client, actor, response_channel,
            text=f':information_source: {result.message}',
        )
    else:
        # invalid_input / inventory_error / atom_error / safety_belt
        _pixel_fire_notify(
            client, actor, response_channel,
            text=f':x: {result.message}',
        )


def _pixel_fire_notify(client, user: str, fallback_channel: str,
                        *, text: str, blocks: list = None) -> None:
    """DM the operator with the result; fall back to an ephemeral message
    in the originating channel if the DM fails (DMs disabled, bot not in
    the user's DM list, etc.). Last-resort fallback is the log."""
    try:
        client.chat_postMessage(channel=user, text=text, blocks=blocks)
        return
    except Exception:
        logger.exception(
            'pixel_fire: DM to %s failed, falling back to ephemeral', user,
        )
    try:
        client.chat_postEphemeral(
            channel=fallback_channel, user=user, text=text, blocks=blocks,
        )
    except Exception:
        logger.exception(
            'pixel_fire: ephemeral fallback to %s in %s also failed',
            user, fallback_channel,
        )


def _pixel_fire_proof_blocks(result) -> list:
    """Build the success-DM Block Kit payload — the proof an MDB would
    have screenshotted from the Pixel Helper extension before this
    command existed.

    Surfaces: old → new for both ID + event, the live URL to spot-check,
    and a reminder to use the Pixel Helper extension as the visual
    sanity check (which the bot can't do server-side without headless
    Chrome — overkill for v1)."""
    d = result.details
    return [
        {'type': 'header', 'text': {
            'type': 'plain_text',
            'text': f':white_check_mark: Pixel updated',
        }},
        {'type': 'section', 'text': {
            'type': 'mrkdwn',
            'text': (
                f'*Lander:* `{PIXEL_FIRE_DOMAIN}/{PIXEL_FIRE_FILE_KEY}`\n'
                f'*Pixel ID:* `{d.get("old_pixel_id")}` → '
                f'`{d.get("new_pixel_id")}`  _(2 spots)_\n'
                f'*Event:* `{d.get("old_event")}` → '
                f'`{d.get("new_event")}`  _(3 spots)_'
            ),
        }},
        {'type': 'section', 'text': {
            'type': 'mrkdwn',
            'text': (
                f'*Live URL:* {PIXEL_FIRE_LIVE_URL}\n'
                '_To verify visually: open the URL → Meta Pixel Helper '
                'Chrome extension → click the buttons → you should see '
                f'`{d.get("new_event")}` firing under pixel '
                f'`{d.get("new_pixel_id")}`._'
            ),
        }},
    ]


# ─── /new-tracker worker ───────────────────────────────────────────────────
# Mirrors _pixel_fire_worker: daemon thread so the slash command can ack
# within Slack's 3s window. Calls the pure-logic tracker_setup module,
# then DMs the operator with the result (success block or failure line).

def _new_tracker_worker(client, actor: str, response_channel: str,
                         cname_name: str, domain: str) -> None:
    """Background worker: run add_tracker + DM the operator."""
    try:
        result = add_tracker(cname_name, domain, actor=actor)
    except Exception as e:
        logger.exception('new-tracker worker crashed (should never happen)')
        _pixel_fire_notify(  # reuse the DM/ephemeral-fallback helper
            client, actor, response_channel,
            text=(f':x: `/new-tracker` crashed unexpectedly: '
                  f'{type(e).__name__}. Flag to TL — traceback in '
                  'Render logs.'),
        )
        return

    if result.status == 'created':
        _pixel_fire_notify(
            client, actor, response_channel,
            blocks=_new_tracker_proof_blocks(result),
            text=result.message,
        )
    elif result.status == 'already_present':
        _pixel_fire_notify(
            client, actor, response_channel,
            text=f':information_source: {result.message}',
        )
    elif result.status == 'dns_done_redtrack_failed':
        # Partial — tell the user clearly that DNS is in place but
        # RedTrack didn't take. Re-run is the recovery path.
        _pixel_fire_notify(
            client, actor, response_channel,
            text=f':warning: {result.message}',
        )
    else:
        # safety_belt / invalid_input / inventory_error / dns_error
        _pixel_fire_notify(
            client, actor, response_channel,
            text=f':x: {result.message}',
        )


def _new_tracker_proof_blocks(result) -> list:
    """Block Kit payload for the success DM — what got created, the live
    URL, the SSL-provisioning caveat."""
    d = result.details
    tracker_url = d.get('tracker_url', '')
    cname_target = d.get('cname_target', '')
    dns_action = d.get('dns_action', '')
    rt_id = d.get('redtrack_id') or '(unknown)'
    rt_already = d.get('redtrack_already_existed')

    dns_line = (
        f'Route 53 CNAME `{tracker_url}` → `{cname_target}` '
        + ('_(already correct)_' if dns_action == 'skipped_already_correct'
           else '_(created)_')
    )
    rt_line = (
        f'RedTrack domain id: `{rt_id}` '
        + ('_(already registered)_' if rt_already else '_(registered fresh)_')
    )
    return [
        {'type': 'header', 'text': {
            'type': 'plain_text',
            'text': ':white_check_mark: Tracker domain set up',
        }},
        {'type': 'section', 'text': {
            'type': 'mrkdwn',
            'text': f'*Tracker:* `https://{tracker_url}/`\n{dns_line}\n{rt_line}',
        }},
        {'type': 'context', 'elements': [{
            'type': 'mrkdwn',
            'text': ('_SSL cert via Let\'s Encrypt may take 2-5 min to '
                     'provision. If you see an SSL error on first hit, '
                     'wait a few minutes and reload._'),
        }]},
    ]


# ─── Slack command + interaction handlers (registered on the bolt app) ─────

if _bolt_app is not None:

    @_bolt_app.command('/list-domains')
    def handle_list_domains(ack, respond, command):
        """Reply with the owned-domain inventory as a clickable card.

        Two filter modes:

          • substring (default) — matches domain / vertical / requester.
            /list-domains              → top results across everything
            /list-domains auto         → matches vertical 'Auto Insurance'
            /list-domains anurag       → matches anything by Anurag

          • state filter (`:keyword`) — matches by lifecycle_state group.
            /list-domains :expiring    → all EXPIRING_30/14/7/1
            /list-domains :idle        → state == IDLE
            /list-domains :awaiting    → all AWAITING_* (waiting on a click)
            /list-domains :inventory   → released to the rotation pool
            (full keyword list in the message footer)

        Each domain has a "Deploy lander" button that starts Path A
        (deploy a lander to that existing owned domain). Each row also
        shows two badges: setup status (✅/⏳) and lifecycle state
        (🟢 active / 💤 idle / ⚠️ expiring / etc.).
        """
        ack()
        from lifecycle import badges as _badges

        filter_text = (command.get('text') or '').strip().lower()
        all_rows = inventory_store.list_domains()
        if not all_rows:
            respond({
                'response_type': 'ephemeral',
                'text': '*No domains in inventory yet.*',
            })
            return

        # State filter takes precedence when the text starts with `:`.
        # Otherwise fall through to the substring search.
        state_pred = (
            _badges.state_filter(filter_text) if filter_text.startswith(':')
            else None
        )
        if state_pred is not None:
            rows = [r for r in all_rows if state_pred(r)]
            filter_label = filter_text  # keep the leading colon in the header
        elif filter_text:
            def _matches(r: dict) -> bool:
                haystacks = (
                    (r.get('domain') or ''),
                    (r.get('vertical') or ''),
                    (r.get('requested_by') or ''),
                )
                return any(filter_text in h.lower() for h in haystacks)
            rows = [r for r in all_rows if _matches(r)]
            filter_label = filter_text
        else:
            rows = all_rows
            filter_label = ''

        if not rows:
            hint = (
                'Try a state keyword like '
                f'{_badges.help_keywords()}, a domain substring, '
                'a vertical, or an owner name — or run `/list-domains` '
                'with no filter.'
            )
            respond({
                'response_type': 'ephemeral',
                'text': (f'*No domains match `{filter_label}`.*\n{hint}'),
            })
            return

        # Slack caps blocks at 50 per message. We render at most ~30 domains
        # per call, leaving room for header/footer/dividers.
        max_per_message = 30
        shown = rows[:max_per_message]
        truncated = len(rows) - len(shown)

        header_text = (
            f'Owned domains — {len(shown)} of {len(all_rows)}'
            + (f' (filtered by "{filter_label}")' if filter_label else '')
        )

        blocks: list = [
            {'type': 'header',
             'text': {'type': 'plain_text', 'text': header_text}},
            {'type': 'context', 'elements': [{
                'type': 'mrkdwn',
                'text': (
                    'Click *Deploy lander* to send a redeployment request '
                    'to Utkarsh. Filter: substring (`/list-domains medicare`) '
                    f'or state keyword ({_badges.help_keywords()}).'
                ),
            }]},
            {'type': 'divider'},
        ]

        for r in shown:
            vert = r.get('vertical') or '_no vertical_'
            requested_by = r.get('requested_by') or '_unknown_'
            setup_emoji = '✅' if r.get('setup_at') else '⏳'
            lc_state = r.get('lifecycle_state')
            lc_emoji = _badges.emoji(lc_state)
            lc_label = _badges.label(lc_state)
            blocks.append({
                'type': 'section',
                'text': {
                    'type': 'mrkdwn',
                    'text': (f'{setup_emoji}{lc_emoji} `{r["domain"]}` '
                             f'_{lc_label}_\n'
                             f'_{vert}_  ·  by `{requested_by}`'),
                },
                'accessory': {
                    'type': 'button',
                    'action_id': 'deploy_lander_existing',
                    'text': {'type': 'plain_text', 'text': 'Deploy lander'},
                    'value': sign_payload({
                        'domain': r['domain'],
                        'vertical': r.get('vertical') or '',
                        'aws_account': r.get('aws_account') or '',
                    }),
                },
            })

        if truncated > 0:
            blocks.append({'type': 'divider'})
            blocks.append({'type': 'context', 'elements': [{
                'type': 'mrkdwn',
                'text': (f'_…and {truncated} more. Narrow with '
                         f'`/list-domains <vertical>`._'),
            }]})

        respond({
            'response_type': 'ephemeral',
            'blocks': blocks,
            'text': header_text,  # fallback for clients without block support
        })

    @_bolt_app.command('/domain-history')
    def handle_domain_history(ack, respond, command):
        """Replay the lifecycle audit timeline for one domain.

            /domain-history mybusiness.com

        Shows current state + the last 25 events from `domain_events`
        (the table the cron + Slack handlers write to on every state
        transition). Read-only, ephemeral reply.
        """
        ack()
        from lifecycle.history_view import render_timeline

        domain = (command.get('text') or '').strip().lower()
        if not domain:
            respond({
                'response_type': 'ephemeral',
                'text': (
                    'Usage: `/domain-history <domain>` — e.g. '
                    '`/domain-history safetyfirstauto.pro`. Replays the '
                    "lifecycle bot's audit log for that domain."
                ),
            })
            return

        # Strip http(s):// + www. + path so users can paste a URL too.
        for prefix in ('https://', 'http://', 'www.'):
            if domain.startswith(prefix):
                domain = domain[len(prefix):]
        domain = domain.split('/')[0]

        row = inventory_store.get_domain(domain)
        events = inventory_store.list_domain_events(domain) if row else []

        blocks = render_timeline(
            row, events, requested_domain=domain,
        )
        respond({
            'response_type': 'ephemeral',
            'blocks': blocks,
            'text': f'Timeline for {domain}',
        })

    @_bolt_app.command('/reassign-domain')
    def handle_reassign_domain(ack, respond, command):
        """Reassign a domain to one or more MDBs.

            /reassign-domain mybusiness.com @anusree
            /reassign-domain mybusiness.com @anusree @rajat   (multi-MDB)
            /reassign-domain mybusiness.com clear             (release to inventory)

        Ends every active assignment for the domain, then inserts new
        rows in domain_assignments. Writes a `reassigned` event for
        /domain-history. Updates legacy domains.assigned_to for
        backwards-compat (first new MDB wins on that column).

        Anyone in the workspace can run this — per Q2 design decision,
        reassignment is operational, not policy.
        """
        ack()
        actor = command.get('user_id', '')
        text = (command.get('text') or '').strip()

        if not text:
            respond({
                'response_type': 'ephemeral',
                'text': (
                    'Usage: `/reassign-domain <domain> @user1 [@user2 ...]`\n'
                    'or: `/reassign-domain <domain> clear` to release into '
                    'the inventory pool.'
                ),
            })
            return

        parts = text.split()
        domain_raw = parts[0]
        # Normalise domain (strip protocol + path)
        domain = domain_raw.lower()
        for prefix in ('https://', 'http://', 'www.'):
            if domain.startswith(prefix):
                domain = domain[len(prefix):]
        domain = domain.split('/')[0]

        row = inventory_store.get_domain(domain)
        if not row:
            respond({
                'response_type': 'ephemeral',
                'text': (f':no_entry: `{domain}` is not in inventory. Check '
                         'the spelling or use `/list-domains` to find it.'),
            })
            return

        # Parse mentions. Slack passes user mentions as `<@U_XXX|username>`
        # in command text. The 'clear' keyword releases the domain.
        targets = parts[1:]
        if len(targets) == 1 and targets[0].lower() == 'clear':
            new_assignees = []
        else:
            import re as _re
            new_assignees = []
            for t in targets:
                m = _re.match(r'<@([A-Z0-9]+)(?:\|.*)?>', t)
                if m:
                    new_assignees.append(m.group(1))
                else:
                    respond({
                        'response_type': 'ephemeral',
                        'text': (f':no_entry: Couldn\'t parse {t!r} as a '
                                 'Slack user mention. Use `@username` so '
                                 'Slack expands it into a user reference.'),
                    })
                    return
            if not new_assignees:
                respond({
                    'response_type': 'ephemeral',
                    'text': (':no_entry: No users specified. Use '
                             '`/reassign-domain <domain> @user` or '
                             '`@user1 @user2 ...` or `clear`.'),
                })
                return

        # Verify each target exists in slack_users (sanity check before write)
        unknown = []
        for uid in new_assignees:
            if inventory_store.get_slack_user(uid) is None:
                unknown.append(uid)
        if unknown:
            respond({
                'response_type': 'ephemeral',
                'text': (f':warning: Don\'t recognise these Slack users in '
                         f'our cache: {unknown}. The daily Slack sync may not '
                         'have run yet, or they\'re not workspace members. '
                         'Try again after the next 7 PM IST cron run, or '
                         '`@`-mention real workspace members only.'),
            })
            return

        # End every existing active assignment
        previous = [a['slack_user_id']
                    for a in inventory_store.current_assignments_for_domain(domain)]
        for uid in previous:
            inventory_store.end_assignment(domain, uid, by=actor)

        # Insert each new assignment
        for uid in new_assignees:
            inventory_store.assign_domain(
                domain, uid,
                assigned_by=actor,
                notes='via /reassign-domain',
            )

        # Keep legacy assigned_to in sync for any code still reading it
        # (first new MDB wins; multi-MDB callers should use the new schema).
        legacy_value = new_assignees[0] if new_assignees else None
        inventory_store.assign_to(domain, legacy_value)

        # Audit event for /domain-history
        inventory_store.record_event(
            domain, 'reassigned', actor=actor,
            metadata={
                'previous_assignees': previous,
                'new_assignees': new_assignees,
                'via': 'slash_command',
            },
        )

        # Friendly confirmation
        if new_assignees:
            who = ', '.join(f'<@{u}>' for u in new_assignees)
            respond({
                'response_type': 'ephemeral',
                'text': (f':white_check_mark: `{domain}` reassigned to {who}.'
                         + (f' Previous: {", ".join(f"<@{u}>" for u in previous)}.'
                            if previous else '')),
            })
        else:
            respond({
                'response_type': 'ephemeral',
                'text': (f':package: `{domain}` released into the inventory '
                         'pool. It will show up in tomorrow\'s '
                         '#developers digest.'
                         + (f' Previous: {", ".join(f"<@{u}>" for u in previous)}.'
                            if previous else '')),
            })

    @_bolt_app.command('/buy-domain')
    def handle_buy_domain_command(ack, body, client):
        """Open the buy-domain modal.

        Inline arg `/buy-domain foo.com` pre-fills the domain input so
        the MDB doesn't have to re-type. Bare `/buy-domain` opens the
        modal with an empty domain field. Either way the domain is
        editable so the MDB can fix typos before submitting.
        """
        ack()
        prefill = _normalise_domain_input(body.get('text', ''))
        client.views_open(
            trigger_id=body['trigger_id'],
            view=_build_buy_domain_modal(prefill_domain=prefill),
        )

    @_bolt_app.view('buy_domain_modal')
    def handle_buy_domain_submission(ack, body, view, client):
        """Modal submitted — run Namecheap availability + price + inventory
        checks, then post a confirm/cancel card to the requester.

        Design rule (feedback_bot_never_hard_rejects_user_choice): every
        finding is surfaced as a labelled bullet on the card. Confirm
        button is always present (except when Namecheap was literally
        unreachable — there's no "fact" to confirm against). The MDB
        decides whether to continue based on the facts they see.

        Confirm reuses ``action_id='pick_domain'`` so the downstream
        chain (TL approval -> Mark Purchased -> deploy) is shared with
        the AI-shortlist flow. Zero duplicated handler code.
        """
        ack()
        values = view['state']['values']
        raw_domain = (values.get('domain_block', {})
                            .get('domain_input', {}).get('value') or '')
        domain = _normalise_domain_input(raw_domain)
        vertical = (values['vertical_block']['vertical_input']['value']
                    or '').strip()
        aws_account = (
            values.get('aws_account_block', {})
                  .get('aws_account_select', {})
                  .get('selected_option', {})
                  .get('value')
            or (Config.AWS_ACCOUNT_OPTIONS[0]
                if Config.AWS_ACCOUNT_OPTIONS else '')
        )
        lander = ((values.get('lander_block') or {})
                  .get('lander_input', {}).get('value') or '').strip()

        operator = body['user']['id']
        mdb_select = (values.get('mdb_block') or {}).get('mdb_select') or {}
        picked_mdb = mdb_select.get('selected_user') or ''
        requester = picked_mdb or operator

        log_event(
            'buy_domain_submitted', domain=domain, vertical=vertical,
            aws_account=aws_account, lander_url=lander or None,
            requester=requester, operator=operator,
        )

        # ── Finding 1: format ─────────────────────────────────────────
        # The ONE technical impossibility: if the domain doesn't parse,
        # we can't Namecheap-check it. Per the no-hard-reject rule, we
        # still post a card, but it carries an instruction to re-run
        # rather than a Confirm button — there's no fact to confirm.
        format_ok = bool(_DOMAIN_RE.match(domain))
        if not format_ok:
            client.chat_postMessage(
                channel=requester,
                text=(
                    f':warning: *Couldn\'t parse `{domain}` as a domain.*\n'
                    'Apex domains only — no subdomains, no paths. Try '
                    '`/buy-domain <name.tld>` again with a clean name.'
                ),
            )
            return

        extension = _extract_extension(domain)

        # ── Finding 2: inventory collision ───────────────────────────
        existing = None
        try:
            existing = inventory_store.get_domain(domain)
        except Exception:
            logger.exception(
                'inventory lookup failed for /buy-domain %s — '
                'proceeding with collision finding marked unknown',
                domain,
            )
        if existing:
            owner = existing.get('assigned_to') or 'unassigned'
            setup_at = existing.get('setup_at') or 'never deployed'
            inventory_finding = (
                f':warning: *Already in our inventory* '
                f'(assigned to <@{owner}>, setup_at: {setup_at}). '
                f'Confirming will create a duplicate-purchase request — '
                f'usually you want `/list-domains {domain}` + Mark Deployed '
                f'instead.'
            )
        else:
            inventory_finding = (
                ':white_check_mark: Not in our inventory — safe to add.'
            )

        # ── Finding 3 + 4: Namecheap availability + price ────────────
        # Cap lookup is cheap and never fails; do it first so the card
        # has a fallback price line even when Namecheap is unreachable.
        cap_price = Config.price_cap_for(extension)
        availability_finding = ''
        price_finding = ''
        namecheap_ok = True
        try:
            results = namecheap_check.check_availability_and_price(
                [domain], extension=extension,
            )
            nc = results[0] if results else {}
        except Exception as e:
            logger.exception(
                'Namecheap check failed for /buy-domain %s', domain,
            )
            nc = {}
            namecheap_ok = False
            availability_finding = (
                f':warning: *Couldn\'t reach Namecheap right now* '
                f'(`{type(e).__name__}: {str(e)[:120]}`). Availability + '
                f'price unknown.'
            )

        if namecheap_ok:
            if nc.get('available'):
                availability_finding = (
                    f':white_check_mark: *Available on Namecheap.*'
                )
            else:
                availability_finding = (
                    ':no_entry: *NOT available on Namecheap right now.* '
                    'Continuing means Utkarsh won\'t be able to buy it via '
                    'the usual flow — confirm only if you have a different '
                    'plan (secondary market, transfer, etc.).'
                )

            price = nc.get('price')
            if price is None:
                price_finding = (
                    f':grey_question: Price unknown for `{extension}`. '
                    f'Cap is ${cap_price:.2f}.'
                )
            else:
                over_cap = price > cap_price
                price_finding = (
                    f':moneybag: *Price:* ${price:.2f}/yr '
                    f'(cap for `{extension}` is ${cap_price:.2f}). '
                    + (':warning: *Over cap* — TL should review carefully.'
                       if over_cap else ':white_check_mark: Under cap.')
                )

        # ── Build the confirm card ────────────────────────────────────
        # Per the no-hard-reject rule, even all-warning findings get a
        # Confirm button. The exception is when Namecheap was literally
        # unreachable: we still post the card (Cancel-only), so the MDB
        # is informed without being asked to confirm a fact we couldn't
        # verify.
        blocks = _build_buy_domain_confirm_blocks(
            domain=domain, vertical=vertical, aws_account=aws_account,
            lander=lander, requester=requester,
            availability_finding=availability_finding,
            inventory_finding=inventory_finding,
            price_finding=price_finding,
            # Pass the actual Namecheap price (or None if API was
            # unreachable) so it propagates through the signed payload
            # chain to TL + Utkarsh cards.
            price=(nc.get('price') if namecheap_ok else None),
        )
        if not namecheap_ok:
            # Replace the actions block with a Cancel-only one. There's
            # no fact to confirm against, so no Confirm button.
            blocks = [b for b in blocks if b.get('type') != 'actions']
            blocks.append({'type': 'actions', 'elements': [
                {
                    'type': 'button',
                    'action_id': 'cancel_buy_domain',
                    'text': {'type': 'plain_text',
                             'text': ':x: Dismiss — try again later'},
                    'style': 'danger',
                    'value': sign_payload({
                        'domain': domain, 'requester': requester,
                    }),
                },
            ]})

        client.chat_postMessage(
            channel=requester,
            text=f'Pre-purchase check for {domain}',
            blocks=blocks,
        )

    @_bolt_app.action('cancel_buy_domain')
    def handle_cancel_buy_domain(ack, body, client):
        """MDB clicked Cancel on the /buy-domain confirm card. Replace
        the card so the buttons can't be re-clicked and emit a
        structured log event for the audit trail."""
        ack()
        data = _verify_button_click(body)
        if data is None:
            return
        domain = data.get('domain', '')
        canceller = body['user']['id']
        log_event(
            'buy_domain_cancelled', domain=domain, canceller=canceller,
        )
        client.chat_update(
            channel=body['channel']['id'],
            ts=body['message']['ts'],
            text=f'Cancelled: {domain}',
            blocks=[
                {'type': 'header', 'text': {
                    'type': 'plain_text',
                    'text': f':x: Cancelled: {domain}',
                }},
                {'type': 'context', 'elements': [{
                    'type': 'mrkdwn',
                    'text': (f'Cancelled by <@{canceller}>. Run '
                             '`/buy-domain` again with a different name '
                             'or extension, or use `/new-domain` for AI '
                             'suggestions.'),
                }]},
            ],
        )

    @_bolt_app.command('/new-domain')
    def handle_new_domain_command(ack, body, client):
        """Open the new-domain modal (internal requests)."""
        ack()
        client.views_open(
            trigger_id=body['trigger_id'],
            view=_build_new_domain_modal(),
        )

    @_bolt_app.command('/new-tracker')
    def handle_new_tracker_command(ack, body, client, respond):
        """Add a tracker subdomain CNAME + register with RedTrack.

        Usage: `/new-tracker {cname} {domain}`
                e.g. `/new-tracker trk neurobloomone.com`

        Open to anyone in the workspace (matches /pixel-fire /
        /reassign-domain). Validation, R53 call, RedTrack call all
        happen in a daemon thread; ack the slash within Slack's 3s
        window and DM the operator with the outcome.

        Uses `respond()` (Slack's response_url webhook) for the
        immediate ephemeral feedback so it works from ANY channel —
        chat_postEphemeral fails with channel_not_found when the bot
        isn't a member of the channel where the slash was invoked
        (production bug 2026-05-18).
        """
        ack()
        # Slash command payload uses flat `user_id` / `channel_id`, NOT
        # the nested `user.id` shape that button/view interactions have.
        actor = body.get('user_id') or ''
        response_channel = body.get('channel_id') or actor
        raw = (body.get('text') or '').strip()

        parts = raw.split()
        if len(parts) != 2:
            respond({
                'response_type': 'ephemeral',
                'text': (
                    ':information_source: *Usage:* '
                    '`/new-tracker {cname} {domain}`\n'
                    'Example: `/new-tracker trk neurobloomone.com`\n'
                    'Creates the Route 53 CNAME + registers the tracker '
                    'with RedTrack. Reserved cnames: `track`, `www`.'
                ),
            })
            return

        cname_arg, domain_arg = parts[0], parts[1]
        respond({
            'response_type': 'ephemeral',
            'text': (f':hourglass_flowing_sand: Setting up tracker '
                     f'`{cname_arg}.{domain_arg}` — Route 53 + RedTrack. '
                     'I\'ll DM you the result in a few seconds.'),
        })

        threading.Thread(
            target=_new_tracker_worker,
            args=(client, actor, response_channel, cname_arg, domain_arg),
            daemon=True,
            name=f'new-tracker-{actor}',
        ).start()

    @_bolt_app.command('/pixel-fire')
    def handle_pixel_fire_command(ack, body, client, respond):
        """Replace the Meta Pixel ID + event on the v1 lander.

        Usage: `/pixel-fire {event} {id}`
                e.g. `/pixel-fire Lead 2714057732308829`

        v1 hardcodes the target to `get-usa-help.com/pixel-fire/index.html`.
        Authorization is open — anyone in the workspace can run it
        (matches /reassign-domain). The actor is recorded on the audit
        row so /domain-history shows who ran it.

        Acks immediately, runs the actual edit in a daemon thread so
        Slack's 3-second response window isn't blocked by ATOM's S3 read
        + write (typically 1-3s, but can spike).

        Uses `respond()` (Slack's response_url webhook) for the
        immediate ephemeral feedback so it works from ANY channel —
        chat_postEphemeral fails with channel_not_found when the bot
        isn't a member of the channel where the slash was invoked
        (production bug 2026-05-18).
        """
        ack()
        # Slash command payload uses flat `user_id` / `channel_id`, NOT
        # the nested `user.id` shape that button/view interactions have.
        actor = body.get('user_id') or ''
        response_channel = body.get('channel_id') or actor
        raw = (body.get('text') or '').strip()

        # Usage check — bare /pixel-fire or wrong arg count.
        parts = raw.split()
        if len(parts) != 2:
            respond({
                'response_type': 'ephemeral',
                'text': (
                    ':information_source: *Usage:* `/pixel-fire {event} {id}`\n'
                    'Example: `/pixel-fire Lead 2714057732308829`\n'
                    f'Updates the Meta Pixel on `{PIXEL_FIRE_DOMAIN}/{PIXEL_FIRE_FILE_KEY}`.'
                ),
            })
            return

        event_arg, id_arg = parts[0], parts[1]

        # Light pre-feedback so the operator sees something IMMEDIATELY
        # while the worker thread does the ATOM round-trip. Real result
        # arrives via DM (or ephemeral fallback) shortly after.
        respond({
            'response_type': 'ephemeral',
            'text': (f':hourglass_flowing_sand: Updating pixel on '
                     f'`{PIXEL_FIRE_DOMAIN}/{PIXEL_FIRE_FILE_KEY}` — '
                     f'event=`{event_arg}`, id=`{id_arg}`. '
                     'I\'ll DM you the result in a few seconds.'),
        })

        threading.Thread(
            target=_pixel_fire_worker,
            args=(client, actor, response_channel, event_arg, id_arg),
            daemon=True,
            name=f'pixel-fire-{actor}',
        ).start()

    @_bolt_app.command('/new-domain-external')
    def handle_new_domain_external_command(ack, body, client):
        """Open the external-requester variant of the new-domain modal.

        Same downstream pipeline as /new-domain — the only difference is
        the entry modal: a required external-requester NAME instead of
        the internal MDB picker. The operator who runs this stays the
        lifecycle owner.
        """
        ack()
        client.views_open(
            trigger_id=body['trigger_id'],
            view=_build_new_domain_external_modal(),
        )

    @_bolt_app.view('new_domain_modal')
    def handle_new_domain_submission(ack, body, view, client):
        """Modal submitted — call the suggestion pipeline and reply with the
        shortlist of available domains.

        Pipeline:
          1. Parse modal inputs
          2. Acknowledge submission, DM the requester with a "summary received"
          3. Call suggest_new_domains() (ChatGPT for naming + Namecheap for
             availability — both fall back to deterministic stubs when API
             keys are absent)
          4. DM the requester with the formatted shortlist

        Next phase: add Approve/Reject buttons + the full purchase + setup +
        copy workflow.
        """
        ack()
        values = view['state']['values']
        vertical = (values['vertical_block']['vertical_input']['value'] or '').strip()
        audience = (values['audience_block']['audience_input']['value'] or '').strip()
        # Lander is OPTIONAL — blank means "provision AWS infrastructure
        # but skip the file copy step." Empty-string sentinel propagates
        # all the way to the deploy worker.
        lander = ((values.get('lander_block') or {})
                  .get('lander_input', {}).get('value') or '').strip()
        extension = values['extension_block']['extension_select']['selected_option']['value']
        # AWS account is REQUIRED (no more implicit auto-insurance default).
        # The modal's static_select always returns a value, but be
        # defensive in case Slack sends a weird payload.
        aws_account = (
            values.get('aws_account_block', {})
                  .get('aws_account_select', {})
                  .get('selected_option', {})
                  .get('value')
            or (Config.AWS_ACCOUNT_OPTIONS[0] if Config.AWS_ACCOUNT_OPTIONS else '')
        )

        # Optional Example domains — multiline text input, one name per line.
        # Anchors the AI's stylistic feel when the prompt's generic defaults
        # don't match the vertical (re-introduced post-Phase-8.1 as an
        # opt-in steering knob alongside Audience).
        examples_raw = (
            (values.get('examples_block') or {})
            .get('examples_input', {})
            .get('value') or ''
        )
        examples = [
            line.strip().lower()
            for line in examples_raw.split('\n')
            if line.strip()
        ]

        # Resolve the effective MDB. When an operator (e.g. Utkarsh during the
        # trial-run period) submits on behalf of someone else, the MDB picker
        # holds that person's user ID. Otherwise the operator IS the MDB.
        operator = body['user']['id']
        mdb_select = (values.get('mdb_block') or {}).get('mdb_select') or {}
        picked_mdb = mdb_select.get('selected_user') or ''
        # External requester: a name only (the person isn't in Slack).
        # The operator stays the lifecycle owner — they get every DM and
        # every expiry/idle alert; external_requester is recorded purely
        # as the "who asked for this" reference. An internal MDB pick
        # takes precedence if somehow both are filled.
        external_requester = (
            (values.get('external_requester_block') or {})
            .get('external_requester_input', {}).get('value') or ''
        ).strip()
        requester = picked_mdb or operator
        on_behalf = bool(picked_mdb and picked_mdb != operator)

        # 1. Confirm receipt up-front so MDB knows we're working.
        examples_summary = (
            f"• Style examples: `{', '.join(examples)}`\n" if examples else ''
        )
        lander_line = (
            f'• Lander URL: {lander}\n'
            if lander
            else '• Lander URL: _none — setup-only run, no file copy_\n'
        )
        external_line = (
            f'• External requester: *{external_requester}* '
            f'— lifecycle owner will be Utkarsh (external domains are '
            f'always Utkarsh-owned)\n'
            if external_requester else ''
        )
        receipt = (
            ':sparkles: *New-domain request received* :sparkles:\n'
            f'• Requested by: <@{requester}>'
            + (f' (submitted by <@{operator}> on their behalf)' if on_behalf else '')
            + f'\n'
            + external_line
            + f'• Vertical: `{vertical}`\n'
            + f'• AWS account: `{aws_account}`\n'
            + (f"• Audience / angle: _{audience}_\n" if audience else '')
            + examples_summary
            + lander_line
            + f'• Extension: `{extension}`\n'
            ':mag: Generating suggestions and checking Namecheap availability…'
        )
        client.chat_postMessage(channel=requester, text=receipt)
        # When the operator is acting on behalf of the MDB, also DM the
        # operator a short ack so they know the request landed.
        if on_behalf:
            client.chat_postMessage(
                channel=operator,
                text=(f':envelope_with_arrow: Submitted new-domain request on '
                      f'behalf of <@{requester}>. They\'ll see suggestions in '
                      'their DMs shortly.'),
            )

        # 2. Call the suggestion engine. It already filters to available +
        # price-capped (.com <$15, other extensions <=$5 per TL 2026-05-05).
        # Falls back to stubs in domain_assistant/ when API keys aren't set.
        try:
            suggestions = suggest_new_domains(
                vertical=vertical,
                audience=audience,
                extension=extension,
                count=5,
                examples=examples,
            )
        except Exception as e:
            client.chat_postMessage(
                channel=requester,
                text=(':warning: Could not generate suggestions: '
                      f'`{type(e).__name__}: {e}`'),
            )
            return

        # `available` is now the full result — the workflow already filtered
        # out taken / premium-priced domains. There's no `unavailable` bucket
        # to render any more.
        available = suggestions
        unavailable = []

        # 3. Build the shortlist message as Block Kit blocks with a "Pick this"
        # button next to each available domain. Per TL, a click on Pick This
        # routes a manual purchase request to Utkarsh in Slack — no Namecheap
        # purchase API call.
        if not available:
            client.chat_postMessage(
                channel=requester,
                text=(':no_entry: *No available domains found.* '
                      'Try different example domains or a different extension.'),
            )
            return

        blocks = _build_new_domain_shortlist_blocks(
            suggestions=available,
            vertical=vertical,
            audience=audience,
            extension=extension,
            lander=lander,
            requester=requester,
            examples=examples,
            aws_account=aws_account,
            external_requester=external_requester,
        )
        client.chat_postMessage(
            channel=requester,
            blocks=blocks,
            text=f'{len(available)} available domains for {vertical}',
        )

    def _send_purchase_request_to_utkarsh(client, *, domain, vertical, lander,
                                          extension, requester,
                                          aws_account='', price=None,
                                          external_requester=''):
        """DM Utkarsh (with dev-reroute applied) the purchase request card.

        Extracted from handle_pick_domain so TL approval can call it after
        the TL clicks Approve. Returns the resolved purchaser id (after
        DEV_REROUTE_DMS_TO override) so callers can name them in UI text.

        Note: `requester` here is already the EFFECTIVE MDB (could be the
        original /new-domain submitter, or the user picked in the MDB
        users_select on the modal). Caller doesn't need to know about
        operator-vs-MDB — that distinction is preserved upstream and
        already shown in the TL approval card.

        `aws_account` is threaded into the Mark Purchased button payload
        so when Utkarsh clicks it the inventory row is created with the
        explicit account choice from the modal (no silent default).

        `lander` may be empty — when so, the Mark Purchased card explains
        this is a "setup-only" run and the deploy worker will skip the
        file-copy step.
        """
        real_purchaser = Config.UTKARSH_SLACK_USER_ID or requester
        purchaser = Config.route_recipient(real_purchaser)

        lander_line = (
            f'• Lander to deploy: {lander}\n' if lander
            else '• Lander to deploy: _none — setup-only (no file copy)_\n'
        )
        aws_line = f'• AWS account: `{aws_account}`\n' if aws_account else ''
        # Utkarsh sees the expected price so he can sanity-check
        # against what Namecheap actually charges at the checkout step
        # — discrepancies (premium markup, currency, promo expired)
        # surface here before the purchase, not after (audit 2026-05-12).
        if isinstance(price, (int, float)) and price > 0:
            price_line = f'• Expected price: *${price:.2f}/yr*\n'
        else:
            price_line = '• Expected price: _unknown — check Namecheap_\n'

        external_line = (
            f'• External requester: *{external_requester}* '
            f'— external domain, it will be assigned to you (Utkarsh) '
            f'as the lifecycle owner\n'
            if external_requester else ''
        )
        utkarsh_text = (
            ':moneybag: *Domain purchase request* :moneybag:\n'
            f'• Requester: <@{requester}>\n'
            + external_line
            + f'• Domain to buy: `{domain}`\n'
            f'• Vertical: `{vertical}`\n'
            + aws_line
            + f'• Extension: `{extension}`\n'
            + price_line
            + lander_line
            + '\n:point_right: Please buy this on Namecheap, then click '
              '*Mark Purchased* below.'
        )
        client.chat_postMessage(
            channel=purchaser,
            text=f'Domain purchase request for {domain}',
            blocks=[
                {'type': 'section',
                 'text': {'type': 'mrkdwn', 'text': utkarsh_text}},
                {'type': 'actions', 'elements': [{
                    'type': 'button',
                    'action_id': 'confirm_purchased',
                    'text': {'type': 'plain_text', 'text': ':white_check_mark: Mark Purchased'},
                    'style': 'primary',
                    'value': sign_payload({
                        'domain': domain,
                        'vertical': vertical,
                        'lander': lander,
                        'requester': requester,
                        'aws_account': aws_account,
                        'external_requester': external_requester,
                        # Price audit-trail: end-of-chain so
                        # confirm_purchased can stamp it on the
                        # domain_events row for /domain-history.
                        'price': price,
                    }),
                }]},
            ],
        )
        return purchaser

    @_bolt_app.action('refresh_domain_suggestions')
    def handle_refresh_suggestions(ack, body, client):
        """User clicked "Show 5 more" on a /new-domain shortlist.

        Regenerates 5 fresh domains with the same vertical / audience /
        extension. The original suggestion message is muted (Pick this
        + Show 5 more buttons removed) so the user isn't tempted to
        click on stale options. A new placeholder message is posted
        immediately ("generating…"); a daemon thread does the slow LLM
        + Namecheap work and edits the placeholder into the new
        shortlist when ready.
        """
        ack()
        data = _verify_button_click(body)
        if data is None:
            return

        vertical = data.get('vertical', '')
        audience = data.get('audience', '')
        extension = data.get('extension', 'any')
        lander = data.get('lander', '')
        requester = data['requester']
        examples = data.get('examples') or []
        aws_account = data.get('aws_account', '')
        external_requester = data.get('external_requester', '')
        channel = body['channel']['id']
        old_ts = body['message']['ts']

        # Mute the old message so the user can't accidentally pick from it
        client.chat_update(
            channel=channel,
            ts=old_ts,
            text='Older suggestions',
            blocks=[
                {'type': 'context', 'elements': [{
                    'type': 'mrkdwn',
                    'text': (':information_source: _Older suggestions — see '
                             'newer ones below._'),
                }]},
            ],
        )

        # Post a placeholder we'll edit in-place when the new list is ready
        placeholder = client.chat_postMessage(
            channel=channel,
            text=':mag: *Generating 5 more suggestions…*\n_15-30 sec; '
                 'checking Namecheap availability + pricing._',
        )

        threading.Thread(
            target=_phase8_refresh_suggestions,
            args=(client, channel, placeholder['ts']),
            kwargs={
                'vertical': vertical, 'audience': audience,
                'extension': extension, 'lander': lander,
                'requester': requester, 'examples': examples,
                'aws_account': aws_account,
                'external_requester': external_requester,
            },
            daemon=True,
            name=f'phase8-refresh-{vertical}',
        ).start()

    @_bolt_app.action('pick_domain')
    def handle_pick_domain(ack, body, client):
        """User clicked "Pick this" on a suggested domain.

        Phase 7.5 routing:
          • If Config.APPROVER_SLACK_USER_IDS is non-empty → send an
            Approve/Reject card to each TL. Utkarsh is only DM'd after a
            TL clicks Approve (handle_confirm_approved).
          • If APPROVER_SLACK_USER_IDS is empty → fall through to the
            Phase 5 behavior (DM Utkarsh directly). Useful for early
            pilot / solo-dev testing.

        Per TL spec: NEVER auto-purchase via Namecheap API. Always route
        through humans (TL approval, then Utkarsh manual buy).
        """
        ack()
        data = _verify_button_click(body)
        if data is None:
            return

        domain = data['domain']
        vertical = data['vertical']
        lander = data['lander']
        extension = data['extension']
        requester = data['requester']
        aws_account = data.get('aws_account', '')
        external_requester = data.get('external_requester', '')
        # Namecheap price flowed in via the Pick / Confirm payload —
        # show it on the TL approval + Utkarsh purchase cards so both
        # know the actual annual cost before approving / buying
        # (audit 2026-05-12).
        price = data.get('price')

        approver_ids = Config.APPROVER_SLACK_USER_IDS
        button_payload = sign_payload({
            'domain': domain,
            'vertical': vertical,
            'lander': lander,
            'extension': extension,
            'requester': requester,
            'aws_account': aws_account,
            'external_requester': external_requester,
            'price': price,
        })

        lander_line = (
            f'• Lander to deploy: {lander}\n' if lander
            else '• Lander to deploy: _none — setup-only (no file copy)_\n'
        )
        aws_line = f'• AWS account: `{aws_account}`\n' if aws_account else ''
        # Display price with cap context when extension is known so TL
        # can see at a glance whether this is within policy.
        if isinstance(price, (int, float)) and price > 0:
            try:
                cap = Config.price_cap_for(extension or '.com')
                over_cap = price > cap
                price_line = (
                    f'• Price: *${price:.2f}/yr* '
                    f'(cap for `{extension}` is ${cap:.2f}'
                    + ('  ⚠ over cap' if over_cap else '')
                    + ')\n'
                )
            except Exception:
                price_line = f'• Price: *${price:.2f}/yr*\n'
        else:
            price_line = '• Price: _unknown_\n'

        if approver_ids:
            # Phase 7.5: send TL approval card to each configured approver
            # (with dev reroute applied).
            approval_text = (
                ':bell: *New-domain approval requested* :bell:\n'
                f'• Requester: <@{requester}>\n'
                f'• Domain: `{domain}`\n'
                f'• Vertical: `{vertical}`\n'
                + aws_line
                + f'• Extension: `{extension}`\n'
                + price_line
                + lander_line
                + '\n:point_right: Approve to forward this to Utkarsh for '
                  'purchase, or Reject to cancel.'
            )
            approval_blocks = [
                {'type': 'section',
                 'text': {'type': 'mrkdwn', 'text': approval_text}},
                {'type': 'actions', 'elements': [
                    {
                        'type': 'button',
                        'action_id': 'confirm_approved',
                        'text': {'type': 'plain_text', 'text': ':white_check_mark: Approve'},
                        'style': 'primary',
                        'value': button_payload,
                    },
                    {
                        'type': 'button',
                        'action_id': 'confirm_rejected',
                        'text': {'type': 'plain_text', 'text': ':x: Reject'},
                        'style': 'danger',
                        'value': button_payload,
                    },
                ]},
            ]
            for approver in approver_ids:
                routed = Config.route_recipient(approver)
                client.chat_postMessage(
                    channel=routed,
                    text=f'Approval requested: {domain}',
                    blocks=approval_blocks,
                )

            # Update the original suggestion message so the buttons go away.
            client.chat_update(
                channel=body['channel']['id'],
                ts=body['message']['ts'],
                text=f'Selected: {domain}',
                blocks=[
                    {'type': 'header', 'text': {
                        'type': 'plain_text',
                        'text': f':white_check_mark: Selected: {domain}',
                    }},
                    {'type': 'context', 'elements': [{
                        'type': 'mrkdwn',
                        'text': (f'Sent to <@{approver_ids[0]}>'
                                 + (f' and {len(approver_ids) - 1} other approver(s)'
                                    if len(approver_ids) > 1 else '')
                                 + ' for approval.'),
                    }]},
                ],
            )

            # Tell the requester their pick is awaiting approval.
            client.chat_postMessage(
                channel=requester,
                text=(f':hourglass_flowing_sand: `{domain}` is awaiting TL '
                      'approval. You\'ll get a DM here when it\'s decided.'),
            )
            return

        # No approvers configured — Phase 5 behavior. DM Utkarsh directly.
        purchaser = _send_purchase_request_to_utkarsh(
            client,
            domain=domain, vertical=vertical, lander=lander,
            extension=extension, requester=requester,
            aws_account=aws_account,
            external_requester=external_requester,
            price=price,
        )
        purchaser_is_requester = (purchaser == requester)

        # Update the original suggestion message.
        client.chat_update(
            channel=body['channel']['id'],
            ts=body['message']['ts'],
            text=f'Selected: {domain}',
            blocks=[
                {'type': 'header', 'text': {
                    'type': 'plain_text',
                    'text': f':white_check_mark: Selected: {domain}',
                }},
                {'type': 'context', 'elements': [{
                    'type': 'mrkdwn',
                    'text': (
                        'Purchase request sent to '
                        f'<@{purchaser}>'
                        + (' (you, since UTKARSH_SLACK_USER_ID isn\'t set)'
                           if purchaser_is_requester else '')
                        + '.'
                    ),
                }]},
            ],
        )

        if not purchaser_is_requester:
            client.chat_postMessage(
                channel=requester,
                text=(f':envelope: Sent purchase request for `{domain}` to '
                      f'<@{purchaser}>. He\'ll confirm here once it\'s bought.'),
            )

    @_bolt_app.action('confirm_approved')
    def handle_confirm_approved(ack, body, client):
        """TL clicked Approve on a Path B approval card.

        Forwards the now-approved domain to Utkarsh (manual purchase) and
        notifies the requester. Replaces the approval card with an
        "Approved by @TL" view so the same TL can't approve twice.
        """
        ack()
        data = _verify_button_click(body)
        if data is None:
            return

        domain = data['domain']
        vertical = data['vertical']
        lander = data['lander']
        extension = data.get('extension') or '.com'
        requester = data['requester']
        aws_account = data.get('aws_account', '')
        external_requester = data.get('external_requester', '')
        price = data.get('price')
        approver = body['user']['id']

        # Forward to Utkarsh
        purchaser = _send_purchase_request_to_utkarsh(
            client,
            domain=domain, vertical=vertical, lander=lander,
            extension=extension, requester=requester,
            aws_account=aws_account,
            external_requester=external_requester,
            price=price,
        )

        # Replace the approval card so it can't be re-approved
        client.chat_update(
            channel=body['channel']['id'],
            ts=body['message']['ts'],
            text=f'Approved: {domain}',
            blocks=[
                {'type': 'header', 'text': {
                    'type': 'plain_text',
                    'text': f':white_check_mark: Approved: {domain}',
                }},
                {'type': 'context', 'elements': [{
                    'type': 'mrkdwn',
                    'text': (f'Approved by <@{approver}>. Forwarded to '
                             f'<@{purchaser}> for purchase.'),
                }]},
            ],
        )

        # Notify the requester
        client.chat_postMessage(
            channel=requester,
            text=(f':white_check_mark: `{domain}` was *approved* by '
                  f'<@{approver}>. Sent to <@{purchaser}> to buy on Namecheap.'),
        )

    @_bolt_app.action('confirm_rejected')
    def handle_confirm_rejected(ack, body, client):
        """TL clicked Reject on a Path B approval card. Stop the flow,
        tell the requester. No domain enters inventory."""
        ack()
        data = _verify_button_click(body)
        if data is None:
            return

        domain = data['domain']
        requester = data['requester']
        rejecter = body['user']['id']

        # Replace the approval card
        client.chat_update(
            channel=body['channel']['id'],
            ts=body['message']['ts'],
            text=f'Rejected: {domain}',
            blocks=[
                {'type': 'header', 'text': {
                    'type': 'plain_text',
                    'text': f':x: Rejected: {domain}',
                }},
                {'type': 'context', 'elements': [{
                    'type': 'mrkdwn',
                    'text': f'Rejected by <@{rejecter}>. Flow stopped.',
                }]},
            ],
        )

        # Notify the requester
        client.chat_postMessage(
            channel=requester,
            text=(f':x: `{domain}` was *rejected* by <@{rejecter}>. '
                  'Try a different suggestion via `/new-domain`.'),
        )

    # ─── Path A: deploy lander to existing owned domain ──────────────────

    @_bolt_app.action('deploy_lander_existing')
    def handle_deploy_lander_click(ack, body, client):
        """User clicked 'Deploy lander' on a domain in /list-domains.

        Open a confirmation modal that:
          • shows the picked target domain + its current setup status
          • when setup is already complete: warns this is destructive
            (overwrites the live lander)
          • when setup is incomplete (e.g. a Path B Mark-Purchased that
            had ATOM crash mid-flight): the modal flips into "Retry /
            finish setup" mode — the warning is replaced with a
            "resuming infrastructure" message, and lander URL becomes
            optional so the operator can run setup-only retries
            without committing to a specific lander up front
            (audit 2026-05-13).
        """
        ack()
        data = _verify_button_click(body)
        if data is None:
            return

        target_domain = data['domain']
        vertical = data.get('vertical') or '_no vertical_'

        # Look up the row so the modal can adapt its copy + validation
        # based on whether setup has actually completed on this domain.
        try:
            existing = inventory_store.get_domain(target_domain) or {}
        except Exception:
            logger.exception(
                'inventory lookup failed for deploy_lander_click %s — '
                'rendering modal in default "redeploy" mode',
                target_domain,
            )
            existing = {}
        setup_completed = bool(existing.get('setup_at'))

        if setup_completed:
            modal_title = 'Deploy lander'
            warning_text = (
                ':warning: *This will overwrite the existing '
                f'lander on `{target_domain}`.*\n'
                'If a campaign is currently live on this domain, '
                'redeploying may interrupt it for up to 24h while '
                'DNS / CloudFront caches refresh. Make sure this '
                'domain is not running a live campaign.'
            )
            lander_label = 'Lander source URL — https://<bucket>/<folder>/'
            lander_hint = (
                'The bucket name and folder are pulled from this URL. '
                'e.g. https://safetyfirstauto.pro/h-insure-c/ '
                'will copy from bucket safetyfirstauto.pro, folder h-insure-c/.'
            )
            lander_required = True
        else:
            modal_title = 'Retry / finish setup'
            warning_text = (
                ':information_source: *ATOM setup is incomplete for '
                f'`{target_domain}`* '
                '(no `setup_at` recorded yet — usually means a previous '
                'attempt crashed mid-flight).\n'
                'Submitting this form re-runs ATOM\'s 9-step setup. '
                'Cert / R53 zone / S3 bucket that already exist will be '
                'reused; failed steps (typically CloudFront) get '
                'retried.'
            )
            lander_label = 'Lander source URL (optional — leave blank for setup-only retry)'
            lander_hint = (
                'When set, the bot copies the lander\'s S3 contents to '
                f'`{target_domain}` after setup completes. When blank, '
                'only the AWS infrastructure is finished — you can '
                'deploy a lander later via /list-domains.'
            )
            lander_required = False

        lander_block = {
            'type': 'input',
            'block_id': 'lander_block',
            'label': {'type': 'plain_text', 'text': lander_label},
            'hint': {'type': 'plain_text', 'text': lander_hint},
            'element': {
                'type': 'url_text_input',
                'action_id': 'lander_input',
                'placeholder': {
                    'type': 'plain_text',
                    'text': (
                        'https://safetyfirstauto.pro/h-insure-c/'
                        if lander_required
                        else 'https://example.com/lander/ (or leave blank)'
                    ),
                },
            },
        }
        if not lander_required:
            lander_block['optional'] = True

        modal = {
            'type': 'modal',
            'callback_id': 'deploy_lander_modal',
            'title': {'type': 'plain_text', 'text': modal_title},
            'submit': {'type': 'plain_text', 'text': 'Send to Utkarsh'},
            'close': {'type': 'plain_text', 'text': 'Cancel'},
            # Stash the picked target domain + setup status so the
            # submission handler knows which validation mode to apply.
            'private_metadata': json.dumps({
                'target_domain': target_domain,
                'vertical': vertical,
                'setup_completed': setup_completed,
            }),
            'blocks': [
                {
                    'type': 'header',
                    'text': {'type': 'plain_text',
                             'text': f'{modal_title}: {target_domain}'},
                },
                {
                    'type': 'context',
                    'elements': [{'type': 'mrkdwn',
                                  'text': f'Vertical: *{vertical}*'}],
                },
                {
                    'type': 'section',
                    'text': {'type': 'mrkdwn', 'text': warning_text},
                },
                {'type': 'divider'},
                {
                    'type': 'input',
                    'block_id': 'mdb_block',
                    'optional': True,
                    'label': {'type': 'plain_text',
                              'text': 'Requesting MDB (leave blank if this is for you)'},
                    'hint': {
                        'type': 'plain_text',
                        'text': ('Pick the marketer you\'re running this on '
                                 'behalf of. The final "deployed" notification '
                                 'goes to them instead of you when set.'),
                    },
                    'element': {
                        'type': 'users_select',
                        'action_id': 'mdb_select',
                        'placeholder': {'type': 'plain_text', 'text': 'Pick an MDB'},
                    },
                },
                lander_block,
                {
                    'type': 'input',
                    'block_id': 'notes_block',
                    'optional': True,
                    'label': {'type': 'plain_text',
                              'text': 'Notes for Utkarsh (optional)'},
                    'element': {
                        'type': 'plain_text_input',
                        'action_id': 'notes_input',
                        'multiline': True,
                        'placeholder': {'type': 'plain_text',
                                        'text': 'e.g. campaign starts Friday'},
                    },
                },
            ],
        }
        client.views_open(trigger_id=body['trigger_id'], view=modal)

    @_bolt_app.view('deploy_lander_modal')
    def handle_deploy_lander_submission(ack, body, view, client):
        """Modal submitted — DM Utkarsh with the deployment request.

        Per TL/Utkarsh's spec: bot routes the request, Utkarsh executes
        the actual deployment manually. Same pattern as Path B's purchase
        request flow, just for redeployment instead of new purchase.

        The lander URL must be in the form `https://<bucket>/<folder>/`
        because Phase 7 parses it into source bucket + folder. We
        validate this up-front and surface inline modal errors so the
        user fixes it before submitting.
        """
        meta = json.loads(view.get('private_metadata') or '{}')
        target_domain = meta.get('target_domain', '')
        vertical = meta.get('vertical', '')
        setup_completed = bool(meta.get('setup_completed'))

        values = view['state']['values']
        # lander_block.lander_input is None when the user left an
        # optional field blank (retry-setup mode). Defensive `.get`
        # chain so we never KeyError on missing pieces.
        lander = ((values.get('lander_block') or {})
                  .get('lander_input', {}).get('value') or '').strip()
        notes = ((values.get('notes_block') or {})
                 .get('notes_input', {}).get('value') or '').strip()

        # Validate URL shape only when a URL was actually provided.
        # Empty lander is a valid setup-only request (incomplete-setup
        # path); empty lander on the deploy-redeploy path is also
        # accepted now and treated as "you want to re-run setup but
        # not touch the lander right now."
        if lander:
            _, _, url_err = _parse_lander_url(lander)
            if url_err:
                ack(response_action='errors',
                    errors={'lander_block': url_err})
                return

        ack()

        # Resolve the effective MDB. Operator (Utkarsh during the trial-run
        # period) may submit on behalf of someone else via the MDB picker.
        operator = body['user']['id']
        mdb_select = (values.get('mdb_block') or {}).get('mdb_select') or {}
        picked_mdb = mdb_select.get('selected_user') or ''
        requester = picked_mdb or operator
        on_behalf = bool(picked_mdb and picked_mdb != operator)

        real_recipient = Config.UTKARSH_SLACK_USER_ID or requester
        recipient = Config.route_recipient(real_recipient)
        recipient_is_requester = (recipient == requester)

        # 1. Send the deployment request to Utkarsh (or fallback to requester),
        # with a Mark Deployed button so he can close the loop in one click.
        # When lander is blank, this is a setup-retry — Utkarsh doesn't
        # need to deploy anything, just confirm the bot can re-run ATOM
        # setup. Card copy adapts so he doesn't go looking for files
        # to copy that don't exist.
        if lander:
            request_kind_emoji = ':rocket:'
            request_kind_title = 'Lander deployment request'
            lander_line = f'• Lander to deploy: {lander}\n'
            action_instruction = (
                '\n:point_right: Please confirm this domain is safe to '
                'redeploy (no live campaign), deploy the lander files, '
                'then click *Mark Deployed* below.'
            )
        else:
            request_kind_emoji = ':wrench:'
            request_kind_title = (
                'Setup-retry request' if not setup_completed
                else 'Setup-only redeploy request'
            )
            lander_line = (
                '• Lander to deploy: _none — re-running ATOM setup only '
                '(no file copy)_\n'
            )
            action_instruction = (
                '\n:point_right: This re-runs ATOM\'s 9-step setup on '
                'the domain. No file copy. Click *Mark Deployed* when '
                'you\'re ready for the bot to start.'
            )

        utkarsh_text = (
            f'{request_kind_emoji} *{request_kind_title}* {request_kind_emoji}\n'
            f'• Requester: <@{requester}>'
            + (f' (submitted by <@{operator}> on their behalf)' if on_behalf else '')
            + f'\n'
            f'• Target domain: `{target_domain}`\n'
            f'• Vertical: `{vertical}`\n'
            + lander_line
            + (f'• Notes: _{notes}_\n' if notes else '')
            + action_instruction
        )
        client.chat_postMessage(
            channel=recipient,
            text=f'Lander deployment request for {target_domain}',
            blocks=[
                {'type': 'section',
                 'text': {'type': 'mrkdwn', 'text': utkarsh_text}},
                {'type': 'actions', 'elements': [{
                    'type': 'button',
                    'action_id': 'confirm_deployed',
                    'text': {'type': 'plain_text', 'text': ':white_check_mark: Mark Deployed'},
                    'style': 'primary',
                    'value': sign_payload({
                        'target_domain': target_domain,
                        'vertical': vertical,
                        'lander': lander,
                        'requester': requester,
                    }),
                }]},
            ],
        )

        # 2. Confirm to the MDB (the effective requester)
        if recipient_is_requester:
            client.chat_postMessage(
                channel=requester,
                text=(f':envelope: Deploy request for `{target_domain}` was '
                      'sent (to you, since UTKARSH_SLACK_USER_ID isn\'t set). '
                      'In production this would route to Utkarsh.'),
            )
        else:
            client.chat_postMessage(
                channel=requester,
                text=(f':envelope: Deploy request for `{target_domain}` sent '
                      f'to <@{recipient}>. He\'ll confirm here when done.'),
            )
        # 3. When operator is acting on behalf of MDB, also DM the operator
        # so they know the request was submitted.
        if on_behalf:
            client.chat_postMessage(
                channel=operator,
                text=(f':envelope_with_arrow: Submitted deploy request for '
                      f'`{target_domain}` on behalf of <@{requester}>.'),
            )

    # ─── Loop closers: Utkarsh clicks "Mark Done" ────────────────────────

    @_bolt_app.action('retry_atom_setup')
    def handle_retry_atom_setup(ack, body, client):
        """Operator clicked Retry on a Phase 7 failure message.

        Re-enqueues the same Phase 7 task — same target_domain,
        vertical, requester, lander_url. The worker reads the
        inventory row's aws_account so the retry uses the same AWS
        account as the original attempt (no need to thread it through
        the button payload — it's authoritative in the DB).

        ATOM's setup_domain is idempotent for resources that already
        exist, so this effectively resumes from the failed step. We
        replace the failure card with a "retry sent" view so the
        button can't be clicked again — the durability contract is
        the new phase7_tasks row, not the in-flight Slack message.
        """
        ack()
        data = _verify_button_click(body)
        if data is None:
            return

        target_domain = data['target_domain']
        vertical = data.get('vertical') or ''
        requester = data['requester']
        lander_url = data.get('lander_url') or ''
        # Original channel + message_ts let the new run's progress
        # updates land in the same Slack thread as the failure
        # they're retrying — same flow continuity the requester sees
        # on a normal Mark Deployed click.
        original_channel = (
            data.get('original_channel') or body['channel']['id']
        )
        original_message_ts = (
            data.get('original_message_ts') or body['message']['ts']
        )
        retrier = body['user']['id']

        log_event(
            'atom_setup_retry_clicked',
            domain=target_domain, vertical=vertical,
            requester=requester, retrier=retrier,
            lander_url=lander_url or None,
        )
        try:
            inventory_store.record_event(
                target_domain, 'phase7_retry_clicked', actor=retrier,
                metadata={'lander_url': lander_url,
                          'vertical': vertical,
                          'requester': requester},
            )
        except Exception:
            logger.exception(
                'record_event(phase7_retry_clicked) failed for %s',
                target_domain,
            )

        # Replace the failure card so the retry button can't be
        # double-clicked. The "Retry sent" view shows who clicked it
        # so anyone else looking at the thread knows a retry is in
        # flight.
        client.chat_update(
            channel=body['channel']['id'],
            ts=body['message']['ts'],
            text=f'Retry sent: {target_domain}',
            blocks=[
                {'type': 'header', 'text': {
                    'type': 'plain_text',
                    'text': f':arrows_counterclockwise: Retry sent: '
                            f'{target_domain}',
                }},
                {'type': 'context', 'elements': [{
                    'type': 'mrkdwn',
                    'text': (f'Triggered by <@{retrier}>. Progress '
                             'updates will appear in this thread.'),
                }]},
            ],
        )

        if Config.ENABLE_PHASE_7:
            from orchestrator import tasks
            from orchestrator.tasks_runner import enqueue_phase7
            enqueue_phase7(
                # Treat retries as Path A — the row exists in inventory
                # already, so the worker's behaviour is identical to a
                # /list-domains Deploy lander click. The Path B kind
                # would have been semantically wrong post-purchase.
                kind=tasks.TASK_KIND_PATH_A,
                channel=original_channel,
                message_ts=original_message_ts,
                target_domain=target_domain,
                vertical=vertical,
                requester=requester,
                lander_url=lander_url,
            )
        else:
            client.chat_postMessage(
                channel=body['channel']['id'],
                thread_ts=body['message']['ts'],
                text=(':warning: ENABLE_PHASE_7 is off — retry was '
                      'recorded but the worker won\'t run. Flip the '
                      'env var to true and click retry again, or '
                      'tell ops to re-enqueue manually.'),
            )

    @_bolt_app.action('confirm_deployed')
    def handle_confirm_deployed(ack, body, client):
        """Utkarsh clicked Mark Deployed on a Path A deployment request.

        Marks the inventory record as setup-complete, replaces the button
        with a confirmation, and DMs the original requester so they know
        their domain is live.

        Phase 7: when ENABLE_PHASE_7 is set, also spawns a background
        worker that calls ATOM to actually run setup_domain + copy_files.
        Progress is posted as thread replies on the original Mark Done
        message; final status is DM'd to the requester.
        """
        ack()
        data = _verify_button_click(body)
        if data is None:
            return

        target_domain = data['target_domain']
        vertical = data.get('vertical') or ''
        lander_url = data.get('lander') or ''
        requester = data['requester']
        confirmer = body['user']['id']
        channel = body['channel']['id']
        message_ts = body['message']['ts']

        log_event(
            'slack_button_clicked', button='confirm_deployed',
            domain=target_domain, vertical=vertical,
            requester=requester, confirmer=confirmer,
            lander_url=lander_url,
        )

        # Update inventory: stamp setup_at so /list-domains shows ✅, and
        # persist the lander URL the operator submitted (Path A previously
        # left lander_url NULL on the row).
        #
        # mark_setup_complete is an UPDATE — when the domain isn't yet in
        # inventory it's a benign no-op (no rows affected, no error). So
        # we do NOT swallow exceptions here: any exception means the DB
        # itself is unhappy (network down, schema drift, auth) and we
        # MUST surface it. Silently passing would let the bot tell Slack
        # "deployed ✅" while the row stayed unmodified, leaving inventory
        # and AWS state divergent (reported in 2026-05-08 audit).
        try:
            inventory_store.mark_setup_complete(
                target_domain, lander_url=lander_url or None,
            )
        except Exception:
            logger.exception(
                'mark_setup_complete failed for domain=%s requester=%s — '
                'inventory and AWS state may diverge',
                target_domain, requester,
            )
            client.chat_postMessage(
                channel=channel, thread_ts=message_ts,
                text=(':warning: Inventory update failed for '
                      f'`{target_domain}`. The Mark Deployed click was '
                      'recorded in Slack, but the inventory row was NOT '
                      'updated. Check the bot logs and re-run once the '
                      'DB is healthy.'),
            )
            raise

        # Audit event for /domain-history. Best-effort — never fail the
        # whole click handler over an event-write blip.
        try:
            inventory_store.record_event(
                target_domain, 'mark_deployed',
                actor=confirmer,
                metadata={'requester': requester, 'lander_url': lander_url,
                          'vertical': vertical, 'flow': 'path_a'},
            )
        except Exception:
            logger.exception(
                'record_event(mark_deployed) failed for %s', target_domain,
            )

        # Replace the button with a "confirmed" view (audit #13:
        # shared with confirm_purchased via _build_confirmed_card so
        # the two paths can't drift apart).
        client.chat_update(
            channel=channel,
            ts=message_ts,
            text=f'Confirmed deployed: {target_domain}',
            blocks=_build_confirmed_card(
                action_label='Deployed',
                target=target_domain,
                confirmer_id=confirmer,
            ),
        )

        # Notify the requester (skip if requester == confirmer to avoid
        # redundant self-DM during single-user testing)
        if requester != confirmer:
            client.chat_postMessage(
                channel=requester,
                text=(f':tada: `{target_domain}` is deployed! '
                      f'Confirmed by <@{confirmer}>.'),
            )

        # Phase 7: enqueue the workflow on the durable task queue
        # instead of spawning a fire-and-forget daemon thread. The
        # task row survives a process restart; if Render redeploys
        # mid-deploy the boot-time recovery sweeper requeues it
        # automatically (audit #2 fix).
        if Config.ENABLE_PHASE_7:
            from orchestrator import tasks
            from orchestrator.tasks_runner import enqueue_phase7
            enqueue_phase7(
                kind=tasks.TASK_KIND_PATH_A,
                channel=channel,
                message_ts=message_ts,
                target_domain=target_domain,
                vertical=vertical,
                requester=requester,
                lander_url=lander_url,
            )

    @_bolt_app.action('confirm_purchased')
    def handle_confirm_purchased(ack, body, client):
        """Utkarsh clicked Mark Purchased on a Path B purchase request.

        Adds the new domain to inventory, replaces the button with a
        confirmation, and DMs the original requester.

        Phase 7: when ENABLE_PHASE_7 is set, also spawns the same
        background worker as confirm_deployed — so a freshly-purchased
        domain gets full setup_domain + lander copy automatically.
        """
        ack()
        data = _verify_button_click(body)
        if data is None:
            return

        domain = data['domain']
        vertical = data.get('vertical') or ''
        lander = data.get('lander') or ''
        requester = data['requester']
        aws_account = data.get('aws_account', '').strip()
        external_requester = data.get('external_requester', '')
        price = data.get('price')
        # Backwards compat: old in-flight buttons (signed before the modal
        # added the picker) carry no aws_account key. Fall back to the
        # first configured option, which preserves the previous implicit
        # default (auto-insurance via init_db backfill). Logged as an info
        # so operators can see when legacy buttons are being consumed.
        if not aws_account:
            aws_account = (
                Config.AWS_ACCOUNT_OPTIONS[0]
                if Config.AWS_ACCOUNT_OPTIONS else ''
            )
            logger.info(
                'confirm_purchased: legacy button without aws_account — '
                'defaulting to %r for domain=%s', aws_account, domain,
            )
        confirmer = body['user']['id']
        channel = body['channel']['id']
        message_ts = body['message']['ts']

        log_event(
            'slack_button_clicked', button='confirm_purchased',
            domain=domain, vertical=vertical, aws_account=aws_account,
            requester=requester, confirmer=confirmer,
            lander_url=lander,
            price=price,
        )

        # Add to inventory so /list-domains starts showing it (and so the
        # Phase 7 workflow can find it — run_existing_domain_workflow
        # rejects domains that aren't in the store).
        #
        # The ONLY benign DB failure here is "domain already exists"
        # (DuplicateDomainError) — that's the user clicking Mark Purchased
        # twice on the same row, idempotent. Every other DB error means
        # the inventory write didn't happen, so we MUST surface it
        # instead of silently telling Slack "purchased ✅" while the
        # backing row was lost (2026-05-08 audit fix).
        # Lifecycle owner. For INTERNAL domains it's the requester (the
        # operator, or the MDB they picked). For EXTERNAL domains it is
        # ALWAYS Utkarsh — he manages every external domain's renewal no
        # matter which operator actually ran /new-domain-external. The
        # operator who ran it stays recorded in requested_by below.
        # Falls back to the requester only if UTKARSH_SLACK_USER_ID is
        # unset (don't leave an external row ownerless).
        if external_requester:
            owner = Config.UTKARSH_SLACK_USER_ID or requester
        else:
            owner = requester

        try:
            inventory_store.add_domain(
                domain=domain,
                vertical=vertical,
                # AWS account explicit choice from the /new-domain modal.
                # Previously this column was NULL on insert and silently
                # backfilled to 'auto-insurance' by init_db — making the
                # provisioning account essentially un-configurable. Now
                # the MDB picks it in the modal and the value flows here.
                aws_account=aws_account or None,
                lander_url=lander or None,
                requested_by=f'Slack:{requester}',
                notes='Purchased via /new-domain bot flow',
                # State machine starts here — Phase 7 worker will move
                # the row to STATUS_DEPLOYING then DEPLOYED|FAILED.
                status=inventory_store.STATUS_PENDING,
                # Lifecycle ownership starts on day one — classifier needs
                # an MDB to DM when this domain expires or goes idle.
                # Internal: the requester. External: always Utkarsh.
                assigned_to=owner,
                # External-requester name lands in its own column (NULL
                # for internal requests) — keeps requested_by clean and
                # makes "how many external domains" a simple WHERE.
                external_requester_name=external_requester or None,
                # Audit trail for /domain-history. Captures who clicked
                # Mark Purchased, plus the lander URL (if any) and the
                # AWS account it'll be deployed to.
                event_source='path_b_mark_purchased',
                event_metadata={'lander_url': lander, 'vertical': vertical,
                                'aws_account': aws_account,
                                'confirmer': confirmer,
                                'price_usd': price,
                                'external_requester': external_requester or None},
            )
        except inventory_store.DuplicateDomainError:
            logger.info(
                'add_domain idempotent skip — domain=%s already in '
                'inventory; treating as benign re-click',
                domain,
            )
        except Exception:
            logger.exception(
                'add_domain failed for domain=%s requester=%s — '
                'inventory write did NOT happen', domain, requester,
            )
            client.chat_postMessage(
                channel=channel, thread_ts=message_ts,
                text=(':warning: Inventory write failed for '
                      f'`{domain}`. Mark Purchased was recorded in '
                      'Slack, but the new row was NOT inserted — '
                      '/list-domains will not show this domain until '
                      'the DB is healthy and Mark Purchased is clicked '
                      'again. Check the bot logs.'),
            )
            raise

        # Replace the button with a "purchased" view (audit #13:
        # shared with confirm_deployed via _build_confirmed_card so
        # the two paths can't drift apart).
        phase7_note = (
            'Triggering ATOM setup now — watch this thread for progress.'
            if Config.ENABLE_PHASE_7
            else 'Phase 7 (auto setup_domain) is OFF — set ENABLE_PHASE_7=true to enable.'
        )
        client.chat_update(
            channel=channel,
            ts=message_ts,
            text=f'Confirmed purchased: {domain}',
            blocks=_build_confirmed_card(
                action_label='Purchased',
                target=domain,
                confirmer_id=confirmer,
                extra_context=f'Added to inventory. {phase7_note}',
            ),
        )

        # Notify the requester
        if requester != confirmer:
            client.chat_postMessage(
                channel=requester,
                text=(f':moneybag: `{domain}` has been purchased! '
                      f'Confirmed by <@{confirmer}>. Setup will follow.'),
            )

        # Phase 7: enqueue on the durable task queue (audit #2 fix —
        # see confirm_deployed above for rationale).
        if Config.ENABLE_PHASE_7:
            from orchestrator import tasks
            from orchestrator.tasks_runner import enqueue_phase7
            enqueue_phase7(
                kind=tasks.TASK_KIND_PATH_B,
                channel=channel,
                message_ts=message_ts,
                target_domain=domain,
                vertical=vertical,
                requester=requester,
                lander_url=lander,
            )


# ─── Modal definition (Block Kit) ──────────────────────────────────────────

def _new_domain_common_blocks() -> list:
    """The modal blocks shared by BOTH /new-domain and
    /new-domain-external — everything except the first "who is this for"
    block. Built fresh each call because the AWS account picker reads
    Config.AWS_ACCOUNT_OPTIONS at modal-open time.

    Design notes:
      • AWS account picker is REQUIRED — the old implicit auto-insurance
        default silently routed every domain into one account.
      • Lander URL is OPTIONAL — blank means provision AWS infra only,
        deploy the lander later.
    """
    account_options = [
        {'text': {'type': 'plain_text', 'text': acct}, 'value': acct}
        for acct in Config.AWS_ACCOUNT_OPTIONS
    ]
    return [
            {
                'type': 'input',
                'block_id': 'vertical_block',
                'label': {'type': 'plain_text', 'text': 'Vertical'},
                'element': {
                    'type': 'plain_text_input',
                    'action_id': 'vertical_input',
                    'placeholder': {'type': 'plain_text',
                                    'text': 'e.g. auto-insurance'},
                },
            },
            {
                'type': 'input',
                'block_id': 'aws_account_block',
                'label': {'type': 'plain_text', 'text': 'AWS account (target)'},
                'hint': {
                    'type': 'plain_text',
                    'text': ('Which AWS account this domain\'s R53 zone, ACM '
                             'cert, S3 bucket, and CloudFront distribution '
                             'should live in. Configure the list of options '
                             'via the AWS_ACCOUNT_OPTIONS env var.'),
                },
                'element': {
                    'type': 'static_select',
                    'action_id': 'aws_account_select',
                    'initial_option': account_options[0],
                    'options': account_options,
                },
            },
            {
                'type': 'input',
                'block_id': 'audience_block',
                'optional': True,
                'label': {'type': 'plain_text',
                          'text': 'Audience or angle (optional)'},
                'hint': {
                    'type': 'plain_text',
                    'text': ('Who are you targeting and how. The bot will use '
                             'this to match name style to the campaign. Leave '
                             'blank if the vertical alone is enough.'),
                },
                'element': {
                    'type': 'plain_text_input',
                    'action_id': 'audience_input',
                    'placeholder': {'type': 'plain_text',
                                    'text': 'e.g. seniors looking for medigap, low-credit drivers, first-time homebuyers'},
                },
            },
            {
                'type': 'input',
                'block_id': 'examples_block',
                'optional': True,
                'label': {'type': 'plain_text',
                          'text': 'Example domain names (optional)'},
                'hint': {
                    'type': 'plain_text',
                    'text': ('One per line. The bot anchors the AI on the STYLE '
                             'of these names (word count, tone, compounding) — '
                             'it will NOT reuse them. Use this when the vertical '
                             'is unusual or the AI\'s defaults don\'t match.'),
                },
                'element': {
                    'type': 'plain_text_input',
                    'action_id': 'examples_input',
                    'multiline': True,
                    'placeholder': {'type': 'plain_text',
                                    'text': 'mymedicareexperts.online\nseniorhealthhub.com\nmedicarequotefinder.pro'},
                },
            },
            {
                'type': 'input',
                'block_id': 'lander_block',
                'optional': True,
                'label': {'type': 'plain_text',
                          'text': 'Lander URL (optional — leave blank for setup-only)'},
                'hint': {
                    'type': 'plain_text',
                    'text': ('When set, the bot copies the lander\'s S3 contents '
                             'to the new domain\'s bucket after ATOM setup. '
                             'When blank, only AWS infrastructure is provisioned '
                             '(R53 zone, ACM cert, S3 bucket, CloudFront) — you '
                             'can deploy a lander later via /list-domains.'),
                },
                'element': {
                    'type': 'url_text_input',
                    'action_id': 'lander_input',
                    'placeholder': {'type': 'plain_text',
                                    'text': 'https://example.com/landing-page (or leave blank)'},
                },
            },
            {
                'type': 'input',
                'block_id': 'extension_block',
                'label': {'type': 'plain_text', 'text': 'Domain extension'},
                'element': {
                    'type': 'static_select',
                    'action_id': 'extension_select',
                    'initial_option': {
                        'text': {'type': 'plain_text', 'text': 'Any (cheapest first)'},
                        'value': 'any',
                    },
                    'options': [
                        {'text': {'type': 'plain_text', 'text': 'Any (cheapest first)'}, 'value': 'any'},
                        {'text': {'type': 'plain_text', 'text': '.com  (under $15)'},    'value': '.com'},
                        {'text': {'type': 'plain_text', 'text': '.pro  (~$4)'},          'value': '.pro'},
                        {'text': {'type': 'plain_text', 'text': '.info (~$4)'},          'value': '.info'},
                        {'text': {'type': 'plain_text', 'text': '.site (~$1)'},          'value': '.site'},
                        {'text': {'type': 'plain_text', 'text': '.live (~$3)'},          'value': '.live'},
                        {'text': {'type': 'plain_text', 'text': '.top  (~$3)'},          'value': '.top'},
                        {'text': {'type': 'plain_text', 'text': '.icu  (~$3)'},          'value': '.icu'},
                    ],
                },
            },
    ]


def _build_new_domain_modal() -> dict:
    """The /new-domain modal — for internal requests. The first block is
    an optional MDB picker (operator-on-behalf-of). Shares everything
    below it with the external modal via _new_domain_common_blocks().
    """
    return {
        'type': 'modal',
        'callback_id': 'new_domain_modal',
        'title': {'type': 'plain_text', 'text': 'Setup New Domain'},
        'submit': {'type': 'plain_text', 'text': 'Continue'},
        'close': {'type': 'plain_text', 'text': 'Cancel'},
        'blocks': [
            {
                'type': 'input',
                'block_id': 'mdb_block',
                'optional': True,
                'label': {'type': 'plain_text',
                          'text': 'Requesting MDB (leave blank if this is for you)'},
                'hint': {
                    'type': 'plain_text',
                    'text': ('Pick the marketer you\'re running this on behalf of. '
                             'When set, all bot DMs (suggestions, approval status, '
                             'final deploy notification) go to them instead of you. '
                             'For someone NOT in our Slack workspace, use '
                             '`/new-domain-external` instead.'),
                },
                'element': {
                    'type': 'users_select',
                    'action_id': 'mdb_select',
                    'placeholder': {'type': 'plain_text',
                                    'text': 'Pick an MDB'},
                },
            },
        ] + _new_domain_common_blocks(),
    }


def _build_new_domain_external_modal() -> dict:
    """The /new-domain-external modal — for requests from people NOT in
    our Slack workspace. The first block is a REQUIRED free-text name
    (the external person has no Slack ID). The internal operator who
    runs this stays the lifecycle owner — all bot DMs + expiry/idle
    alerts go to them; the external name is recorded for reference and
    is queryable via the domains.external_requester_name column.

    Same callback_id as the internal modal — handle_new_domain_submission
    handles both (it reads mdb_block / external_requester_block with
    .get(), so a modal missing one just yields an empty value there).
    """
    return {
        'type': 'modal',
        'callback_id': 'new_domain_modal',
        'title': {'type': 'plain_text', 'text': 'New Domain (External)'},
        'submit': {'type': 'plain_text', 'text': 'Continue'},
        'close': {'type': 'plain_text', 'text': 'Cancel'},
        'blocks': [
            {
                'type': 'input',
                'block_id': 'external_requester_block',
                'optional': False,
                'label': {'type': 'plain_text',
                          'text': 'External requester name'},
                'hint': {
                    'type': 'plain_text',
                    'text': ('The person this domain is for, who is NOT in our '
                             'Slack workspace — name only. You, the operator '
                             'running this, stay the owner: every bot DM and '
                             'expiry/idle alert comes to you. The domain is '
                             'tagged external and recorded under this name.'),
                },
                'element': {
                    'type': 'plain_text_input',
                    'action_id': 'external_requester_input',
                    'placeholder': {'type': 'plain_text',
                                    'text': 'e.g. John from AcmeCorp'},
                },
            },
        ] + _new_domain_common_blocks(),
    }


_DOMAIN_RE = re.compile(
    # apex domain only — at least one label, then a single TLD label
    # 2+ chars. Labels are lowercase alphanumeric with internal hyphens
    # (no leading / trailing hyphen). Rejects subdomains and wildcards.
    r'^(?!-)[a-z0-9-]{1,63}(?<!-)\.[a-z]{2,63}$'
)


_KNOWN_EXTENSIONS = ('.com', '.pro', '.info', '.site', '.live', '.top', '.icu')


def _normalise_domain_input(raw: str) -> str:
    """Strip the noise an MDB might paste — protocol, www, trailing slash,
    whitespace, mixed case — and return a candidate apex domain string.

    Doesn't reject anything; just normalises. The caller validates the
    result against ``_DOMAIN_RE`` separately so we can surface a
    targeted error rather than a generic ValueError.
    """
    s = (raw or '').strip().lower()
    s = re.sub(r'^https?://', '', s)
    s = re.sub(r'^www\.', '', s)
    s = s.rstrip('/')
    # Drop any path that snuck in (e.g. someone pasted a full URL).
    if '/' in s:
        s = s.split('/', 1)[0]
    return s


def _extract_extension(domain: str) -> str:
    """Return the TLD portion including the leading dot, or empty string
    if the domain doesn't parse cleanly. Last-dot-onward — keeps it simple
    for the apex-only domains this flow accepts.
    """
    if '.' not in domain:
        return ''
    return '.' + domain.rsplit('.', 1)[1]


def _build_buy_domain_modal(prefill_domain: str = '') -> dict:
    """Build the /buy-domain modal payload.

    Unlike /new-domain (which generates suggestions), this modal takes a
    domain name the MDB already chose somewhere else — a tool, a vendor,
    their own brainstorming. The bot still runs the same Namecheap
    availability + price check, surfaces the findings, and lets the MDB
    confirm before continuing into the existing Path B chain.

    `prefill_domain` populates the domain input when the slash command
    was invoked with an inline argument (`/buy-domain foo.com`). Empty
    when the slash command was invoked bare.

    Per feedback_bot_never_hard_rejects_user_choice: the modal does NO
    validation itself. All checks happen on submit and are surfaced on
    a confirm/cancel card. The bot reports; the human decides.
    """
    account_options = [
        {'text': {'type': 'plain_text', 'text': acct}, 'value': acct}
        for acct in Config.AWS_ACCOUNT_OPTIONS
    ]
    domain_input = {
        'type': 'plain_text_input',
        'action_id': 'domain_input',
        'placeholder': {'type': 'plain_text',
                        'text': 'e.g. mymedicareexperts.online'},
    }
    if prefill_domain:
        domain_input['initial_value'] = prefill_domain

    return {
        'type': 'modal',
        'callback_id': 'buy_domain_modal',
        'title': {'type': 'plain_text', 'text': 'Buy a domain you picked'},
        'submit': {'type': 'plain_text', 'text': 'Check & Continue'},
        'close': {'type': 'plain_text', 'text': 'Cancel'},
        'blocks': [
            {
                'type': 'context',
                'elements': [{
                    'type': 'mrkdwn',
                    'text': ('_For domains you already have in mind. The '
                             'bot will check Namecheap and show you the '
                             'availability + price before anything else '
                             'happens._'),
                }],
            },
            {
                'type': 'input',
                'block_id': 'mdb_block',
                'optional': True,
                'label': {'type': 'plain_text',
                          'text': 'Requesting MDB (leave blank if this is for you)'},
                'hint': {
                    'type': 'plain_text',
                    'text': ('Pick the marketer you\'re running this on '
                             'behalf of. When set, all bot DMs (approval '
                             'status, deploy notification) go to them '
                             'instead of you.'),
                },
                'element': {
                    'type': 'users_select',
                    'action_id': 'mdb_select',
                    'placeholder': {'type': 'plain_text', 'text': 'Pick an MDB'},
                },
            },
            {
                'type': 'input',
                'block_id': 'domain_block',
                'label': {'type': 'plain_text', 'text': 'Domain you picked'},
                'hint': {
                    'type': 'plain_text',
                    'text': ('Apex domain only. We\'ll strip http:// / www. / '
                             'trailing slashes automatically.'),
                },
                'element': domain_input,
            },
            {
                'type': 'input',
                'block_id': 'vertical_block',
                'label': {'type': 'plain_text', 'text': 'Vertical'},
                'element': {
                    'type': 'plain_text_input',
                    'action_id': 'vertical_input',
                    'placeholder': {'type': 'plain_text',
                                    'text': 'e.g. medicare'},
                },
            },
            {
                'type': 'input',
                'block_id': 'aws_account_block',
                'label': {'type': 'plain_text', 'text': 'AWS account (target)'},
                'hint': {
                    'type': 'plain_text',
                    'text': ('Where this domain\'s R53 zone, ACM cert, S3 '
                             'bucket, and CloudFront distribution will be '
                             'provisioned.'),
                },
                'element': {
                    'type': 'static_select',
                    'action_id': 'aws_account_select',
                    'initial_option': account_options[0],
                    'options': account_options,
                },
            },
            {
                'type': 'input',
                'block_id': 'lander_block',
                'optional': True,
                'label': {'type': 'plain_text',
                          'text': 'Lander URL (optional — leave blank for setup-only)'},
                'hint': {
                    'type': 'plain_text',
                    'text': ('When set, the bot copies the lander\'s S3 '
                             'contents to the new domain\'s bucket after '
                             'purchase. When blank, only AWS infrastructure '
                             'is provisioned.'),
                },
                'element': {
                    'type': 'url_text_input',
                    'action_id': 'lander_input',
                    'placeholder': {'type': 'plain_text',
                                    'text': 'https://example.com/landing-page (or leave blank)'},
                },
            },
        ],
    }


def _build_buy_domain_confirm_blocks(*, domain, vertical, aws_account,
                                     lander, requester, availability_finding,
                                     inventory_finding, price_finding,
                                     price=None):
    """Render the confirm-or-cancel card the MDB sees after /buy-domain
    validation runs. Surfaces every finding factually; never auto-rejects.

    The Confirm button reuses ``action_id='pick_domain'`` so it routes
    into the same downstream chain an AI-shortlist Pick this would —
    TL approval (if configured) -> Mark Purchased -> deploy. Zero
    duplicated handler code, single test surface for "post-pick" logic.
    """
    extension = _extract_extension(domain)

    # Build a compact findings block — emoji on the left so the MDB can
    # scan vertically and see which items need their attention.
    finding_lines = [
        availability_finding,
        price_finding,
        inventory_finding,
    ]
    findings_text = '\n'.join(line for line in finding_lines if line)

    lander_line = (
        f'• Lander: {lander}' if lander
        else '• Lander: _none — setup-only (no file copy)_'
    )

    header_text = (
        f':receipt: *Pre-purchase check for* `{domain}`\n'
        f'• Vertical: `{vertical}`\n'
        f'• AWS account: `{aws_account}`\n'
        f'{lander_line}\n'
        f'• Requester: <@{requester}>\n\n'
        f'{findings_text}\n\n'
        f':point_right: Review the findings above and confirm to '
        f'continue, or cancel.'
    )

    # Reuse pick_domain's payload contract so the existing handler picks
    # up the click identically to an AI-shortlist selection. Price is
    # threaded through so TL approval + Utkarsh purchase cards display
    # the actual annual cost (audit 2026-05-12).
    confirm_payload = sign_payload({
        'domain': domain,
        'vertical': vertical,
        'lander': lander,
        'extension': extension,
        'requester': requester,
        'aws_account': aws_account,
        'price': price,
    })
    cancel_payload = sign_payload({'domain': domain, 'requester': requester})

    return [
        {'type': 'section', 'text': {'type': 'mrkdwn', 'text': header_text}},
        {'type': 'actions', 'elements': [
            {
                'type': 'button',
                'action_id': 'pick_domain',
                'text': {'type': 'plain_text',
                         'text': ':white_check_mark: Confirm — continue'},
                'style': 'primary',
                'value': confirm_payload,
            },
            {
                'type': 'button',
                'action_id': 'cancel_buy_domain',
                'text': {'type': 'plain_text', 'text': ':x: Cancel'},
                'style': 'danger',
                'value': cancel_payload,
            },
        ]},
    ]


# ─── Flask routes (forward everything to bolt) ─────────────────────────────

@slack_bp.route('/health', methods=['GET'])
def slack_health():
    """Slack-blueprint-scoped health probe.

    Reports both the Slack bolt-app status AND the inventory DB. The
    Slack interaction handlers all touch the inventory (mark deployed,
    add purchased, list domains), so a healthy slack blueprint with a
    dead DB would still 500 on every interaction. Returning 503 here
    keeps the signals consistent with `/health` (2026-05-08 audit fix).
    """
    try:
        inventory_store.health_check()
        db_status = 'reachable'
    except inventory_store.StoreUnavailable as e:
        return jsonify({
            'status': 'unhealthy',
            'reason': 'db_unavailable',
            'error': str(e),
            'bolt_active': _bolt_app is not None,
        }), 503
    return jsonify({
        'status': 'slack blueprint mounted',
        'phase': 2,
        'bolt_active': _bolt_app is not None,
        'db': db_status,
    })


def _bolt_or_stub(stub_text: str):
    """Return a real bolt response if configured, else a phase-1 stub.

    Phase-1 stub keeps the old behaviour usable so the app still boots without
    Slack credentials in .env (e.g. on a fresh checkout).
    """
    if _handler is None:
        return jsonify({
            'response_type': 'ephemeral',
            'text': stub_text,
        })
    return _handler.handle(request)


@slack_bp.route('/slash/new-domain', methods=['POST'])
def slash_new_domain():
    return _bolt_or_stub('Coming soon: /new-domain (Phase 2). '
                         'Set SLACK_BOT_TOKEN + SLACK_SIGNING_SECRET to enable.')


@slack_bp.route('/slash/new-domain-external', methods=['POST'])
def slash_new_domain_external():
    return _bolt_or_stub('Coming soon: /new-domain-external. '
                         'Set SLACK_BOT_TOKEN + SLACK_SIGNING_SECRET to enable.')


@slack_bp.route('/slash/pixel-fire', methods=['POST'])
def slash_pixel_fire():
    return _bolt_or_stub('Coming soon: /pixel-fire. '
                         'Set SLACK_BOT_TOKEN + SLACK_SIGNING_SECRET to enable.')


@slack_bp.route('/slash/new-tracker', methods=['POST'])
def slash_new_tracker():
    return _bolt_or_stub('Coming soon: /new-tracker. '
                         'Set SLACK_BOT_TOKEN + SLACK_SIGNING_SECRET to enable.')


@slack_bp.route('/slash/buy-domain', methods=['POST'])
def slash_buy_domain():
    return _bolt_or_stub('Coming soon: /buy-domain. '
                         'Set SLACK_BOT_TOKEN + SLACK_SIGNING_SECRET to enable.')


@slack_bp.route('/slash/list-domains', methods=['POST'])
def slash_list_domains():
    return _bolt_or_stub('Coming soon: /list-domains (Phase 3). '
                         'Set SLACK_BOT_TOKEN + SLACK_SIGNING_SECRET to enable.')


@slack_bp.route('/slash/domain-history', methods=['POST'])
def slash_domain_history():
    return _bolt_or_stub('Coming soon: /domain-history (Phase D). '
                         'Set SLACK_BOT_TOKEN + SLACK_SIGNING_SECRET to enable.')


@slack_bp.route('/slash/reassign-domain', methods=['POST'])
def slash_reassign_domain():
    return _bolt_or_stub('Coming soon: /reassign-domain (Phase E). '
                         'Set SLACK_BOT_TOKEN + SLACK_SIGNING_SECRET to enable.')


@slack_bp.route('/interactions', methods=['POST'])
def interactions():
    return _bolt_or_stub('Coming soon: interactive callbacks (Phase 5).')
