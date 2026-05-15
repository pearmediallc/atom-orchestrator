"""Tests for lifecycle.dm.dm — rate-limit retry + pacing.

The retry-on-ratelimited path was added 2026-05-15 after the first live
cron's catch-up wave (~700 sends to one channel) had every send beyond
the first dozen cascade-fail with Slack's 'ratelimited' response. With
retry + Retry-After respect + post-send pacing, the burst self-paces.
"""
import time
from unittest.mock import MagicMock

from config import Config
from lifecycle import dm as _dm


class _FakeSlackResponse(dict):
    """Mimics enough of slack_sdk.web.SlackResponse for the retry logic
    to read .get('error') and headers['Retry-After']."""

    def __init__(self, error, retry_after=None):
        super().__init__()
        self['error'] = error
        self['ok'] = False
        self.headers = {}
        if retry_after is not None:
            self.headers['Retry-After'] = str(retry_after)


class _FakeSlackApiError(Exception):
    """Stand-in for slack_sdk.errors.SlackApiError — same shape (response
    attribute with .get + .headers)."""

    def __init__(self, error, retry_after=None):
        super().__init__(f'slack api error: {error}')
        self.response = _FakeSlackResponse(error, retry_after=retry_after)


def _patch_no_pace(monkeypatch):
    """Zero the post-send sleep so tests don't wait 1.2s/each."""
    monkeypatch.setattr(Config, 'LIFECYCLE_DM_PACE_SECONDS', 0.0)
    monkeypatch.setattr(Config, 'LIFECYCLE_DM_RETRY_SLEEP_SECONDS', 0.0)
    monkeypatch.setattr(Config, 'LIFECYCLE_DRY_RUN', False)
    monkeypatch.setattr(Config, 'DEV_REROUTE_DMS_TO', '')


def test_dm_retries_once_on_ratelimited_then_succeeds(monkeypatch):
    """One 'ratelimited' then success → retry kicks in, dm returns the
    success response. Without this the wave would lose every DM after
    the first dozen."""
    _patch_no_pace(monkeypatch)
    # No-op sleep so the test runs instantly even with a "Retry-After".
    monkeypatch.setattr(time, 'sleep', lambda _s: None)

    client = MagicMock(name='slack_client')
    client.chat_postMessage.side_effect = [
        _FakeSlackApiError('ratelimited', retry_after=2),
        {'ok': True, 'channel': 'U_X', 'ts': '1.001'},
    ]
    result = _dm.dm(client, real_recipient='U_X', text='hi',
                    dry_run_label='test')
    assert result == {'ok': True, 'channel': 'U_X', 'ts': '1.001'}
    assert client.chat_postMessage.call_count == 2


def test_dm_gives_up_after_max_retries(monkeypatch):
    """Three 'ratelimited' in a row → exhausts the retry budget and
    re-raises so the caller's per-row exception handler can log it."""
    _patch_no_pace(monkeypatch)
    monkeypatch.setattr(time, 'sleep', lambda _s: None)

    client = MagicMock(name='slack_client')
    client.chat_postMessage.side_effect = [
        _FakeSlackApiError('ratelimited', retry_after=1),
        _FakeSlackApiError('ratelimited', retry_after=1),
        _FakeSlackApiError('ratelimited', retry_after=1),
    ]
    try:
        _dm.dm(client, real_recipient='U_X', text='hi', dry_run_label='test')
        raised = False
    except _FakeSlackApiError:
        raised = True
    assert raised
    # 1 initial + 2 retries = 3 calls (the _RATELIMIT_MAX_RETRIES default).
    assert client.chat_postMessage.call_count == 3


def test_dm_does_not_retry_on_non_ratelimit_errors(monkeypatch):
    """Other Slack errors (channel_not_found, invalid_auth, etc.) must
    NOT trigger retry — they're permanent failures, not transient."""
    _patch_no_pace(monkeypatch)
    monkeypatch.setattr(time, 'sleep', lambda _s: None)

    client = MagicMock(name='slack_client')
    client.chat_postMessage.side_effect = _FakeSlackApiError('channel_not_found')
    try:
        _dm.dm(client, real_recipient='U_X', text='hi', dry_run_label='test')
        raised = False
    except _FakeSlackApiError:
        raised = True
    assert raised
    assert client.chat_postMessage.call_count == 1  # no retry


def test_dm_paces_after_successful_send(monkeypatch):
    """The post-send sleep keeps a burst under Slack's ~1/sec per-channel
    cap. Verify time.sleep is called with the configured pace."""
    monkeypatch.setattr(Config, 'LIFECYCLE_DRY_RUN', False)
    monkeypatch.setattr(Config, 'DEV_REROUTE_DMS_TO', '')
    monkeypatch.setattr(Config, 'LIFECYCLE_DM_PACE_SECONDS', 1.2)

    sleeps: list = []
    monkeypatch.setattr(time, 'sleep', lambda s: sleeps.append(s))

    client = MagicMock(name='slack_client')
    client.chat_postMessage.return_value = {'ok': True, 'channel': 'U_X',
                                            'ts': '1.001'}
    _dm.dm(client, real_recipient='U_X', text='hi', dry_run_label='test')
    assert 1.2 in sleeps


def test_dm_does_not_pace_on_dry_run(monkeypatch):
    """Dry-run logs only — never calls Slack, never sleeps."""
    monkeypatch.setattr(Config, 'LIFECYCLE_DRY_RUN', True)
    monkeypatch.setattr(Config, 'LIFECYCLE_DM_PACE_SECONDS', 1.2)

    sleeps: list = []
    monkeypatch.setattr(time, 'sleep', lambda s: sleeps.append(s))

    client = MagicMock(name='slack_client')
    result = _dm.dm(client, real_recipient='U_X', text='hi',
                    dry_run_label='test')
    assert result.get('dry_run') is True
    assert sleeps == []
    client.chat_postMessage.assert_not_called()
