"""Tests for lifecycle.handlers — Slack button handlers.

Each test simulates a Slack action body, calls the handler directly,
and asserts: state transition in DB, event row written, downstream DM
sent (or not, in dry-run), and the original card was replaced.

Handlers register onto a bolt App via register(). The tests use a
minimal FakeApp that just captures the registered callbacks so we can
invoke them directly without booting bolt.
"""
import datetime as dt
import json
from unittest.mock import MagicMock

import pytest

from config import Config
from inventory import store
from lifecycle import handlers, states as S


# ─── Fake bolt App that captures action callbacks ─────────────────────────

class FakeApp:
    """Minimal stand-in for slack_bolt.App — collects callbacks by
    action_id so tests can call them directly."""
    def __init__(self):
        self._actions = {}

    def action(self, action_id):
        def decorator(fn):
            self._actions[action_id] = fn
            return fn
        return decorator

    def call(self, action_id, body, client=None):
        ack = MagicMock(name='ack')
        client = client or MagicMock(name='slack_client')
        cb = self._actions[action_id]
        cb(ack, body, client)
        return ack, client


def _body(action_id: str, value: dict, *, user='U_NEERAJ',
          channel='C_DM', message_ts='123.45') -> dict:
    return {
        'user':    {'id': user},
        'channel': {'id': channel},
        'message': {'ts': message_ts},
        'actions': [{'action_id': action_id, 'value': json.dumps(value)}],
    }


@pytest.fixture
def app(monkeypatch):
    """Fake bolt app with all lifecycle handlers registered, plus the
    standard env config the tests assume.
    """
    a = FakeApp()
    handlers.register(a)
    monkeypatch.setattr(Config, 'LIFECYCLE_DRY_RUN', False)
    monkeypatch.setattr(Config, 'DEV_REROUTE_DMS_TO', '')
    monkeypatch.setattr(Config, 'TL_SLACK_USER_ID',      'U_TL')
    monkeypatch.setattr(Config, 'UTKARSH_SLACK_USER_ID', 'U_UTKARSH')
    monkeypatch.setattr(Config, 'LIFECYCLE_ACTIVE_SPEND_USD', 1.0)
    return a


# ─── using_yes (MDB confirms in use → forwards to Utkarsh) ────────────────

def test_using_yes_flips_state_and_dms_utkarsh(tmp_inventory, app):
    store.add_domain(domain='dying.com', assigned_to='U_NEERAJ')
    store.set_lifecycle_state('dying.com', S.AWAITING_MDB_USAGE_RESPONSE)

    _ack, client = app.call(
        'lifecycle_using_yes',
        _body('lifecycle_using_yes',
              {'domain': 'dying.com', 'assigned_to': 'U_NEERAJ'}),
    )

    row = tmp_inventory.get_domain('dying.com')
    assert row['lifecycle_state'] == S.AWAITING_UTKARSH_RENEW
    # Card replaced (chat_update) + DM to Utkarsh
    assert client.chat_update.call_count == 1
    dm_calls = [c for c in client.chat_postMessage.call_args_list
                if c.kwargs.get('channel') == 'U_UTKARSH']
    assert len(dm_calls) == 1
    # Event written
    events = store.list_domain_events('dying.com')
    assert events[0]['event_type'] == 'mdb_said_using_yes'


def test_using_yes_rejects_wrong_user(tmp_inventory, app):
    """Anyone OTHER than the assigned MDB clicks → ephemeral reject + no
    state change."""
    store.add_domain(domain='dying.com', assigned_to='U_NEERAJ')
    store.set_lifecycle_state('dying.com', S.AWAITING_MDB_USAGE_RESPONSE)

    _ack, client = app.call(
        'lifecycle_using_yes',
        _body('lifecycle_using_yes',
              {'domain': 'dying.com', 'assigned_to': 'U_NEERAJ'},
              user='U_INTRUDER'),
    )

    # State unchanged
    assert tmp_inventory.get_domain('dying.com')['lifecycle_state'] == \
        S.AWAITING_MDB_USAGE_RESPONSE
    # Ephemeral reject sent
    assert client.chat_postEphemeral.call_count == 1


# ─── using_no (MDB says not using → contradiction guard or forward) ──────

def test_using_no_forwards_to_utkarsh_when_no_recent_spend(
    tmp_inventory, app, monkeypatch,
):
    """No recent spend → straightforward forward to Utkarsh for disable."""
    store.add_domain(domain='dying.com', assigned_to='U_NEERAJ')
    store.set_lifecycle_state('dying.com', S.AWAITING_MDB_USAGE_RESPONSE)

    # Stub the spend lookup so the contradiction guard sees $0.
    monkeypatch.setattr(handlers, '_recent_spend', lambda d: 0.0)

    _ack, client = app.call(
        'lifecycle_using_no',
        _body('lifecycle_using_no',
              {'domain': 'dying.com', 'assigned_to': 'U_NEERAJ'}),
    )

    row = tmp_inventory.get_domain('dying.com')
    assert row['lifecycle_state'] == S.AWAITING_UTKARSH_DISABLE_RENEW
    dm_calls = [c for c in client.chat_postMessage.call_args_list
                if c.kwargs.get('channel') == 'U_UTKARSH']
    assert len(dm_calls) == 1


def test_using_no_triggers_contradiction_guard_when_recent_spend(
    tmp_inventory, app, monkeypatch,
):
    """MDB says "no" but RedTrack shows real spend → escalate to TL,
    do NOT auto-flip to AWAITING_UTKARSH_DISABLE_RENEW. We must never
    accidentally kill a live revenue source."""
    store.add_domain(domain='dying.com', assigned_to='U_NEERAJ')
    store.set_lifecycle_state('dying.com', S.AWAITING_MDB_USAGE_RESPONSE)
    monkeypatch.setattr(handlers, '_recent_spend', lambda d: 250.0)

    _ack, client = app.call(
        'lifecycle_using_no',
        _body('lifecycle_using_no',
              {'domain': 'dying.com', 'assigned_to': 'U_NEERAJ'}),
    )

    # State did NOT advance to AWAITING_UTKARSH_DISABLE_RENEW
    row = tmp_inventory.get_domain('dying.com')
    assert row['lifecycle_state'] == S.AWAITING_MDB_USAGE_RESPONSE
    # TL got the contradiction DM, Utkarsh did NOT
    tl_dms = [c for c in client.chat_postMessage.call_args_list
              if c.kwargs.get('channel') == 'U_TL']
    utkarsh_dms = [c for c in client.chat_postMessage.call_args_list
                   if c.kwargs.get('channel') == 'U_UTKARSH']
    assert len(tl_dms) == 1
    assert len(utkarsh_dms) == 0
    assert 'shows $250' in tl_dms[0].kwargs['text'] or \
           '250.00' in tl_dms[0].kwargs['text']
    # And the contradiction was logged so it shows up on /domain-history
    events = [e['event_type'] for e in store.list_domain_events('dying.com')]
    assert 'mdb_no_but_recent_spend' in events


# ─── renewed (Utkarsh closes the loop) ────────────────────────────────────

def test_renewed_dms_mdb_and_tl_and_sets_state(tmp_inventory, app, monkeypatch):
    """Utkarsh clicks Renewed → state → RENEWED, MDB + TL get DMs.
    Namecheap re-sync is best-effort and should not block the click."""
    store.add_domain(domain='alive.com', assigned_to='U_NEERAJ')
    store.set_lifecycle_state('alive.com', S.AWAITING_UTKARSH_RENEW)

    monkeypatch.setattr(
        'domain_assistant.namecheap_check.get_domain_info',
        lambda d: {'expire_at': dt.datetime(2027, 1, 1),
                   'auto_renew_enabled': True},
    )

    _ack, client = app.call(
        'lifecycle_renewed',
        _body('lifecycle_renewed',
              {'domain': 'alive.com', 'requester': 'U_NEERAJ'},
              user='U_UTKARSH'),
    )

    row = tmp_inventory.get_domain('alive.com')
    assert row['lifecycle_state'] == S.RENEWED
    # New expire_at was synced
    assert '2027' in str(row['expire_at'])
    # MDB + TL both got DMs
    channels = {c.kwargs['channel']
                for c in client.chat_postMessage.call_args_list}
    assert {'U_NEERAJ', 'U_TL'} <= channels


def test_renewed_does_not_block_on_namecheap_failure(tmp_inventory, app, monkeypatch):
    """If the post-renewal Namecheap re-sync raises, we still finish the
    state transition + send DMs. Namecheap blip can't break the UX."""
    store.add_domain(domain='alive.com', assigned_to='U_NEERAJ')
    store.set_lifecycle_state('alive.com', S.AWAITING_UTKARSH_RENEW)

    def boom(d):
        raise RuntimeError('namecheap down')
    monkeypatch.setattr(
        'domain_assistant.namecheap_check.get_domain_info', boom,
    )

    _ack, client = app.call(
        'lifecycle_renewed',
        _body('lifecycle_renewed',
              {'domain': 'alive.com', 'requester': 'U_NEERAJ'},
              user='U_UTKARSH'),
    )

    assert tmp_inventory.get_domain('alive.com')['lifecycle_state'] == S.RENEWED
    # MDB still got their celebration DM despite the resync failure
    channels = {c.kwargs['channel']
                for c in client.chat_postMessage.call_args_list}
    assert 'U_NEERAJ' in channels


def test_renewed_rejects_non_utkarsh(tmp_inventory, app):
    store.add_domain(domain='alive.com', assigned_to='U_NEERAJ')
    store.set_lifecycle_state('alive.com', S.AWAITING_UTKARSH_RENEW)

    _ack, client = app.call(
        'lifecycle_renewed',
        _body('lifecycle_renewed',
              {'domain': 'alive.com', 'requester': 'U_NEERAJ'},
              user='U_RANDOM_PERSON'),
    )

    assert tmp_inventory.get_domain('alive.com')['lifecycle_state'] == \
        S.AWAITING_UTKARSH_RENEW   # unchanged
    assert client.chat_postEphemeral.call_count == 1


# ─── disable_renew_done ────────────────────────────────────────────────────

def test_disable_renew_done_clears_state(tmp_inventory, app):
    """Utkarsh confirms auto-renew off → state cleared so cron re-classifies
    on the next pass (will become EXPIRED on the day)."""
    store.add_domain(domain='dying.com', assigned_to='U_NEERAJ')
    store.set_lifecycle_state('dying.com', S.AWAITING_UTKARSH_DISABLE_RENEW)

    _ack, _client = app.call(
        'lifecycle_disable_renew_done',
        _body('lifecycle_disable_renew_done',
              {'domain': 'dying.com', 'requester': 'U_NEERAJ'},
              user='U_UTKARSH'),
    )

    assert tmp_inventory.get_domain('dying.com')['lifecycle_state'] is None
    events = [e['event_type'] for e in store.list_domain_events('dying.com')]
    assert 'auto_renew_disabled' in events


# ─── keep_30 / keep_15 / push_inventory ────────────────────────────────────

def test_keep_30_sets_extended_state(tmp_inventory, app):
    store.add_domain(domain='quiet.com', assigned_to='U_NEERAJ')
    store.set_lifecycle_state('quiet.com', S.AWAITING_MDB_INVENTORY_RESPONSE)

    _ack, client = app.call(
        'lifecycle_keep_30',
        _body('lifecycle_keep_30',
              {'domain': 'quiet.com', 'assigned_to': 'U_NEERAJ'}),
    )

    assert tmp_inventory.get_domain('quiet.com')['lifecycle_state'] == S.EXTENDED_30
    # TL gets an FYI
    tl_dms = [c for c in client.chat_postMessage.call_args_list
              if c.kwargs.get('channel') == 'U_TL']
    assert len(tl_dms) == 1
    events = [e['event_type'] for e in store.list_domain_events('quiet.com')]
    assert 'mdb_extended_30' in events


def test_keep_15_sets_extended_15(tmp_inventory, app):
    store.add_domain(domain='quiet.com', assigned_to='U_NEERAJ')
    store.set_lifecycle_state('quiet.com', S.AWAITING_MDB_INVENTORY_RESPONSE)

    app.call(
        'lifecycle_keep_15',
        _body('lifecycle_keep_15',
              {'domain': 'quiet.com', 'assigned_to': 'U_NEERAJ'}),
    )
    assert tmp_inventory.get_domain('quiet.com')['lifecycle_state'] == S.EXTENDED_15


def test_push_inventory_clears_owner_and_keeps_aws(tmp_inventory, app):
    """Per design: pushing to inventory clears assigned_to but leaves
    AWS resources alive. We verify the assigned_to part here; the AWS
    side is "we don't call setup_domain teardown" — easy to verify by
    just not patching anything."""
    store.add_domain(domain='quiet.com', assigned_to='U_NEERAJ')
    store.set_lifecycle_state('quiet.com', S.AWAITING_MDB_INVENTORY_RESPONSE)

    _ack, client = app.call(
        'lifecycle_push_inventory',
        _body('lifecycle_push_inventory',
              {'domain': 'quiet.com', 'assigned_to': 'U_NEERAJ'}),
    )

    row = tmp_inventory.get_domain('quiet.com')
    assert row['assigned_to'] is None
    assert row['lifecycle_state'] == S.INVENTORY
    events = [e['event_type'] for e in store.list_domain_events('quiet.com')]
    assert 'pushed_to_inventory' in events
    # TL DM
    tl_dms = [c for c in client.chat_postMessage.call_args_list
              if c.kwargs.get('channel') == 'U_TL']
    assert len(tl_dms) == 1


def test_keep_30_rejects_wrong_user(tmp_inventory, app):
    store.add_domain(domain='quiet.com', assigned_to='U_NEERAJ')
    store.set_lifecycle_state('quiet.com', S.AWAITING_MDB_INVENTORY_RESPONSE)

    _ack, client = app.call(
        'lifecycle_keep_30',
        _body('lifecycle_keep_30',
              {'domain': 'quiet.com', 'assigned_to': 'U_NEERAJ'},
              user='U_INTRUDER'),
    )
    assert tmp_inventory.get_domain('quiet.com')['lifecycle_state'] == \
        S.AWAITING_MDB_INVENTORY_RESPONSE
    assert client.chat_postEphemeral.call_count == 1


# ─── DRY_RUN gate end-to-end on a handler ─────────────────────────────────

def test_handler_dry_run_does_not_send_real_dms(tmp_inventory, app, monkeypatch):
    """Even after a button click, DRY_RUN must suppress all DMs.
    The state transition + audit event still happen — that's the whole
    point of DRY_RUN being safe."""
    monkeypatch.setattr(Config, 'LIFECYCLE_DRY_RUN', True)
    store.add_domain(domain='quiet.com', assigned_to='U_NEERAJ')
    store.set_lifecycle_state('quiet.com', S.AWAITING_MDB_INVENTORY_RESPONSE)

    _ack, client = app.call(
        'lifecycle_keep_30',
        _body('lifecycle_keep_30',
              {'domain': 'quiet.com', 'assigned_to': 'U_NEERAJ'}),
    )

    # State changed
    assert tmp_inventory.get_domain('quiet.com')['lifecycle_state'] == S.EXTENDED_30
    # No real DM sent
    assert client.chat_postMessage.call_count == 0


# ─── DEV_REROUTE allows solo testing ──────────────────────────────────────

def test_dev_reroute_lets_dev_user_act_as_anyone(tmp_inventory, app, monkeypatch):
    """When DEV_REROUTE_DMS_TO is set, the dev user can click any button
    that would normally be gated to a different user. Lets a solo dev
    walk the whole flow without 5 fake accounts."""
    monkeypatch.setattr(Config, 'DEV_REROUTE_DMS_TO', 'U_DEV_ANAND')
    store.add_domain(domain='alive.com', assigned_to='U_NEERAJ')
    store.set_lifecycle_state('alive.com', S.AWAITING_UTKARSH_RENEW)

    monkeypatch.setattr(
        'domain_assistant.namecheap_check.get_domain_info',
        lambda d: None,
    )

    # Dev clicks a button that's normally Utkarsh-only
    _ack, client = app.call(
        'lifecycle_renewed',
        _body('lifecycle_renewed',
              {'domain': 'alive.com', 'requester': 'U_NEERAJ'},
              user='U_DEV_ANAND'),
    )

    # Click went through
    assert tmp_inventory.get_domain('alive.com')['lifecycle_state'] == S.RENEWED
    assert client.chat_postEphemeral.call_count == 0


# ─── Phase F — fan-out sibling sync + atomic first-click-wins ─────────────

def _seed_idle_with_fanout(domain='quiet.com'):
    """A domain in AWAITING_MDB_INVENTORY_RESPONSE with a 2-recipient
    fan-out ledger: the MDB (C_DM/123.45) and the TL (C_TL/999.99)."""
    store.add_domain(domain=domain, assigned_to='U_NEERAJ')
    store.set_lifecycle_state(domain, S.AWAITING_MDB_INVENTORY_RESPONSE)
    store.record_prompt_recipients(domain, [
        {'recipient_slack_id': 'U_NEERAJ', 'channel_id': 'C_DM',
         'message_ts': '123.45', 'is_tl': False},
        {'recipient_slack_id': 'U_TL', 'channel_id': 'C_TL',
         'message_ts': '999.99', 'is_tl': True},
    ])


def test_resolution_syncs_sibling_cards(tmp_inventory, app):
    """When one recipient resolves, every OTHER recipient's card gets
    chat_update'd and the fan-out ledger is cleared."""
    _seed_idle_with_fanout()

    body = _body('lifecycle_keep_30',
                 {'domain': 'quiet.com', 'assigned_to': 'U_NEERAJ'},
                 user='U_NEERAJ', channel='C_DM', message_ts='123.45')
    ack, client = app.call('lifecycle_keep_30', body)

    assert tmp_inventory.get_domain('quiet.com')['lifecycle_state'] == S.EXTENDED_30
    # The TL's sibling card was chat_update'd.
    updated = {(c.kwargs.get('channel'), c.kwargs.get('ts'))
               for c in client.chat_update.call_args_list}
    assert ('C_TL', '999.99') in updated
    # Ledger cleared after the sync.
    assert store.get_prompt_recipients('quiet.com') == []


def test_second_clicker_loses_race_and_is_named(tmp_inventory, app):
    """First click wins atomically; a sibling clicking afterwards loses,
    the row is untouched, and the ephemeral names who already resolved."""
    _seed_idle_with_fanout()

    # First click — the MDB keeps it 30 days → wins.
    app.call('lifecycle_keep_30',
             _body('lifecycle_keep_30',
                   {'domain': 'quiet.com', 'assigned_to': 'U_NEERAJ'},
                   user='U_NEERAJ', channel='C_DM', message_ts='123.45'))
    assert tmp_inventory.get_domain('quiet.com')['lifecycle_state'] == S.EXTENDED_30

    # Second click — the TL clicks push_inventory on the now-stale card.
    ack, client = app.call(
        'lifecycle_push_inventory',
        _body('lifecycle_push_inventory',
              {'domain': 'quiet.com', 'assigned_to': 'U_TL'},
              user='U_TL', channel='C_TL', message_ts='999.99'))

    # State unchanged — the loser's click did nothing.
    assert tmp_inventory.get_domain('quiet.com')['lifecycle_state'] == S.EXTENDED_30
    # Ephemeral names the actual winner, not "someone else".
    eph = client.chat_postEphemeral.call_args
    assert 'U_NEERAJ' in eph.kwargs['text']


def test_tl_can_resolve_the_prompt(tmp_inventory, app):
    """The TL is a full interactive recipient — clicking their own card
    resolves the prompt just like an MDB would."""
    _seed_idle_with_fanout()

    ack, client = app.call(
        'lifecycle_push_inventory',
        _body('lifecycle_push_inventory',
              {'domain': 'quiet.com', 'assigned_to': 'U_TL'},
              user='U_TL', channel='C_TL', message_ts='999.99'))

    assert tmp_inventory.get_domain('quiet.com')['lifecycle_state'] == S.INVENTORY
    # The MDB's sibling card got synced.
    updated = {(c.kwargs.get('channel'), c.kwargs.get('ts'))
               for c in client.chat_update.call_args_list}
    assert ('C_DM', '123.45') in updated
