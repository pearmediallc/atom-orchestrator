"""Tests for the 48h SLA escalator (lifecycle.scan.run_sla_escalation)
and the supporting store query (get_awaiting_domains_past_sla).
"""
import datetime as dt
from unittest.mock import MagicMock

import pytest

from config import Config
from inventory import store
from lifecycle import scan, states as S


def _slack_client():
    return MagicMock(name='slack_client')


def _setup(monkeypatch, *, sla_hours=48):
    monkeypatch.setattr(Config, 'LIFECYCLE_DRY_RUN', False)
    monkeypatch.setattr(Config, 'DEV_REROUTE_DMS_TO', '')
    monkeypatch.setattr(Config, 'TL_SLACK_USER_ID', 'U_TL')
    monkeypatch.setattr(Config, 'UTKARSH_SLACK_USER_ID', 'U_UTKARSH')
    monkeypatch.setattr(Config, 'LIFECYCLE_MDB_RESPONSE_SLA_HOURS', sla_hours)


def _set_prompted_at(domain: str, days_ago: int):
    """Manually backdate last_prompted_at so tests don't actually wait."""
    when = (dt.datetime.now() - dt.timedelta(days=days_ago)).isoformat()
    with store._conn() as c:
        c.execute(
            "UPDATE domains SET last_prompted_at = ? WHERE domain = ?",
            (when, domain),
        )


# ─── store query: get_awaiting_domains_past_sla ───────────────────────────

def test_query_returns_only_past_sla(tmp_inventory):
    store.add_domain(domain='ghosted.com', assigned_to='U_MDB')
    store.set_lifecycle_state('ghosted.com', S.AWAITING_MDB_USAGE_RESPONSE)
    _set_prompted_at('ghosted.com', days_ago=3)  # 72h ago, past 48h SLA

    store.add_domain(domain='fresh.com', assigned_to='U_MDB')
    store.set_lifecycle_state('fresh.com', S.AWAITING_MDB_USAGE_RESPONSE)
    _set_prompted_at('fresh.com', days_ago=0)  # just prompted

    rows = store.get_awaiting_domains_past_sla(
        awaiting_states=S.AWAITING_MDB_STATES, hours_ago=48,
    )
    domains = {r['domain'] for r in rows}
    assert 'ghosted.com' in domains
    assert 'fresh.com' not in domains


def test_query_excludes_non_awaiting_states(tmp_inventory):
    """ACTIVE / IDLE / EXTENDED_* etc. must not be returned by the SLA
    query — only the MDB-side AWAITING states the escalator covers."""
    store.add_domain(domain='active.com', assigned_to='U_MDB')
    store.set_lifecycle_state('active.com', S.ACTIVE)
    _set_prompted_at('active.com', days_ago=10)  # ancient, but wrong state

    rows = store.get_awaiting_domains_past_sla(
        awaiting_states=S.AWAITING_MDB_STATES, hours_ago=48,
    )
    assert all(r['domain'] != 'active.com' for r in rows)


def test_query_excludes_null_last_prompted_at(tmp_inventory):
    """Defensive: a row in AWAITING_MDB_* with NULL last_prompted_at
    means the prompt was never actually sent, so the SLA clock hasn't
    started. Don't escalate."""
    store.add_domain(domain='weird.com', assigned_to='U_MDB')
    store.set_lifecycle_state('weird.com', S.AWAITING_MDB_USAGE_RESPONSE)

    rows = store.get_awaiting_domains_past_sla(
        awaiting_states=S.AWAITING_MDB_STATES, hours_ago=0,
    )
    assert all(r['domain'] != 'weird.com' for r in rows)


def test_query_respects_limit(tmp_inventory):
    for i in range(5):
        store.add_domain(domain=f'ghost{i}.com', assigned_to='U_MDB')
        store.set_lifecycle_state(f'ghost{i}.com', S.AWAITING_MDB_USAGE_RESPONSE)
        _set_prompted_at(f'ghost{i}.com', days_ago=3)

    rows = store.get_awaiting_domains_past_sla(
        awaiting_states=S.AWAITING_MDB_STATES, hours_ago=48, limit=2,
    )
    assert len(rows) == 2


# ─── escalator end-to-end ──────────────────────────────────────────────────

def test_escalator_dms_tl_with_usage_card(tmp_inventory, monkeypatch):
    _setup(monkeypatch)
    store.add_domain(domain='dying.com', assigned_to='U_NEERAJ')
    store.set_lifecycle_state('dying.com', S.AWAITING_MDB_USAGE_RESPONSE)
    _set_prompted_at('dying.com', days_ago=3)

    client = _slack_client()
    counters = scan.run_sla_escalation(slack_client=client)

    assert counters['escalated'] == 1
    # State advanced to TL override
    row = tmp_inventory.get_domain('dying.com')
    assert row['lifecycle_state'] == S.AWAITING_TL_OVERRIDE_USAGE
    # last_prompted_at was bumped (so the SLA clock resets and we
    # don't re-escalate next run for the same row)
    assert row['last_prompted_at'] is not None
    # TL got DM'd — and the buttons match the usage flow
    dm_call = client.chat_postMessage.call_args
    assert dm_call.kwargs['channel'] == 'U_TL'
    blocks = dm_call.kwargs['blocks']
    action_ids = {b['action_id'] for el in blocks if el.get('type') == 'actions'
                  for b in el['elements']}
    assert action_ids == {
        'lifecycle_tl_force_renew', 'lifecycle_tl_force_disable_renew',
    }
    # Event written
    events = [e['event_type'] for e in store.list_domain_events('dying.com')]
    assert 'escalated_to_tl' in events


def test_escalator_dms_tl_with_inventory_card(tmp_inventory, monkeypatch):
    _setup(monkeypatch)
    store.add_domain(domain='quiet.com', assigned_to='U_NEERAJ')
    store.set_lifecycle_state('quiet.com', S.AWAITING_MDB_INVENTORY_RESPONSE)
    _set_prompted_at('quiet.com', days_ago=3)

    client = _slack_client()
    scan.run_sla_escalation(slack_client=client)

    row = tmp_inventory.get_domain('quiet.com')
    assert row['lifecycle_state'] == S.AWAITING_TL_OVERRIDE_INVENTORY
    blocks = client.chat_postMessage.call_args.kwargs['blocks']
    action_ids = {b['action_id'] for el in blocks if el.get('type') == 'actions'
                  for b in el['elements']}
    assert action_ids == {
        'lifecycle_tl_force_push', 'lifecycle_tl_force_keep_30',
    }


def test_escalator_skips_within_sla(tmp_inventory, monkeypatch):
    """A row prompted 24h ago should NOT be escalated (SLA is 48h)."""
    _setup(monkeypatch, sla_hours=48)
    store.add_domain(domain='still-time.com', assigned_to='U_NEERAJ')
    store.set_lifecycle_state('still-time.com', S.AWAITING_MDB_USAGE_RESPONSE)
    _set_prompted_at('still-time.com', days_ago=1)  # 24h ago

    client = _slack_client()
    counters = scan.run_sla_escalation(slack_client=client)

    assert counters['escalated'] == 0
    assert client.chat_postMessage.call_count == 0
    # State unchanged
    assert tmp_inventory.get_domain(
        'still-time.com')['lifecycle_state'] == S.AWAITING_MDB_USAGE_RESPONSE


def test_escalator_does_not_re_escalate_already_in_tl_override(tmp_inventory, monkeypatch):
    """A row already in AWAITING_TL_OVERRIDE_* must NOT be re-escalated
    by a second cron pass — that would double-DM TL."""
    _setup(monkeypatch)
    store.add_domain(domain='re-ghosted.com', assigned_to='U_NEERAJ')
    store.set_lifecycle_state('re-ghosted.com', S.AWAITING_TL_OVERRIDE_USAGE)
    _set_prompted_at('re-ghosted.com', days_ago=10)  # ancient

    client = _slack_client()
    counters = scan.run_sla_escalation(slack_client=client)
    assert counters['escalated'] == 0
    assert client.chat_postMessage.call_count == 0


def test_escalator_dry_run_skips_dms_but_state_still_advances(
    tmp_inventory, monkeypatch,
):
    """DRY_RUN must NOT send DMs, but the audit (state + event) still
    happens so we can verify the escalator's decisions on real data."""
    _setup(monkeypatch)
    monkeypatch.setattr(Config, 'LIFECYCLE_DRY_RUN', True)
    store.add_domain(domain='dying.com', assigned_to='U_NEERAJ')
    store.set_lifecycle_state('dying.com', S.AWAITING_MDB_USAGE_RESPONSE)
    _set_prompted_at('dying.com', days_ago=3)

    client = _slack_client()
    scan.run_sla_escalation(slack_client=client)

    # No real DM sent
    assert client.chat_postMessage.call_count == 0
    # But state DID advance (audit still happens)
    assert tmp_inventory.get_domain(
        'dying.com')['lifecycle_state'] == S.AWAITING_TL_OVERRIDE_USAGE


def test_escalator_one_row_failure_does_not_abort(tmp_inventory, monkeypatch):
    _setup(monkeypatch)
    store.add_domain(domain='good.com', assigned_to='U_MDB')
    store.set_lifecycle_state('good.com', S.AWAITING_MDB_USAGE_RESPONSE)
    _set_prompted_at('good.com', days_ago=3)

    store.add_domain(domain='bad.com', assigned_to='U_MDB')
    store.set_lifecycle_state('bad.com', S.AWAITING_MDB_USAGE_RESPONSE)
    _set_prompted_at('bad.com', days_ago=3)

    real = scan._escalate_to_tl

    def faulty(client, row):
        if row['domain'] == 'bad.com':
            raise RuntimeError('synthetic test failure')
        return real(client, row)

    monkeypatch.setattr(scan, '_escalate_to_tl', faulty)
    client = _slack_client()
    counters = scan.run_sla_escalation(slack_client=client)

    assert counters['errors'] == 1
    assert counters['escalated'] == 1
    # good.com still got the escalation
    assert tmp_inventory.get_domain(
        'good.com')['lifecycle_state'] == S.AWAITING_TL_OVERRIDE_USAGE


# ─── TL handler dynamic from_state ─────────────────────────────────────────

def test_tl_force_renew_records_actual_from_state(tmp_inventory, monkeypatch):
    """When SLA escalator advances state to AWAITING_TL_OVERRIDE_USAGE,
    TL clicking the override card should record from_state =
    AWAITING_TL_OVERRIDE_USAGE in the audit log, not the old hardcoded
    AWAITING_MDB_USAGE_RESPONSE."""
    from lifecycle import handlers
    import json

    _setup(monkeypatch)
    store.add_domain(domain='dying.com', assigned_to='U_NEERAJ')
    store.set_lifecycle_state('dying.com', S.AWAITING_TL_OVERRIDE_USAGE)

    # Fake bolt app
    class FakeApp:
        def __init__(self):
            self._actions = {}
        def action(self, action_id):
            def deco(fn):
                self._actions[action_id] = fn
                return fn
            return deco

    app = FakeApp()
    handlers.register(app)

    body = {
        'user': {'id': 'U_TL'},
        'channel': {'id': 'C_DM'},
        'message': {'ts': '123.45'},
        'actions': [{
            'action_id': 'lifecycle_tl_force_renew',
            'value': json.dumps({'domain': 'dying.com',
                                 'assigned_to': 'U_NEERAJ'}),
        }],
    }
    ack = MagicMock()
    client = _slack_client()
    app._actions['lifecycle_tl_force_renew'](ack, body, client)

    events = store.list_domain_events('dying.com')
    forced = [e for e in events if e['event_type'] == 'tl_forced_renew']
    assert len(forced) == 1
    assert forced[0]['from_state'] == S.AWAITING_TL_OVERRIDE_USAGE
