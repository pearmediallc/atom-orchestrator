"""Tests for lifecycle.slack_sync — the daily users.list → slack_users sync.

Mocks slack_sdk's WebClient response shape; verifies UPSERT + soft-delete
behavior end-to-end against tmp SQLite.
"""
from unittest.mock import MagicMock

import pytest

from config import Config
from inventory import store
from lifecycle import slack_sync


def _fake_client(members):
    """Builds a MagicMock with users_list returning one page of members."""
    client = MagicMock()
    client.users_list.return_value = {
        'members': members,
        'response_metadata': {'next_cursor': ''},
    }
    return client


def _m(uid, real_name, email=None, deleted=False, is_bot=False,
       display_name=None):
    """Build a Slack-API-shaped member dict."""
    return {
        'id': uid,
        'real_name': real_name,
        'deleted': deleted,
        'is_bot': is_bot,
        'name': (display_name or real_name).lower().replace(' ', '_'),
        'profile': {
            'real_name': real_name,
            'display_name': display_name or real_name,
            'email': email,
        },
    }


# ─── Happy path ───────────────────────────────────────────────────────────

def test_sync_upserts_all_active_humans(tmp_inventory, monkeypatch):
    monkeypatch.setattr(Config, 'SLACK_BOT_TOKEN', 'fake-xoxb-token')
    members = [
        _m('U_A', 'Anusree Madhu', 'anusree@pearmediallc.com'),
        _m('U_B', 'Rajat Grover',  'rajat@pearmediallc.com'),
    ]
    client = _fake_client(members)

    counters = slack_sync.run_slack_users_sync(slack_client=client)

    assert counters['fetched'] == 2
    assert counters['upserted'] == 2
    assert counters['errors'] == 0

    assert store.get_slack_user('U_A')['real_name'] == 'Anusree Madhu'
    assert store.get_slack_user('U_A')['email'] == 'anusree@pearmediallc.com'
    assert store.get_slack_user('U_B')['real_name'] == 'Rajat Grover'


def test_sync_filters_bots_and_deactivated_and_slackbot(tmp_inventory, monkeypatch):
    """The sync should skip bots, deactivated members, and the
    USLACKBOT pseudo-user. They never reach upsert."""
    monkeypatch.setattr(Config, 'SLACK_BOT_TOKEN', 'token')
    members = [
        _m('U_REAL', 'Real Human', 'real@pearmediallc.com'),
        _m('U_BOT',  'Some Bot', is_bot=True),
        _m('U_DEAD', 'Quit Person', 'quit@pearmediallc.com', deleted=True),
        _m('USLACKBOT', 'Slackbot', is_bot=False),  # special-case ID
    ]
    client = _fake_client(members)

    counters = slack_sync.run_slack_users_sync(slack_client=client)
    assert counters['fetched'] == 1
    assert counters['upserted'] == 1
    assert store.get_slack_user('U_REAL') is not None
    assert store.get_slack_user('U_BOT') is None
    assert store.get_slack_user('U_DEAD') is None
    assert store.get_slack_user('USLACKBOT') is None


def test_sync_soft_deletes_members_no_longer_in_workspace(tmp_inventory, monkeypatch):
    """A second sync run that doesn't return user U_LEFT should mark
    them deleted=True (not actually delete the row)."""
    monkeypatch.setattr(Config, 'SLACK_BOT_TOKEN', 'token')

    # First sync: 2 active members
    client1 = _fake_client([
        _m('U_STAY', 'Stays Active', 'stay@pearmediallc.com'),
        _m('U_LEFT', 'Will Leave',   'left@pearmediallc.com'),
    ])
    slack_sync.run_slack_users_sync(slack_client=client1)
    assert store.get_slack_user('U_STAY')['deleted'] in (0, False)
    assert store.get_slack_user('U_LEFT')['deleted'] in (0, False)

    # Second sync: U_LEFT no longer in workspace response
    client2 = _fake_client([
        _m('U_STAY', 'Stays Active', 'stay@pearmediallc.com'),
    ])
    counters = slack_sync.run_slack_users_sync(slack_client=client2)
    assert counters['newly_deleted'] == 1
    # Row still exists (for audit), but flagged
    assert store.get_slack_user('U_LEFT') is not None
    assert store.get_slack_user('U_LEFT')['deleted'] in (1, True)
    # And the active one isn't touched
    assert store.get_slack_user('U_STAY')['deleted'] in (0, False)


def test_sync_re_activates_when_rejoined(tmp_inventory, monkeypatch):
    """If someone was marked deleted then comes back (e.g. re-invited),
    the next sync's UPSERT sets deleted=False again."""
    monkeypatch.setattr(Config, 'SLACK_BOT_TOKEN', 'token')

    # First sync: 1 member, then they leave
    slack_sync.run_slack_users_sync(slack_client=_fake_client([
        _m('U_REJOIN', 'Will Rejoin', 'rj@pearmediallc.com'),
    ]))
    slack_sync.run_slack_users_sync(slack_client=_fake_client([]))
    assert store.get_slack_user('U_REJOIN')['deleted'] in (1, True)

    # Third sync: they're back
    slack_sync.run_slack_users_sync(slack_client=_fake_client([
        _m('U_REJOIN', 'Will Rejoin', 'rj@pearmediallc.com'),
    ]))
    assert store.get_slack_user('U_REJOIN')['deleted'] in (0, False)


def test_sync_handles_member_with_no_email(tmp_inventory, monkeypatch):
    """Some accounts don't have an email visible (rare). Sync shouldn't
    crash — just stores email = None."""
    monkeypatch.setattr(Config, 'SLACK_BOT_TOKEN', 'token')
    members = [_m('U_NO_EMAIL', 'No Email', email=None)]
    client = _fake_client(members)

    counters = slack_sync.run_slack_users_sync(slack_client=client)
    assert counters['errors'] == 0
    assert store.get_slack_user('U_NO_EMAIL')['email'] is None


# ─── Off-switches + resilience ────────────────────────────────────────────

def test_sync_skips_when_no_bot_token(tmp_inventory, monkeypatch):
    """Without SLACK_BOT_TOKEN configured, sync is a clean no-op."""
    monkeypatch.setattr(Config, 'SLACK_BOT_TOKEN', '')
    counters = slack_sync.run_slack_users_sync()
    assert counters['fetched'] == 0
    assert counters['errors'] == 0


def test_sync_resilient_to_api_failure(tmp_inventory, monkeypatch):
    """If users_list raises, sync logs + returns errors=1 instead of
    crashing the entire daily cron run."""
    monkeypatch.setattr(Config, 'SLACK_BOT_TOKEN', 'token')

    client = MagicMock()
    client.users_list.side_effect = RuntimeError('slack down')

    counters = slack_sync.run_slack_users_sync(slack_client=client)
    assert counters['fetched'] == 0
    assert counters['errors'] == 1
