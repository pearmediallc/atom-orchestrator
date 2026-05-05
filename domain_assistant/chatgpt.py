"""Domain-name suggestions via an OpenAI-compatible LLM.

Works with OpenAI directly OR any compatible provider (e.g. Grok / xAI)
when OPENAI_BASE_URL is set in the env.
"""
import re
from typing import List, Optional
from config import Config


# Common placeholder strings we don't want mistaken for real keys.
_KEY_PLACEHOLDERS = {
    'sk-...',
    'xai-...',
    'your-key-here',
    'your-openai-key-here',
}


def _is_real_openai_key(value: Optional[str]) -> bool:
    """Tell apart a real LLM API key from a placeholder / empty value.

    Real keys (OpenAI `sk-...`, Grok `xai-...`, etc.) are 30+ chars.
    Placeholders from .env.example or empty strings are rejected so
    the stub fallback kicks in instead.
    """
    if not value:
        return False
    if value.strip() in _KEY_PLACEHOLDERS:
        return False
    return len(value) >= 20


def _stub_suggestions(vertical: str, extension: str, count: int) -> List[str]:
    """Deterministic fallback used when no real OpenAI key is configured."""
    ext = extension.lstrip('.')
    return [f'{vertical}-stub-{i}.{ext}' for i in range(1, count + 1)]


def _build_prompt(vertical: str, audience: str,
                  extension: str, count: int) -> str:
    audience_line = (
        f'Audience / angle: {audience}.'
        if audience else
        '(No specific audience given — generate broad options for the vertical.)'
    )
    return (
        f'You are a domain-name generator for an affiliate-marketing team '
        f'(Pear Media). Suggest {count} landing-page domain-name ideas '
        f'for the *{vertical}* vertical.\n'
        f'{audience_line}\n\n'
        f'CRITICAL — names must actually be available to register on '
        f'Namecheap. Short single-word names like "cheapauto.com" or '
        f'"lowrate.com" are virtually ALWAYS taken by squatters. Aim for:\n'
        f'- 3-word compound names — e.g. "carguardianpro", "safetyfirstauto", '
        f'"fixyourhomenow", "drivesafetyhub"\n'
        f'- Brandable made-up words — e.g. "instapolicy", "flexicover", '
        f'"swiftquoter", "easyrater"\n'
        f'- Descriptive phrases joined with hyphens — e.g. "best-{vertical}-2026", '
        f'"smart-{vertical}-finder", "your-{vertical}-quote"\n'
        f'\n'
        f'Other rules:\n'
        f'- Every name must end with "{extension}"\n'
        f'- Lowercase, no spaces; hyphens OK\n'
        f'- 12-30 chars including the extension (avoid both very short and very long)\n'
        f'- DO NOT suggest big-brand names (Geico, Allstate, Aetna, etc.)\n'
        f'- DO NOT use "the" / "my" / "your" excessively — they don\'t make '
        f'  a name more available\n'
        f'- One name per line\n'
        f'- No numbering, no quotes, no commentary\n\n'
        f'Generate {count} truly varied options that prioritise '
        f'availability over brevity.'
    )


def _parse_model_response(content: str, extension: str,
                          count: int) -> List[str]:
    """Strip numbering / bullets / quotes from each line, validate the
    extension, and return a clean list."""
    names: List[str] = []
    for raw in content.split('\n'):
        line = raw.strip()
        if not line:
            continue
        # Drop leading "1." / "1)" / "•" / "-" / "*" markers
        line = re.sub(r'^\d+[\.\)]\s*', '', line)
        line = re.sub(r'^[-*•]\s*', '', line)
        line = line.strip().strip('\'"`').lower()
        if line.endswith(extension.lower()):
            names.append(line)
    return names[:count]


def suggest_domains(vertical: str, audience: str = '',
                    extension: str = '.com', count: int = 10) -> List[str]:
    """Generate `count` domain-name suggestions for the given vertical.

    Falls back to deterministic stub names if no real OpenAI key is
    configured, so local dev / unit tests work without external creds.
    Otherwise calls OpenAI Chat Completions with a structured prompt.

    Args:
      vertical: e.g. "auto-insurance"
      audience: optional free-text describing target audience / angle
                (e.g. "seniors looking for medigap"). Empty string OK.
      extension: ".com" / ".pro" / ".site" / etc.
      count: how many to suggest (before any availability filtering)
    """
    if not _is_real_openai_key(Config.OPENAI_API_KEY):
        return _stub_suggestions(vertical, extension, count)

    # Lazy import: keeps the import cheap when the stub path is taken,
    # and avoids forcing test environments to have the openai package
    # if they only exercise the stub.
    from openai import OpenAI

    client_kwargs = {'api_key': Config.OPENAI_API_KEY}
    if Config.OPENAI_BASE_URL:
        # Routes the SDK at a non-OpenAI provider (Grok / xAI, etc.) using
        # the OpenAI-compatible API spec they expose.
        client_kwargs['base_url'] = Config.OPENAI_BASE_URL

    client = OpenAI(**client_kwargs)
    response = client.chat.completions.create(
        model=Config.OPENAI_MODEL,
        messages=[
            {
                'role': 'system',
                'content': (
                    'You are a domain-name generator. Reply with ONLY the '
                    'requested domain names, one per line, with no '
                    'numbering, quotes, or commentary.'
                ),
            },
            {
                'role': 'user',
                'content': _build_prompt(
                    vertical, audience, extension, count,
                ),
            },
        ],
        temperature=0.8,   # some variety, not random
        max_tokens=400,
    )

    content = (response.choices[0].message.content or '').strip()
    return _parse_model_response(content, extension, count)
