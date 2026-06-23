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

# MiniMax M3 emits its chain-of-thought wrapped in these native tags. OpenRouter
# normally extracts that into the structured ``reasoning`` field, but its
# streaming extraction is imperfect and occasionally leaks a tag fragment
# (typically the bare closing tag) into the ``content`` stream. The OpenRouter
# model profile defaults ``thinking_tags`` to the generic ``<think>`` pair, so
# pydantic-ai's inline-tag splitter doesn't recognize MiniMax's variant and the
# fragments render as visible assistant text. We point the profile at the right
# tags (so well-formed inline blocks become proper ThinkingParts) and scrub any
# orphan tag the provider still leaks.
MM_THINK_TAGS = ("<mm:think>", "</mm:think>")
_MM_MAX_PARTIAL = max(len(t) for t in MM_THINK_TAGS) - 1


def scrub_orphan_thinking_tags(text: str, carry: str) -> tuple[str, str]:
    """Strip orphan MiniMax thinking-tag literals from already-split stream text.

    pydantic-ai's splitter consumes every *well-formed* ``<mm:think>…</mm:think>``
    pair into a ThinkingPart, so any tag literal still present in the text it
    emits is necessarily an orphan the provider leaked — safe to drop. Returns
    ``(clean_text, carry)`` where ``carry`` holds a trailing partial tag (a
    proper prefix of one of the tags) to be completed by the next chunk; the
    caller threads ``carry`` back in on the following delta."""
    s = carry + text
    for tag in MM_THINK_TAGS:
        s = s.replace(tag, "")
    # Hold back the longest suffix that could be the head of a tag split across
    # the chunk boundary, so a ``</mm:th`` + ``ink>`` split still gets removed.
    hold = 0
    for i in range(1, min(len(s), _MM_MAX_PARTIAL) + 1):
        if any(t.startswith(s[-i:]) for t in MM_THINK_TAGS):
            hold = i
    return (s[:-hold], s[-hold:]) if hold else (s, "")


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
    import dataclasses

    from pydantic_ai.messages import (
        PartDeltaEvent,
        PartStartEvent,
        TextPart,
        TextPartDelta,
    )
    from pydantic_ai.models.openai import (
        OpenAIChatModel,
        OpenAIChatModelSettings,
        OpenAIStreamedResponse,
    )
    from pydantic_ai.providers.openrouter import OpenRouterProvider

    class _CostStreamedResponse(OpenAIStreamedResponse):
        def _map_usage(self, response):
            return _with_cost(response, super()._map_usage(response))

        def _map_text_delta(self, choice):
            # Only active for MiniMax (the sole profile carrying these tags):
            # scrub orphan thinking tags the base splitter couldn't pair. Real
            # pairs are already routed to ThinkingParts upstream, so anything
            # left in the text stream is a leaked fragment.
            if self._model_profile.thinking_tags != MM_THINK_TAGS:
                yield from super()._map_text_delta(choice)
                return
            carry = getattr(self, "_mm_carry", "")
            for event in super()._map_text_delta(choice):
                if isinstance(event, PartStartEvent) and isinstance(
                    event.part, TextPart
                ):
                    clean, carry = scrub_orphan_thinking_tags(event.part.content, carry)
                    event = dataclasses.replace(
                        event, part=dataclasses.replace(event.part, content=clean)
                    )
                elif isinstance(event, PartDeltaEvent) and isinstance(
                    event.delta, TextPartDelta
                ):
                    clean, carry = scrub_orphan_thinking_tags(
                        event.delta.content_delta or "", carry
                    )
                    event = dataclasses.replace(
                        event,
                        delta=dataclasses.replace(event.delta, content_delta=clean),
                    )
                yield event
            self._mm_carry = carry

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
    # MiniMax's native thinking tags aren't the OpenRouter profile default, so
    # point the profile at them: pydantic-ai then splits inline thinking into
    # ThinkingParts (in both the streaming and non-streaming paths) instead of
    # letting the tags leak into visible text.
    profile = None
    if "minimax" in model_id.lower():
        base = provider.model_profile(model_id)
        if base is not None:
            profile = dataclasses.replace(base, thinking_tags=MM_THINK_TAGS)
    settings = OpenAIChatModelSettings(extra_body={"usage": {"include": True}})
    return _CostOpenRouterModel(
        model_id, provider=provider, settings=settings, profile=profile
    )
