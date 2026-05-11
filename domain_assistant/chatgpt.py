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
                  extension: str, count: int,
                  examples: Optional[List[str]] = None) -> str:
    audience_line = (
        f'Audience / angle: {audience}.'
        if audience else
        '(No specific audience given — generate broad options for the vertical.)'
    )

    # Stylistic anchor — user-supplied examples take precedence over the
    # generic patterns. Without examples we describe the SHAPE of good
    # names without naming any specific brands, so the LLM doesn't anchor
    # on (e.g.) auto-insurance vocabulary when the vertical is something
    # else.
    if examples:
        cleaned_examples = [e.strip() for e in examples if e and e.strip()]
    else:
        cleaned_examples = []

    if cleaned_examples:
        style_block = (
            'Match the STYLE of these user-provided examples '
            '(stylistic anchor only — do NOT reuse these exact names):\n'
            + '\n'.join(f'  • {e}' for e in cleaned_examples)
            + '\n\nGenerate names that feel like the same family — '
            'similar word count, tone, compounding pattern, vocabulary '
            'register — but appropriate to the *' + vertical + '* vertical.\n'
        )
    else:
        style_block = (
            'Aim for one of these patterns (without using these exact names):\n'
            '- 2–3 word compounds built from words native to the vertical '
            '(domain noun + benefit/quality word, e.g. <core-noun><guard|hub|pro|expert>)\n'
            '- Brandable invented words formed by blending a vertical word '
            'with a short positive modifier (e.g. <prefix-syllable><vertical-syllable>)\n'
            '- Hyphenated descriptive phrases anchored to the vertical '
            f'(e.g. best-{vertical}-finder, smart-{vertical}-quote)\n'
            'Vocabulary must come from the *' + vertical + '* vertical itself, '
            'NOT from auto-insurance unless that IS the vertical.\n'
        )

    return (
        f'You are a domain-name generator for an affiliate-marketing team '
        f'(Pear Media). Suggest {count} landing-page domain-name ideas '
        f'for the *{vertical}* vertical.\n'
        f'{audience_line}\n\n'
        f'CRITICAL — names must actually be available to register on '
        f'Namecheap. Short single-word names ("cheapauto", "lowrate", etc.) '
        f'are virtually ALWAYS taken by squatters.\n\n'
        f'{style_block}\n'
        f'Other rules:\n'
        f'- Every name must end with "{extension}"\n'
        f'- Lowercase, no spaces; hyphens OK\n'
        f'- 12-30 chars including the extension (avoid both very short and very long)\n'
        f'- DO NOT suggest big-brand names (Geico, Allstate, Aetna, '
        f'UnitedHealth, etc.)\n'
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
                    extension: str = '.com', count: int = 10,
                    examples: Optional[List[str]] = None) -> List[str]:
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
      examples: optional list of user-supplied sample domain names whose
                stylistic feel the AI should match (NOT reuse). Anchors
                the suggestions to the vertical's vocabulary instead of
                the prompt's generic defaults.
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
                    vertical, audience, extension, count, examples=examples,
                ),
            },
        ],
        temperature=0.8,   # some variety, not random
        max_tokens=400,
    )

    content = (response.choices[0].message.content or '').strip()
    return _parse_model_response(content, extension, count)
