"""Unit tests for orchestrator.workflow.

These tests use a mocked AtomClient (so they're FAST and don't need ATOM
running). The integration tests in test_atom_client.py already prove the
real client works against a real ATOM. Here we prove the workflow layer
correctly composes the client + inventory store.
"""
from unittest.mock import MagicMock
import pytest

from orchestrator.workflow import (
    ExistingDomainRequest,
    run_existing_domain_workflow,
    suggest_new_domains,
)
from config import Config


def _ok_setup_response(task_id='abc-task'):
    return {'tasks': [{'task_id': task_id, 'domain': 'x.com'}]}


def _completed_status():
    return {
        'status': 'completed',
        'failed_at_step': None,
        'completed_steps': ['certificate', 'route53_zone',
                            's3_buckets', 'cloudfront'],
    }


def _failed_status_at(step='cloudfront'):
    return {
        'status': 'failed',
        'failed_at_step': step,
        'completed_steps': ['certificate', 'route53_zone', 's3_buckets'],
        'error': {
            'step_key': step,
            'aws_error_code': 'InvalidViewerCertificate',
            'message': 'cert is not in us-east-1',
        },
    }


def test_returns_failed_when_target_not_in_inventory(tmp_inventory):
    """Inventory miss → workflow rejects before any ATOM calls."""
    client = MagicMock()
    req = ExistingDomainRequest(
        target_domain='nope.com',
        source_account='auto-insurance',
        source_bucket='lander-source',
        source_folders=['lander-v3/'],
    )
    result = run_existing_domain_workflow(req, client=client)

    assert result.status == 'failed'
    assert 'not in our inventory' in result.message.lower()
    assert result.details['reason'] == 'not_in_inventory'
    # Should never have touched ATOM
    client.setup_domain.assert_not_called()
    client.copy_files.assert_not_called()


def test_completes_when_setup_and_copy_both_succeed(tmp_inventory):
    """Happy path — setup completes, copy completes, inventory marked."""
    tmp_inventory.add_domain(
        domain='owned-by-us.com',
        vertical='auto-insurance',
        aws_account='auto-insurance',
    )
    client = MagicMock()
    client.setup_domain.return_value = _ok_setup_response()
    client.wait_for_setup.return_value = _completed_status()
    client.copy_files.return_value = {'message': 'copied 7 files'}

    req = ExistingDomainRequest(
        target_domain='owned-by-us.com',
        source_account='auto-insurance',
        source_bucket='lander-source.com',
        source_folders=['lander-v3/'],
    )
    result = run_existing_domain_workflow(req, client=client)

    assert result.status == 'completed'
    assert 'https://owned-by-us.com' in result.message
    assert result.details['live_url'] == 'https://owned-by-us.com'
    client.setup_domain.assert_called_once()
    client.copy_files.assert_called_once()

    # Inventory record should be marked complete
    record = tmp_inventory.get_domain('owned-by-us.com')
    assert record['setup_at'] is not None


def test_returns_failed_when_atom_setup_fails(tmp_inventory):
    """ATOM setup fails → workflow forwards the structured error info."""
    tmp_inventory.add_domain(
        domain='will-fail.com', aws_account='auto-insurance')
    client = MagicMock()
    client.setup_domain.return_value = _ok_setup_response()
    client.wait_for_setup.return_value = _failed_status_at('cloudfront')

    req = ExistingDomainRequest(
        target_domain='will-fail.com',
        source_account='auto-insurance',
        source_bucket='lander-source.com',
        source_folders=['lander-v3/'],
    )
    result = run_existing_domain_workflow(req, client=client)

    assert result.status == 'failed'
    assert "'cloudfront'" in result.message
    assert result.details['reason'] == 'atom_setup_failed'
    # The full ATOM error structure should be preserved for debugging
    err = result.details['setup_result']['error']
    assert err['aws_error_code'] == 'InvalidViewerCertificate'
    # copy_files should never have been attempted after setup failure
    client.copy_files.assert_not_called()


def test_returns_failed_when_no_source_specified(tmp_inventory):
    """Setup completes but caller forgot to specify what to copy."""
    tmp_inventory.add_domain(
        domain='no-source.com', aws_account='auto-insurance')
    client = MagicMock()
    client.setup_domain.return_value = _ok_setup_response()
    client.wait_for_setup.return_value = _completed_status()

    req = ExistingDomainRequest(
        target_domain='no-source.com',
        source_account='auto-insurance',
        source_bucket='lander-source.com',
        source_folders=[],
        source_files=[],
    )
    result = run_existing_domain_workflow(req, client=client)

    assert result.status == 'failed'
    assert result.details['reason'] == 'no_source_specified'
    client.copy_files.assert_not_called()


def test_returns_failed_when_copy_files_reports_error(tmp_inventory):
    """copy_files returned a 4xx-style {error: ...} body — surface it."""
    tmp_inventory.add_domain(
        domain='copy-fail.com', aws_account='auto-insurance')
    client = MagicMock()
    client.setup_domain.return_value = _ok_setup_response()
    client.wait_for_setup.return_value = _completed_status()
    client.copy_files.return_value = {
        'error': "Cannot copy because the following folder(s) already exist"
    }

    req = ExistingDomainRequest(
        target_domain='copy-fail.com',
        source_account='auto-insurance',
        source_bucket='lander-source.com',
        source_folders=['lander-v3/'],
    )
    result = run_existing_domain_workflow(req, client=client)

    assert result.status == 'failed'
    assert result.details['reason'] == 'copy_files_error'
    # Inventory should NOT be marked complete on a copy failure
    assert tmp_inventory.get_domain('copy-fail.com')['setup_at'] is None


# ─── suggest_new_domains (Path B step 1) ──────────────────────────────────

def test_suggest_returns_requested_count(monkeypatch):
    """Stub path: returns count items, all marked available."""
    monkeypatch.setattr(Config, 'OPENAI_API_KEY', '')
    monkeypatch.setattr(Config, 'NAMECHEAP_API_USER', '')

    out = suggest_new_domains(
        vertical='auto-insurance',
        example_domains=['cheaprates.com'],
        extension='.com',
        count=4,
    )
    assert len(out) == 4
    for entry in out:
        assert 'domain' in entry and 'available' in entry
        assert entry['available'] is True


def test_suggest_sorts_available_first(monkeypatch):
    """When availability mix is partial, available entries come first."""
    monkeypatch.setattr(Config, 'OPENAI_API_KEY', '')

    # Mix: even-indexed names available, odd-indexed taken
    def fake_check(domains):
        return {d: (i % 2 == 0) for i, d in enumerate(domains)}
    monkeypatch.setattr(
        'orchestrator.workflow.namecheap_check.check_availability',
        fake_check,
    )

    out = suggest_new_domains(
        vertical='x', example_domains=[], extension='.com', count=6,
    )
    available_flags = [r['available'] for r in out]
    # All Trues should appear before any Falses
    assert available_flags == sorted(available_flags, reverse=True)


def test_suggest_normalises_extension_without_dot(monkeypatch):
    """User passes 'pro' instead of '.pro' — we still produce .pro names."""
    monkeypatch.setattr(Config, 'OPENAI_API_KEY', '')
    monkeypatch.setattr(Config, 'NAMECHEAP_API_USER', '')

    out = suggest_new_domains(
        vertical='x', example_domains=[], extension='pro', count=2,
    )
    for r in out:
        assert r['domain'].endswith('.pro')


def test_suggest_rejects_empty_vertical():
    with pytest.raises(ValueError):
        suggest_new_domains(
            vertical='', example_domains=[], extension='.com', count=5,
        )
