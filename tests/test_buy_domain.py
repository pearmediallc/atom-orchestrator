"""Tests for /buy-domain — the manual-pick alternative to /new-domain.

Anand 2026-05-12: an MDB sometimes already has a domain in mind from a
tool, vendor, or brainstorming session and wants to skip the AI
suggestion flow. They invoke `/buy-domain foo.com`, the bot runs
Namecheap + inventory + price checks, posts a confirm/cancel card
listing every finding, and routes Confirm into the existing Path B
chain (TL approval -> Mark Purchased -> deploy).

Design rule under test (feedback_bot_never_hard_rejects_user_choice):
the bot NEVER auto-rejects. Every finding — unavailable, over price
cap, inventory collision — is surfaced as a labelled bullet on the
confirm card. The MDB decides. Only Namecheap-API-failures replace
Confirm with Cancel-only (no fact to confirm against).
"""
from unittest.mock import MagicMock, patch
import pytest

from config import Config
from slack_bot.routes import (
    _build_buy_domain_modal,
    _build_buy_domain_confirm_blocks,
    _normalise_domain_input,
    _extract_extension,
    _DOMAIN_RE,
)


# ─── Domain normaliser ─────────────────────────────────────────────────────

@pytest.mark.parametrize('raw, expected', [
    ('foo.com',                          'foo.com'),
    (' foo.com ',                        'foo.com'),
    ('Foo.COM',                          'foo.com'),
    ('https://foo.com',                  'foo.com'),
    ('http://foo.com/',                  'foo.com'),
    ('https://www.foo.com/path/',        'foo.com'),
    ('www.foo.com',                      'foo.com'),
    ('https://Foo.COM/landing-page/',    'foo.com'),
    ('',                                 ''),
    (None,                               ''),
])
def test_normalise_domain_input_strips_noise(raw, expected):
    assert _normalise_domain_input(raw) == expected


# ─── Format validation regex ───────────────────────────────────────────────

@pytest.mark.parametrize('domain, valid', [
    ('foo.com',                       True),
    ('my-medicare-experts.online',    True),
    ('a.io',                          True),
    ('123numbers.com',                True),
    ('sub.foo.com',                   False),    # subdomains rejected
    ('-foo.com',                      False),    # leading hyphen
    ('foo-.com',                      False),    # trailing hyphen
    ('foo',                           False),    # no TLD
    ('foo.',                          False),    # empty TLD
    ('foo.c',                         False),    # 1-char TLD
    ('foo bar.com',                   False),    # space
    ('foo_bar.com',                   False),    # underscore (invalid in DNS)
    ('foo.com/path',                  False),    # path (caller should normalise)
])
def test_domain_regex_accepts_apex_rejects_garbage(domain, valid):
    assert bool(_DOMAIN_RE.match(domain)) is valid


def test_extract_extension_returns_tld_with_dot():
    assert _extract_extension('foo.com') == '.com'
    assert _extract_extension('my-medicare.online') == '.online'
    assert _extract_extension('nodot') == ''


# ─── Modal builder ─────────────────────────────────────────────────────────

def test_buy_domain_modal_callback_id():
    """callback_id MUST be 'buy_domain_modal' so the view handler routes
    to handle_buy_domain_submission and NOT to handle_new_domain_submission.
    Wrong callback_id would silently dead-letter the modal submit."""
    modal = _build_buy_domain_modal()
    assert modal['callback_id'] == 'buy_domain_modal'


def test_buy_domain_modal_has_domain_block():
    """The whole point of this flow is the MDB types a name. The
    domain_block must exist and use the documented action_id so the
    submission handler can find it."""
    modal = _build_buy_domain_modal()
    block_ids = [b.get('block_id') for b in modal['blocks']]
    assert 'domain_block' in block_ids
    dom_block = next(b for b in modal['blocks'] if b.get('block_id') == 'domain_block')
    assert dom_block['element']['action_id'] == 'domain_input'


def test_buy_domain_modal_prefills_when_slash_arg_provided():
    """`/buy-domain foo.com` populates the input so the MDB doesn't
    re-type. Bare `/buy-domain` leaves it blank."""
    modal_blank = _build_buy_domain_modal()
    modal_pre = _build_buy_domain_modal('foo.com')
    dom_blank = next(
        b for b in modal_blank['blocks'] if b.get('block_id') == 'domain_block'
    )
    dom_pre = next(
        b for b in modal_pre['blocks'] if b.get('block_id') == 'domain_block'
    )
    assert 'initial_value' not in dom_blank['element']
    assert dom_pre['element']['initial_value'] == 'foo.com'


def test_buy_domain_modal_excludes_audience_examples_extension(monkeypatch):
    """The fields that only make sense for AI-naming (audience, examples,
    extension) are NOT on this modal — the MDB already picked, so the
    AI naming inputs are dead weight."""
    monkeypatch.setattr(
        Config, 'AWS_ACCOUNT_OPTIONS', ['auto-insurance', 'other-vertical'],
    )
    modal = _build_buy_domain_modal()
    block_ids = [b.get('block_id') for b in modal['blocks']]
    assert 'audience_block' not in block_ids
    assert 'examples_block' not in block_ids
    assert 'extension_block' not in block_ids


def test_buy_domain_modal_has_aws_account_picker(monkeypatch):
    """AWS account picker sourced from Config.AWS_ACCOUNT_OPTIONS — same
    contract as /new-domain. Test patches the env so the assertion isn't
    coupled to whatever's in the dev .env."""
    monkeypatch.setattr(
        Config, 'AWS_ACCOUNT_OPTIONS', ['auto-insurance', 'other-vertical'],
    )
    modal = _build_buy_domain_modal()
    acct_block = next(
        b for b in modal['blocks'] if b.get('block_id') == 'aws_account_block'
    )
    option_values = [o['value'] for o in acct_block['element']['options']]
    assert option_values == ['auto-insurance', 'other-vertical']


def test_buy_domain_modal_lander_block_is_optional(monkeypatch):
    """Setup-only runs (no lander) work the same way as in /new-domain."""
    monkeypatch.setattr(
        Config, 'AWS_ACCOUNT_OPTIONS', ['auto-insurance'],
    )
    modal = _build_buy_domain_modal()
    lander = next(
        b for b in modal['blocks'] if b.get('block_id') == 'lander_block'
    )
    assert lander.get('optional') is True


# ─── Confirm card builder ──────────────────────────────────────────────────

@pytest.fixture
def signing_secret(monkeypatch):
    """sign_payload refuses dev-default secret; patch a real one so the
    confirm card actually builds in tests."""
    monkeypatch.setattr(Config, 'FLASK_SECRET_KEY', 'x' * 64)


def test_confirm_card_carries_confirm_and_cancel_buttons(signing_secret):
    blocks = _build_buy_domain_confirm_blocks(
        domain='foo.com', vertical='medicare', aws_account='auto-insurance',
        lander='https://x/y', requester='U_MDB',
        availability_finding=':white_check_mark: Available.',
        inventory_finding=':white_check_mark: Not in inventory.',
        price_finding=':moneybag: $9.99, under cap.',
    )
    actions = next(b for b in blocks if b.get('type') == 'actions')
    action_ids = [e['action_id'] for e in actions['elements']]
    assert 'pick_domain' in action_ids
    assert 'cancel_buy_domain' in action_ids


def test_confirm_button_reuses_pick_domain_payload_shape(signing_secret):
    """The whole point of action_id='pick_domain' is so the existing
    handle_pick_domain handler picks up the click identically to an
    AI-shortlist Pick this. The payload shape MUST match what
    handle_pick_domain reads (domain, vertical, lander, extension,
    requester, aws_account)."""
    from slack_bot.payload_signing import verify_payload
    blocks = _build_buy_domain_confirm_blocks(
        domain='foo.com', vertical='medicare', aws_account='other-vertical',
        lander='', requester='U_MDB',
        availability_finding='', inventory_finding='', price_finding='',
    )
    actions = next(b for b in blocks if b.get('type') == 'actions')
    confirm_btn = next(
        e for e in actions['elements'] if e['action_id'] == 'pick_domain'
    )
    parsed = verify_payload(confirm_btn['value'])
    assert parsed['domain'] == 'foo.com'
    assert parsed['vertical'] == 'medicare'
    assert parsed['aws_account'] == 'other-vertical'
    assert parsed['lander'] == ''
    assert parsed['extension'] == '.com'   # derived from domain
    assert parsed['requester'] == 'U_MDB'


def test_confirm_card_surfaces_all_findings_in_header(signing_secret):
    """The MDB scans the findings vertically to decide. Every finding
    string we pass in must appear in the header section text — no
    silent dropping."""
    blocks = _build_buy_domain_confirm_blocks(
        domain='foo.com', vertical='medicare', aws_account='auto-insurance',
        lander='', requester='U_MDB',
        availability_finding=':no_entry: NOT available.',
        inventory_finding=':warning: Already in inventory since 2025-01-15.',
        price_finding=':moneybag: $89/yr (over cap $15).',
    )
    section = next(b for b in blocks if b.get('type') == 'section')
    text = section['text']['text']
    assert 'NOT available' in text
    assert 'Already in inventory' in text
    assert 'over cap' in text


def test_confirm_card_lander_blank_shows_setup_only_note(signing_secret):
    blocks = _build_buy_domain_confirm_blocks(
        domain='foo.com', vertical='medicare', aws_account='auto-insurance',
        lander='', requester='U_MDB',
        availability_finding='', inventory_finding='', price_finding='',
    )
    section = next(b for b in blocks if b.get('type') == 'section')
    assert 'setup-only' in section['text']['text']


# ─── Submission handler — branches under test ──────────────────────────────
#
# Bolt's view decorator wraps the function in a private registry, so we
# can't call the registered handler directly. Instead we exercise the
# UNDERLYING helpers (already done above) and the integration paths via
# the lightweight in-process bolt app fixture pattern used by
# test_phase7_5_tl_approval. Per-finding edge cases are covered by
# parametrised tests against _build_buy_domain_confirm_blocks's inputs.

def test_confirm_card_handles_unavailable_with_confirm_still_present(signing_secret):
    """Critical: NOT available on Namecheap does NOT remove the Confirm
    button. The MDB might have a different acquisition plan (secondary
    market, transfer) — the bot just reports the fact.
    """
    blocks = _build_buy_domain_confirm_blocks(
        domain='foo.com', vertical='medicare', aws_account='auto-insurance',
        lander='', requester='U_MDB',
        availability_finding=':no_entry: NOT available.',
        inventory_finding='', price_finding='',
    )
    actions = next(b for b in blocks if b.get('type') == 'actions')
    action_ids = [e['action_id'] for e in actions['elements']]
    assert 'pick_domain' in action_ids


def test_confirm_card_handles_over_cap_with_confirm_still_present(signing_secret):
    blocks = _build_buy_domain_confirm_blocks(
        domain='foo.com', vertical='medicare', aws_account='auto-insurance',
        lander='', requester='U_MDB',
        availability_finding=':white_check_mark: Available.',
        inventory_finding='', price_finding=':moneybag: $89/yr over cap.',
    )
    actions = next(b for b in blocks if b.get('type') == 'actions')
    action_ids = [e['action_id'] for e in actions['elements']]
    assert 'pick_domain' in action_ids


def test_confirm_card_handles_inventory_collision_with_confirm_still_present(
        signing_secret):
    blocks = _build_buy_domain_confirm_blocks(
        domain='foo.com', vertical='medicare', aws_account='auto-insurance',
        lander='', requester='U_MDB',
        availability_finding='',
        inventory_finding=':warning: Already in our inventory.',
        price_finding='',
    )
    actions = next(b for b in blocks if b.get('type') == 'actions')
    action_ids = [e['action_id'] for e in actions['elements']]
    assert 'pick_domain' in action_ids
