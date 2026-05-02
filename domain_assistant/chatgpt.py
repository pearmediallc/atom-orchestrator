"""Domain-name suggestions via OpenAI.

Phase 5 wiring. Phase 1 keeps this as a stub returning a fixed list so
upstream code can be developed without burning OpenAI API quota.
"""
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


# Phase 5 TODO: replace with `from openai import OpenAI` and the real call.
def suggest_domains(vertical: str, example_domains: List[str],
                    extension: str = '.com', count: int = 10) -> List[str]:
    """Generate `count` domain-name suggestions for the given vertical.

    Args:
      vertical: e.g. "auto-insurance"
      example_domains: 2-3 seed names the MDB likes the style of
      extension: ".com" / ".pro" / ".site"
      count: how many to suggest before availability filtering
    """
    if not _is_real_openai_key(Config.OPENAI_API_KEY):
        # Phase 1 fallback so the rest of the flow is testable without keys.
        ext = extension.lstrip('.')
        return [f'{vertical}-stub-{i}.{ext}' for i in range(1, count + 1)]

    # Phase 5: real implementation
    raise NotImplementedError(
        'OpenAI integration is Phase 5. See README for the planned prompt.'
    )
