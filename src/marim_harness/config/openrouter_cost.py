"""Capture OpenRouter's exact billed cost into the run usage.

OpenRouter returns the billed ``usage.cost`` (a float, in dollars) when the
request body carries ``{"usage": {"include": true}}``. pydantic-ai's OpenAI
usage mapper, however, keeps only *integer*-valued usage fields, so the float
cost is silently dropped. This module subclasses the chat model and its streamed
response to re-inject the cost as integer micro-USD under
``RunUsage.details[COST_DETAIL_KEY]`` (where genai-based code expects it), and
sets the default request body so accounting is always on.

Because it reaches into pydantic-ai internals, every hook fails soft: a missing
or non-numeric cost simply yields no detail, and callers fall back to the
genai-prices estimate.
"""

from contextlib import asynccontextmanager
from typing import Optional

from ..usage import COST_DETAIL_KEY


def read_cost_micro_usd(response) -> Optional[int]:
    """The billed cost on a chat completion / chunk as integer micro-USD, or
    ``None`` if absent or non-numeric. Reads ``usage.cost`` (the typed field)
    and falls back to pydantic's ``model_extra`` when the SDK didn't model it."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    cost = getattr(usage, "cost", None)
    if cost is None:
        cost = (getattr(usage, "model_extra", None) or {}).get("cost")
    if cost is None:
        return None
    try:
        return round(float(cost) * 1_000_000)
    except (TypeError, ValueError):
        return None


def _with_cost(response, mapped):
    """Re-inject the billed cost (if any) into a freshly mapped RequestUsage."""
    micro = read_cost_micro_usd(response)
    if micro is not None:
        mapped.details[COST_DETAIL_KEY] = micro
    return mapped


def build_openrouter_model(model_id: str, api_key: Optional[str]):
    """An OpenRouter chat model with prompt caching enabled that records the
    provider's billed cost.

    Built on pydantic-ai's official ``OpenRouterModel`` so cache-token mapping
    and OpenRouter usage parsing come natively; the only thing it adds is
    re-injecting the billed ``usage.cost`` (a float the base usage mapper drops)
    into ``RunUsage.details`` as integer micro-USD, where ``usage.py`` reads it.

    Imported lazily (it pulls in provider packages) so config-only code paths
    stay dependency-free."""
    from pydantic_ai.models.openrouter import (
        OpenRouterModel,
        OpenRouterModelSettings,
        OpenRouterStreamedResponse,
    )
    from pydantic_ai.providers.openrouter import OpenRouterProvider

    class _CostStreamedResponse(OpenRouterStreamedResponse):
        # Subclass the OpenRouter streamed response (not the plain OpenAI one)
        # so super()._map_usage still maps cache_read/cache_write tokens.
        def _map_usage(self, response):
            return _with_cost(response, super()._map_usage(response))

    class _CostOpenRouterModel(OpenRouterModel):
        # Non-streaming path (e.g. the summarizer/titler agents).
        def _map_usage(self, response):
            return _with_cost(response, super()._map_usage(response))

        # Streaming path (every interactive/headless turn): swap the live
        # streamed-response instance to the cost-capturing subclass. Both
        # classes share OpenRouterStreamedResponse's layout, so the swap keeps
        # native cache-token mapping intact.
        @asynccontextmanager
        async def request_stream(self, *args, **kwargs):
            async with super().request_stream(*args, **kwargs) as stream:
                if isinstance(stream, OpenRouterStreamedResponse):
                    try:
                        stream.__class__ = _CostStreamedResponse
                    except TypeError:  # layout mismatch on some pydantic-ai build
                        pass
                yield stream

    provider = OpenRouterProvider(api_key=api_key)
    settings = OpenRouterModelSettings(
        openrouter_usage={"include": True},
        openrouter_cache_instructions="5m",
        openrouter_cache_tool_definitions="5m",
        openrouter_cache_messages="5m",
    )
    return _CostOpenRouterModel(model_id, provider=provider, settings=settings)
