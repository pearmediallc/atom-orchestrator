"""Slack endpoints. Phase 1 = stubs only; Phase 2 wires in real Slack handlers."""
from flask import Blueprint, jsonify, request

slack_bp = Blueprint('slack', __name__)


@slack_bp.route('/health', methods=['GET'])
def slack_health():
    """Sanity check that the blueprint is mounted."""
    return jsonify({'status': 'slack blueprint mounted', 'phase': 1})


@slack_bp.route('/slash/new-domain', methods=['POST'])
def new_domain_slash_command():
    """Slack will POST here when a user runs `/new-domain`.

    Phase 2 will:
      1. Verify Slack signature
      2. Open a modal asking: vertical, examples, lander URL, extension
      3. On submit, kick off the orchestrator workflow
    """
    return jsonify({
        'response_type': 'ephemeral',
        'text': 'Coming soon: /new-domain (Phase 2).',
    })


@slack_bp.route('/slash/list-domains', methods=['POST'])
def list_domains_slash_command():
    """Phase 3: read from inventory and reply with the owned-domain list."""
    return jsonify({
        'response_type': 'ephemeral',
        'text': 'Coming soon: /list-domains (Phase 3).',
    })


@slack_bp.route('/interactions', methods=['POST'])
def interactions():
    """Phase 5: TL clicks Approve/Reject buttons → land here."""
    return jsonify({'text': 'Coming soon: interactive callbacks (Phase 5).'})
