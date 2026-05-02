"""Unit tests for domain_assistant.

These tests run against the Phase 1 stubs (no API keys present). When real
OPENAI_API_KEY / NAMECHEAP_* values arrive in production, those code paths
get separate tests (or get mocked) — the stub behaviour stays for local dev.
"""
import pytest
from config import Config
from domain_assistant import chatgpt, namecheap_check


# ─── chatgpt.suggest_domains ──────────────────────────────────────────────

def test_chatgpt_stub_returns_requested_count(monkeypatch):
    monkeypatch.setattr(Config, 'OPENAI_API_KEY', '')
    out = chatgpt.suggest_domains(
        vertical='auto-insurance',
        example_domains=['cheaprates.com', 'quickquote.com'],
        extension='.com',
        count=5,
    )
    assert len(out) == 5


def test_chatgpt_stub_uses_vertical_in_names(monkeypatch):
    monkeypatch.setattr(Config, 'OPENAI_API_KEY', '')
    out = chatgpt.suggest_domains(
        vertical='health-quotes', example_domains=[], extension='.com', count=3,
    )
    for name in out:
        assert 'health-quotes' in name


def test_chatgpt_stub_respects_extension(monkeypatch):
    monkeypatch.setattr(Config, 'OPENAI_API_KEY', '')
    out = chatgpt.suggest_domains(
        vertical='x', example_domains=[], extension='.pro', count=2,
    )
    for name in out:
        assert name.endswith('.pro')


def test_chatgpt_real_path_raises_when_key_present_but_unimplemented(monkeypatch):
    """Phase 5+ wiring isn't done yet — real path should raise clearly.

    Uses a 40+ char fake key so it passes the _is_real_openai_key heuristic
    (short keys like 'sk-...' from the .env.example placeholder fall through
    to the stub instead).
    """
    monkeypatch.setattr(
        Config, 'OPENAI_API_KEY',
        'sk-fakekey-that-is-clearly-long-enough-to-look-real-12345',
    )
    with pytest.raises(NotImplementedError):
        chatgpt.suggest_domains(vertical='x', example_domains=[],
                                extension='.com', count=1)


def test_chatgpt_short_placeholder_key_falls_back_to_stub(monkeypatch):
    """The .env.example placeholder 'sk-...' must NOT be treated as real."""
    monkeypatch.setattr(Config, 'OPENAI_API_KEY', 'sk-...')
    out = chatgpt.suggest_domains(
        vertical='x', example_domains=[], extension='.com', count=2,
    )
    assert len(out) == 2
    # Returns the stub names — not a NotImplementedError
    assert all('stub' in name for name in out)


# ─── namecheap_check.check_availability ───────────────────────────────────

def test_namecheap_stub_returns_all_available_when_creds_missing(monkeypatch):
    monkeypatch.setattr(Config, 'NAMECHEAP_API_USER', '')
    monkeypatch.setattr(Config, 'NAMECHEAP_API_KEY', '')
    monkeypatch.setattr(Config, 'NAMECHEAP_CLIENT_IP', '')

    out = namecheap_check.check_availability(['a.com', 'b.com', 'c.com'])
    assert out == {'a.com': True, 'b.com': True, 'c.com': True}


def test_namecheap_stub_handles_empty_list(monkeypatch):
    monkeypatch.setattr(Config, 'NAMECHEAP_API_USER', '')
    assert namecheap_check.check_availability([]) == {}
