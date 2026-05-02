"""Integration tests for orchestrator.atom_client.AtomClient.

Requires ATOM to be running locally on port 5500. The fixtures in conftest.py
auto-skip if it isn't.

These tests prove the HTTP contract between this orchestrator and ATOM —
including that the structured-error feature (built earlier in the ATOM repo)
flows through correctly to consumers like this one.
"""
import time
import pytest


pytestmark = pytest.mark.integration


def test_health_returns_status_healthy(client):
    """Anonymous /api/health endpoint returns the expected shape."""
    data = client.health()
    assert data['status'] == 'healthy'


def test_login_succeeds_with_test_credentials(client):
    """We can log in with the local-dev sunny/test123 credentials."""
    assert client.login('sunny', 'test123') is True


def test_check_existing_returns_resources_shape(logged_in_client):
    """check_existing returns a dict with a 'resources' key for any domain.

    For a fresh test domain that doesn't exist in AWS, every resource subkey
    should report exists=False.
    """
    domain = 'test-anand-doesnt-exist.com'
    data = logged_in_client.check_existing(domain)

    assert data['domain'] == domain
    assert 'resources' in data
    assert 'cloudfront' in data['resources']
    assert 'route53' in data['resources']
    assert 's3_main' in data['resources']
    assert 's3_www' in data['resources']
    # All four should report no existing resources.
    assert data['resources']['route53']['exists'] is False
    assert data['resources']['s3_main']['exists'] is False
    assert data['resources']['s3_www']['exists'] is False


def test_setup_domain_returns_task_id(logged_in_client):
    """Kicking off a domain setup returns the task id immediately
    (work happens in a background thread on ATOM's side)."""
    response = logged_in_client.setup_domain('test-from-pytest.com')

    assert 'tasks' in response
    assert len(response['tasks']) == 1
    task = response['tasks'][0]
    assert task['domain'] == 'test-from-pytest.com'
    assert 'task_id' in task and len(task['task_id']) > 0


def test_status_eventually_returns_structured_failure(logged_in_client):
    """End-to-end: kicks off setup with bad AWS creds, polls until it fails,
    asserts the structured-error fields are present.

    This is the test that validates BOTH:
      • the AtomClient HTTP wrapper, AND
      • the structured-error feature shipped in the ATOM repo
    work end-to-end together.
    """
    response = logged_in_client.setup_domain('test-failure-pytest.com')
    task_id = response['tasks'][0]['task_id']

    # Poll up to ~30s. With empty/fake AWS creds in .env, it fails fast.
    deadline = time.time() + 30
    final = None
    while time.time() < deadline:
        s = logged_in_client.status(task_id)
        if s.get('status') in ('completed', 'failed'):
            final = s
            break
        time.sleep(1)

    assert final is not None, 'status never resolved within 30s'
    assert final['status'] == 'failed', f"expected failed, got {final['status']}"

    # Structured-error fields shipped in the ATOM error-panel feature
    assert 'failed_at_step' in final
    assert 'completed_steps' in final
    err = final['error']
    assert isinstance(err, dict), 'error should be a structured dict, not a string'
    assert err['step_key'] == 'certificate'
    assert 'message' in err
    assert 'exception' in err
