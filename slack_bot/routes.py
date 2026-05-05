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
from orchestrator.workflow import (
    ExistingDomainRequest,
    run_existing_domain_workflow,
    suggest_new_domains,
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
        return

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

        Optional argument: substring filter that searches across the
        domain name, the vertical, AND the requester. Lets you find
        a specific domain quickly even when there are hundreds.

            /list-domains                → top results across everything
            /list-domains auto           → matches vertical 'Auto Insurance'
            /list-domains flashburn      → matches domains like
                                            'instantflashburn.com'
            /list-domains anurag         → matches anything by Anurag

        Each domain has a "Deploy lander" button that starts Path A
        (deploy a lander to that existing owned domain).
        """
        ack()
        filter_text = (command.get('text') or '').strip().lower()
        all_rows = inventory_store.list_domains()
        if not all_rows:
            respond({
                'response_type': 'ephemeral',
                'text': '*No domains in inventory yet.*',
            })
            return

        # Substring filter across domain / vertical / requester. Most
        # useful columns for "find the domain I'm thinking of".
        if filter_text:
            def _matches(r: dict) -> bool:
                haystacks = (
                    (r.get('domain') or ''),
                    (r.get('vertical') or ''),
                    (r.get('requested_by') or ''),
                )
                return any(filter_text in h.lower() for h in haystacks)
            rows = [r for r in all_rows if _matches(r)]
        else:
            rows = all_rows

        if not rows:
            respond({
                'response_type': 'ephemeral',
                'text': (f'*No domains match `{filter_text}`.* Try part of a '
                         f'domain name, vertical, or owner — or run '
                         f'`/list-domains` with no filter.'),
            })
            return

        # Slack caps blocks at 50 per message. We render at most ~30 domains
        # per call, leaving room for header/footer/dividers.
        max_per_message = 30
        shown = rows[:max_per_message]
        truncated = len(rows) - len(shown)

        header_text = (
            f'Owned domains — {len(shown)} of {len(all_rows)}'
            + (f' (filtered by "{filter_text}")' if filter_text else '')
        )

        blocks: list = [
            {'type': 'header',
             'text': {'type': 'plain_text', 'text': header_text}},
            {'type': 'context', 'elements': [{
                'type': 'mrkdwn',
                'text': ('Click *Deploy lander* to send a redeployment '
                         'request to Utkarsh. '
                         'Filter by any text: `/list-domains flashburn` '
                         '· `/list-domains medicare` · `/list-domains anurag`.'),
            }]},
            {'type': 'divider'},
        ]

        for r in shown:
            vert = r.get('vertical') or '_no vertical_'
            requested_by = r.get('requested_by') or '_unknown_'
            stat_emoji = '✅' if r.get('setup_at') else '⏳'
            blocks.append({
                'type': 'section',
                'text': {
                    'type': 'mrkdwn',
                    'text': (f'{stat_emoji} `{r["domain"]}`\n'
                             f'_{vert}_  ·  by `{requested_by}`'),
                },
                'accessory': {
                    'type': 'button',
                    'action_id': 'deploy_lander_existing',
                    'text': {'type': 'plain_text', 'text': 'Deploy lander'},
                    'value': json.dumps({
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
        examples_raw = (values['examples_block']['examples_input']['value'] or '').strip()
        lander = (values['lander_block']['lander_input']['value'] or '').strip()
        extension = values['extension_block']['extension_select']['selected_option']['value']

        # Parse comma-separated examples into a list, dropping blanks.
        example_domains = [
            e.strip() for e in examples_raw.split(',') if e.strip()
        ]

        requester = body['user']['id']

        # 1. Confirm receipt up-front so the user knows we're working.
        receipt = (
            ':sparkles: *New-domain request received* :sparkles:\n'
            f'• Requested by: <@{requester}>\n'
            f'• Vertical: `{vertical}`\n'
            f"• Example domains: `{examples_raw or '(none)'}`\n"
            f'• Lander URL: {lander}\n'
            f'• Extension: `{extension}`\n'
            ':mag: Generating suggestions and checking Namecheap availability…'
        )
        client.chat_postMessage(channel=requester, text=receipt)

        # 2. Call the suggestion engine. It already filters to available +
        # price-capped (.com <$15, other extensions <=$5 per TL 2026-05-05).
        # Falls back to stubs in domain_assistant/ when API keys aren't set.
        try:
            suggestions = suggest_new_domains(
                vertical=vertical,
                example_domains=example_domains,
                extension=extension,
                count=5,
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

        # Context that the click handler needs to know what was picked. Encoded
        # into each button's `value` field (Slack caps button values at ~2k
        # chars, this fits comfortably).
        def _button_value(domain: str) -> str:
            return json.dumps({
                'domain': domain,
                'vertical': vertical,
                'lander': lander,
                'extension': extension,
                'requester': requester,
            })

        # Per-extension price cap (just for display in the header)
        cap_usd = Config.price_cap_for(extension)
        blocks = [
            {
                'type': 'header',
                'text': {
                    'type': 'plain_text',
                    'text': f'{len(available)} available — pick one to continue',
                },
            },
            {
                'type': 'context',
                'elements': [{
                    'type': 'mrkdwn',
                    'text': (f'Vertical: *{vertical}*  ·  Extension: `{extension}`'
                             f'  ·  Lander: {lander}\n'
                             f'_All shown are available on Namecheap and '
                             f'priced at-or-below ${cap_usd:.2f}/yr._'),
                }],
            },
            {'type': 'divider'},
        ]
        for s in available:
            price = s.get('price')
            price_label = f'  ·  ${price:.2f}/yr' if price is not None else ''
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
                    'value': _button_value(s['domain']),
                },
            })

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
                    'value': json.dumps({
                        'domain': domain,
                        'vertical': vertical,
                        'lander': lander,
                        'requester': requester,
                    }),
                }]},
            ],
        )
        return purchaser

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

        try:
            data = json.loads(body['actions'][0]['value'])
        except (KeyError, json.JSONDecodeError, IndexError):
            return

        domain = data['domain']
        vertical = data['vertical']
        lander = data['lander']
        extension = data['extension']
        requester = data['requester']

        approver_ids = Config.APPROVER_SLACK_USER_IDS
        button_payload = json.dumps({
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
        try:
            data = json.loads(body['actions'][0]['value'])
        except (KeyError, json.JSONDecodeError, IndexError):
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
        try:
            data = json.loads(body['actions'][0]['value'])
        except (KeyError, json.JSONDecodeError, IndexError):
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
        try:
            data = json.loads(body['actions'][0]['value'])
        except (KeyError, json.JSONDecodeError, IndexError):
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

        requester = body['user']['id']
        real_recipient = Config.UTKARSH_SLACK_USER_ID or requester
        recipient = Config.route_recipient(real_recipient)
        recipient_is_requester = (recipient == requester)

        # 1. Send the deployment request to Utkarsh (or fallback to requester),
        # with a Mark Deployed button so he can close the loop in one click.
        utkarsh_text = (
            ':rocket: *Lander deployment request* :rocket:\n'
            f'• Requester: <@{requester}>\n'
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
                    'value': json.dumps({
                        'target_domain': target_domain,
                        'vertical': vertical,
                        'lander': lander,
                        'requester': requester,
                    }),
                }]},
            ],
        )

        # 2. Confirm to the requester
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
        try:
            data = json.loads(body['actions'][0]['value'])
        except (KeyError, json.JSONDecodeError, IndexError):
            return

        target_domain = data['target_domain']
        vertical = data.get('vertical') or ''
        lander_url = data.get('lander') or ''
        requester = data['requester']
        confirmer = body['user']['id']
        channel = body['channel']['id']
        message_ts = body['message']['ts']

        # Update inventory: stamp setup_at so /list-domains shows ✅
        try:
            inventory_store.mark_setup_complete(target_domain)
        except Exception:
            pass  # Domain may not be in inventory yet (Phase 6 covers that)

        # Replace the button with a "confirmed" view
        client.chat_update(
            channel=channel,
            ts=message_ts,
            text=f'Confirmed deployed: {target_domain}',
            blocks=[
                {'type': 'header', 'text': {
                    'type': 'plain_text',
                    'text': f':white_check_mark: Deployed: {target_domain}',
                }},
                {'type': 'context', 'elements': [{
                    'type': 'mrkdwn',
                    'text': f'Confirmed by <@{confirmer}>.',
                }]},
            ],
        )

        # Notify the requester (skip if requester == confirmer to avoid
        # redundant self-DM during single-user testing)
        if requester != confirmer:
            client.chat_postMessage(
                channel=requester,
                text=(f':tada: `{target_domain}` is deployed! '
                      f'Confirmed by <@{confirmer}>.'),
            )

        # Phase 7: trigger ATOM in a background thread (the workflow blocks
        # for minutes; Slack interactions must ack within 3s).
        if Config.ENABLE_PHASE_7:
            threading.Thread(
                target=_phase7_run_atom_setup,
                args=(client, channel, message_ts, target_domain,
                      vertical, requester, lander_url),
                daemon=True,
                name=f'phase7-deploy-{target_domain}',
            ).start()

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
        try:
            data = json.loads(body['actions'][0]['value'])
        except (KeyError, json.JSONDecodeError, IndexError):
            return

        domain = data['domain']
        vertical = data.get('vertical') or ''
        lander = data.get('lander') or ''
        requester = data['requester']
        confirmer = body['user']['id']
        channel = body['channel']['id']
        message_ts = body['message']['ts']

        # Add to inventory so /list-domains starts showing it (and so the
        # Phase 7 workflow can find it — run_existing_domain_workflow
        # rejects domains that aren't in the store).
        try:
            inventory_store.add_domain(
                domain=domain,
                vertical=vertical,
                lander_url=lander,
                requested_by=f'Slack:{requester}',
                notes='Purchased via /new-domain bot flow',
            )
        except Exception:
            pass  # Already exists or other issue — non-fatal for the UX update

        # Replace the button with a "purchased" view
        phase7_note = (
            'Triggering ATOM setup now — watch this thread for progress.'
            if Config.ENABLE_PHASE_7
            else 'Phase 7 (auto setup_domain) is OFF — set ENABLE_PHASE_7=true to enable.'
        )
        client.chat_update(
            channel=channel,
            ts=message_ts,
            text=f'Confirmed purchased: {domain}',
            blocks=[
                {'type': 'header', 'text': {
                    'type': 'plain_text',
                    'text': f':white_check_mark: Purchased: {domain}',
                }},
                {'type': 'context', 'elements': [{
                    'type': 'mrkdwn',
                    'text': (f'Confirmed by <@{confirmer}>. Added to '
                             f'inventory. {phase7_note}'),
                }]},
            ],
        )

        # Notify the requester
        if requester != confirmer:
            client.chat_postMessage(
                channel=requester,
                text=(f':moneybag: `{domain}` has been purchased! '
                      f'Confirmed by <@{confirmer}>. Setup will follow.'),
            )

        # Phase 7: trigger ATOM in a background thread.
        if Config.ENABLE_PHASE_7:
            threading.Thread(
                target=_phase7_run_atom_setup,
                args=(client, channel, message_ts, domain,
                      vertical, requester, lander),
                daemon=True,
                name=f'phase7-purchase-{domain}',
            ).start()


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
            'block_id': 'examples_block',
            'optional': True,
            'label': {'type': 'plain_text',
                      'text': 'Example domain names (comma-separated, optional)'},
            'element': {
                'type': 'plain_text_input',
                'action_id': 'examples_input',
                'placeholder': {'type': 'plain_text',
                                'text': 'cheaprates.com, quickquote.com'},
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
                    'text': {'type': 'plain_text', 'text': '.com'},
                    'value': '.com',
                },
                'options': [
                    {'text': {'type': 'plain_text', 'text': '.com'},  'value': '.com'},
                    {'text': {'type': 'plain_text', 'text': '.pro'},  'value': '.pro'},
                    {'text': {'type': 'plain_text', 'text': '.site'}, 'value': '.site'},
                    {'text': {'type': 'plain_text', 'text': '.net'},  'value': '.net'},
                    {'text': {'type': 'plain_text', 'text': '.io'},   'value': '.io'},
                ],
            },
        },
    ],
}


# ─── Flask routes (forward everything to bolt) ─────────────────────────────

@slack_bp.route('/health', methods=['GET'])
def slack_health():
    return jsonify({
        'status': 'slack blueprint mounted',
        'phase': 2,
        'bolt_active': _bolt_app is not None,
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


@slack_bp.route('/interactions', methods=['POST'])
def interactions():
    return _bolt_or_stub('Coming soon: interactive callbacks (Phase 5).')
