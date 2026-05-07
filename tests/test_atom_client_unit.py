"""Unit tests for orchestrator.atom_client typed-exception layer.

These tests run WITHOUT a live ATOM upstream — the requests.Session is
mocked. Coverage:
  • Typed error translation: 5xx → AtomServerError, 4xx → AtomClientError,
    401/403 → AtomAuthenticationError, 200+non-JSON → AtomInvalidResponse,
    network failure → AtomConnectionError
  • login() refuses to follow redirects so silent auth failures
    (HTTP 200 re-renders of the login form) get caught as
    AtomAuthenticationError instead of leaking into downstream
    .json() calls (root cause of the 2026-05-08 outage).
  • wait_for_setup tolerates transient connection / 5xx errors and
    only escalates a hard timeout.

Companion to tests/test_atom_client.py which exercises the same
interface against a live ATOM (integration tests).
"""
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from orchestrator.atom_client import (
    AtomClient,
    AtomAuthenticationError,
    AtomClientError,
    AtomConnectionError,
    AtomError,
    AtomInvalidResponse,
    AtomServerError,
)


# ─── helpers ──────────────────────────────────────────────────────────────

def _make_response(status_code: int, body: str, content_type: str = None):
    """Build a fake `requests.Response` we can hand to the translator."""
    r = requests.Response()
    r.status_code = status_code
    r._content = body.encode() if isinstance(body, str) else body
    if content_type:
        r.headers['Content-Type'] = content_type
    return r


def _make_client(mock_session):
    return AtomClient(base_url='http://atom.test', session=mock_session)


# ─── exception class hierarchy ────────────────────────────────────────────

def test_exception_hierarchy_lets_callers_catch_AtomError():
    assert issubclass(AtomConnectionError, AtomError)
    assert issubclass(AtomServerError, AtomError)
    assert issubclass(AtomClientError, AtomError)
    assert issubclass(AtomInvalidResponse, AtomError)
    assert issubclass(AtomAuthenticationError, AtomClientError)


# ─── 5xx / 4xx / non-JSON translation ─────────────────────────────────────

def test_get_5xx_raises_AtomServerError():
    sess = MagicMock()
    sess.get.return_value = _make_response(503, 'service unavailable')
    client = _make_client(sess)
    with pytest.raises(AtomServerError) as exc_info:
        client.health()
    assert '503' in str(exc_info.value)
    assert 'service unavailable' in str(exc_info.value)


def test_get_400_raises_AtomClientError():
    sess = MagicMock()
    sess.get.return_value = _make_response(400, 'bad request payload')
    client = _make_client(sess)
    with pytest.raises(AtomClientError):
        client.check_existing('whatever.com')


def test_get_401_raises_AtomAuthenticationError():
    sess = MagicMock()
    sess.get.return_value = _make_response(401, 'login required')
    client = _make_client(sess)
    with pytest.raises(AtomAuthenticationError):
        client.check_existing('whatever.com')


def test_get_403_raises_AtomAuthenticationError():
    sess = MagicMock()
    sess.get.return_value = _make_response(403, 'forbidden')
    client = _make_client(sess)
    with pytest.raises(AtomAuthenticationError):
        client.check_existing('whatever.com')


def test_get_200_html_body_raises_AtomInvalidResponse():
    """The exact 2026-05-08 bug — ATOM redirected through /login (HTML)
    on an unauthenticated session. .json() used to crash with the
    cryptic 'Expecting value: line 1 column 1' error; now it surfaces
    as AtomInvalidResponse with a body sniff so the cause is obvious."""
    sess = MagicMock()
    sess.get.return_value = _make_response(
        200, '<!doctype html><html>login form...</html>',
        content_type='text/html',
    )
    client = _make_client(sess)
    with pytest.raises(AtomInvalidResponse) as exc_info:
        client.health()
    assert '<!doctype html' in str(exc_info.value).lower()


def test_post_connection_failure_raises_AtomConnectionError():
    sess = MagicMock()
    sess.post.side_effect = requests.ConnectionError('DNS lookup failed')
    client = _make_client(sess)
    with pytest.raises(AtomConnectionError) as exc_info:
        client.setup_domain('x.com')
    assert 'DNS lookup failed' in str(exc_info.value)


def test_post_timeout_raises_AtomConnectionError():
    sess = MagicMock()
    sess.post.side_effect = requests.Timeout('read timed out')
    client = _make_client(sess)
    with pytest.raises(AtomConnectionError):
        client.setup_domain('x.com')


def test_post_200_returns_parsed_json():
    sess = MagicMock()
    sess.post.return_value = _make_response(
        200, json.dumps({'tasks': [{'task_id': 't1', 'domain': 'x.com'}]}),
        content_type='application/json',
    )
    client = _make_client(sess)
    out = client.setup_domain('x.com')
    assert out['tasks'][0]['task_id'] == 't1'


# ─── login redirect-checking ──────────────────────────────────────────────

def test_login_302_to_home_succeeds():
    sess = MagicMock()
    resp = _make_response(302, '')
    resp.headers['Location'] = '/'
    sess.post.return_value = resp
    client = _make_client(sess)
    assert client.login('sunny', 'good-password') is True
    # allow_redirects must be False — that's the whole point of the fix.
    _, kwargs = sess.post.call_args
    assert kwargs['allow_redirects'] is False


def test_login_200_re_render_raises_AtomAuthenticationError():
    """Bad credentials make Flask-Login re-render the login form with
    HTTP 200. allow_redirects=True would hide this; we explicitly check
    for it and raise so subsequent /api/setup-domain calls don't end up
    receiving login HTML and crashing on .json() (the 2026-05-08 bug)."""
    sess = MagicMock()
    sess.post.return_value = _make_response(
        200, '<html><form>login form re-rendered</form></html>',
    )
    client = _make_client(sess)
    with pytest.raises(AtomAuthenticationError) as exc_info:
        client.login('sunny', 'wrong-password')
    # Message must point operator at the right env vars to fix.
    assert 'ATOM_USERNAME' in str(exc_info.value)
    assert 'ATOM_PASSWORD' in str(exc_info.value)


def test_login_302_back_to_login_raises_AtomAuthenticationError():
    """Some Flask-Login configs redirect failed auth back to /login.
    Make sure that's still treated as auth failure, not success."""
    sess = MagicMock()
    resp = _make_response(302, '')
    resp.headers['Location'] = '/login?next=%2F'
    sess.post.return_value = resp
    client = _make_client(sess)
    with pytest.raises(AtomAuthenticationError):
        client.login('sunny', 'wrong-password')


def test_login_connection_failure_raises_AtomConnectionError():
    sess = MagicMock()
    sess.post.side_effect = requests.ConnectionError('refused')
    client = _make_client(sess)
    with pytest.raises(AtomConnectionError):
        client.login('sunny', 'pwd')


# ─── wait_for_setup transient tolerance ───────────────────────────────────

def test_wait_for_setup_returns_completed_status():
    sess = MagicMock()
    sess.get.return_value = _make_response(
        200, json.dumps({'status': 'completed', 'foo': 'bar'}),
        content_type='application/json',
    )
    client = _make_client(sess)
    out = client.wait_for_setup('t1', timeout=10, poll_interval=0)
    assert out['status'] == 'completed'


def test_wait_for_setup_swallows_transient_5xx_then_succeeds():
    """A 503 mid-poll is expected during Render redeploys / Cloudflare
    blips. The wait must keep polling rather than abort the workflow."""
    sess = MagicMock()
    sess.get.side_effect = [
        _make_response(503, 'transient'),
        _make_response(
            200, json.dumps({'status': 'completed'}),
            content_type='application/json',
        ),
    ]
    client = _make_client(sess)
    out = client.wait_for_setup('t1', timeout=10, poll_interval=0)
    assert out['status'] == 'completed'


def test_wait_for_setup_swallows_connection_error_then_succeeds():
    sess = MagicMock()
    sess.get.side_effect = [
        requests.ConnectionError('blip'),
        _make_response(
            200, json.dumps({'status': 'completed'}),
            content_type='application/json',
        ),
    ]
    client = _make_client(sess)
    out = client.wait_for_setup('t1', timeout=10, poll_interval=0)
    assert out['status'] == 'completed'


def test_wait_for_setup_raises_TimeoutError_when_status_never_terminal():
    sess = MagicMock()
    sess.get.return_value = _make_response(
        200, json.dumps({'status': 'in_progress'}),
        content_type='application/json',
    )
    client = _make_client(sess)
    with pytest.raises(TimeoutError):
        # timeout=0 means deadline already passed before first poll.
        client.wait_for_setup('t1', timeout=0, poll_interval=0)
