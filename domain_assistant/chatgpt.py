"""Domain-name suggestions via OpenAI."""
import re
from typing import List, Optional
from config import Config


def _is_real_openai_key(value: Optional[str]) -> bool:
    """Distinguish a real OpenAI key from the placeholder / empty value.

    Real OpenAI keys look like `sk-...` or `sk-proj-...` and are 40+ chars.
    The .env.example placeholder is the literal string `sk-...` (6 chars),
    which we treat as "no key set" so the stub keeps running for local dev.
    """
    if not value:
        return False
    return value.startswith('sk-') and len(value) > 20


def _stub_suggestions(vertical: str, extension: str, count: int) -> List[str]:
    """Deterministic fallback used when no real OpenAI key is configured."""
    ext = extension.lstrip('.')
    return [f'{vertical}-stub-{i}.{ext}' for i in range(1, count + 1)]


def _build_prompt(vertical: str, example_domains: List[str],
                  extension: str, count: int) -> str:
    examples_line = (
        f'Names I like the style of: {", ".join(example_domains)}.'
        if example_domains else
        '(No example names provided — use your judgement on style.)'
    )
    return (
        f'Suggest {count} domain-name ideas for a {vertical!r} '
        f'landing page.\n'
        f'{examples_line}\n\n'
        f'Requirements:\n'
        f'- Every name must end with "{extension}"\n'
        f'- Lowercase, no spaces; hyphens OK\n'
        f'- Short and memorable (under ~25 chars including the extension)\n'
        f"- Plausibly available — don't suggest big-brand names\n"
        f'- One name per line\n'
        f'- No numbering, no quotes, no commentary\n\n'
        f'Just {count} domain names, nothing else.'
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


def suggest_domains(vertical: str, example_domains: List[str],
                    extension: str = '.com', count: int = 10) -> List[str]:
    """Generate `count` domain-name suggestions for the given vertical.

    Falls back to deterministic stub names if no real OpenAI key is
    configured, so local dev / unit tests work without external creds.
    Otherwise calls OpenAI Chat Completions with a structured prompt.

    Args:
      vertical: e.g. "auto-insurance"
      example_domains: 2-3 seed names the MDB likes the style of
      extension: ".com" / ".pro" / ".site" / etc.
      count: how many to suggest (before any availability filtering)
    """
    if not _is_real_openai_key(Config.OPENAI_API_KEY):
        return _stub_suggestions(vertical, extension, count)

    # Lazy import: keeps the import cheap when the stub path is taken,
    # and avoids forcing test environments to have the openai package
    # if they only exercise the stub.
    from openai import OpenAI

    client = OpenAI(api_key=Config.OPENAI_API_KEY)
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
                    vertical, example_domains, extension, count,
                ),
            },
        ],
        temperature=0.8,   # some variety, not random
        max_tokens=400,
    )

    content = (response.choices[0].message.content or '').strip()
    return _parse_model_response(content, extension, count)
