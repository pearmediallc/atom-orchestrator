"""Tests for orchestrator.pixel_fire.

Covers every PixelFireResult.status branch + the key safety properties:
the safety belt aborts on shape changes, the no-change path doesn't write,
audit rows land for every operator-visible outcome, and validation
rejects bad inputs without touching ATOM at all.

AtomClient is fully mocked — these are unit tests. The integration check
(actual ATOM round-trip on a real lander) happens in production via the
slash command.
"""
from unittest.mock import MagicMock

import pytest

from orchestrator import pixel_fire as pf
from orchestrator.atom_client import (
    AtomClientError,
    AtomConnectionError,
    AtomServerError,
)


# ─── Fixtures ──────────────────────────────────────────────────────────────

# A minimal lander mirroring the v1 file's pixel block: 2 ID spots
# (fbq('init', ...) + noscript URL) + 3 event spots (head + 2 button
# handlers). Whitespace + button comments match the real template.
SAMPLE_LANDER = """<!doctype html>
<html><head>
<title>get-usa-help</title>
<!-- Meta Pixel Code -->
<script>
!function(f,b,e,v,n,t,s){}();
fbq('init', '2714057732308829');
fbq('track', 'Lead');
</script>
<noscript><img height="1" width="1" style="display:none"
src="https://www.facebook.com/tr?id=2714057732308829&ev=PageView&noscript=1"
/></noscript>
<!-- End Meta Pixel Code -->
</head>
<body>
<button id="callyes">yes</button>
<button id="callno">no</button>
<script>
document.getElementById("callyes").addEventListener("click", function () {
  fbq('track', 'Lead') // Replace 'ButtonClicked' with your custom event name
});
</script>
<script>
document.getElementById("callno").addEventListener("click", function () {
  fbq('track', 'Lead') // Replace 'ButtonClicked' with your custom event name
});
</script>
</body></html>
"""

NEW_ID = '9988776655443322'
NEW_EVENT = 'Purchase'


@pytest.fixture
def seeded_inventory(tmp_inventory):
    """Per-test SQLite inventory with get-usa-help.com pre-seeded with a
    valid aws_account. update_pixel_on_lander needs this row to resolve
    the AWS account before calling ATOM."""
    tmp_inventory.add_domain(
        pf.PIXEL_FIRE_DOMAIN,
        vertical='auto',
        aws_account='auto-insurance',
        requested_by='U_TEST',
    )
    return tmp_inventory


@pytest.fixture
def mock_atom():
    """Mocked AtomClient pre-loaded with the sample lander. Tests can
    override the `get_file_content` return / raise per case."""
    m = MagicMock()
    m.get_file_content.return_value = SAMPLE_LANDER
    m.save_file_content.return_value = {
        'message': 'File pixel-fire/index.html saved successfully',
        'modified_file': pf.PIXEL_FIRE_FILE_KEY,
    }
    return m


# ─── Happy path ────────────────────────────────────────────────────────────

def test_updated_swaps_id_and_event_in_correct_counts(seeded_inventory, mock_atom):
    res = pf.update_pixel_on_lander(
        NEW_EVENT, NEW_ID, actor='U_TEST', atom_client=mock_atom,
    )

    assert res.status == 'updated'
    assert res.details['old_pixel_id'] == '2714057732308829'
    assert res.details['old_event'] == 'Lead'
    assert res.details['new_pixel_id'] == NEW_ID
    assert res.details['new_event'] == NEW_EVENT
    assert res.details['id_count'] == 2
    assert res.details['event_count'] == 3

    # ATOM save MUST have been called with the rewritten content.
    assert mock_atom.save_file_content.called
    saved = mock_atom.save_file_content.call_args.args
    saved_content = saved[3]
    assert NEW_ID in saved_content
    assert '2714057732308829' not in saved_content
    assert NEW_EVENT in saved_content
    # Sanity: the noscript ev=PageView stays untouched (we only swap
    # `ev=` if the old event matched, but the regex specifically targets
    # `fbq('track', ...)`, not the noscript URL).
    assert 'ev=PageView' in saved_content


def test_updated_writes_audit_row_with_old_and_new(seeded_inventory, mock_atom):
    pf.update_pixel_on_lander(
        NEW_EVENT, NEW_ID, actor='U_TEST', atom_client=mock_atom,
    )
    events = seeded_inventory.list_domain_events(pf.PIXEL_FIRE_DOMAIN)
    assert any(
        e['event_type'] == 'pixel_updated' and e['actor'] == 'U_TEST'
        and e['metadata']['old_pixel_id'] == '2714057732308829'
        and e['metadata']['new_pixel_id'] == NEW_ID
        for e in events
    ), f'expected pixel_updated event, got {[e["event_type"] for e in events]}'


# ─── No-change (idempotent re-run) ────────────────────────────────────────

def test_no_change_when_values_already_match(seeded_inventory, mock_atom):
    """Running with the exact values already in the file should NOT
    write to S3 — the regex would replace `Lead`->`Lead` etc., producing
    identical content. Detect via content equality and return no_change."""
    res = pf.update_pixel_on_lander(
        'Lead', '2714057732308829', actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'no_change'
    assert mock_atom.save_file_content.call_count == 0


# ─── Safety belt ──────────────────────────────────────────────────────────

def test_safety_belt_trips_when_extra_event_call(seeded_inventory, mock_atom):
    """Lander with an extra fbq('track', ...) call (4 instead of 3) must
    abort without saving — template shape changed, operator must verify."""
    extra = SAMPLE_LANDER + "\n<script>fbq('track', 'Lead')</script>\n"
    mock_atom.get_file_content.return_value = extra

    res = pf.update_pixel_on_lander(
        NEW_EVENT, NEW_ID, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'safety_belt'
    assert res.details['actual_event_count'] == 4
    assert res.details['expected_event_count'] == 3
    assert mock_atom.save_file_content.call_count == 0


def test_safety_belt_trips_when_missing_id_spot(seeded_inventory, mock_atom):
    """Lander with the noscript stripped out (1 ID instead of 2) must abort."""
    no_noscript = SAMPLE_LANDER.replace(
        '<noscript><img height="1" width="1" style="display:none"\n'
        'src="https://www.facebook.com/tr?id=2714057732308829&ev=PageView&noscript=1"\n'
        '/></noscript>\n', '',
    )
    mock_atom.get_file_content.return_value = no_noscript

    res = pf.update_pixel_on_lander(
        NEW_EVENT, NEW_ID, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'safety_belt'
    assert res.details['actual_id_count'] == 1
    assert mock_atom.save_file_content.call_count == 0


def test_safety_belt_writes_audit_row(seeded_inventory, mock_atom):
    """Even on safety-belt aborts, /domain-history should show the event
    so operators can see the bot tried + what counts it found."""
    extra = SAMPLE_LANDER + "\n<script>fbq('track', 'Lead')</script>\n"
    mock_atom.get_file_content.return_value = extra

    pf.update_pixel_on_lander(
        NEW_EVENT, NEW_ID, actor='U_TEST', atom_client=mock_atom,
    )
    events = seeded_inventory.list_domain_events(pf.PIXEL_FIRE_DOMAIN)
    assert any(e['event_type'] == 'pixel_safety_belt' for e in events)


# ─── Input validation ────────────────────────────────────────────────────

@pytest.mark.parametrize('bad_id', [
    '',                       # empty
    '12345',                  # too short
    '12345678901234567890',   # too long
    '27140577a2308829',       # non-digit
    'abc',                    # not numeric at all
])
def test_invalid_pixel_id_rejected(seeded_inventory, mock_atom, bad_id):
    res = pf.update_pixel_on_lander(
        'Lead', bad_id, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'invalid_input'
    assert res.details['reason'] == 'bad_pixel_id'
    # Validation MUST fail before we touch ATOM.
    assert mock_atom.get_file_content.call_count == 0


@pytest.mark.parametrize('bad_event', [
    '',                       # empty
    'Lead Form',              # space
    'Lead!',                  # special char
    "Lea'd",                  # embedded quote (boundary quotes get stripped)
    'a' * 41,                 # over 40 chars
    'Lead-Form',              # dash not in allowed charset
])
def test_invalid_event_rejected(seeded_inventory, mock_atom, bad_event):
    res = pf.update_pixel_on_lander(
        bad_event, '2714057732308829', actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'invalid_input'
    assert res.details['reason'] == 'bad_event'
    assert mock_atom.get_file_content.call_count == 0


def test_validation_strips_quotes_and_whitespace(seeded_inventory, mock_atom):
    """Slack users sometimes paste IDs with surrounding quotes or extra
    spaces — those should be cleaned, not rejected."""
    res = pf.update_pixel_on_lander(
        '  Purchase  ', '"9988776655443322"',
        actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'updated'


def test_validation_strips_commas_in_pixel_id(seeded_inventory, mock_atom):
    """A user might paste the ID with thousands separators."""
    res = pf.update_pixel_on_lander(
        'Purchase', '9,988,776,655,443,322',
        actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'updated'
    assert res.details['new_pixel_id'] == '9988776655443322'


def test_invalid_input_does_not_write_audit_row(seeded_inventory, mock_atom):
    """invalid_input means we never touched the file — no audit row should
    be written. Audit is for actions taken, not validation failures."""
    pf.update_pixel_on_lander(
        'Lead', 'not-a-number', actor='U_TEST', atom_client=mock_atom,
    )
    events = seeded_inventory.list_domain_events(pf.PIXEL_FIRE_DOMAIN)
    assert events == []


# ─── Inventory errors ─────────────────────────────────────────────────────

def test_domain_missing_from_inventory(tmp_inventory, mock_atom):
    """No `seeded_inventory` fixture — domain isn't in the store."""
    res = pf.update_pixel_on_lander(
        'Lead', '2714057732308829', actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'inventory_error'
    assert res.details['reason'] == 'domain_missing'
    assert mock_atom.get_file_content.call_count == 0


def test_aws_account_missing(tmp_inventory, mock_atom):
    """Domain row exists but aws_account is NULL — refuse rather than
    silently routing to a default account."""
    # init_db's _backfill_legacy_aws_account sets NULL/empty aws_account
    # to 'auto-insurance', so we have to insert with a value, then NULL it
    # via direct SQL (mirrors the path where someone manually clears it).
    tmp_inventory.add_domain(
        pf.PIXEL_FIRE_DOMAIN, aws_account='temp', requested_by='U_TEST',
    )
    import sqlite3
    from config import Config
    with sqlite3.connect(Config.INVENTORY_DB_PATH) as c:
        c.execute("UPDATE domains SET aws_account = NULL WHERE domain = ?",
                  (pf.PIXEL_FIRE_DOMAIN,))

    res = pf.update_pixel_on_lander(
        'Lead', '2714057732308829', actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'inventory_error'
    assert res.details['reason'] == 'aws_account_missing'


# ─── ATOM errors ──────────────────────────────────────────────────────────

def test_atom_read_404_distinguishes_file_not_found(seeded_inventory, mock_atom):
    """A 4xx with 'http 404' or 'does not exist' in the body should map
    to a friendly 'file moved/deleted' message, not generic ATOM error."""
    mock_atom.get_file_content.side_effect = AtomClientError(
        'GET /api/get-file-content/... -> HTTP 404 (client error). '
        'Body: {"error": "File pixel-fire/index.html does not exist..."}'
    )
    res = pf.update_pixel_on_lander(
        NEW_EVENT, NEW_ID, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'atom_error'
    assert res.details['reason'] == 'file_not_found'


def test_atom_read_generic_5xx(seeded_inventory, mock_atom):
    mock_atom.get_file_content.side_effect = AtomServerError(
        'GET /api/... -> HTTP 502 (server error)'
    )
    res = pf.update_pixel_on_lander(
        NEW_EVENT, NEW_ID, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'atom_error'
    assert res.details['reason'] == 'atom_read_failed'
    assert mock_atom.save_file_content.call_count == 0


def test_atom_read_connection_error(seeded_inventory, mock_atom):
    mock_atom.get_file_content.side_effect = AtomConnectionError(
        'GET /api/... could not reach ATOM: ConnectionError'
    )
    res = pf.update_pixel_on_lander(
        NEW_EVENT, NEW_ID, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'atom_error'
    assert res.details['reason'] == 'atom_read_failed'


def test_atom_write_failure_reports_clearly(seeded_inventory, mock_atom):
    mock_atom.save_file_content.side_effect = AtomServerError(
        'POST /api/save-file-content -> HTTP 500'
    )
    res = pf.update_pixel_on_lander(
        NEW_EVENT, NEW_ID, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'atom_error'
    assert res.details['reason'] == 'atom_write_failed'
    # The file edit was attempted but never persisted — operator needs
    # to know NOT to assume the change is live.
    assert 'NOT modified' in res.message


# ─── File-shape sanity checks ─────────────────────────────────────────────

def test_oversized_file_rejected(seeded_inventory, mock_atom):
    """1MB cap protects against accidentally trying to regex through a
    huge file (e.g. binary upload at the wrong key)."""
    huge = SAMPLE_LANDER + ('x' * (pf.MAX_LANDER_BYTES + 100))
    mock_atom.get_file_content.return_value = huge
    res = pf.update_pixel_on_lander(
        NEW_EVENT, NEW_ID, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'atom_error'
    assert res.details['reason'] == 'file_too_large'
    assert mock_atom.save_file_content.call_count == 0


def test_non_html_rejected(seeded_inventory, mock_atom):
    mock_atom.get_file_content.return_value = (
        'this is not html, no doctype, no html tag'
    )
    res = pf.update_pixel_on_lander(
        NEW_EVENT, NEW_ID, actor='U_TEST', atom_client=mock_atom,
    )
    assert res.status == 'atom_error'
    assert res.details['reason'] == 'not_html'


# ─── Regex unit tests ─────────────────────────────────────────────────────

def test_replace_pixel_id_handles_double_quotes():
    """Some templates use double quotes for fbq args."""
    src = '''fbq("init", "1111111111111111");'''
    out, count = pf._replace_pixel_id(src, '2222222222222222')
    assert count == 1
    assert '2222222222222222' in out
    assert '1111111111111111' not in out


def test_replace_event_handles_extra_whitespace():
    """Whitespace inside fbq(...) shouldn't fool the regex."""
    src = "fbq( 'track' , 'Lead' )"
    out, count = pf._replace_event(src, 'Purchase')
    assert count == 1
    assert "'Purchase'" in out
