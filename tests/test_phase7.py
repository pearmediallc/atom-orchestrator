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
from slack_bot.routes import (
    _phase7_run_atom_setup,
    _parse_lander_url,
    _render_progress_checklist,
    _make_atom_progress_callback,
    _build_retry_setup_blocks,
)


# ─── ATOM live-progress checklist ─────────────────────────────────────────

def test_progress_checklist_renders_all_9_steps_as_pending_when_empty():
    """Pre-first-poll the requester should see the full outline, not a
    blank message that says nothing. Reassures them the job is queued."""
    out = _render_progress_checklist({})
    # All 9 step labels must appear so the requester sees the roadmap.
    for label_substr in (
        'Initializing setup',
        'Requesting SSL certificate',
        'Adding CNAME validation records',
        'Creating Route 53 hosted zone',
        'Creating S3 buckets',
        'Waiting for SSL certificate validation',
        'Creating CloudFront distribution',
        'Creating Route 53 alias records',
        'Updating Namecheap nameservers',
    ):
        assert label_substr in out
    # All rows start as ⬜ (white_large_square) before ATOM reports
    # anything.
    assert out.count(':white_large_square:') == 9


def test_progress_checklist_uses_distinct_glyphs_per_state():
    """Each state maps to a unique glyph:
       ✅ white_check_mark, ⏳ hourglass_flowing_sand, ❌ x, ⬜ white_large_square.
    """
    out = _render_progress_checklist({
        'cloudfront':       {'status': 'in_progress'},
        'route53_records':  {'status': 'failed'},
        # Set the last-active step to route53_records (index 7) via the
        # 'failed' state, so the only "after that" step is
        # nameserver_update (index 8) — which has no state and renders
        # ⬜. Everything before route53_records is implicitly completed.
    })
    assert ':hourglass_flowing_sand:' in out    # cloudfront in_progress
    assert ':x:' in out                          # route53_records failed
    # Steps 0-6 (init, cert, namecheap_cname, r53_zone, s3_buckets,
    # cert_validation, cloudfront) are all ✅ — either explicit (cf)
    # or implicit (everything before the last-active step). Step 8
    # (nameserver_update) is ⬜ pending since nothing later is active.
    assert out.count(':white_large_square:') == 1


def test_progress_checklist_implicit_completion_on_retry(monkeypatch):
    """ATOM's retry path short-circuits steps whose AWS resources already
    exist (cert reused, R53 zone already there) — its progress callback
    never re-emits those steps. Without implicit-completion logic those
    steps render as ⬜ alongside the later ✅ steps, which looks broken
    even though the deploy succeeded.

    Regression guard for 2026-05-13 retry on naturalfitnessguide.com —
    deploy completed end-to-end but the requester saw two unchecked
    boxes ("Initializing setup" + "Adding CNAME validation records")
    sitting between ✅ rows.
    """
    # Mimics the steps dict ATOM returns after a successful retry where
    # the cert was already valid and the R53 zone already existed:
    out = _render_progress_checklist({
        'certificate':            {'status': 'completed'},
        # 'namecheap_cname' intentionally absent — ATOM didn't add
        # new records because cert was already valid.
        'route53_zone':           {'status': 'completed'},
        's3_buckets':              {'status': 'completed'},
        'certificate_validation': {'status': 'completed'},
        'cloudfront':              {'status': 'completed'},
        'route53_records':         {'status': 'completed'},
        'nameserver_update':       {'status': 'completed'},
        # 'initialization' also intentionally absent — ATOM doesn't
        # explicitly mark it.
    })
    # All 9 rows should be ✅ — both the explicit-completed ones AND
    # the implicit ones (initialization, namecheap_cname) that
    # precede the last-active step.
    assert out.count(':white_check_mark:') == 9
    assert ':white_large_square:' not in out


def test_progress_checklist_does_not_imply_completion_for_steps_after_active():
    """Implicit-completion looks BACK only, never forward. A step with
    no state that comes AFTER the highest-active step is still ⬜
    pending — we don't claim work that hasn't happened yet."""
    out = _render_progress_checklist({
        'certificate': {'status': 'in_progress'},
        # Cert is the second step (index 1). Everything before
        # (initialization, index 0) is implicitly ✅. Everything after
        # is ⬜.
    })
    # 1 implicit ✅ (init) + 1 ⏳ (cert) + 7 ⬜ (everything after cert)
    assert out.count(':white_check_mark:') == 1
    assert out.count(':hourglass_flowing_sand:') == 1
    assert out.count(':white_large_square:') == 7


def test_progress_callback_skips_chat_update_when_nothing_changed():
    """Slack rate-limits message edits and shows new-activity badges on
    every update — calling chat_update with the same content is both
    wasteful and noisy. The callback should de-dupe."""
    client = MagicMock()
    cb = _make_atom_progress_callback(
        client=client, channel='C1', message_ts='123.456',
        header_text='hdr', target_domain='x.com',
    )
    status = {'status': 'running',
              'steps': {'certificate': {'status': 'in_progress'}}}

    cb(status)
    cb(status)  # identical → should NOT re-edit
    cb(status)  # identical → should NOT re-edit

    assert client.chat_update.call_count == 1


def test_progress_callback_updates_when_step_status_changes():
    client = MagicMock()
    cb = _make_atom_progress_callback(
        client=client, channel='C1', message_ts='123.456',
        header_text='hdr', target_domain='x.com',
    )
    cb({'steps': {'certificate': {'status': 'in_progress'}}})
    cb({'steps': {'certificate': {'status': 'completed'},
                  'route53_zone': {'status': 'in_progress'}}})
    cb({'steps': {'certificate': {'status': 'completed'},
                  'route53_zone': {'status': 'completed'},
                  's3_buckets':   {'status': 'in_progress'}}})

    assert client.chat_update.call_count == 3
    # Final edit body must show all the progress so far:
    last_call_text = client.chat_update.call_args.kwargs.get('text', '')
    assert ':white_check_mark: Requesting SSL certificate' in last_call_text
    assert ':white_check_mark: Creating Route 53 hosted zone' in last_call_text
    assert ':hourglass_flowing_sand: Creating S3 buckets' in last_call_text


def test_progress_callback_no_ts_means_no_chat_update():
    """If posting the initial message failed (no ts), we silently
    accept that no live progress is possible. Don't crash the worker."""
    client = MagicMock()
    cb = _make_atom_progress_callback(
        client=client, channel='C1', message_ts=None,
        header_text='hdr', target_domain='x.com',
    )
    cb({'steps': {'certificate': {'status': 'in_progress'}}})
    client.chat_update.assert_not_called()


def test_progress_callback_chat_update_failure_is_swallowed():
    """Slack hiccups during a 5-min deploy are common (rate limits,
    workspace momentarily unreachable). The callback must NOT raise —
    wait_for_setup would otherwise abort the whole deploy on a Slack
    blip that has nothing to do with the actual ATOM run."""
    client = MagicMock()
    client.chat_update.side_effect = RuntimeError('slack 429')
    cb = _make_atom_progress_callback(
        client=client, channel='C1', message_ts='123.456',
        header_text='hdr', target_domain='x.com',
    )
    # Must not raise.
    cb({'steps': {'certificate': {'status': 'in_progress'}}})


# ─── Worker header text: fresh vs already-setup ───────────────────────────

def test_worker_posts_setup_checklist_for_fresh_domain(monkeypatch, tmp_inventory):
    """Brand-new domain (setup_at IS NULL) → header must include the
    9-step setup checklist AND a progress_callback must reach the
    workflow, so the live in-progress edits work."""
    tmp_inventory.add_domain(
        domain='fresh.com', aws_account='auto-insurance',
    )  # setup_at stays NULL
    captured = _patch_workflow(
        monkeypatch,
        WorkflowResult(status='completed',
                       message='Lander deployed.',
                       details={'live_url': 'https://fresh.com/lander/'}),
    )
    client = _slack_client()
    _phase7_run_atom_setup(
        client=client, channel='C1', message_ts='100.0',
        target_domain='fresh.com', vertical='auto-insurance',
        requester='U_MDB', lander_url='https://src.com/lander/',
    )

    posted = _all_text(client)
    assert 'Triggering ATOM setup' in posted
    assert 'Setup progress' in posted
    # All 9 steps must be in the first posted body so the user sees
    # the full roadmap before any of them complete.
    assert 'Initializing setup' in posted
    assert 'Updating Namecheap nameservers' in posted
    # progress_callback must have been threaded into the workflow so
    # live updates can fire.
    assert captured['progress_callback'] is not None


def test_worker_posts_no_setup_checklist_when_setup_already_done(
        monkeypatch, tmp_inventory):
    """Already-setup domain (setup_at populated) → workflow's skip path
    fires, so the 9-step checklist would stay all-pending forever and
    just confuse the requester. Header must NOT include the checklist
    and progress_callback must be None.

    Regression guard for the 2026-05-11 visual confusion where the
    requester saw "Setup progress: ⬜⬜⬜⬜⬜⬜⬜⬜⬜" alongside a
    success message — making them think the deploy hung at step 1.
    """
    tmp_inventory.add_domain(
        domain='already.com', aws_account='auto-insurance',
    )
    from inventory import store
    store.mark_setup_complete('already.com')

    captured = _patch_workflow(
        monkeypatch,
        WorkflowResult(status='completed',
                       message='Lander deployed.',
                       details={'live_url': 'https://already.com/lander/'}),
    )
    client = _slack_client()
    _phase7_run_atom_setup(
        client=client, channel='C1', message_ts='100.0',
        target_domain='already.com', vertical='auto-insurance',
        requester='U_MDB', lander_url='https://src.com/lander/',
    )

    posted = _all_text(client)
    assert 'Deploying lander' in posted
    assert 'already complete' in posted
    # The literal checklist heading and the long-cert-validation
    # latency promise must NOT be there — those mislead the requester.
    assert 'Setup progress' not in posted
    assert '5–20 minutes' not in posted
    # And no progress_callback should be wired in — there's nothing
    # for it to update.
    assert captured['progress_callback'] is None


def test_worker_runs_setup_only_when_lander_url_is_blank(
        monkeypatch, tmp_inventory):
    """Empty lander_url is a VALID Path B setup-only run, not an error.

    Regression guard for 2026-05-12 prod incident: after the Path B
    modal was made optional-lander, the worker still treated empty
    lander_url as "no source configured" and short-circuited with the
    legacy "URL is empty" warning. That left freshly-purchased
    setup-only domains stuck at setup_at=NULL forever — Mark Purchased
    succeeded but ATOM setup never ran.

    The fix: empty lander_url -> pass empty source through to
    run_existing_domain_workflow, which has the setup-only branch.
    """
    # No defaults configured — proves the fix doesn't rely on the
    # legacy fallback path. Empty lander_url + empty defaults must
    # mean "setup-only," not "abort."
    monkeypatch.setattr(Config, 'PHASE7_DEFAULT_SOURCE_BUCKET', '')
    monkeypatch.setattr(Config, 'PHASE7_DEFAULT_SOURCE_FOLDERS', [])
    monkeypatch.setattr(Config, 'PHASE7_DEFAULT_SOURCE_ACCOUNT', 'auto-insurance')
    monkeypatch.setattr(Config, 'PHASE7_LANDER_DEFAULTS', {})

    tmp_inventory.add_domain(
        domain='setup-only.com', aws_account='auto-insurance',
    )
    captured = _patch_workflow(
        monkeypatch,
        WorkflowResult(
            status='completed',
            message='AWS infrastructure provisioned for setup-only.com. '
                    'No lander was deployed.',
            details={'live_url': None, 'setup_only': True},
        ),
    )
    client = _slack_client()
    _phase7_run_atom_setup(
        client=client, channel='C1', message_ts='100.0',
        target_domain='setup-only.com', vertical='auto-insurance',
        requester='U_MDB', lander_url='',
    )

    # Workflow MUST have been called — the old code short-circuited
    # before reaching it, which is the bug we're guarding against.
    assert 'req' in captured
    req = captured['req']
    assert req.source_bucket == ''
    assert req.source_folders == []
    assert req.source_files == []

    # The header should explain this is setup-only, NOT show the
    # legacy "URL is empty" warning.
    posted = _all_text(client)
    assert 'setup-only' in posted.lower()
    assert 'cannot deploy' not in posted.lower()
    assert 'URL is empty' not in posted


def test_worker_warns_when_lander_url_unparseable_and_no_fallback(monkeypatch):
    """If the MDB types something that isn't a parseable URL AND no
    per-vertical fallback exists, that IS a genuine error — surface
    the warning. This path stayed loud while the empty-URL path
    became silent setup-only.
    """
    monkeypatch.setattr(Config, 'PHASE7_DEFAULT_SOURCE_BUCKET', '')
    monkeypatch.setattr(Config, 'PHASE7_DEFAULT_SOURCE_FOLDERS', [])
    monkeypatch.setattr(Config, 'PHASE7_DEFAULT_SOURCE_ACCOUNT', 'auto-insurance')
    monkeypatch.setattr(Config, 'PHASE7_LANDER_DEFAULTS', {})

    client = _slack_client()
    _phase7_run_atom_setup(
        client=client, channel='C1', message_ts='100.0',
        target_domain='garbled.com', vertical='auto-insurance',
        requester='U_MDB', lander_url='not-a-real-url',  # unparseable
    )

    posted = _all_text(client)
    assert 'cannot deploy' in posted.lower()
    assert 'did not parse' in posted.lower()


# ─── Retry-setup blocks ───────────────────────────────────────────────────

def test_retry_setup_blocks_carry_payload_for_re_enqueue(monkeypatch):
    """Failure messages need a Retry-setup button whose signed payload
    carries everything handle_retry_atom_setup needs to enqueue a new
    Phase 7 task — target_domain, vertical, requester, lander_url,
    and the original channel + message_ts so the retry's progress
    updates land in the same Slack thread.
    """
    from config import Config
    from slack_bot.payload_signing import verify_payload
    monkeypatch.setattr(Config, 'FLASK_SECRET_KEY', 'x' * 64)

    blocks = _build_retry_setup_blocks(
        heading=':x: *ATOM workflow failed* at step `cloudfront`.',
        target_domain='retry.com', vertical='medicare',
        requester='U_MDB', lander_url='https://src/lander/',
        original_channel='C_THREAD', original_message_ts='1234.5678',
    )
    actions = next(b for b in blocks if b.get('type') == 'actions')
    retry_btn = next(
        e for e in actions['elements']
        if e['action_id'] == 'retry_atom_setup'
    )
    parsed = verify_payload(retry_btn['value'])
    assert parsed['target_domain'] == 'retry.com'
    assert parsed['vertical'] == 'medicare'
    assert parsed['requester'] == 'U_MDB'
    assert parsed['lander_url'] == 'https://src/lander/'
    assert parsed['original_channel'] == 'C_THREAD'
    assert parsed['original_message_ts'] == '1234.5678'


def test_retry_setup_blocks_handle_blank_lander(monkeypatch):
    """Setup-only retries (blank lander) must carry empty-string
    lander_url through the signed payload — NOT drop the field. The
    worker reads `data.get('lander_url') or ''` and the empty value
    routes back into the setup-only path on retry.
    """
    from config import Config
    from slack_bot.payload_signing import verify_payload
    monkeypatch.setattr(Config, 'FLASK_SECRET_KEY', 'x' * 64)

    blocks = _build_retry_setup_blocks(
        heading=':x: failure',
        target_domain='retry.com', vertical='medicare',
        requester='U_MDB', lander_url='',
        original_channel='C_THREAD', original_message_ts='1234.5678',
    )
    actions = next(b for b in blocks if b.get('type') == 'actions')
    retry_btn = next(
        e for e in actions['elements']
        if e['action_id'] == 'retry_atom_setup'
    )
    parsed = verify_payload(retry_btn['value'])
    assert parsed['lander_url'] == ''


def test_failure_message_includes_retry_button(monkeypatch, tmp_inventory):
    """When the Phase 7 workflow returns a failed WorkflowResult, the
    Slack thread reply must include a Retry-setup button — not just
    the text "ATOM workflow failed." Anyone watching the thread
    should be one click away from re-running.
    """
    from config import Config
    monkeypatch.setattr(Config, 'FLASK_SECRET_KEY', 'x' * 64)
    _set_default_bucket(monkeypatch)
    tmp_inventory.add_domain(
        domain='will-fail.com', aws_account='auto-insurance',
    )

    _patch_workflow(monkeypatch, WorkflowResult(
        status='failed',
        message="ATOM domain setup failed at step 'cloudfront'.",
        details={
            'reason': 'atom_setup_failed',
            'setup_result': {'failed_at_step': 'cloudfront'},
        },
    ))

    client = _slack_client()
    _phase7_run_atom_setup(
        client=client, channel='C1', message_ts='100.0',
        target_domain='will-fail.com', vertical='auto-insurance',
        requester='U_REQUESTER',
        lander_url='https://safetyfirstauto.pro/h-insure-c/',
    )

    # Find the thread reply (chat_postMessage with thread_ts kwarg)
    thread_replies = [
        c for c in client.chat_postMessage.call_args_list
        if c.kwargs.get('thread_ts')
    ]
    # At least one reply should carry a Retry button via blocks=
    retry_replies = [
        c for c in thread_replies
        if any(
            elt.get('action_id') == 'retry_atom_setup'
            for blk in (c.kwargs.get('blocks') or [])
            if blk.get('type') == 'actions'
            for elt in blk.get('elements') or []
        )
    ]
    assert retry_replies, 'failure thread reply must include a Retry button'


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

    def fake_workflow(req, progress_callback=None, **_kwargs):
        captured['req'] = req
        captured['progress_callback'] = progress_callback
        return result

    monkeypatch.setattr(
        'slack_bot.routes.run_existing_domain_workflow', fake_workflow,
    )
    return captured


# ─── Tests ─────────────────────────────────────────────────────────────────

def test_worker_warns_when_unparseable_url_and_no_default_bucket(monkeypatch, tmp_inventory):
    """Unparseable lander URL + no fallback defaults → clear warning, abort.

    Note: empty lander_url is NO LONGER an error — it intentionally
    means "setup-only run." See
    test_worker_runs_setup_only_when_lander_url_is_blank for that
    case. Only an unparseable-but-non-empty URL triggers the legacy
    warning path now (audit 2026-05-12 prod fix).
    """
    monkeypatch.setattr(Config, 'PHASE7_DEFAULT_SOURCE_BUCKET', '')
    monkeypatch.setattr(Config, 'PHASE7_DEFAULT_SOURCE_FOLDERS', [])
    monkeypatch.setattr(Config, 'PHASE7_LANDER_DEFAULTS', {})
    tmp_inventory.add_domain(
        domain='example.com', aws_account='auto-insurance',
    )

    client = _slack_client()
    _phase7_run_atom_setup(
        client=client, channel='C1', message_ts='123.45',
        target_domain='example.com', vertical='auto-insurance',
        requester='U_REQUESTER',
        lander_url='not-a-real-url',  # unparseable
    )

    msg = _all_text(client)
    assert 'cannot deploy' in msg.lower()
    assert 'did not parse' in msg.lower()


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


def test_worker_falls_back_to_config_defaults_when_url_unparseable(
        monkeypatch, tmp_inventory):
    """Unparseable URL + per-vertical defaults → workflow still runs
    using defaults. This preserves the original "URL parse failed,
    fall back to vertical config" safety net for legacy /list-domains
    Mark Deployed clicks whose URL field accepted free-text before
    today's validation (audit 2026-05-12 prod fix).
    """
    _set_default_bucket(monkeypatch)
    tmp_inventory.add_domain(
        domain='example.com', aws_account='auto-insurance',
    )
    captured = _patch_workflow(monkeypatch, WorkflowResult(
        status='completed', message='ok',
        details={'live_url': 'https://example.com'},
    ))
    client = _slack_client()
    _phase7_run_atom_setup(
        client=client, channel='C1', message_ts='123.45',
        target_domain='example.com', vertical='auto-insurance',
        requester='U_REQUESTER',
        lander_url='not-a-real-url',  # unparseable triggers default fallback
    )
    req = captured['req']
    # Falls back to the config defaults when URL didn't parse
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

    def raising_workflow(req, progress_callback=None, **_kwargs):
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


def test_worker_uses_per_vertical_override_when_url_unparseable(
        monkeypatch, tmp_inventory):
    """Unparseable URL + per-vertical override → vertical config wins
    over the global default. Audit 2026-05-12: empty URL is now
    setup-only, not "use defaults" — this test pivoted to the
    unparseable-URL fallback case.
    """
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
    tmp_inventory.add_domain(
        domain='m.com', aws_account='other-vertical',
    )

    captured = _patch_workflow(monkeypatch, WorkflowResult(
        status='completed', message='ok', details={'live_url': 'https://m.com'},
    ))

    client = _slack_client()
    _phase7_run_atom_setup(
        client=client, channel='C1', message_ts='123.45',
        target_domain='m.com', vertical='medicare',
        requester='U_REQUESTER',
        lander_url='not-a-real-url',  # unparseable triggers fallback to defaults
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
