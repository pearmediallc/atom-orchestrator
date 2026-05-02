"""HTTP endpoints for the owned-domain inventory.

Phase 3: simple CRUD over the SQLite store. No auth yet (single-tenant local
dev). Phase 6 will add API-key auth so external tools (or the Slack bot, when
running in a separate process) can call these.
"""
from flask import Blueprint, jsonify, request
from inventory import store

inventory_bp = Blueprint('inventory', __name__)


@inventory_bp.route('/list', methods=['GET'])
def list_domains():
    """GET /inventory/list?vertical=auto-insurance

    Returns all owned domains, optionally filtered by vertical.
    """
    vertical = request.args.get('vertical')
    rows = store.list_domains(vertical=vertical)
    return jsonify({'count': len(rows), 'domains': rows})


@inventory_bp.route('/add', methods=['POST'])
def add_domain():
    """POST /inventory/add  body: {domain, vertical?, aws_account?, lander_url?, requested_by?, notes?}

    Inserts a record. Returns the new id. Phase 4 will call this automatically
    after a successful new-domain setup.
    """
    body = request.get_json(silent=True) or {}
    domain = body.get('domain', '').strip()
    if not domain:
        return jsonify({'error': "'domain' is required"}), 400

    try:
        new_id = store.add_domain(
            domain=domain,
            vertical=body.get('vertical'),
            aws_account=body.get('aws_account'),
            lander_url=body.get('lander_url'),
            requested_by=body.get('requested_by'),
            notes=body.get('notes'),
        )
    except Exception as e:
        # Most common cause: UNIQUE constraint (domain already exists).
        return jsonify({'error': str(e)}), 409

    return jsonify({'id': new_id, 'domain': domain}), 201


@inventory_bp.route('/<domain>', methods=['GET'])
def get_domain(domain):
    """GET /inventory/<domain> — single record lookup."""
    row = store.get_domain(domain)
    if not row:
        return jsonify({'error': f'{domain} not found'}), 404
    return jsonify(row)


@inventory_bp.route('/<domain>/setup-complete', methods=['POST'])
def mark_setup_complete(domain):
    """POST /inventory/<domain>/setup-complete — bumps the setup_at timestamp.

    Phase 4 will call this from the orchestrator after AtomClient.setup_domain
    finishes successfully.
    """
    if not store.get_domain(domain):
        return jsonify({'error': f'{domain} not found'}), 404
    store.mark_setup_complete(domain)
    return jsonify({'domain': domain, 'setup_complete': True})
