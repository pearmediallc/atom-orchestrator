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
from flask import Blueprint, jsonify, request

from config import Config
from inventory import store as inventory_store
from orchestrator.workflow import suggest_new_domains

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

        # 2. Call the suggestion engine. Stays cheap thanks to the stubs in
        # domain_assistant/ when API keys aren't set.
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

        available = [s for s in suggestions if s['available']]
        unavailable = [s for s in suggestions if not s['available']]

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
                             f'  ·  Lander: {lander}'),
                }],
            },
            {'type': 'divider'},
        ]
        for s in available:
            blocks.append({
                'type': 'section',
                'text': {'type': 'mrkdwn', 'text': f'`{s["domain"]}`'},
                'accessory': {
                    'type': 'button',
                    'action_id': 'pick_domain',
                    'text': {'type': 'plain_text', 'text': 'Pick this'},
                    'style': 'primary',
                    'value': _button_value(s['domain']),
                },
            })

        if unavailable:
            blocks.append({'type': 'divider'})
            taken = ', '.join(f'`{u["domain"]}`' for u in unavailable)
            blocks.append({
                'type': 'context',
                'elements': [{
                    'type': 'mrkdwn',
                    'text': f'_Already taken ({len(unavailable)}): {taken}_',
                }],
            })

        client.chat_postMessage(
            channel=requester,
            blocks=blocks,
            text=f'{len(available)} available domains for {vertical}',
        )

    @_bolt_app.action('pick_domain')
    def handle_pick_domain(ack, body, client):
        """User clicked "Pick this" on a suggested domain.

        Per TL: do NOT auto-purchase via Namecheap API. Instead DM Utkarsh
        (the human procurer) with the request. He'll buy it on Namecheap
        and confirm in the thread, at which point Phase 6 will trigger the
        ATOM domain setup + lander copy.
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

        # If UTKARSH_SLACK_USER_ID isn't set, fall back to DMing the requester
        # themselves. Lets a single dev test the whole flow without a second
        # Slack identity.
        purchaser = Config.UTKARSH_SLACK_USER_ID or requester
        purchaser_is_requester = (purchaser == requester)

        # 1. DM Utkarsh (or fallback) with full context so he knows what to buy
        # and where it'll be deployed.
        utkarsh_msg = (
            ':moneybag: *Domain purchase request* :moneybag:\n'
            f'• Requester: <@{requester}>\n'
            f'• Domain to buy: `{domain}`\n'
            f'• Vertical: `{vertical}`\n'
            f'• Extension: `{extension}`\n'
            f'• Lander to deploy: {lander}\n\n'
            ':point_right: Please buy this on Namecheap and reply '
            ':white_check_mark: in this thread when done. The bot will then '
            'kick off the ATOM domain setup and copy the lander files.'
        )
        client.chat_postMessage(channel=purchaser, text=utkarsh_msg)

        # 2. Update the original suggestion message so the buttons disappear
        # and the user can see which one they picked.
        client.chat_update(
            channel=body['channel']['id'],
            ts=body['message']['ts'],
            text=f'Selected: {domain}',
            blocks=[
                {
                    'type': 'header',
                    'text': {
                        'type': 'plain_text',
                        'text': f':white_check_mark: Selected: {domain}',
                    },
                },
                {
                    'type': 'context',
                    'elements': [{
                        'type': 'mrkdwn',
                        'text': (
                            'Purchase request sent to '
                            f'<@{purchaser}>'
                            + (' (you, since UTKARSH_SLACK_USER_ID isn\'t set)'
                               if purchaser_is_requester else '')
                            + '.'
                        ),
                    }],
                },
            ],
        )

        # 3. If we DMed someone other than the requester, also confirm to the
        # requester so they know their click did something.
        if not purchaser_is_requester:
            client.chat_postMessage(
                channel=requester,
                text=(f':envelope: Sent purchase request for `{domain}` to '
                      f'<@{purchaser}>. He\'ll confirm here once it\'s bought.'),
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
                              'text': 'Lander URL (the page you want deployed)'},
                    'element': {
                        'type': 'url_text_input',
                        'action_id': 'lander_input',
                        'placeholder': {'type': 'plain_text',
                                        'text': 'https://existing-lander.com/page'},
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
        """
        ack()

        meta = json.loads(view.get('private_metadata') or '{}')
        target_domain = meta.get('target_domain', '')
        vertical = meta.get('vertical', '')

        values = view['state']['values']
        lander = (values['lander_block']['lander_input']['value'] or '').strip()
        notes = (values['notes_block']['notes_input']['value'] or '').strip()

        requester = body['user']['id']
        recipient = Config.UTKARSH_SLACK_USER_ID or requester
        recipient_is_requester = (recipient == requester)

        # 1. Send the deployment request to Utkarsh (or fallback to requester)
        utkarsh_msg = (
            ':rocket: *Lander deployment request* :rocket:\n'
            f'• Requester: <@{requester}>\n'
            f'• Target domain: `{target_domain}`\n'
            f'• Vertical: `{vertical}`\n'
            f'• Lander to deploy: {lander}\n'
            + (f'• Notes: _{notes}_\n' if notes else '')
            + '\n:point_right: Please confirm this domain is safe to redeploy '
            '(no live campaign), then deploy the lander files. Reply '
            ':white_check_mark: in this thread when done.'
        )
        client.chat_postMessage(channel=recipient, text=utkarsh_msg)

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
