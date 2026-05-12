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


def test_resolves_real_aws_account_and_self_heals_drifted_inventory(
        tmp_inventory, monkeypatch):
    """When inventory says auto-insurance but ATOM proves the bucket
    lives in other-vertical, the workflow must:
      1. use the REAL account for setup_domain / copy_files
      2. update the inventory row so next time we don't pay the lookup
      3. emit aws_account_drift_corrected log so /domain-history shows
         what happened

    Regression guard for the 2026-05-11 incident where 409 inventory
    rows had drifted aws_account values, causing silent AccessDenied
    storms on Mark Deployed.
    """
    from config import Config
    monkeypatch.setattr(
        Config, 'AWS_ACCOUNT_OPTIONS',
        ['auto-insurance', 'other-vertical'],
    )

    # Inventory thinks it's in auto-insurance.
    tmp_inventory.add_domain(
        domain='drifted.com', aws_account='auto-insurance',
    )
    # Mark setup_at so we hit the skip-setup + resolve branch.
    from inventory import store
    store.mark_setup_complete('drifted.com')

    client = MagicMock()
    # ATOM's reality: bucket lives in other-vertical, NOT auto-insurance.
    def fake_list_buckets(account_key):
        if account_key == 'other-vertical':
            return ['drifted.com', 'something-else.com']
        return ['unrelated-a.com', 'unrelated-b.com']
    client.list_buckets.side_effect = fake_list_buckets
    client.copy_files.return_value = {
        'message': 'Successfully copied 5 of 5 files from src to drifted.com',
        'succeeded_count': 5, 'failed_count': 0, 'total_count': 5,
    }

    req = ExistingDomainRequest(
        target_domain='drifted.com',
        source_account='auto-insurance',
        source_bucket='src',
        source_folders=['lander/'],
    )
    result = run_existing_domain_workflow(req, client=client)

    assert result.status == 'completed'
    # copy_files must have been called with the CORRECTED target_account.
    call_kwargs = client.copy_files.call_args.kwargs
    assert call_kwargs['target_account'] == 'other-vertical'
    assert call_kwargs['source_account'] == 'auto-insurance'
    # Inventory must have been self-healed.
    record = tmp_inventory.get_domain('drifted.com')
    assert record['aws_account'] == 'other-vertical'


def test_no_drift_no_inventory_update(tmp_inventory, monkeypatch):
    """When inventory and reality agree, don't touch the column —
    avoids spurious updated_at bumps and write traffic."""
    from config import Config
    monkeypatch.setattr(
        Config, 'AWS_ACCOUNT_OPTIONS',
        ['auto-insurance', 'other-vertical'],
    )
    tmp_inventory.add_domain(
        domain='aligned.com', aws_account='auto-insurance',
    )
    from inventory import store
    store.mark_setup_complete('aligned.com')
    original_updated_at = tmp_inventory.get_domain('aligned.com')['updated_at']

    client = MagicMock()
    client.list_buckets.return_value = ['aligned.com']  # both accounts say yes — caller picks first match
    client.copy_files.return_value = {
        'message': 'Successfully copied 1 of 1 files from src to aligned.com',
        'succeeded_count': 1, 'failed_count': 0, 'total_count': 1,
    }

    req = ExistingDomainRequest(
        target_domain='aligned.com',
        source_account='auto-insurance',
        source_bucket='src',
        source_folders=['lander/'],
    )
    result = run_existing_domain_workflow(req, client=client)
    assert result.status == 'completed'
    record = tmp_inventory.get_domain('aligned.com')
    assert record['aws_account'] == 'auto-insurance'
    # updated_at should be different ONLY because mark_setup_complete
    # bumped it — NOT because set_aws_account ran spuriously. We can't
    # easily separate the two timestamp writes; sanity check is that
    # the column value stayed the same.


def test_resolution_falls_back_to_inventory_when_atom_unreachable(
        tmp_inventory, monkeypatch):
    """If list_buckets fails for every account, we can't ground-truth.
    Don't crash — fall back to the inventory value and let the
    subsequent copy attempt surface any account mismatch as a normal
    AccessDenied failure."""
    from config import Config
    monkeypatch.setattr(
        Config, 'AWS_ACCOUNT_OPTIONS',
        ['auto-insurance', 'other-vertical'],
    )
    tmp_inventory.add_domain(
        domain='unreachable.com', aws_account='auto-insurance',
    )
    from inventory import store
    store.mark_setup_complete('unreachable.com')

    client = MagicMock()
    client.list_buckets.side_effect = RuntimeError('ATOM 503')
    client.copy_files.return_value = {
        'message': 'Successfully copied 1 of 1 files from src to unreachable.com',
        'succeeded_count': 1, 'failed_count': 0, 'total_count': 1,
    }

    req = ExistingDomainRequest(
        target_domain='unreachable.com',
        source_account='auto-insurance',
        source_bucket='src',
        source_folders=['lander/'],
    )
    result = run_existing_domain_workflow(req, client=client)

    assert result.status == 'completed'
    # Should have used the configured account because resolution
    # couldn't reach ATOM.
    assert client.copy_files.call_args.kwargs['target_account'] == 'auto-insurance'


def test_skips_atom_setup_when_setup_at_already_populated(tmp_inventory):
    """Re-deploying to a domain ATOM has already set up MUST NOT call
    setup_domain again. The bucket already exists, ACM cert already
    issued, etc. — re-running setup wastes 30-90s and can fail with
    AccessDenied on PutBucketWebsite when the bucket lives under
    different ownership than the local AWS creds.

    Regression guard for the 2026-05-11 incident where every Mark
    Deployed click on `mymedicareexperts.online` re-ran ATOM setup and
    failed at step `s3_buckets`. Fix: gate on the `setup_at` column,
    which mark_setup_complete() only writes after a successful run.
    """
    tmp_inventory.add_domain(
        domain='already-setup.com', aws_account='auto-insurance',
    )
    from inventory import store
    store.mark_setup_complete('already-setup.com')

    client = MagicMock()
    client.copy_files.return_value = {'message': 'copied 7 files'}

    req = ExistingDomainRequest(
        target_domain='already-setup.com',
        source_account='auto-insurance',
        source_bucket='lander-source.com',
        source_folders=['lander-v3/'],
    )
    result = run_existing_domain_workflow(req, client=client)

    assert result.status == 'completed'
    # setup_domain MUST be skipped; wait_for_setup MUST not be called
    # (no atom_task_id existed to wait on).
    client.setup_domain.assert_not_called()
    client.wait_for_setup.assert_not_called()
    # The lander still gets copied — that's the whole point of the
    # re-deploy.
    client.copy_files.assert_called_once()


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
    # AWS error code + ATOM's message should be folded into the human
    # summary so Slack DM + DB latest_error carry the real cause.
    assert 'InvalidViewerCertificate' in result.message
    assert 'cert is not in us-east-1' in result.message
    assert result.details['reason'] == 'atom_setup_failed'
    # The full ATOM error structure should be preserved for debugging
    err = result.details['setup_result']['error']
    assert err['aws_error_code'] == 'InvalidViewerCertificate'
    # copy_files should never have been attempted after setup failure
    client.copy_files.assert_not_called()


def test_workflow_failed_log_carries_structured_atom_error(tmp_inventory, caplog):
    """The `workflow_failed` log line must include the structured ATOM
    fields (failed_at_step, aws_error_code, atom_error_message) as
    top-level keys — that's the diagnostic surface operators grep on
    Render, where the in-process WorkflowResult is invisible.

    Regression guard for the 2026-05-11 incident where the log only
    carried "step 'unknown'" while the rich detail lived in an
    in-memory dict that never reached the logs.
    """
    import logging as _logging
    tmp_inventory.add_domain(
        domain='diag.com', aws_account='auto-insurance')
    client = MagicMock()
    client.setup_domain.return_value = _ok_setup_response()
    client.wait_for_setup.return_value = _failed_status_at('certificate')

    req = ExistingDomainRequest(
        target_domain='diag.com',
        source_account='auto-insurance',
        source_bucket='lander-source.com',
        source_folders=['lander-v3/'],
    )
    with caplog.at_level(_logging.ERROR, logger='orchestrator.workflow'):
        run_existing_domain_workflow(req, client=client)

    failed_records = [
        r for r in caplog.records
        if getattr(r, 'event', None) == 'workflow_failed'
    ]
    assert failed_records, 'no workflow_failed event was emitted'
    rec = failed_records[-1]
    assert rec.failed_at_step == 'certificate'
    assert rec.aws_error_code == 'InvalidViewerCertificate'
    assert rec.atom_error_message == 'cert is not in us-east-1'
    assert rec.completed_steps == ['certificate', 'route53_zone', 's3_buckets']


def test_setup_only_when_no_source_specified(tmp_inventory):
    """Caller didn't specify a lander source → setup-only run.

    This is the /new-domain modal "Lander URL blank" path: provision the
    AWS infrastructure (R53 zone, ACM cert, S3 bucket, CloudFront) and
    return success without copying any files. The inventory row gets
    marked setup_complete but lander_url stays NULL so /list-domains
    can render it as "provisioned, no lander yet" (audit 2026-05-11).

    Previously this was treated as a fatal error (reason=no_source_specified);
    that was the wrong default — the team often wants infra-only runs.
    """
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

    assert result.status == 'completed'
    assert result.details['setup_only'] is True
    assert result.details['live_url'] is None
    assert 'AWS infrastructure provisioned' in result.message
    # The whole point: copy_files must NOT be called.
    client.copy_files.assert_not_called()
    # Setup still ran (the actual provisioning).
    client.setup_domain.assert_called_once()
    # Row should be marked complete but with NO lander_url.
    record = tmp_inventory.get_domain('no-source.com')
    assert record['setup_at'] is not None
    assert record['lander_url'] is None


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


def test_atom_diagnosis_surfaced_in_failure_message_and_log(tmp_inventory, caplog):
    """ATOM (post 2026-05-13) attaches a structured `diagnosis` to its
    error response for known AWS error patterns
    (CNAMEAlreadyExists, InvalidViewerCertificate, NoSuchHostedZone).
    The workflow MUST surface the diagnosis fields so:
      • workflow_failed log line carries root_cause + summary + action
        as top-level keys (greppable in Render logs)
      • result.message prefers the diagnosis.summary over the raw
        AWS error text — operators see the actionable one-liner

    Regression guard: previously workflow.py only read
    error.{step_key, message, exception, aws_error_code} and dropped
    everything else, including ATOM's structured diagnosis.
    """
    import logging as _logging
    tmp_inventory.add_domain(
        domain='diag.com', aws_account='auto-insurance')

    client = MagicMock()
    client.setup_domain.return_value = _ok_setup_response()
    client.wait_for_setup.return_value = {
        'status': 'failed',
        'failed_at_step': 'cloudfront',
        'completed_steps': ['certificate', 'route53_zone', 's3_buckets'],
        'error': {
            'step_key': 'cloudfront',
            'step_label': 'Creating CloudFront distribution',
            'message': 'An error occurred (CNAMEAlreadyExists)...',
            'exception': 'ClientError',
            'aws_error_code': 'CNAMEAlreadyExists',
            'diagnosis': {
                'root_cause': 'CLOUDFRONT_DOMAIN_ALREADY_IN_USE',
                'severity': 'fatal',
                'summary': 'This domain is already used as an alias on '
                           'another CloudFront distribution.',
                'suggested_action': (
                    'AWS error: CNAMEAlreadyExists\n'
                    'How to fix:\n'
                    '  1. Open CloudFront console: https://us-east-1...\n'
                    '  2. Search for distribution claiming this CNAME\n'
                    '  3. Disable + delete the orphan, or remove the alias\n'
                    '  4. Retry domain setup'
                ),
            },
        },
    }

    req = ExistingDomainRequest(
        target_domain='diag.com',
        source_account='auto-insurance',
        source_bucket='lander-source.com',
        source_folders=['lander-v3/'],
    )
    with caplog.at_level(_logging.ERROR, logger='orchestrator.workflow'):
        result = run_existing_domain_workflow(req, client=client)

    assert result.status == 'failed'
    # Diagnosis summary wins over raw exception text in the human
    # message — operators see "domain already in use" not "ClientError".
    assert 'already used as an alias' in result.message
    assert "step 'cloudfront'" in result.message

    # Structured log fields surfaced for grep.
    failed_records = [
        r for r in caplog.records
        if getattr(r, 'event', None) == 'workflow_failed'
    ]
    assert failed_records
    rec = failed_records[-1]
    assert rec.atom_diagnosis_root_cause == 'CLOUDFRONT_DOMAIN_ALREADY_IN_USE'
    assert 'already used as an alias' in rec.atom_diagnosis_summary
    assert 'CloudFront console' in rec.atom_diagnosis_action
    assert '1. Open CloudFront console' in rec.atom_diagnosis_action


def test_atom_diagnosis_missing_falls_back_gracefully(tmp_inventory, caplog):
    """When ATOM didn't classify the error (older ATOM, novel failure
    pattern), the diagnosis fields should be None and the workflow
    should still surface the raw AWS error text + exception message.
    Defensive contract — never KeyError on missing diagnosis."""
    import logging as _logging
    tmp_inventory.add_domain(
        domain='no-diag.com', aws_account='auto-insurance')

    client = MagicMock()
    client.setup_domain.return_value = _ok_setup_response()
    client.wait_for_setup.return_value = {
        'status': 'failed',
        'failed_at_step': 'cloudfront',
        'completed_steps': [],
        'error': {
            'step_key': 'cloudfront',
            'message': 'AccessDenied: cloudfront:CreateDistribution',
            'exception': 'ClientError',
            'aws_error_code': 'AccessDenied',
            # No 'diagnosis' field — older ATOM or unclassified error.
        },
    }

    req = ExistingDomainRequest(
        target_domain='no-diag.com',
        source_account='auto-insurance',
        source_bucket='lander-source.com',
        source_folders=['lander-v3/'],
    )
    with caplog.at_level(_logging.ERROR, logger='orchestrator.workflow'):
        result = run_existing_domain_workflow(req, client=client)

    assert result.status == 'failed'
    # Falls back to raw error.message when no diagnosis.summary
    assert 'AccessDenied: cloudfront:CreateDistribution' in result.message

    failed_records = [
        r for r in caplog.records
        if getattr(r, 'event', None) == 'workflow_failed'
    ]
    assert failed_records
    rec = failed_records[-1]
    # Diagnosis fields should be absent (not present in extras), because
    # _fail strips None-valued log_fields entries.
    assert not hasattr(rec, 'atom_diagnosis_root_cause')
    assert not hasattr(rec, 'atom_diagnosis_summary')
    assert not hasattr(rec, 'atom_diagnosis_action')


def test_returns_failed_when_copy_files_partial_failure(tmp_inventory, caplog):
    """Audit 2026-05-11: when ATOM's per-file PutObject calls fail (e.g.
    AccessDenied on the target bucket), it now returns 200 with
    `failed_count > 0` instead of pretending success. The workflow must
    treat that as failure — otherwise users see `workflow_completed` and
    a deploy URL that 404s (the actual incident on
    mymedicareexperts.online).
    """
    import logging as _logging
    tmp_inventory.add_domain(
        domain='partial.com', aws_account='auto-insurance')
    client = MagicMock()
    client.setup_domain.return_value = _ok_setup_response()
    client.wait_for_setup.return_value = _completed_status()
    client.copy_files.return_value = {
        'message': 'Successfully copied 0 of 47 files from src to partial.com',
        'succeeded_count': 0,
        'failed_count': 47,
        'total_count': 47,
        'failed_files': [
            {'key': 'cons-td/index.html',
             'error': 'An error occurred (AccessDenied) when calling the '
                      'PutObject operation: Access Denied'},
            {'key': 'cons-td/styles.css',
             'error': 'An error occurred (AccessDenied) when calling the '
                      'PutObject operation: Access Denied'},
        ],
        'warning': '47 files failed to copy. Bucket is in a partially-deployed state.',
    }

    req = ExistingDomainRequest(
        target_domain='partial.com',
        source_account='auto-insurance',
        source_bucket='src',
        source_folders=['cons-td/'],
    )
    with caplog.at_level(_logging.ERROR, logger='orchestrator.workflow'):
        result = run_existing_domain_workflow(req, client=client)

    assert result.status == 'failed'
    assert result.details['reason'] == 'copy_files_partial_failure'
    # The first failed file should be named in the human message so the
    # operator can grep ATOM/IAM logs for the right key.
    assert 'cons-td/index.html' in result.message
    assert 'AccessDenied' in result.message
    # Structured log fields must surface so /domain-history + Render
    # log greps can root-cause without re-running.
    failed_records = [
        r for r in caplog.records
        if getattr(r, 'event', None) == 'workflow_failed'
    ]
    assert failed_records
    rec = failed_records[-1]
    assert rec.copy_failed_count == 47
    assert rec.copy_succeeded_count == 0
    assert rec.copy_first_failed_key == 'cons-td/index.html'
    # Inventory MUST stay un-marked so /list-domains shows this domain
    # as failed, not as deployed-with-broken-URL.
    assert tmp_inventory.get_domain('partial.com')['setup_at'] is None


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
