"""Tests for lifecycle.inventory_digest.

Two layers covered:
  1. render_digest_blocks(...)  — pure block renderer.
  2. run_inventory_digest(...)  — orchestrator: skip rules, DRY_RUN,
                                  Slack call, error resilience.
"""
import datetime as dt
from unittest.mock import MagicMock

import pytest

from config import Config
from inventory import store
from lifecycle import inventory_digest


TODAY = dt.date(2026, 6, 1)


def _row(domain: str, **overrides) -> dict:
    base = {
        'domain': domain,
        'vertical': 'Auto Insurance',
        'assigned_to': None,
        'expire_at': None,
        'notes': None,
    }
    base.update(overrides)
    return base


def _all_text(blocks) -> str:
    out = []
    for b in blocks:
        if b.get('type') == 'header':
            out.append(b['text']['text'])
        elif b.get('type') == 'section':
            out.append(b['text']['text'])
        elif b.get('type') == 'context':
            for el in b.get('elements', []):
                out.append(el.get('text', ''))
    return '\n'.join(out)


# ─── Pure renderer ────────────────────────────────────────────────────────

def test_renders_header_with_total_count():
    rows = [_row('a.com'), _row('b.com'), _row('c.com')]
    blocks = inventory_digest.render_digest_blocks(rows, today=TODAY)
    assert blocks[0]['type'] == 'header'
    assert '3 available' in blocks[0]['text']['text']


def test_groups_by_vertical_alphabetised():
    """Verticals should be A-Z, with 'no vertical' at the end."""
    rows = [
        _row('z.com', vertical='Medicare'),
        _row('a.com', vertical='Auto Insurance'),
        _row('m.com', vertical=None),  # bucket → '_no vertical_'
        _row('b.com', vertical='auto insurance'),  # case-insensitive sort
    ]
    text = _all_text(inventory_digest.render_digest_blocks(rows, today=TODAY))
    auto_pos = text.lower().find('*auto insurance*')
    medicare_pos = text.lower().find('*medicare*')
    no_vertical_pos = text.lower().find('*_no vertical_*')
    assert auto_pos < medicare_pos < no_vertical_pos


def test_row_renders_expire_date_with_urgency_band():
    rows = [
        _row('soon.com', expire_at=dt.datetime(2026, 6, 10)),    # 9d → urgent
        _row('later.com', expire_at=dt.datetime(2027, 1, 1)),    # 214d → calm
    ]
    text = _all_text(inventory_digest.render_digest_blocks(rows, today=TODAY))
    assert '9d to expire' in text
    assert '2027-01-01' in text


def test_row_renders_unknown_when_no_expire():
    rows = [_row('mystery.com', expire_at=None)]
    text = _all_text(inventory_digest.render_digest_blocks(rows, today=TODAY))
    assert 'expire date unknown' in text


def test_row_renders_notes_snippet():
    rows = [_row('x.com', notes='Purchased via /new-domain bot flow')]
    text = _all_text(inventory_digest.render_digest_blocks(rows, today=TODAY))
    assert 'Purchased via /new-domain bot flow' in text


def test_truncates_with_more_line_when_over_cap():
    """When pool size > _MAX_DOMAINS_PER_MESSAGE, render the cap and
    a single '…and N more' line at the bottom."""
    cap = inventory_digest._MAX_DOMAINS_PER_MESSAGE
    rows = [_row(f'd{i}.com') for i in range(cap + 7)]
    blocks = inventory_digest.render_digest_blocks(rows, today=TODAY)
    text = _all_text(blocks)
    assert '…and 7 more available' in text
    # Stays under Slack's 50-block ceiling
    assert len(blocks) <= 50


def test_short_pool_does_not_show_more_line():
    rows = [_row('a.com'), _row('b.com')]
    text = _all_text(inventory_digest.render_digest_blocks(rows, today=TODAY))
    assert 'more available' not in text


# ─── Orchestrator: off-switches ───────────────────────────────────────────

def test_skips_when_developers_channel_id_empty(tmp_inventory, monkeypatch):
    """Empty DEVELOPERS_CHANNEL_ID = digest disabled. Don't even query."""
    monkeypatch.setattr(Config, 'DEVELOPERS_CHANNEL_ID', '')

    client = MagicMock()
    counters = inventory_digest.run_inventory_digest(slack_client=client)

    assert counters['skipped'] == 1
    assert client.chat_postMessage.call_count == 0


def test_skips_when_pool_is_empty(tmp_inventory, monkeypatch):
    """No unassigned domains → no message. Don't spam channel with
    'Inventory pool: 0 available'."""
    monkeypatch.setattr(Config, 'DEVELOPERS_CHANNEL_ID', 'C_DEVS')
    monkeypatch.setattr(Config, 'LIFECYCLE_DRY_RUN', False)
    # Add a domain WITH assigned_to — should NOT appear in pool.
    store.add_domain(domain='taken.com', assigned_to='U_NEERAJ')

    client = MagicMock()
    counters = inventory_digest.run_inventory_digest(slack_client=client)

    assert counters['unassigned'] == 0
    assert counters['skipped'] == 1
    assert client.chat_postMessage.call_count == 0


def test_dry_run_does_not_post(tmp_inventory, monkeypatch):
    """DRY_RUN must log intent but never actually post. Mirrors the
    classifier + SLA escalator's dry-run gate."""
    monkeypatch.setattr(Config, 'DEVELOPERS_CHANNEL_ID', 'C_DEVS')
    monkeypatch.setattr(Config, 'LIFECYCLE_DRY_RUN', True)
    store.add_domain(domain='free.com', assigned_to=None)

    client = MagicMock()
    counters = inventory_digest.run_inventory_digest(slack_client=client)

    assert counters['unassigned'] == 1
    assert counters['posted'] == 0
    assert client.chat_postMessage.call_count == 0


# ─── Orchestrator: happy path ─────────────────────────────────────────────

def test_posts_to_configured_channel(tmp_inventory, monkeypatch):
    monkeypatch.setattr(Config, 'DEVELOPERS_CHANNEL_ID', 'C_DEVS')
    monkeypatch.setattr(Config, 'LIFECYCLE_DRY_RUN', False)
    store.add_domain(domain='free1.com', vertical='Auto Insurance',
                     assigned_to=None)
    store.add_domain(domain='free2.com', vertical='Medicare',
                     assigned_to=None)
    # Distractor: assigned domain shouldn't appear.
    store.add_domain(domain='taken.com', vertical='Auto Insurance',
                     assigned_to='U_NEERAJ')

    client = MagicMock()
    counters = inventory_digest.run_inventory_digest(slack_client=client)

    assert counters['unassigned'] == 2
    assert counters['posted'] == 1
    call = client.chat_postMessage.call_args
    assert call.kwargs['channel'] == 'C_DEVS'
    blocks_text = _all_text(call.kwargs['blocks'])
    assert 'free1.com' in blocks_text
    assert 'free2.com' in blocks_text
    assert 'taken.com' not in blocks_text


def test_orchestrator_resilient_to_post_failure(tmp_inventory, monkeypatch):
    """If chat_postMessage raises, the cron should keep going — log
    the error and move on. Don't crash the daily cron over a Slack blip."""
    monkeypatch.setattr(Config, 'DEVELOPERS_CHANNEL_ID', 'C_DEVS')
    monkeypatch.setattr(Config, 'LIFECYCLE_DRY_RUN', False)
    store.add_domain(domain='free.com', assigned_to=None)

    client = MagicMock()
    client.chat_postMessage.side_effect = RuntimeError('slack down')
    counters = inventory_digest.run_inventory_digest(slack_client=client)

    # Didn't crash; posted=0 + skipped=1 reflects the failure
    assert counters['posted'] == 0
    assert counters['skipped'] == 1


# ─── Store query: list_unassigned_domains ─────────────────────────────────

def test_list_unassigned_excludes_rows_with_assigned_to(tmp_inventory):
    store.add_domain(domain='free.com', assigned_to=None)
    store.add_domain(domain='owned.com', assigned_to='U_NEERAJ')
    store.add_domain(domain='empty-string.com', assigned_to='')

    rows = store.list_unassigned_domains()
    domains = {r['domain'] for r in rows}
    assert 'free.com' in domains
    assert 'empty-string.com' in domains   # treated as unassigned
    assert 'owned.com' not in domains


def test_list_unassigned_orders_known_expiry_first(tmp_inventory):
    """Sort: rows with known expire_at come first (sorted ASC),
    NULL expire_at rows come last."""
    store.add_domain(domain='unknown.com', assigned_to=None)
    store.add_domain(domain='soon.com', assigned_to=None)
    store.add_domain(domain='later.com', assigned_to=None)
    store.update_namecheap_sync(
        'soon.com', expire_at=dt.datetime(2026, 6, 15),
        auto_renew_enabled=True,
    )
    store.update_namecheap_sync(
        'later.com', expire_at=dt.datetime(2027, 1, 1),
        auto_renew_enabled=True,
    )

    rows = store.list_unassigned_domains()
    domains = [r['domain'] for r in rows]
    # soon.com (closest expiry) first, then later.com, unknown last
    assert domains.index('soon.com') < domains.index('later.com')
    assert domains.index('later.com') < domains.index('unknown.com')


def test_list_unassigned_respects_limit(tmp_inventory):
    for i in range(10):
        store.add_domain(domain=f'd{i}.com', assigned_to=None)
    rows = store.list_unassigned_domains(limit=3)
    assert len(rows) == 3
