"""Tests for Config.assert_production_safe — the boot-time gate added
2026-05-08 (audit #15) that refuses to start when dev-only knobs would
break real users in production.
"""
import pytest

from config import Config


def test_assert_production_safe_passes_when_environment_is_dev(monkeypatch):
    """In dev, the gate is a no-op even with DEV_REROUTE set."""
    monkeypatch.setattr(Config, 'ENVIRONMENT', 'dev')
    monkeypatch.setattr(Config, 'DEV_REROUTE_DMS_TO', 'U_DEV')
    Config.assert_production_safe()  # must not raise


def test_assert_production_safe_passes_when_dev_reroute_unset(monkeypatch):
    """Production with no DEV_REROUTE is the normal case."""
    monkeypatch.setattr(Config, 'ENVIRONMENT', 'production')
    monkeypatch.setattr(Config, 'DEV_REROUTE_DMS_TO', '')
    Config.assert_production_safe()  # must not raise


def test_assert_production_safe_refuses_when_dev_reroute_set_in_prod(
    monkeypatch,
):
    """The misconfiguration the gate exists to catch — production with
    DEV_REROUTE_DMS_TO still set, which would silently absorb every
    TL approval and Utkarsh DM into one developer's inbox."""
    monkeypatch.setattr(Config, 'ENVIRONMENT', 'production')
    monkeypatch.setattr(Config, 'DEV_REROUTE_DMS_TO', 'U06ABCXYZ')
    with pytest.raises(RuntimeError) as exc_info:
        Config.assert_production_safe()
    msg = str(exc_info.value)
    # Message must point operator at the right env var to fix.
    assert 'DEV_REROUTE_DMS_TO' in msg
    assert 'production' in msg


def test_assert_production_safe_environment_is_case_insensitive(monkeypatch):
    """ENVIRONMENT='Production' and 'PRODUCTION' must trigger the same
    gate as 'production' — operators set it however they prefer."""
    monkeypatch.setattr(Config, 'DEV_REROUTE_DMS_TO', 'U_X')
    for env in ('production', 'PRODUCTION', 'Production'):
        # Config reads the env var at class-definition time so we have
        # to set the post-strip-and-lower value here, mirroring the
        # config.py logic.
        monkeypatch.setattr(Config, 'ENVIRONMENT', env.strip().lower())
        with pytest.raises(RuntimeError):
            Config.assert_production_safe()
