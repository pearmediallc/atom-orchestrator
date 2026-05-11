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
from orchestrator.log_setup import configure_logging, log_event


def create_app() -> Flask:
    # Install the JSON formatter FIRST so every subsequent log line
    # — including init_db's migration output, the production-safety
    # assertion, recover_stale_running_tasks() — emits in the
    # structured format Render's log viewer can grep.
    configure_logging()
    log_event('boot_start', service='atom-orchestrator')

    app = Flask(__name__)
    app.secret_key = Config.FLASK_SECRET_KEY or 'dev-only-not-for-prod'

    # Refuse to start with dev-only knobs left on in production. This is
    # the gate that catches a forgotten DEV_REROUTE_DMS_TO before the
    # bot starts silently absorbing real users' approvals (audit #15).
    Config.assert_production_safe()

    # Initialise local storage (idempotent — safe to call every boot).
    # Includes ALTER TABLE migrations + legacy aws_account backfill on
    # first boot after the schema bump.
    inventory_store.init_db()

    # Recover Phase 7 tasks whose worker died mid-flight (e.g. Render
    # redeploy, OOM kill). recover_stale_running_tasks() requeues any
    # 'running' row whose heartbeat is stale and dispatches a fresh
    # worker per task. Idempotent — no-op when the queue is empty
    # (audit #2 fix).
    from orchestrator import tasks
    try:
        recovered = tasks.recover_stale_running_tasks()
        if recovered:
            log_event(
                'task_recovery_completed',
                recovered_task_ids=recovered,
                count=len(recovered),
            )
        else:
            log_event('task_recovery_completed', count=0)
    except Exception as e:
        # Never block the app from booting on a recovery failure —
        # the tasks stay in 'running' and the next boot will try
        # again. The DB itself being unreachable will fail /health,
        # and Render's load balancer will drain the pod.
        log_event(
            'task_recovery_failed',
            level=__import__('logging').ERROR,
            error=f'{type(e).__name__}: {e}',
        )

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

    log_event('boot_completed', service='atom-orchestrator')
    return app


app = create_app()


def _start_socket_mode_in_background() -> None:
    """Boot Slack Socket Mode so the local process receives slash
    commands + button clicks over a WebSocket — no public URL or tunnel
    required. Runs in a daemon thread so Flask still serves /health.

    Refuses to start if SLACK_APP_TOKEN is missing or the Bolt App
    wasn't initialised (SLACK_BOT_TOKEN/SIGNING_SECRET unset).
    """
    import threading
    from slack_bot.routes import _bolt_app
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    if _bolt_app is None:
        raise RuntimeError(
            'SLACK_USE_SOCKET_MODE=true but the Bolt App did not '
            'initialise — set SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET.'
        )
    if not Config.SLACK_APP_TOKEN:
        raise RuntimeError(
            'SLACK_USE_SOCKET_MODE=true but SLACK_APP_TOKEN is empty. '
            'Get an `xapp-…` token from your Slack app\'s Socket Mode '
            'settings and put it in .env.'
        )

    handler = SocketModeHandler(_bolt_app, Config.SLACK_APP_TOKEN)
    t = threading.Thread(
        target=handler.start, daemon=True, name='slack-socket-mode',
    )
    t.start()
    log_event(
        'slack_socket_mode_started',
        note='Slack events arriving over WebSocket; '
             'Render webhook deliveries are inactive while this runs.',
    )


if __name__ == '__main__':
    print(f"Starting atom-orchestrator on port {Config.PORT}")
    print(f"  → ATOM upstream: {Config.ATOM_BASE_URL}")
    print(f"  → Health check:  http://localhost:{Config.PORT}/health")
    if Config.SLACK_USE_SOCKET_MODE:
        print(f"  → Slack:         Socket Mode (no tunnel needed)")
        _start_socket_mode_in_background()
    else:
        print(f"  → Slack:         HTTP webhooks "
              f"(POST /slack/events expected from public URL)")
    # Flask's debug reloader forks a child process; if Socket Mode is on
    # we'd open TWO WebSockets with the same xapp- token, and Slack
    # routes events to whichever one it picked last — usually the dead
    # one. Disable the reloader when Socket Mode is on; keep debug pages
    # for stack traces. For HTTP-webhook mode the reloader is fine.
    app.run(
        debug=True,
        use_reloader=not Config.SLACK_USE_SOCKET_MODE,
        host='0.0.0.0', port=Config.PORT,
    )
