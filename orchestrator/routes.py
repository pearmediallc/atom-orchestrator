"""HTTP endpoints that drive the orchestration workflows.

Phase 4 exposes Path A (existing domain). Phase 5 will add Path B.
For now these are plain HTTP — the Slack bot (Phase 2) will call them
internally rather than reimplementing the logic.
"""
from flask import Blueprint, jsonify, request

from orchestrator.workflow import (
    ExistingDomainRequest,
    run_existing_domain_workflow,
)

orchestrator_bp = Blueprint('orchestrator', __name__)


@orchestrator_bp.route('/existing-domain', methods=['POST'])
def existing_domain():
    """POST /workflow/existing-domain
    body: {
      target_domain: "...",          required — must be in inventory
      source_account: "auto-insurance",
      source_bucket: "...",          required — bucket holding the lander
      source_folders: ["lander-v3/"],
      source_files: [],
      requested_by: "U123ABC"        Slack user id, optional
    }
    """
    body = request.get_json(silent=True) or {}

    target_domain = (body.get('target_domain') or '').strip()
    source_bucket = (body.get('source_bucket') or '').strip()

    if not target_domain:
        return jsonify({'error': "'target_domain' is required"}), 400
    if not source_bucket:
        return jsonify({'error': "'source_bucket' is required"}), 400

    req = ExistingDomainRequest(
        target_domain=target_domain,
        source_account=body.get('source_account') or 'auto-insurance',
        source_bucket=source_bucket,
        source_folders=body.get('source_folders') or [],
        source_files=body.get('source_files') or [],
        requested_by=body.get('requested_by'),
    )

    result = run_existing_domain_workflow(req)

    code = 200 if result.status == 'completed' else 500
    return jsonify({
        'status': result.status,
        'message': result.message,
        'details': result.details,
    }), code
