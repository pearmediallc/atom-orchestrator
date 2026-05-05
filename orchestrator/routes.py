"""HTTP endpoints that drive the orchestration workflows.

Phase 4 exposes Path A (existing domain). Phase 5 will add Path B.
For now these are plain HTTP — the Slack bot (Phase 2) will call them
internally rather than reimplementing the logic.
"""
from flask import Blueprint, jsonify, request

from orchestrator.workflow import (
    ExistingDomainRequest,
    run_existing_domain_workflow,
    suggest_new_domains,
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


@orchestrator_bp.route('/new-domain/suggest', methods=['POST'])
def new_domain_suggest():
    """POST /workflow/new-domain/suggest
    body: {
      vertical:  "auto-insurance",                required
      audience:  "seniors looking for medigap",   optional, free text
      extension: ".com" | ".pro" | "any",         default "any"
      count:     5                                default 5
    }

    Returns availability + price-filtered list:
      { suggestions: [{domain, available, price, extension}, ...], count: N }

    Phase 8 — when extension is "any", sweeps cheap TLDs and returns
    cheapest-available first. Per-extension price caps from Config
    (.com under $15, others ≤$5). Falls back to deterministic stubs in
    domain_assistant/ when API keys are absent.
    """
    body = request.get_json(silent=True) or {}

    vertical = (body.get('vertical') or '').strip()
    if not vertical:
        return jsonify({'error': "'vertical' is required"}), 400

    try:
        results = suggest_new_domains(
            vertical=vertical,
            audience=(body.get('audience') or '').strip(),
            extension=body.get('extension') or 'any',
            count=int(body.get('count') or 5),
        )
    except (ValueError, NotImplementedError) as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        # Catches OpenAI API errors (RateLimitError, AuthenticationError,
        # APIConnectionError, etc.) and returns a clean JSON response
        # instead of Flask's 500-with-debug-HTML.
        return jsonify({
            'error': 'Suggestion engine failed.',
            'exception': type(e).__name__,
            'message': str(e),
        }), 502

    return jsonify({'count': len(results), 'suggestions': results})
