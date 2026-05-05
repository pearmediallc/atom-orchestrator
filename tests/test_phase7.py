"""Phase 7 tests — Mark Done click triggers ATOM setup_domain.

The Slack handlers spawn a background thread that calls
run_existing_domain_workflow. These tests exercise the worker
function directly with a mocked Slack WebClient and a monkey-patched
workflow, asserting:
  • progress + completion messages are posted
  • DMs go to the requester on success/failure
  • missing-bucket-config short-circuits with a clear warning
  • the worker doesn't blow up if the workflow itself raises
"""
from unittest.mock import MagicMock
import pytest

from config import Config
from orchestrator.workflow import WorkflowResult
from slack_bot.routes import _phase7_run_atom_setup, _parse_lander_url


# ─── Helpers ───────────────────────────────────────────────────────────────

def _slack_client():
    """A MagicMock that mimics slack_sdk.WebClient — chat_postMessage etc."""
    return MagicMock(name='slack_client')


def _all_text(client) -> str:
    """Concatenated text of every chat_postMessage call. Lets us assert on
    'somewhere in the messages we mentioned X' without coupling to call order.
    """
    return '\n'.join(
        (c.kwargs.get('text') or '')
        for c in client.chat_postMessage.call_args_list
    )


def _set_default_bucket(monkeypatch, bucket: str = 'lander-source-default'):
    monkeypatch.setattr(Config, 'PHASE7_DEFAULT_SOURCE_BUCKET', bucket)
    monkeypatch.setattr(Config, 'PHASE7_DEFAULT_SOURCE_FOLDERS', ['lander/'])
    monkeypatch.setattr(Config, 'PHASE7_DEFAULT_SOURCE_ACCOUNT', 'auto-insurance')
    monkeypatch.setattr(Config, 'PHASE7_LANDER_DEFAULTS', {})


def _patch_workflow(monkeypatch, result: WorkflowResult):
    """Replace run_existing_domain_workflow with a stub that returns `result`."""
    captured = {}

    def fake_workflow(req):
        captured['req'] = req
        return result

    monkeypatch.setattr(
        'slack_bot.routes.run_existing_domain_workflow', fake_workflow,
    )
    return captured


# ─── Tests ─────────────────────────────────────────────────────────────────

def test_worker_warns_when_no_url_and_no_default_bucket(monkeypatch):
    """No lander URL + no default bucket → clear warning, abort."""
    monkeypatch.setattr(Config, 'PHASE7_DEFAULT_SOURCE_BUCKET', '')
    monkeypatch.setattr(Config, 'PHASE7_DEFAULT_SOURCE_FOLDERS', [])
    monkeypatch.setattr(Config, 'PHASE7_LANDER_DEFAULTS', {})

    client = _slack_client()
    _phase7_run_atom_setup(
        client=client, channel='C1', message_ts='123.45',
        target_domain='example.com', vertical='auto-insurance',
        requester='U_REQUESTER',
        lander_url='',
    )

    assert client.chat_postMessage.call_count == 1
    msg = _all_text(client)
    assert 'cannot deploy' in msg.lower()


def test_worker_uses_url_derived_bucket_and_folder(monkeypatch):
    """Happy path — kickoff + completion + DM, source pulled from URL."""
    _set_default_bucket(monkeypatch)  # set defaults so we can verify URL wins
    captured = _patch_workflow(monkeypatch, WorkflowResult(
        status='completed',
        message='Lander deployed. Live at https://example.com',
        details={'live_url': 'https://example.com'},
    ))

    client = _slack_client()
    _phase7_run_atom_setup(
        client=client, channel='C1', message_ts='123.45',
        target_domain='example.com', vertical='auto-insurance',
        requester='U_REQUESTER',
        lander_url='https://safetyfirstauto.pro/h-insure-c/',
    )

    assert client.chat_postMessage.call_count == 3
    text = _all_text(client)
    assert 'Triggering ATOM setup' in text
    assert 'parsed from lander URL' in text   # the new origin label
    assert 'ATOM finished' in text

    # URL-derived source overrides config defaults
    req = captured['req']
    assert req.source_bucket == 'safetyfirstauto.pro'
    assert req.source_folders == ['h-insure-c/']
    assert req.requested_by == 'Slack:U_REQUESTER'

    dm_calls = [
        c for c in client.chat_postMessage.call_args_list
        if c.kwargs.get('channel') == 'U_REQUESTER'
    ]
    assert len(dm_calls) == 1
    assert 'fully deployed' in (dm_calls[0].kwargs.get('text') or '')


def test_worker_falls_back_to_config_defaults_when_no_url(monkeypatch):
    """Empty URL but valid config default → workflow still runs using defaults."""
    _set_default_bucket(monkeypatch)
    captured = _patch_workflow(monkeypatch, WorkflowResult(
        status='completed', message='ok',
        details={'live_url': 'https://example.com'},
    ))
    client = _slack_client()
    _phase7_run_atom_setup(
        client=client, channel='C1', message_ts='123.45',
        target_domain='example.com', vertical='auto-insurance',
        requester='U_REQUESTER',
        lander_url='',
    )
    req = captured['req']
    # Falls back to the config defaults
    assert req.source_bucket == 'lander-source-default'
    assert req.source_folders == ['lander/']
    text = _all_text(client)
    assert 'config defaults' in text


def test_worker_reports_failure_with_failed_step(monkeypatch):
    """Workflow reports failure → thread + DM both name the failed step."""
    _set_default_bucket(monkeypatch)
    _patch_workflow(monkeypatch, WorkflowResult(
        status='failed',
        message="ATOM domain setup failed at step 'cloudfront'.",
        details={
            'reason': 'atom_setup_failed',
            'setup_result': {
                'failed_at_step': 'cloudfront',
                'error': {'aws_error_code': 'InvalidViewerCertificate'},
            },
        },
    ))

    client = _slack_client()
    _phase7_run_atom_setup(
        client=client, channel='C1', message_ts='123.45',
        target_domain='will-fail.com', vertical='auto-insurance',
        requester='U_REQUESTER',
        lander_url='https://safetyfirstauto.pro/h-insure-c/',
    )

    text = _all_text(client)
    assert 'ATOM workflow failed' in text
    assert 'cloudfront' in text

    # Requester DM mentions the failure
    dm_calls = [
        c for c in client.chat_postMessage.call_args_list
        if c.kwargs.get('channel') == 'U_REQUESTER'
    ]
    assert len(dm_calls) == 1
    assert 'did not complete' in (dm_calls[0].kwargs.get('text') or '')


def test_worker_recovers_from_workflow_exception(monkeypatch):
    """If run_existing_domain_workflow raises, the worker shouldn't crash —
    it should post a thread error + DM the requester.
    """
    _set_default_bucket(monkeypatch)

    def raising_workflow(req):
        raise RuntimeError('atom went poof')

    monkeypatch.setattr(
        'slack_bot.routes.run_existing_domain_workflow', raising_workflow,
    )

    client = _slack_client()
    # Must not raise
    _phase7_run_atom_setup(
        client=client, channel='C1', message_ts='123.45',
        target_domain='boom.com', vertical='auto-insurance',
        requester='U_REQUESTER',
        lander_url='https://safetyfirstauto.pro/h-insure-c/',
    )

    text = _all_text(client)
    assert 'crashed' in text.lower()
    assert 'atom went poof' in text


def test_worker_uses_per_vertical_override_when_no_url(monkeypatch):
    """No URL passed → per-vertical config wins over the global default."""
    monkeypatch.setattr(Config, 'PHASE7_DEFAULT_SOURCE_BUCKET', 'global-default')
    monkeypatch.setattr(Config, 'PHASE7_DEFAULT_SOURCE_FOLDERS', ['default/'])
    monkeypatch.setattr(Config, 'PHASE7_DEFAULT_SOURCE_ACCOUNT', 'auto-insurance')
    monkeypatch.setattr(Config, 'PHASE7_LANDER_DEFAULTS', {
        'medicare': {
            'source_account': 'other-vertical',
            'source_bucket': 'medicare-special-bucket',
            'source_folders': ['v2-lander/'],
        },
    })

    captured = _patch_workflow(monkeypatch, WorkflowResult(
        status='completed', message='ok', details={'live_url': 'https://m.com'},
    ))

    client = _slack_client()
    _phase7_run_atom_setup(
        client=client, channel='C1', message_ts='123.45',
        target_domain='m.com', vertical='medicare',
        requester='U_REQUESTER',
        lander_url='',  # no URL → fall back to config
    )

    req = captured['req']
    assert req.source_bucket == 'medicare-special-bucket'
    assert req.source_account == 'other-vertical'
    assert req.source_folders == ['v2-lander/']


# ─── _parse_lander_url ────────────────────────────────────────────────

@pytest.mark.parametrize('url,want_bucket,want_folders', [
    ('https://safetyfirstauto.pro/h-insure-c/',  'safetyfirstauto.pro', ['h-insure-c/']),
    ('https://safetyfirstauto.pro/h-insure-c',   'safetyfirstauto.pro', ['h-insure-c/']),
    ('http://example.com/lander-v3/',            'example.com',         ['lander-v3/']),
    ('https://abc.com/nested/path/',             'abc.com',             ['nested/path/']),
])
def test_parse_lander_url_happy_paths(url, want_bucket, want_folders):
    bucket, folders, err = _parse_lander_url(url)
    assert err is None
    assert bucket == want_bucket
    assert folders == want_folders


@pytest.mark.parametrize('url,err_substr', [
    ('',                                'empty'),
    ('https://abc.com/',                'missing a folder path'),
    ('https://abc.com',                 'missing a folder path'),
    ('abc.com/lander/',                 'must start with https'),  # no scheme
    ('ftp://abc.com/lander/',           'must start with https'),
])
def test_parse_lander_url_failure_modes(url, err_substr):
    bucket, folders, err = _parse_lander_url(url)
    assert bucket == ''
    assert folders == []
    assert err and err_substr.lower() in err.lower()


def test_phase7_defaults_for_falls_back_to_global():
    """Helper sanity check: unknown vertical → global defaults."""
    Config.PHASE7_LANDER_DEFAULTS = {'medicare': {'source_bucket': 'm-bucket'}}
    Config.PHASE7_DEFAULT_SOURCE_BUCKET = 'g-bucket'
    Config.PHASE7_DEFAULT_SOURCE_FOLDERS = ['g/']
    Config.PHASE7_DEFAULT_SOURCE_ACCOUNT = 'g-account'

    out = Config.phase7_defaults_for('unknown-vertical')
    assert out['source_bucket'] == 'g-bucket'
    assert out['source_folders'] == ['g/']
    assert out['source_account'] == 'g-account'

    out2 = Config.phase7_defaults_for('medicare')
    assert out2['source_bucket'] == 'm-bucket'
