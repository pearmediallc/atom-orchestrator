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
import logging
import threading
from urllib.parse import urlparse
from flask import Blueprint, jsonify, request

from config import Config
from inventory import store as inventory_store
from orchestrator.log_setup import log_event
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
                                       examples=None):
    """Render the /new-domain shortlist as Slack Block Kit blocks.

    Used by both the initial modal submission and the "Show 5 more"
    refresh handler. Each domain row gets a Pick this button; the
    bottom of the message gets a Show-5-more button that re-runs
    the same query with fresh LLM output.

    `examples` is the user-supplied list of stylistic anchor domains
    from the modal. Carried through the Show-5-more button so the
    refresh worker can produce names in the same family.
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
                         f'{audience_line}  ·  Lander: {lander}\n_{cap_label}_'),
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
            }),
        }],
    })
    return blocks


def _phase8_refresh_suggestions(client, channel, placeholder_ts, *,
                                vertical, audience, extension,
                                lander, requester, examples=None):
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
    )
    client.chat_update(
        channel=channel,
        ts=placeholder_ts,
        blocks=new_blocks,
        text=f'{len(suggestions)} fresh domains for {vertical}',
    )


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
            'config defaults '
            f'(URL parse failed: {url_err})' if lander_url
            else 'config defaults (no lander URL provided)'
        )

    if not source_bucket:
        client.chat_postMessage(
            channel=channel, thread_ts=message_ts,
            text=(f':warning: *Phase 7 cannot deploy* — '
                  f'{url_err or "no source bucket configured"}.\n'
                  'Inventory was updated but the lander was NOT actually deployed.\n'
                  '_Tip: use the form `https://<bucket>/<folder>/` in the lander '
                  'URL field next time and the bot will figure out the rest._'),
        )
        try:
            inventory_store.record_event(
                target_domain, 'phase7_skipped', actor='cron',
                metadata={'reason': url_err or 'no_source_bucket',
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

    client.chat_postMessage(
        channel=channel, thread_ts=message_ts,
        text=(f':rocket: *Triggering ATOM setup* for `{target_domain}`\n'
              f'• source bucket: `{source_bucket}`\n'
              f'• source folders: `{source_folders or "—"}`\n'
              f'• source resolved from: _{source_origin}_\n'
              '_This usually takes 5–20 minutes (cert validation + '
              'CloudFront)._'),
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
        result = run_existing_domain_workflow(req)
    except Exception as e:
        logger.exception('Phase 7 worker crashed for %s', target_domain)
        try:
            inventory_store.record_event(
                target_domain, 'phase7_crashed', actor='cron',
                metadata={'exception': type(e).__name__, 'message': str(e)[:500]},
            )
        except Exception:
            pass
        client.chat_postMessage(
            channel=channel, thread_ts=message_ts,
            text=f':x: *ATOM workflow crashed:* `{type(e).__name__}: {e}`',
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
        failed_step = (result.details.get('setup_result') or {}).get(
            'failed_at_step', '?')
        try:
            inventory_store.record_event(
                target_domain, 'phase7_failed', actor='cron',
                metadata={
                    'failed_at_step': failed_step,
                    'reason': (result.details or {}).get('reason'),
                    'message': result.message[:500],
                },
            )
        except Exception:
            logger.exception('record_event(phase7_failed) failed')
        client.chat_postMessage(
            channel=channel, thread_ts=message_ts,
            text=(f':x: *ATOM workflow failed* at step `{failed_step}`.\n'
                  f'Reason: {result.message}'),
        )
        client.chat_postMessage(
            channel=requester,
            text=(f':x: `{target_domain}` deploy did not complete. '
                  f'Step `{failed_step}` failed: {result.message}'),
        )


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

    @_bolt_app.command('/new-domain')
    def handle_new_domain_command(ack, body, client):
        """Open the new-domain modal."""
        ack()
        client.views_open(
            trigger_id=body['trigger_id'],
            view=_NEW_DOMAIN_MODAL,
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
        lander = (values['lander_block']['lander_input']['value'] or '').strip()
        extension = values['extension_block']['extension_select']['selected_option']['value']

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
        requester = picked_mdb or operator
        on_behalf = bool(picked_mdb and picked_mdb != operator)

        # 1. Confirm receipt up-front so MDB knows we're working.
        examples_summary = (
            f"• Style examples: `{', '.join(examples)}`\n" if examples else ''
        )
        receipt = (
            ':sparkles: *New-domain request received* :sparkles:\n'
            f'• Requested by: <@{requester}>'
            + (f' (submitted by <@{operator}> on their behalf)' if on_behalf else '')
            + f'\n'
            f'• Vertical: `{vertical}`\n'
            + (f"• Audience / angle: _{audience}_\n" if audience else '')
            + examples_summary
            + f'• Lander URL: {lander}\n'
            f'• Extension: `{extension}`\n'
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
        )
        client.chat_postMessage(
            channel=requester,
            blocks=blocks,
            text=f'{len(available)} available domains for {vertical}',
        )

    def _send_purchase_request_to_utkarsh(client, *, domain, vertical, lander,
                                          extension, requester):
        """DM Utkarsh (with dev-reroute applied) the purchase request card.

        Extracted from handle_pick_domain so TL approval can call it after
        the TL clicks Approve. Returns the resolved purchaser id (after
        DEV_REROUTE_DMS_TO override) so callers can name them in UI text.

        Note: `requester` here is already the EFFECTIVE MDB (could be the
        original /new-domain submitter, or the user picked in the MDB
        users_select on the modal). Caller doesn't need to know about
        operator-vs-MDB — that distinction is preserved upstream and
        already shown in the TL approval card.
        """
        real_purchaser = Config.UTKARSH_SLACK_USER_ID or requester
        purchaser = Config.route_recipient(real_purchaser)

        utkarsh_text = (
            ':moneybag: *Domain purchase request* :moneybag:\n'
            f'• Requester: <@{requester}>\n'
            f'• Domain to buy: `{domain}`\n'
            f'• Vertical: `{vertical}`\n'
            f'• Extension: `{extension}`\n'
            f'• Lander to deploy: {lander}\n\n'
            ':point_right: Please buy this on Namecheap, then click '
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

        approver_ids = Config.APPROVER_SLACK_USER_IDS
        button_payload = sign_payload({
            'domain': domain,
            'vertical': vertical,
            'lander': lander,
            'extension': extension,
            'requester': requester,
        })

        if approver_ids:
            # Phase 7.5: send TL approval card to each configured approver
            # (with dev reroute applied).
            approval_text = (
                ':bell: *New-domain approval requested* :bell:\n'
                f'• Requester: <@{requester}>\n'
                f'• Domain: `{domain}`\n'
                f'• Vertical: `{vertical}`\n'
                f'• Extension: `{extension}`\n'
                f'• Lander to deploy: {lander}\n\n'
                ':point_right: Approve to forward this to Utkarsh for '
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
        approver = body['user']['id']

        # Forward to Utkarsh
        purchaser = _send_purchase_request_to_utkarsh(
            client,
            domain=domain, vertical=vertical, lander=lander,
            extension=extension, requester=requester,
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
          • shows the picked target domain
          • warns about the destructive nature (overwrites existing lander)
          • asks for the source lander URL to deploy
        """
        ack()
        data = _verify_button_click(body)
        if data is None:
            return

        target_domain = data['domain']
        vertical = data.get('vertical') or '_no vertical_'

        modal = {
            'type': 'modal',
            'callback_id': 'deploy_lander_modal',
            'title': {'type': 'plain_text', 'text': 'Deploy lander'},
            'submit': {'type': 'plain_text', 'text': 'Send to Utkarsh'},
            'close': {'type': 'plain_text', 'text': 'Cancel'},
            # Stash the picked target domain so the submission handler
            # knows what was clicked. private_metadata is the standard
            # bolt mechanism for this.
            'private_metadata': json.dumps({
                'target_domain': target_domain,
                'vertical': vertical,
            }),
            'blocks': [
                {
                    'type': 'header',
                    'text': {'type': 'plain_text',
                             'text': f'Deploy to: {target_domain}'},
                },
                {
                    'type': 'context',
                    'elements': [{'type': 'mrkdwn',
                                  'text': f'Vertical: *{vertical}*'}],
                },
                {
                    'type': 'section',
                    'text': {
                        'type': 'mrkdwn',
                        'text': (
                            ':warning: *This will overwrite the existing '
                            f'lander on `{target_domain}`.*\n'
                            'If a campaign is currently live on this domain, '
                            'redeploying may interrupt it for up to 24h while '
                            'DNS / CloudFront caches refresh. Make sure this '
                            'domain is not running a live campaign.'
                        ),
                    },
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
                {
                    'type': 'input',
                    'block_id': 'lander_block',
                    'label': {'type': 'plain_text',
                              'text': 'Lander source URL — https://<bucket>/<folder>/'},
                    'hint': {
                        'type': 'plain_text',
                        'text': ('The bucket name and folder are pulled from this URL. '
                                 'e.g. https://safetyfirstauto.pro/h-insure-c/ '
                                 'will copy from bucket safetyfirstauto.pro, folder h-insure-c/.'),
                    },
                    'element': {
                        'type': 'url_text_input',
                        'action_id': 'lander_input',
                        'placeholder': {'type': 'plain_text',
                                        'text': 'https://safetyfirstauto.pro/h-insure-c/'},
                    },
                },
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

        values = view['state']['values']
        lander = (values['lander_block']['lander_input']['value'] or '').strip()
        notes = (values['notes_block']['notes_input']['value'] or '').strip()

        # Validate URL shape — must be parseable into bucket + folder so
        # Phase 7 can use it as the source. Failing here keeps the modal
        # open with the field highlighted in red.
        _, _, url_err = _parse_lander_url(lander)
        if url_err:
            ack(response_action='errors', errors={'lander_block': url_err})
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
        utkarsh_text = (
            ':rocket: *Lander deployment request* :rocket:\n'
            f'• Requester: <@{requester}>'
            + (f' (submitted by <@{operator}> on their behalf)' if on_behalf else '')
            + f'\n'
            f'• Target domain: `{target_domain}`\n'
            f'• Vertical: `{vertical}`\n'
            f'• Lander to deploy: {lander}\n'
            + (f'• Notes: _{notes}_\n' if notes else '')
            + '\n:point_right: Please confirm this domain is safe to redeploy '
            '(no live campaign), deploy the lander files, then click '
            '*Mark Deployed* below.'
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
        confirmer = body['user']['id']
        channel = body['channel']['id']
        message_ts = body['message']['ts']

        log_event(
            'slack_button_clicked', button='confirm_purchased',
            domain=domain, vertical=vertical,
            requester=requester, confirmer=confirmer,
            lander_url=lander,
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
        try:
            inventory_store.add_domain(
                domain=domain,
                vertical=vertical,
                lander_url=lander,
                requested_by=f'Slack:{requester}',
                notes='Purchased via /new-domain bot flow',
                # State machine starts here — Phase 7 worker will move
                # the row to STATUS_DEPLOYING then DEPLOYED|FAILED.
                status=inventory_store.STATUS_PENDING,
                # Lifecycle ownership starts on day one — classifier needs
                # an MDB to DM when this domain expires or goes idle.
                # Legacy CSV-imported rows get this via the boot-time
                # backfill in store.init_db().
                assigned_to=requester,
                # Audit trail for /domain-history. Captures who clicked
                # Mark Purchased, plus the lander URL it'll be deployed to.
                event_source='path_b_mark_purchased',
                event_metadata={'lander_url': lander, 'vertical': vertical,
                                'confirmer': confirmer},
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

_NEW_DOMAIN_MODAL = {
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
                         'final deploy notification) go to them instead of you.'),
            },
            'element': {
                'type': 'users_select',
                'action_id': 'mdb_select',
                'placeholder': {'type': 'plain_text',
                                'text': 'Pick an MDB'},
            },
        },
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
            'label': {'type': 'plain_text',
                      'text': 'Lander URL (which page to deploy)'},
            'element': {
                'type': 'url_text_input',
                'action_id': 'lander_input',
                'placeholder': {'type': 'plain_text',
                                'text': 'https://example.com/landing-page'},
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
    ],
}


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


@slack_bp.route('/slash/list-domains', methods=['POST'])
def slash_list_domains():
    return _bolt_or_stub('Coming soon: /list-domains (Phase 3). '
                         'Set SLACK_BOT_TOKEN + SLACK_SIGNING_SECRET to enable.')


@slack_bp.route('/slash/domain-history', methods=['POST'])
def slash_domain_history():
    return _bolt_or_stub('Coming soon: /domain-history (Phase D). '
                         'Set SLACK_BOT_TOKEN + SLACK_SIGNING_SECRET to enable.')


@slack_bp.route('/interactions', methods=['POST'])
def interactions():
    return _bolt_or_stub('Coming soon: interactive callbacks (Phase 5).')
