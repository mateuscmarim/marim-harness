"""Build the cache-enabled OpenRouter model and capture its billed cost.

The model is pydantic-ai's official ``OpenRouterModel`` configured via
``OpenRouterModelSettings`` to enable prompt caching (``cache_control`` on
instructions, tool definitions, and the rolling message tail) and usage
accounting (``openrouter_usage={"include": True}``). OpenRouter then reports the
billed ``usage.cost`` (a float, in dollars), but pydantic-ai's usage mapper keeps
only *integer*-valued fields and drops the float. So this module subclasses the
model and its ``OpenRouterStreamedResponse`` to re-inject the cost as integer
micro-USD under ``RunUsage.details[COST_DETAIL_KEY]`` (where genai-based code
expects it); each subclass calls ``super()._map_usage`` first, preserving the
native cache-token mapping.

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
