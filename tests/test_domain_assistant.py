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
        audience='seniors looking for medigap',
        extension='.com',
        count=5,
    )
    assert len(out) == 5


def test_chatgpt_stub_uses_vertical_in_names(monkeypatch):
    monkeypatch.setattr(Config, 'OPENAI_API_KEY', '')
    out = chatgpt.suggest_domains(
        vertical='health-quotes', audience="", extension='.com', count=3,
    )
    for name in out:
        assert 'health-quotes' in name


def test_chatgpt_stub_respects_extension(monkeypatch):
    monkeypatch.setattr(Config, 'OPENAI_API_KEY', '')
    out = chatgpt.suggest_domains(
        vertical='x', audience="", extension='.pro', count=2,
    )
    for name in out:
        assert name.endswith('.pro')


def test_chatgpt_short_placeholder_key_falls_back_to_stub(monkeypatch):
    """The .env.example placeholder 'sk-...' must NOT be treated as real."""
    monkeypatch.setattr(Config, 'OPENAI_API_KEY', 'sk-...')
    out = chatgpt.suggest_domains(
        vertical='x', audience="", extension='.com', count=2,
    )
    assert len(out) == 2
    assert all('stub' in name for name in out)


def test_grok_placeholder_also_falls_back_to_stub(monkeypatch):
    """Grok / xAI placeholder should also be ignored."""
    monkeypatch.setattr(Config, 'OPENAI_API_KEY', 'xai-...')
    out = chatgpt.suggest_domains(
        vertical='x', audience="", extension='.com', count=2,
    )
    assert len(out) == 2
    assert all('stub' in name for name in out)


def test_grok_realistic_key_treated_as_real(monkeypatch):
    """A long xai- key (Grok) should be treated as real and trigger the
    real-LLM path. We can't actually call Grok in a unit test, but we can
    verify _is_real_openai_key accepts it."""
    fake_grok_key = 'xai-' + 'a' * 80
    assert chatgpt._is_real_openai_key(fake_grok_key) is True


def test_empty_key_not_real(monkeypatch):
    assert chatgpt._is_real_openai_key('') is False
    assert chatgpt._is_real_openai_key(None) is False


# ─── chatgpt._build_prompt (post-Phase-8.x debias work) ───────────────────

def test_prompt_does_not_hardcode_auto_insurance_brands():
    """The default prompt must NOT anchor on auto-insurance brand names —
    that's what biased Utkarsh's medicare suggestions in 2026-05-08."""
    prompt = chatgpt._build_prompt(
        vertical='medicare', audience='', extension='.com', count=5,
    )
    forbidden = ['carguardianpro', 'safetyfirstauto', 'fixyourhomenow',
                 'drivesafetyhub', 'instapolicy', 'flexicover',
                 'swiftquoter', 'easyrater']
    for f in forbidden:
        assert f not in prompt, (
            f'Old hardcoded auto-insurance example {f!r} leaked into '
            f'a generic prompt — anchors LLM on wrong vertical.'
        )


def test_prompt_includes_user_examples_when_provided():
    prompt = chatgpt._build_prompt(
        vertical='medicare', audience='seniors',
        extension='.com', count=5,
        examples=['mymedicareexperts.online', 'seniorhealthhub.com'],
    )
    assert 'mymedicareexperts.online' in prompt
    assert 'seniorhealthhub.com' in prompt
    # The prompt must instruct NOT to reuse the examples verbatim.
    assert 'do NOT reuse' in prompt or 'NOT reuse' in prompt


def test_prompt_falls_back_to_generic_pattern_when_no_examples():
    """Without examples, the prompt should describe SHAPE patterns
    (compounds, brandable invented words) and explicitly tie the
    vocabulary to the *vertical*, not to auto-insurance."""
    prompt = chatgpt._build_prompt(
        vertical='legal-aid', audience='', extension='.com', count=5,
    )
    assert 'legal-aid' in prompt
    # Should reference the vertical's vocabulary explicitly
    assert ('vocabulary must come from' in prompt.lower()
            or 'words native to the vertical' in prompt.lower())


def test_chatgpt_stub_passes_through_examples_param(monkeypatch):
    """The new `examples` kwarg must be accepted on the stub path
    without error so workflow can pass it through unconditionally."""
    monkeypatch.setattr(Config, 'OPENAI_API_KEY', '')
    out = chatgpt.suggest_domains(
        vertical='auto-insurance', audience='', extension='.com', count=3,
        examples=['carguardianpro.com', 'safetyfirstauto.com'],
    )
    assert len(out) == 3


# ─── chatgpt._parse_model_response (parses OpenAI replies) ────────────────

def test_parse_strips_numbering_prefixes():
    out = chatgpt._parse_model_response(
        '1. quickauto.com\n2) rateshero.com\n3. clickquote.com',
        '.com', count=10,
    )
    assert out == ['quickauto.com', 'rateshero.com', 'clickquote.com']


def test_parse_strips_bullet_prefixes():
    out = chatgpt._parse_model_response(
        '- quickauto.com\n* rateshero.com\n• clickquote.com',
        '.com', count=10,
    )
    assert out == ['quickauto.com', 'rateshero.com', 'clickquote.com']


def test_parse_strips_quotes_and_normalises_case():
    out = chatgpt._parse_model_response(
        '"QuickAuto.COM"\n\'RatesHero.com\'\n`ClickQuote.com`',
        '.com', count=10,
    )
    assert out == ['quickauto.com', 'rateshero.com', 'clickquote.com']


def test_parse_filters_wrong_extension():
    """If the model ignores instructions and returns a different TLD,
    we drop it rather than confuse the caller."""
    out = chatgpt._parse_model_response(
        'quickauto.com\nrateshero.net\nclickquote.com',
        '.com', count=10,
    )
    assert out == ['quickauto.com', 'clickquote.com']


def test_parse_respects_count_limit():
    out = chatgpt._parse_model_response(
        'a.com\nb.com\nc.com\nd.com\ne.com',
        '.com', count=3,
    )
    assert out == ['a.com', 'b.com', 'c.com']


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
