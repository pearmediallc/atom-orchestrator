"""Tests for /health and /slack/health DB-readiness probes.

Both endpoints must:
  • Return 200 when the inventory DB is reachable.
  • Return 503 with reason='db_unavailable' when StoreUnavailable is
    raised — so Render's load balancer drains the pod instead of
    sending traffic to a process that will only return 500s.

Added 2026-05-08 per audit finding: the previous /health returned 200
unconditionally, hiding DB outages from monitoring.
"""
import pytest

from inventory import store


@pytest.fixture
def flask_client(tmp_inventory):
    """Flask test client wired to an isolated SQLite inventory."""
    # Import inside the fixture so app picks up the monkey-patched
    # Config.DATABASE_URL set by tmp_inventory.
    from app import app
    app.config['TESTING'] = True
    return app.test_client()


def test_root_health_returns_200_when_db_reachable(flask_client):
    r = flask_client.get('/health')
    assert r.status_code == 200
    body = r.get_json()
    assert body['status'] == 'healthy'
    assert body['db'] == 'reachable'


def test_root_health_returns_503_when_db_unavailable(
    flask_client, monkeypatch,
):
    """Simulate a DB outage by patching health_check to raise
    StoreUnavailable. The endpoint must return 503 with the
    structured reason so monitors can alert on this signal."""
    def boom():
        raise store.StoreUnavailable('OperationalError: connection refused')

    monkeypatch.setattr(store, 'health_check', boom)

    r = flask_client.get('/health')
    assert r.status_code == 503
    body = r.get_json()
    assert body['status'] == 'unhealthy'
    assert body['reason'] == 'db_unavailable'
    assert 'OperationalError' in body['error']


def test_slack_health_returns_503_when_db_unavailable(
    flask_client, monkeypatch,
):
    def boom():
        raise store.StoreUnavailable('connection refused')

    monkeypatch.setattr(store, 'health_check', boom)

    r = flask_client.get('/slack/health')
    assert r.status_code == 503
    body = r.get_json()
    assert body['status'] == 'unhealthy'
    assert body['reason'] == 'db_unavailable'
