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
    # Live URL must include the source folder path — the lander lives at
    # /lander-v3/, not at the apex (post-2026-05-06 false-positive fix).
    assert result.details['live_url'] == 'https://owned-by-us.com/lander-v3/'
    assert 'https://owned-by-us.com/lander-v3/' in result.message
    client.setup_domain.assert_called_once()
    client.copy_files.assert_called_once()

    # Inventory record should be marked complete AND lander_url persisted.
    record = tmp_inventory.get_domain('owned-by-us.com')
    assert record['setup_at'] is not None
    assert record['lander_url'] == 'https://owned-by-us.com/lander-v3/'


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


def test_workflow_fails_loud_when_aws_account_missing(tmp_inventory):
    """Audit #6 fix 2026-05-08: a row with NULL/empty aws_account
    used to silently default to 'auto-insurance' inside workflow.py.
    That hid bugs where rows for non-auto-insurance verticals routed
    to the wrong AWS account. Now we refuse to proceed and return a
    clear error instead.
    """
    # Insert a row WITHOUT calling init_db's backfill so aws_account
    # stays NULL — simulating a row inserted by someone bypassing the
    # bot's add_domain (which requires status etc.).
    with tmp_inventory._conn() as c:
        tmp_inventory._execute(
            c,
            'INSERT INTO domains (domain, vertical) VALUES (?, ?)',
            ('null-account.com', 'medicare'),
        )

    client = MagicMock()
    req = ExistingDomainRequest(
        target_domain='null-account.com',
        source_account='auto-insurance',
        source_bucket='lander-source.com',
        source_folders=['lander-v3/'],
    )
    result = run_existing_domain_workflow(req, client=client)

    assert result.status == 'failed'
    assert result.details['reason'] == 'aws_account_missing'
    # Workflow must not have called ATOM at all.
    client.setup_domain.assert_not_called()
    client.copy_files.assert_not_called()


def test_workflow_transitions_to_deployed_on_success(tmp_inventory):
    """Phase 7 worker stamps STATUS_DEPLOYING then STATUS_DEPLOYED so
    /list-domains can show in-flight state without polling Slack."""
    tmp_inventory.add_domain(
        domain='happy-path.com', aws_account='auto-insurance',
        status=tmp_inventory.STATUS_PENDING,
    )
    client = MagicMock()
    client.setup_domain.return_value = _ok_setup_response()
    client.wait_for_setup.return_value = _completed_status()
    client.copy_files.return_value = {'message': 'copied 1 files'}

    req = ExistingDomainRequest(
        target_domain='happy-path.com',
        source_account='auto-insurance',
        source_bucket='src.com',
        source_folders=['lander-v1/'],
    )
    result = run_existing_domain_workflow(req, client=client)
    assert result.status == 'completed'
    row = tmp_inventory.get_domain('happy-path.com')
    assert row['status'] == tmp_inventory.STATUS_DEPLOYED
    # task_id from _ok_setup_response should be persisted.
    assert row['latest_task_id'] is not None
    assert row['latest_error'] is None


def test_workflow_transitions_to_failed_with_error_on_atom_setup_failure(
    tmp_inventory,
):
    """When ATOM reports a non-completed status, the row is marked
    STATUS_FAILED and the error message is captured in latest_error so
    operators can inspect failure cause without scrolling Slack."""
    tmp_inventory.add_domain(
        domain='will-fail.com', aws_account='auto-insurance',
        status=tmp_inventory.STATUS_PENDING,
    )
    client = MagicMock()
    client.setup_domain.return_value = _ok_setup_response()
    client.wait_for_setup.return_value = _failed_status_at('cloudfront')

    req = ExistingDomainRequest(
        target_domain='will-fail.com',
        source_account='auto-insurance',
        source_bucket='src.com',
        source_folders=['lander-v1/'],
    )
    result = run_existing_domain_workflow(req, client=client)
    assert result.status == 'failed'
    row = tmp_inventory.get_domain('will-fail.com')
    assert row['status'] == tmp_inventory.STATUS_FAILED
    assert 'cloudfront' in row['latest_error']
    assert row['latest_task_id'] is not None


def test_workflow_uses_configured_phase7_timeout(tmp_inventory, monkeypatch):
    """Audit fix 2026-05-08: timeout was hardcoded to 600s, too short
    for fresh-domain ACM cert validation. Now read from
    Config.PHASE7_SETUP_TIMEOUT_SEC so ops can lengthen without a code
    change. Default must remain >= 1800 sec (30 min).
    """
    from config import Config
    # Confirm default is generous enough for fresh-domain runs.
    assert Config.PHASE7_SETUP_TIMEOUT_SEC >= 1800

    tmp_inventory.add_domain(
        domain='timeout-config.com', aws_account='auto-insurance',
    )
    monkeypatch.setattr(Config, 'PHASE7_SETUP_TIMEOUT_SEC', 4242)

    captured_timeout = {}

    def fake_wait(task_id, timeout, *args, **kwargs):
        captured_timeout['value'] = timeout
        return _completed_status()

    client = MagicMock()
    client.setup_domain.return_value = _ok_setup_response()
    client.wait_for_setup.side_effect = fake_wait
    client.copy_files.return_value = {'message': 'copied 1 files'}

    req = ExistingDomainRequest(
        target_domain='timeout-config.com',
        source_account='auto-insurance',
        source_bucket='lander-source.com',
        source_folders=['lander-v3/'],
    )
    run_existing_domain_workflow(req, client=client)

    assert captured_timeout['value'] == 4242


def test_returns_failed_when_copy_files_reports_zero_files(tmp_inventory):
    """ATOM returns 200 OK with 'Successfully copied 0 files from ...' when
    the source path is empty. This was a Path A false-positive bug — the
    bot used to report 'deployed' on a no-op. Must now fail loudly.
    """
    tmp_inventory.add_domain(
        domain='copy-zero.com', aws_account='auto-insurance')
    client = MagicMock()
    client.setup_domain.return_value = _ok_setup_response()
    client.wait_for_setup.return_value = _completed_status()
    client.copy_files.return_value = {
        'message': 'Successfully copied 0 files from empty-source.com to copy-zero.com'
    }

    req = ExistingDomainRequest(
        target_domain='copy-zero.com',
        source_account='auto-insurance',
        source_bucket='empty-source.com',
        source_folders=['nonexistent-folder/'],
    )
    result = run_existing_domain_workflow(req, client=client)

    assert result.status == 'failed'
    assert result.details['reason'] == 'copy_files_zero'
    # Inventory must NOT be marked complete on a 0-file no-op
    assert tmp_inventory.get_domain('copy-zero.com')['setup_at'] is None


# ─── suggest_new_domains (Path B step 1) ──────────────────────────────────

def test_suggest_returns_exactly_count_in_stub_mode(monkeypatch):
    """Stub fallback: returns exactly `count` items, all available, with
    fake prices under the per-extension cap."""
    monkeypatch.setattr(Config, 'OPENAI_API_KEY', '')
    monkeypatch.setattr(Config, 'NAMECHEAP_API_USER', '')

    out = suggest_new_domains(
        vertical='auto-insurance',
        audience='seniors looking for medigap',
        extension='.com',
        count=5,
    )
    assert len(out) == 5
    for entry in out:
        assert entry['available'] is True
        assert entry['price'] is not None
        # Price must respect the .com cap ($15) per Config
        assert entry['price'] <= 15.0


def test_suggest_filters_taken_domains(monkeypatch):
    """Only available + price-capped domains come through."""
    monkeypatch.setattr(Config, 'OPENAI_API_KEY', '')

    # 12 candidates, only the first 5 are "available", rest are taken
    def fake_check_avail_price(domains, extension):
        results = []
        for i, d in enumerate(domains):
            results.append({
                'domain': d,
                'available': i < 5,
                'price': 9.99,
            })
        return results
    monkeypatch.setattr(
        'orchestrator.workflow.namecheap_check.check_availability_and_price',
        fake_check_avail_price,
    )

    out = suggest_new_domains(
        vertical='x', audience="", extension='.com', count=5,
    )
    assert len(out) == 5
    assert all(r['available'] for r in out)


def test_suggest_filters_overpriced_domains(monkeypatch):
    """Available but priced above the .com cap of $15 → excluded."""
    monkeypatch.setattr(Config, 'OPENAI_API_KEY', '')

    def fake_check_avail_price(domains, extension):
        # Half are at $9 (under cap), half at $50 (premium - over cap)
        return [
            {
                'domain': d,
                'available': True,
                'price': 9.0 if i % 2 == 0 else 50.0,
            }
            for i, d in enumerate(domains)
        ]
    monkeypatch.setattr(
        'orchestrator.workflow.namecheap_check.check_availability_and_price',
        fake_check_avail_price,
    )

    out = suggest_new_domains(
        vertical='x', audience="", extension='.com', count=5,
    )
    # Every returned domain is under the cap
    for r in out:
        assert r['price'] <= 15.0


def test_suggest_uses_stricter_cap_for_non_com_extensions(monkeypatch):
    """`.pro` cap is $5, not $15. A $9 .pro domain should be filtered out."""
    monkeypatch.setattr(Config, 'OPENAI_API_KEY', '')

    def fake_check_avail_price(domains, extension):
        return [
            {'domain': d, 'available': True, 'price': 9.0}
            for d in domains
        ]
    monkeypatch.setattr(
        'orchestrator.workflow.namecheap_check.check_availability_and_price',
        fake_check_avail_price,
    )

    out = suggest_new_domains(
        vertical='x', audience="", extension='.pro', count=5,
    )
    # All candidates priced $9, .pro cap is $5, nothing qualifies
    assert out == []


def test_suggest_excludes_unknown_price(monkeypatch):
    """If Namecheap doesn't return a price (creds missing / API failure),
    those domains are excluded — we can't confirm they're under the cap."""
    monkeypatch.setattr(Config, 'OPENAI_API_KEY', '')

    def fake_check_avail_price(domains, extension):
        return [
            {'domain': d, 'available': True, 'price': None}
            for d in domains
        ]
    monkeypatch.setattr(
        'orchestrator.workflow.namecheap_check.check_availability_and_price',
        fake_check_avail_price,
    )

    out = suggest_new_domains(
        vertical='x', audience="", extension='.com', count=5,
    )
    assert out == []


def test_suggest_normalises_extension_without_dot(monkeypatch):
    """User passes 'pro' instead of '.pro' — we still produce .pro names."""
    monkeypatch.setattr(Config, 'OPENAI_API_KEY', '')
    monkeypatch.setattr(Config, 'NAMECHEAP_API_USER', '')

    out = suggest_new_domains(
        vertical='x', audience="", extension='pro', count=2,
    )
    for r in out:
        assert r['domain'].endswith('.pro')


def test_suggest_rejects_empty_vertical():
    with pytest.raises(ValueError):
        suggest_new_domains(
            vertical='', audience="", extension='.com', count=5,
        )
