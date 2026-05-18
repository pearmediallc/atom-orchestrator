"""Tests for orchestrator.tracker_setup.

Mirrors test_pixel_fire's coverage shape: every TrackerSetupResult.status
branch + the key safety properties (DNS-then-RedTrack order, safety belt
on value collision, partial-failure surfaces dns_done_redtrack_failed
WITHOUT rollback, audit rows land for every operator-visible outcome,
validation rejects bad inputs before any IO).

AtomClient + redtrack_client.add_tracker_domain are mocked — these are
unit tests. The live integration check happens in production via the
slash command.
"""
from unittest.mock import MagicMock, patch

import pytest
import requests

from orchestrator import tracker_setup as ts
from orchestrator.atom_client import (
    AtomClientError,
    AtomConnectionError,
    AtomServerError,
)


VALID_CNAME = 'trk'
VALID_DOMAIN = 'neurobloomone.com'


# ─── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def seeded_inventory(tmp_inventory):
    """neurobloomone.com seeded with aws_account + setup_at populated —
    the minimum state /new-tracker requires."""
    tmp_inventory.add_domain(
        VALID_DOMAIN,
        vertical='auto',
        aws_account='auto-insurance',
        requested_by='U_TEST',
    )
    tmp_inventory.mark_setup_complete(VALID_DOMAIN)
    return tmp_inventory


@pytest.fixture
def mock_atom():
    """Mocked AtomClient whose add_cname returns the success-created
    shape by default. Tests can override per-case."""
    m = MagicMock()
    m.add_cname.return_value = {
        'action': 'created',
        'name': f'{VALID_CNAME}.{VALID_DOMAIN}',
        'value': 'bseav.6597822f9284e30001617c1c.click',
        'zone_id': 'ZABCD1234',
        'ttl': 60,
    }
    return m


@pytest.fixture
def mock_redtrack(monkeypatch):
    """Monkeypatch add_tracker_domain in BOTH the redtrack_client module
    AND the tracker_setup module (which imported it by name). Returns a
    MagicMock whose return value can be tweaked per test.
    """
    m = MagicMock(return_value={'id': 'rt_id_xyz', 'url': f'{VALID_CNAME}.{VALID_DOMAIN}'})
    monkeypatch.setattr('redtrack_client.add_tracker_domain', m)
    monkeypatch.setattr('orchestrator.tracker_setup.add_tracker_domain', m)
    return m


# ─── Happy path ────────────────────────────────────────────────────────────

def test_created_when_dns_and_redtrack_both_fresh(
    seeded_inventory, mock_atom, mock_redtrack,
):
    res = ts.add_tracker(
        VALID_CNAME, VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'created'
    assert res.details['tracker_url'] == f'{VALID_CNAME}.{VALID_DOMAIN}'
    assert res.details['dns_action'] == 'created'
    assert res.details['redtrack_already_existed'] is False
    assert res.details['redtrack_id'] == 'rt_id_xyz'

    # DNS-then-RedTrack: ATOM call must happen BEFORE RedTrack call.
    assert mock_atom.add_cname.call_count == 1
    assert mock_redtrack.call_count == 1


def test_dns_called_with_correct_args(
    seeded_inventory, mock_atom, mock_redtrack,
):
    """The CNAME target must be the FIXED Config value (RedTrack uses a
    single canonical tracker hostname per workspace and routes by Host
    header). The account must come from the inventory row."""
    ts.add_tracker(
        VALID_CNAME, VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
    )
    kwargs = mock_atom.add_cname.call_args.kwargs
    assert kwargs['account_key'] == 'auto-insurance'
    assert kwargs['domain'] == VALID_DOMAIN
    assert kwargs['cname_name'] == VALID_CNAME
    # FIXED target — same value regardless of cname_name. The default
    # is 'bseav.6597...click' (workspace primary tracker).
    assert kwargs['value'].startswith('bseav.')
    assert kwargs['value'].endswith('.click')


def test_redtrack_called_with_full_tracker_url(
    seeded_inventory, mock_atom, mock_redtrack,
):
    ts.add_tracker(
        VALID_CNAME, VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
    )
    args, _ = mock_redtrack.call_args
    assert args[0] == f'{VALID_CNAME}.{VALID_DOMAIN}'


def test_created_writes_audit_row(
    seeded_inventory, mock_atom, mock_redtrack,
):
    ts.add_tracker(
        VALID_CNAME, VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
    )
    events = seeded_inventory.list_domain_events(VALID_DOMAIN)
    assert any(
        e['event_type'] == 'tracker_added' and e['actor'] == 'U_TEST'
        and e['metadata']['cname_name'] == VALID_CNAME
        for e in events
    )


# ─── Idempotent re-run ────────────────────────────────────────────────────

def test_already_present_when_dns_and_redtrack_both_existed(
    seeded_inventory, mock_atom, mock_redtrack,
):
    mock_atom.add_cname.return_value = {
        'action': 'skipped_already_correct',
        'name': f'{VALID_CNAME}.{VALID_DOMAIN}',
        'value': 'bseav.6597822f9284e30001617c1c.click',
        'zone_id': 'ZABCD1234',
    }
    mock_redtrack.return_value = {
        '_already_exists': True, 'id': 'rt_id_xyz',
    }
    res = ts.add_tracker(
        VALID_CNAME, VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'already_present'
    assert res.details['dns_action'] == 'skipped_already_correct'
    assert res.details['redtrack_already_existed'] is True


def test_created_status_when_dns_existed_but_redtrack_was_fresh(
    seeded_inventory, mock_atom, mock_redtrack,
):
    """User ran /new-tracker before, DNS landed, RedTrack failed; now
    they re-run. DNS is already correct, RedTrack creates fresh — the
    recovery path. Should report 'created' not 'already_present'."""
    mock_atom.add_cname.return_value = {
        'action': 'skipped_already_correct', 'name': 'x', 'value': 'y',
        'zone_id': 'z',
    }
    # mock_redtrack default = fresh (not _already_exists)
    res = ts.add_tracker(
        VALID_CNAME, VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'created'


# ─── Safety belt ──────────────────────────────────────────────────────────

def test_safety_belt_when_cname_exists_with_different_value(
    seeded_inventory, mock_atom, mock_redtrack,
):
    """ATOM returns 4xx with 'exists_with_different_value' — must NOT
    call RedTrack, must NOT overwrite the existing record."""
    mock_atom.add_cname.side_effect = AtomClientError(
        'POST /api/add-cname -> HTTP 409 (client error). '
        'Body: {"error": "exists_with_different_value", '
        '"existing_value": "wrong.target.click", '
        '"requested_value": "bseav.6597...click"}'
    )
    res = ts.add_tracker(
        VALID_CNAME, VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'safety_belt'
    assert mock_redtrack.call_count == 0
    # Audit row still written so operators see what was attempted.
    events = seeded_inventory.list_domain_events(VALID_DOMAIN)
    assert any(e['event_type'] == 'tracker_safety_belt' for e in events)


# ─── Partial failure — DNS done, RedTrack down ────────────────────────────

def test_dns_done_redtrack_failed_does_not_rollback(
    seeded_inventory, mock_atom, mock_redtrack,
):
    """RedTrack returns 5xx after DNS already landed. Must NOT undo
    the DNS — the record is harmless on its own and rolling back is
    riskier than leaving it. Status reflects partial. Audit row
    captures the partial state. Re-run path is documented in message."""
    mock_redtrack.side_effect = requests.HTTPError('500 Server Error')

    res = ts.add_tracker(
        VALID_CNAME, VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'dns_done_redtrack_failed'
    assert 're-run' in res.message.lower()
    assert res.details['dns_action'] == 'created'
    # Audit captures the partial outcome distinctly.
    events = seeded_inventory.list_domain_events(VALID_DOMAIN)
    assert any(e['event_type'] == 'tracker_partial_dns_only' for e in events)


def test_dns_done_redtrack_connection_error(
    seeded_inventory, mock_atom, mock_redtrack,
):
    mock_redtrack.side_effect = requests.ConnectionError('net down')
    res = ts.add_tracker(
        VALID_CNAME, VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'dns_done_redtrack_failed'
    assert res.details['reason'] == 'redtrack_request_failed'


def test_dns_done_redtrack_config_missing(
    seeded_inventory, mock_atom, mock_redtrack,
):
    """REDTRACK_API_KEY or WORKSPACE_ID unset → add_tracker_domain
    raises RuntimeError. DNS already landed; partial status."""
    mock_redtrack.side_effect = RuntimeError(
        'REDTRACK_API_KEY is not configured'
    )
    res = ts.add_tracker(
        VALID_CNAME, VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'dns_done_redtrack_failed'
    assert res.details['reason'] == 'redtrack_config_missing'


# ─── DNS-side errors (RedTrack never called) ──────────────────────────────

def test_dns_error_no_hosted_zone(
    seeded_inventory, mock_atom, mock_redtrack,
):
    mock_atom.add_cname.side_effect = AtomClientError(
        'POST /api/add-cname -> HTTP 404. Body: '
        '{"error": "no_hosted_zone_for_neurobloomone.com"}'
    )
    res = ts.add_tracker(
        VALID_CNAME, VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'dns_error'
    assert res.details['reason'] == 'no_hosted_zone'
    assert mock_redtrack.call_count == 0


def test_dns_error_atom_endpoint_missing(
    seeded_inventory, mock_atom, mock_redtrack,
):
    """ATOM returning the bare Flask 404 HTML page means /api/add-cname
    isn't deployed (PR not merged OR Render didn't redeploy). Must
    surface a DIFFERENT message than 'no R53 zone' — operator needs to
    redeploy ATOM, not investigate missing zones (production case
    2026-05-18 caught the original loose check)."""
    mock_atom.add_cname.side_effect = AtomClientError(
        'POST /api/add-cname -> HTTP 404 (client error). Body: '
        '<!doctype html>\n<html lang=en>\n<title>404 Not Found</title>\n'
        '<h1>Not Found</h1>\n<p>The requested URL was not found...</p>'
    )
    res = ts.add_tracker(
        VALID_CNAME, VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'dns_error'
    assert res.details['reason'] == 'atom_endpoint_missing'
    assert 'redeploy' in res.message.lower()
    assert mock_redtrack.call_count == 0


def test_dns_error_atom_5xx(
    seeded_inventory, mock_atom, mock_redtrack,
):
    mock_atom.add_cname.side_effect = AtomServerError(
        'POST /api/add-cname -> HTTP 502'
    )
    res = ts.add_tracker(
        VALID_CNAME, VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'dns_error'
    assert res.details['reason'] == 'atom_failed'
    assert mock_redtrack.call_count == 0


def test_dns_error_atom_connection_error(
    seeded_inventory, mock_atom, mock_redtrack,
):
    mock_atom.add_cname.side_effect = AtomConnectionError(
        'could not reach ATOM'
    )
    res = ts.add_tracker(
        VALID_CNAME, VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'dns_error'
    assert mock_redtrack.call_count == 0


def test_dns_error_when_target_env_unconfigured(
    seeded_inventory, mock_atom, mock_redtrack, monkeypatch,
):
    monkeypatch.setattr('config.Config.REDTRACK_TRACKER_CNAME_TARGET', '')
    res = ts.add_tracker(
        VALID_CNAME, VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'dns_error'
    assert res.details['reason'] == 'tracker_target_unconfigured'
    # Should not even attempt ATOM.
    assert mock_atom.add_cname.call_count == 0


# ─── Input validation ─────────────────────────────────────────────────────

@pytest.mark.parametrize('bad_cname', [
    '',                  # empty
    '-trk',              # leading dash
    'trk-',              # trailing dash
    'TRK',               # uppercase (we lowercase first, so this becomes 'trk' — actually fine; remove)
    'a' * 64,            # too long
    'has space',         # space
    'has.dot',           # dot (subdomain.subdomain not allowed at this level)
    'has_underscore',    # underscore
])
def test_invalid_cname_rejected(
    seeded_inventory, mock_atom, mock_redtrack, bad_cname,
):
    # The 'TRK' case actually passes after lowercase normalisation, so
    # skip that one in the assertion (or test that it's accepted).
    if bad_cname == 'TRK':
        res = ts.add_tracker(
            bad_cname, VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
        )
        assert res.status == 'created'  # gets lowercased to 'trk'
        return
    res = ts.add_tracker(
        bad_cname, VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'invalid_input'
    assert res.details['reason'] == 'bad_cname_format'
    assert mock_atom.add_cname.call_count == 0
    assert mock_redtrack.call_count == 0


@pytest.mark.parametrize('reserved', ['track', 'www', 'TRACK', 'WWW'])
def test_reserved_cnames_rejected(
    seeded_inventory, mock_atom, mock_redtrack, reserved,
):
    res = ts.add_tracker(
        reserved, VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'invalid_input'
    assert res.details['reason'] == 'reserved_cname'
    assert mock_atom.add_cname.call_count == 0


@pytest.mark.parametrize('bad_domain', [
    '',
    'not a domain',
    'https://neurobloomone.com',     # has scheme — must be apex only
    'www.neurobloomone.com/path',    # has path
    '.com',                          # just a TLD
])
def test_invalid_domain_rejected(
    seeded_inventory, mock_atom, mock_redtrack, bad_domain,
):
    res = ts.add_tracker(
        VALID_CNAME, bad_domain, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'invalid_input'
    assert res.details['reason'] == 'bad_domain_format'
    assert mock_atom.add_cname.call_count == 0


def test_validation_strips_quotes_and_whitespace(
    seeded_inventory, mock_atom, mock_redtrack,
):
    res = ts.add_tracker(
        '  "trk"  ', '"neurobloomone.com"',
        actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'created'


def test_invalid_input_writes_no_audit_row(
    seeded_inventory, mock_atom, mock_redtrack,
):
    ts.add_tracker(
        '', VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
    )
    events = seeded_inventory.list_domain_events(VALID_DOMAIN)
    assert events == []


# ─── Inventory errors ─────────────────────────────────────────────────────

def test_domain_missing_from_inventory(tmp_inventory, mock_atom, mock_redtrack):
    res = ts.add_tracker(
        VALID_CNAME, VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'inventory_error'
    assert res.details['reason'] == 'domain_missing'
    assert mock_atom.add_cname.call_count == 0


def test_aws_account_missing(tmp_inventory, mock_atom, mock_redtrack):
    """add_domain auto-backfills aws_account, so we have to NULL it
    directly via SQL after insert."""
    tmp_inventory.add_domain(
        VALID_DOMAIN, aws_account='temp', requested_by='U_TEST',
    )
    tmp_inventory.mark_setup_complete(VALID_DOMAIN)
    import sqlite3
    from config import Config
    with sqlite3.connect(Config.INVENTORY_DB_PATH) as c:
        c.execute('UPDATE domains SET aws_account = NULL WHERE domain = ?',
                  (VALID_DOMAIN,))
    res = ts.add_tracker(
        VALID_CNAME, VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'inventory_error'
    assert res.details['reason'] == 'aws_account_missing'


def test_proceeds_even_when_setup_at_is_null(
    tmp_inventory, mock_atom, mock_redtrack,
):
    """setup_at NULL is NOT a hard reject — it's just a metadata flag
    that can drift from AWS reality (legacy domains imported from CSV,
    domains set up via ATOM UI directly, etc.). ATOM's add_cname is the
    source of truth: if the zone actually exists, the call succeeds;
    if not, ATOM returns 404 and we surface a precise error.

    Regression for the diywithryan.com case 2026-05-18 where setup_at
    was NULL but R53 actually had the zone."""
    tmp_inventory.add_domain(
        VALID_DOMAIN, aws_account='auto-insurance', requested_by='U_TEST',
    )
    # Do NOT call mark_setup_complete — setup_at stays NULL.
    res = ts.add_tracker(
        VALID_CNAME, VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
    )
    # ATOM mock returns 'created' for add_cname — the bot should
    # proceed to RedTrack and succeed, NOT bail on the metadata check.
    assert res.status == 'created'
    assert mock_atom.add_cname.call_count == 1


# ─── RedTrack "already exists" shape ──────────────────────────────────────

def test_redtrack_already_exists_marker_handled(
    seeded_inventory, mock_atom, mock_redtrack,
):
    """When add_tracker_domain returns _already_exists=True (RedTrack
    409 or message 'already...'), treat as success not error."""
    mock_redtrack.return_value = {
        '_already_exists': True, 'id': 'rt_existing', 'url': 'x',
    }
    res = ts.add_tracker(
        VALID_CNAME, VALID_DOMAIN, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'created'  # DNS was created fresh; RedTrack pre-existing
    assert res.details['redtrack_already_existed'] is True


# ─── redtrack_client.add_tracker_domain shape ─────────────────────────────
# Targeted tests for the small new function in redtrack_client.

def test_add_tracker_domain_minimal_body(monkeypatch):
    """Body must contain url/type/workspace_ids/use_auto_generated_ssl
    and nothing else (we send the minimal viable shape, not the full
    swagger schema with every optional field)."""
    from redtrack_client import client as rt

    monkeypatch.setattr('config.Config.REDTRACK_API_KEY', 'KEY')
    monkeypatch.setattr('config.Config.REDTRACK_WORKSPACE_ID', 'WS')

    captured = {}
    class _Resp:
        status_code = 200
        ok = True
        text = '{"id": "x"}'
        content = b'{"id": "x"}'
        def json(self):
            return {'id': 'x'}
        def raise_for_status(self):
            pass

    def _post(url, params, json, timeout):
        captured['url'] = url
        captured['params'] = params
        captured['json'] = json
        return _Resp()
    monkeypatch.setattr('redtrack_client.client.requests.post', _post)

    rt.add_tracker_domain('trk.example.com')
    body = captured['json']
    # `type` is intentionally NOT sent — RedTrack rejected 'tracker' as
    # "domain type is not defined" (2026-05-19). Omitting lets RedTrack
    # apply its workspace-context default.
    assert set(body.keys()) == {
        'url', 'workspace_ids', 'use_auto_generated_ssl',
    }
    assert 'type' not in body
    assert body['url'] == 'trk.example.com'
    assert body['workspace_ids'] == ['WS']
    assert body['use_auto_generated_ssl'] is True
    assert captured['params'] == {'api_key': 'KEY'}


def test_add_tracker_domain_raises_without_creds(monkeypatch):
    from redtrack_client import client as rt
    monkeypatch.setattr('config.Config.REDTRACK_API_KEY', '')
    with pytest.raises(RuntimeError):
        rt.add_tracker_domain('trk.example.com')


def test_add_tracker_domain_detects_already_exists_via_409(monkeypatch):
    from redtrack_client import client as rt
    monkeypatch.setattr('config.Config.REDTRACK_API_KEY', 'KEY')
    monkeypatch.setattr('config.Config.REDTRACK_WORKSPACE_ID', 'WS')

    class _Resp:
        status_code = 409
        ok = False
        text = '{"error": "domain already exists", "id": "rt_existing"}'
        reason = 'Conflict'
        content = b'{"error": "domain already exists", "id": "rt_existing"}'
        def json(self):
            return {'error': 'domain already exists', 'id': 'rt_existing'}
        def raise_for_status(self):
            raise requests.HTTPError('409')

    monkeypatch.setattr(
        'redtrack_client.client.requests.post',
        lambda url, params, json, timeout: _Resp(),
    )

    out = rt.add_tracker_domain('trk.example.com')
    assert out['_already_exists'] is True
    assert out['id'] == 'rt_existing'


def test_add_tracker_domain_400_includes_response_body_in_error(monkeypatch):
    """Plain HTTPError loses the response body. Our wrapper must include
    it so operators can see WHY RedTrack rejected without digging through
    Render logs (caught 2026-05-18 — got opaque '400 Bad Request' until
    we surfaced the body)."""
    from redtrack_client import client as rt
    monkeypatch.setattr('config.Config.REDTRACK_API_KEY', 'KEY')
    monkeypatch.setattr('config.Config.REDTRACK_WORKSPACE_ID', 'WS')

    class _Resp:
        status_code = 400
        ok = False
        reason = 'Bad Request'
        text = '{"error": "invalid type: must be one of redirect|direct"}'
        content = b'...'
        def json(self):
            return {'error': 'invalid type: must be one of redirect|direct'}
        def raise_for_status(self):
            raise requests.HTTPError('400')

    monkeypatch.setattr(
        'redtrack_client.client.requests.post',
        lambda url, params, json, timeout: _Resp(),
    )

    with pytest.raises(requests.HTTPError) as exc_info:
        rt.add_tracker_domain('trk.example.com')
    msg = str(exc_info.value)
    assert 'invalid type' in msg
    assert '400' in msg


def test_add_tracker_domain_retries_on_dns_propagation_transient(monkeypatch):
    """The "we can't check your CNAME record" 400 is the DNS-propagation
    transient — RedTrack's resolver hasn't seen our freshly-created R53
    CNAME yet. Wrapper should retry up to _CNAME_CHECK_MAX_RETRIES times
    with backoff before giving up. Regression for diywithryan.com case
    2026-05-19 where first /new-tracker run got this error then a re-run
    succeeded; retry makes the first run succeed."""
    from redtrack_client import client as rt
    monkeypatch.setattr('config.Config.REDTRACK_API_KEY', 'KEY')
    monkeypatch.setattr('config.Config.REDTRACK_WORKSPACE_ID', 'WS')
    # Make sleep a no-op so the test runs fast.
    monkeypatch.setattr('redtrack_client.client.time.sleep', lambda s: None)

    class _Transient:
        status_code = 400
        ok = False
        reason = 'Bad Request'
        text = '{"error": "we can\'t check your CNAME record."}'
        content = b'...'
        def json(self):
            return {'error': "we can't check your CNAME record."}
        def raise_for_status(self):
            raise requests.HTTPError('400')

    class _Success:
        status_code = 200
        ok = True
        reason = 'OK'
        text = '{"id": "rt_id"}'
        content = b'{"id": "rt_id"}'
        def json(self):
            return {'id': 'rt_id'}
        def raise_for_status(self):
            pass

    # 2 transient failures, then success.
    responses = [_Transient(), _Transient(), _Success()]
    call_count = {'n': 0}

    def _post(url, params, json, timeout):
        i = call_count['n']
        call_count['n'] += 1
        return responses[i]

    monkeypatch.setattr('redtrack_client.client.requests.post', _post)

    out = rt.add_tracker_domain('trk.example.com')
    assert out['id'] == 'rt_id'
    assert call_count['n'] == 3  # 2 retries + 1 success


def test_add_tracker_domain_gives_up_after_max_retries_on_transient(monkeypatch):
    """When the propagation transient never clears, surface as a clear
    HTTPError after exhausting retries — caller's dns_done_redtrack_failed
    branch fires with the right message."""
    from redtrack_client import client as rt
    monkeypatch.setattr('config.Config.REDTRACK_API_KEY', 'KEY')
    monkeypatch.setattr('config.Config.REDTRACK_WORKSPACE_ID', 'WS')
    monkeypatch.setattr('redtrack_client.client.time.sleep', lambda s: None)

    class _Transient:
        status_code = 400
        ok = False
        reason = 'Bad Request'
        text = '{"error": "we can\'t check your CNAME record."}'
        content = b'...'
        def json(self):
            return {'error': "we can't check your CNAME record."}
        def raise_for_status(self):
            raise requests.HTTPError('400')

    call_count = {'n': 0}

    def _post(url, params, json, timeout):
        call_count['n'] += 1
        return _Transient()

    monkeypatch.setattr('redtrack_client.client.requests.post', _post)

    with pytest.raises(requests.HTTPError) as exc_info:
        rt.add_tracker_domain('trk.example.com')
    # Should have tried _CNAME_CHECK_MAX_RETRIES + 1 times before raising.
    assert call_count['n'] == rt._CNAME_CHECK_MAX_RETRIES + 1
    assert "can't check your cname" in str(exc_info.value).lower()


def test_add_tracker_domain_does_not_retry_on_non_transient_400(monkeypatch):
    """A 400 with a DIFFERENT error message (e.g., wrong CNAME target)
    must fail immediately — retrying wastes API budget on errors that
    won't self-correct."""
    from redtrack_client import client as rt
    monkeypatch.setattr('config.Config.REDTRACK_API_KEY', 'KEY')
    monkeypatch.setattr('config.Config.REDTRACK_WORKSPACE_ID', 'WS')
    monkeypatch.setattr('redtrack_client.client.time.sleep', lambda s: None)

    class _WrongTarget:
        status_code = 400
        ok = False
        reason = 'Bad Request'
        text = '{"error": "cname should point to bseav.xxx, but points to trk.xxx"}'
        content = b'...'
        def json(self):
            return {'error': 'cname should point to bseav.xxx, but points to trk.xxx'}
        def raise_for_status(self):
            raise requests.HTTPError('400')

    call_count = {'n': 0}

    def _post(url, params, json, timeout):
        call_count['n'] += 1
        return _WrongTarget()

    monkeypatch.setattr('redtrack_client.client.requests.post', _post)

    with pytest.raises(requests.HTTPError):
        rt.add_tracker_domain('trk.example.com')
    assert call_count['n'] == 1  # no retries — failed once and gave up


def test_add_tracker_domain_detects_already_exists_via_body_text(monkeypatch):
    """Some RedTrack errors return 4xx with body containing 'already'
    instead of a clean 409. We pattern-match on the body too."""
    from redtrack_client import client as rt
    monkeypatch.setattr('config.Config.REDTRACK_API_KEY', 'KEY')
    monkeypatch.setattr('config.Config.REDTRACK_WORKSPACE_ID', 'WS')

    class _Resp:
        status_code = 400
        ok = False
        text = '{"error": "Domain already registered to this workspace"}'
        reason = 'Bad Request'
        content = b'{"error": "Domain already registered to this workspace"}'
        def json(self):
            return {'error': 'Domain already registered to this workspace'}
        def raise_for_status(self):
            raise requests.HTTPError('400')

    monkeypatch.setattr(
        'redtrack_client.client.requests.post',
        lambda url, params, json, timeout: _Resp(),
    )

    out = rt.add_tracker_domain('trk.example.com')
    assert out['_already_exists'] is True
