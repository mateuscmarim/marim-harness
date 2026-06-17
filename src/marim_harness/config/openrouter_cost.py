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
    """An OpenRouter chat model that records the provider's billed cost.

    Imported lazily (it pulls in provider packages) so config-only code paths
    stay dependency-free."""
    from pydantic_ai.models.openai import (
        OpenAIChatModel,
        OpenAIChatModelSettings,
        OpenAIStreamedResponse,
    )
    from pydantic_ai.providers.openrouter import OpenRouterProvider

    class _CostStreamedResponse(OpenAIStreamedResponse):
        def _map_usage(self, response):
            return _with_cost(response, super()._map_usage(response))

    class _CostOpenRouterModel(OpenAIChatModel):
        # Non-streaming path (e.g. the summarizer/titler agents).
        def _map_usage(self, response):
            return _with_cost(response, super()._map_usage(response))

        # Streaming path (every interactive/headless turn): swap the live
        # streamed-response instance to the cost-capturing subclass.
        @asynccontextmanager
        async def request_stream(self, *args, **kwargs):
            async with super().request_stream(*args, **kwargs) as stream:
                if isinstance(stream, OpenAIStreamedResponse):
                    try:
                        stream.__class__ = _CostStreamedResponse
                    except TypeError:  # layout mismatch on some pydantic-ai build
                        pass
                yield stream

    provider = OpenRouterProvider(api_key=api_key)
    settings = OpenAIChatModelSettings(extra_body={"usage": {"include": True}})
    return _CostOpenRouterModel(model_id, provider=provider, settings=settings)
