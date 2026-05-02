"""Slack endpoints — Phase 2 (real handlers via slack_bolt).

This module wires Slack to our orchestration logic. Two slash commands:
  • /list-domains  — replies with inventory contents
  • /new-domain    — opens a modal collecting vertical/examples/lander/extension

Every incoming Slack request is verified against the signing secret automatically
by slack_bolt (refuses requests not actually from Slack).

Architecture note: Anand registered THREE separate Request URLs in the Slack
app config (one per slash command, plus interactivity). Rather than reconfigure
Slack, each Flask route below forwards to the SAME SlackRequestHandler — bolt
internally dispatches based on the request body.
"""
from flask import Blueprint, jsonify, request

from config import Config
from inventory import store as inventory_store

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
        """Reply with the current owned-domain inventory."""
        ack()  # acknowledge within 3s — required by Slack
        rows = inventory_store.list_domains()
        if not rows:
            respond({
                'response_type': 'ephemeral',
                'text': '*No domains in inventory yet.* Add one with `/new-domain`.',
            })
            return

        # Build a Slack-formatted list. Group display by vertical so humans can scan.
        lines = [f'*Owned domains ({len(rows)} total):*']
        for r in rows:
            vert = r.get('vertical') or '_no vertical_'
            acct = r.get('aws_account') or '_no account_'
            stat = '✅' if r.get('setup_at') else '⏳'
            lines.append(
                f"  {stat}  `{r['domain']}`  —  *{vert}*  ·  account: `{acct}`"
            )
        respond({'response_type': 'ephemeral', 'text': '\n'.join(lines)})

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
        """Modal submitted — collect inputs and (Phase 5) kick off the workflow.

        For now we just confirm what we collected and DM the requester so they
        can see we got it. Phase 5 wires the chatgpt-suggest pipeline in here.
        """
        ack()
        values = view['state']['values']
        vertical = values['vertical_block']['vertical_input']['value'] or ''
        examples = values['examples_block']['examples_input']['value'] or ''
        lander = values['lander_block']['lander_input']['value'] or ''
        extension = values['extension_block']['extension_select']['selected_option']['value']

        requester = body['user']['id']
        summary = (
            ':sparkles: *New-domain request received* :sparkles:\n'
            f'• Requested by: <@{requester}>\n'
            f'• Vertical: `{vertical.strip()}`\n'
            f"• Example domains: `{examples.strip() or '(none)'}`\n"
            f'• Lander URL: {lander.strip()}\n'
            f'• Extension: `{extension}`\n'
            '_Phase 5 will plug this into ChatGPT + Namecheap availability._'
        )
        # DM the requester with the confirmation
        client.chat_postMessage(channel=requester, text=summary)


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
