"""Claude API access. The key comes from the server's .env only."""
from functools import lru_cache

import anthropic

from buddy.config import get_settings


def _key() -> str:
    key = get_settings().anthropic_api_key
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")
    return key


@lru_cache
def sync_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=_key())


@lru_cache
def async_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=_key(), timeout=120.0)
