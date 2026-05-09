"""Shared pytest fixtures.

Integration tests require ATOM (the upstream Flask app) to be running locally
on port 5500. If it's not, we skip the test instead of failing — that way
unit tests still run cleanly when ATOM isn't booted.
"""
import os
import pytest
import requests
from orchestrator.atom_client import AtomClient

ATOM_URL = os.environ.get('ATOM_BASE_URL', 'http://localhost:5500')

# Test credentials baked into the local-dev .env we created earlier.
# These are NOT the production passwords — they're the throwaway values from
# atom-orchestrator/../aws_automation/.env (sunny / test123).
ATOM_TEST_USER = 'sunny'
ATOM_TEST_PASS = 'test123'


def _atom_is_up() -> bool:
    try:
        r = requests.get(f'{ATOM_URL}/api/health', timeout=2)
        return r.status_code == 200
    except requests.RequestException:
        return False


@pytest.fixture(scope='session')
def atom_running():
    """Skip the entire integration test if ATOM isn't running locally."""
    if not _atom_is_up():
        pytest.skip(
            f'ATOM not reachable at {ATOM_URL}. Start it with:\n'
            '  cd /Users/pear/Desktop/Projects/aws_automation\n'
            '  source venv/bin/activate && python app.py\n'
            'then re-run pytest.'
        )


@pytest.fixture
def client(atom_running):
    """Fresh, anonymous AtomClient (no login)."""
    return AtomClient(base_url=ATOM_URL)


@pytest.fixture
def logged_in_client(atom_running):
    """AtomClient with an authenticated session cookie."""
    c = AtomClient(base_url=ATOM_URL)
    c.login(ATOM_TEST_USER, ATOM_TEST_PASS)
    return c


@pytest.fixture(autouse=True)
def _isolate_from_production_db(tmp_path, monkeypatch, request):
    """Belt-and-braces guard: clear DATABASE_URL and point INVENTORY_DB_PATH
    at a per-test SQLite file for EVERY test, automatically.

    Why: tests that don't explicitly use the `tmp_inventory` fixture used
    to be DB-free in practice. After Phase D added `record_event` calls
    inside the Phase 7 worker, even those tests can now write to the
    DB — and without this guard, DATABASE_URL from .env would point them
    at production Postgres. Each accidental write is a real prod row +
    a 1-2s round-trip per call (caught 2026-05-10 when the suite slowed
    from ~5s to ~30s).

    `tmp_inventory` itself still works — it overrides the same vars
    AND calls init_db, so tests that want a real schema get one.

    Tests that legitimately need to talk to a real database can opt
    out by adding `pytestmark = pytest.mark.uses_real_db` (none today,
    but the marker is documented in pytest.ini).
    """
    if request.node.get_closest_marker('uses_real_db'):
        return
    monkeypatch.setattr('config.Config.DATABASE_URL', '')
    monkeypatch.setattr(
        'config.Config.INVENTORY_DB_PATH',
        str(tmp_path / '_isolated_inventory.db'),
    )


@pytest.fixture
def tmp_inventory(tmp_path, monkeypatch):
    """Per-test SQLite inventory at a temp path. Tests using this fixture
    get a freshly initialised, empty store, isolated from each other and
    from the dev database — and CRITICALLY also isolated from any
    DATABASE_URL pointing at production Postgres.

    Without the DATABASE_URL override, tests would write to production
    Postgres (we found this out the hard way on 2026-05-05 when
    Phase 8 changes triggered import-csv tests that inserted real1.com /
    ex.com / etc. into production before this fix landed).
    """
    from config import Config
    from inventory import store

    db_path = str(tmp_path / 'inventory.db')
    monkeypatch.setattr(Config, 'INVENTORY_DB_PATH', db_path)
    monkeypatch.setattr(Config, 'DATABASE_URL', '')   # force SQLite path
    store.init_db()
    return store
