"""Claude API access. The key comes from the server's .env only."""
from functools import lru_cache

import anthropic

from buddy.config import get_settings


def _options() -> dict:
    s = get_settings()
    if not s.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")
    opts: dict = {"api_key": s.anthropic_api_key}
    if s.anthropic_workspace_id:
        opts["default_headers"] = {"anthropic-workspace-id": s.anthropic_workspace_id}
    return opts


@lru_cache
def sync_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(**_options())


@lru_cache
def async_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(**_options(), timeout=120.0)
