"""Domain-name suggestions via OpenAI.

Phase 5 wiring. Phase 1 keeps this as a stub returning a fixed list so
upstream code can be developed without burning OpenAI API quota.
"""
from typing import List
from config import Config


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
    if not Config.OPENAI_API_KEY:
        # Phase 1 fallback so the rest of the flow is testable without keys.
        ext = extension.lstrip('.')
        return [f'{vertical}-stub-{i}.{ext}' for i in range(1, count + 1)]

    # Phase 5: real implementation
    raise NotImplementedError(
        'OpenAI integration is Phase 5. See README for the planned prompt.'
    )
