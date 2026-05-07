"""ATOM Orchestrator — Flask entry point.

Boots the Flask app, registers blueprints, exposes /health.
Real workflow logic lives in the imported modules; this file is the wiring.
"""
from flask import Flask, jsonify
from config import Config

# Blueprint imports — each phase wires in another one.
from slack_bot.routes import slack_bp
from inventory.routes import inventory_bp
from inventory import store as inventory_store
from orchestrator.routes import orchestrator_bp


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = Config.FLASK_SECRET_KEY or 'dev-only-not-for-prod'

    # Initialise local storage (idempotent — safe to call every boot).
    inventory_store.init_db()

    app.register_blueprint(slack_bp, url_prefix='/slack')
    app.register_blueprint(inventory_bp, url_prefix='/inventory')
    app.register_blueprint(orchestrator_bp, url_prefix='/workflow')

    @app.route('/health')
    def health():
        """Liveness + readiness combined.

        Returns 200 only when the bot can both serve HTTP AND reach its
        Postgres inventory. Returns 503 with a structured `reason` when
        the DB is unreachable so Render's load balancer (or any external
        monitor) can drain a pod whose data plane went away — instead of
        sending traffic to a process that will only return 500s once it
        tries to query inventory (2026-05-08 audit fix).

        Intentionally cheap: a single `SELECT 1` plus `fetchone`. Adds
        ~5ms typical, dominated by DB RTT.
        """
        try:
            inventory_store.health_check()
        except inventory_store.StoreUnavailable as e:
            return jsonify({
                'status': 'unhealthy',
                'service': 'atom-orchestrator',
                'reason': 'db_unavailable',
                'error': str(e),
            }), 503
        return jsonify({
            'status': 'healthy',
            'service': 'atom-orchestrator',
            'atom_base_url': Config.ATOM_BASE_URL,
            'db': 'reachable',
        })

    return app


app = create_app()


if __name__ == '__main__':
    print(f"Starting atom-orchestrator on port {Config.PORT}")
    print(f"  → ATOM upstream: {Config.ATOM_BASE_URL}")
    print(f"  → Health check:  http://localhost:{Config.PORT}/health")
    app.run(debug=True, host='0.0.0.0', port=Config.PORT)
