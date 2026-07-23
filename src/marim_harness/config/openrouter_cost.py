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

The streamed subclass also scrubs MiniMax's leaked ``<mm:think>`` tag fragments
(see ``scrub_orphan_thinking_tags``), and ``build_openrouter_model`` points the
MiniMax profile at those tags so pydantic-ai routes well-formed inline thinking
into ThinkingParts.

Because it reaches into pydantic-ai internals, every hook fails soft: a missing
or non-numeric cost simply yields no detail, and callers fall back to the
genai-prices estimate.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING

from ..usage import COST_DETAIL_KEY

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelResponseStreamEvent
    from pydantic_ai.usage import RequestUsage

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


def read_cost_micro_usd(response) -> int | None:
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


def _with_cost(response, mapped: RequestUsage) -> RequestUsage:
    """Re-inject the billed cost (if any) into a freshly mapped RequestUsage."""
    micro = read_cost_micro_usd(response)
    if micro is not None:
        mapped.details[COST_DETAIL_KEY] = micro
    return mapped


def _scrub_text_event(event, carry: str):
    """Scrub orphan MiniMax tag literals from one already-split stream event.

    Threads ``carry`` (a held trailing partial tag) through and returns
    ``(possibly-replaced event, new carry)``. Only text-start / text-delta
    events carry content to scrub; anything else passes through untouched.
    Lives at module scope (rather than inside ``_make_cost_classes``) so its
    isinstance branches don't fold into that factory's McCabe count — and so it
    can be unit-tested directly."""
    import dataclasses

    from pydantic_ai.messages import (
        PartDeltaEvent,
        PartStartEvent,
        TextPart,
        TextPartDelta,
    )

    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
        clean, carry = scrub_orphan_thinking_tags(event.part.content, carry)
        event = dataclasses.replace(event, part=dataclasses.replace(event.part, content=clean))
    elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        clean, carry = scrub_orphan_thinking_tags(event.delta.content_delta or "", carry)
        event = dataclasses.replace(
            event, delta=dataclasses.replace(event.delta, content_delta=clean)
        )
    return event, carry


def _make_streamed_cls(base):
    """Build the cost-capturing ``OpenRouterStreamedResponse`` subclass.

    Split out of ``_make_cost_classes`` so each factory keeps its McCabe count
    low — a nested class's method bodies fold their branches into the enclosing
    function for ruff's C901 count, so one factory holding both classes trips
    the ceiling. ``base`` is pydantic-ai's ``OpenRouterStreamedResponse``,
    imported lazily by the caller."""

    class _CostStreamedResponse(base):
        # Subclass the OpenRouter streamed response (not the plain OpenAI one)
        # so super()._map_usage still maps cache_read/cache_write tokens.
        def _map_usage(self, response) -> RequestUsage:
            return _with_cost(response, super()._map_usage(response))

        def _map_text_delta(self, choice) -> Iterator[ModelResponseStreamEvent]:
            # Only active for MiniMax (the sole profile carrying these tags):
            # scrub orphan thinking tags the base splitter couldn't pair. Real
            # pairs are already routed to ThinkingParts upstream, so anything
            # left in the text stream is a leaked fragment.
            # ModelProfile is a TypedDict in pydantic-ai 2.x — key access only.
            if self._model_profile.get("thinking_tags") != MM_THINK_TAGS:
                yield from super()._map_text_delta(choice)
                return
            carry = getattr(self, "_mm_carry", "")
            for event in super()._map_text_delta(choice):
                event, carry = _scrub_text_event(event, carry)
                yield event
            self._mm_carry = carry

        def _flush_orphan_carry(self) -> Iterator[ModelResponseStreamEvent]:
            # Re-emit any held partial-tag text at end-of-stream. scrub_orphan_
            # thinking_tags holds back a trailing proper-prefix of a tag (a lone
            # ``<`` or ``<mm:th``) in case the tag completes on the next delta. When
            # the stream ends it never will, so that text is real content — emit it
            # as plain text (no thinking_tags, or the parts manager would hold it
            # back again) instead of silently dropping the final characters.
            carry = getattr(self, "_mm_carry", "")
            if not carry:
                return
            self._mm_carry = ""
            yield from self._parts_manager.handle_text_delta(
                vendor_part_id="content", content=carry
            )

        async def _get_event_iterator(self):
            async for event in super()._get_event_iterator():
                yield event
            for event in self._flush_orphan_carry():
                yield event

    return _CostStreamedResponse


def _make_model_cls(base, streamed_base, streamed_cls):
    """Build the cost-capturing ``OpenRouterModel`` subclass. Split out of
    ``_make_cost_classes`` for the same C901 reason as ``_make_streamed_cls``.
    ``base``/``streamed_base`` are pydantic-ai's ``OpenRouterModel`` and
    ``OpenRouterStreamedResponse``; ``streamed_cls`` is the streamed subclass
    the live response instance is retyped to."""

    class _CostOpenRouterModel(base):
        # Non-streaming path (e.g. the summarizer/titler agents).
        def _map_usage(self, response) -> RequestUsage:
            return _with_cost(response, super()._map_usage(response))

        # Streaming path (every interactive/headless turn): swap the live
        # streamed-response instance to the cost-capturing subclass. Both
        # classes share OpenRouterStreamedResponse's layout, so the swap keeps
        # native cache-token mapping intact.
        @asynccontextmanager
        async def request_stream(self, *args, **kwargs):
            async with super().request_stream(*args, **kwargs) as stream:
                if isinstance(stream, streamed_base):
                    with suppress(TypeError):  # layout mismatch on some pydantic-ai build
                        stream.__class__ = streamed_cls
                yield stream

    return _CostOpenRouterModel


def _make_cost_classes():
    """Build the ``_CostStreamedResponse``/``_CostOpenRouterModel`` subclasses.

    Hoisted out of ``build_openrouter_model`` (rather than left as nested
    classes) purely to keep that factory's McCabe count low. The classes need
    pydantic-ai's OpenRouter types, which are imported lazily here (called once,
    at import-adjacent construction time) rather than at module scope. The two
    subclasses are themselves built by dedicated sub-factories so no single
    function's McCabe count folds in both class bodies at once."""
    from pydantic_ai.models.openrouter import (
        OpenRouterModel,
        OpenRouterStreamedResponse,
    )

    streamed_cls = _make_streamed_cls(OpenRouterStreamedResponse)
    model_cls = _make_model_cls(OpenRouterModel, OpenRouterStreamedResponse, streamed_cls)
    return model_cls, streamed_cls


def build_openrouter_model(model_id: str, api_key: str | None):
    """An OpenRouter chat model with prompt caching enabled that records the
    provider's billed cost.

    Built on pydantic-ai's official ``OpenRouterModel`` so cache-token mapping
    and OpenRouter usage parsing come natively; the only thing it adds is
    re-injecting the billed ``usage.cost`` (a float the base usage mapper drops)
    into ``RunUsage.details`` as integer micro-USD, where ``usage.py`` reads it,
    plus MiniMax thinking-tag handling.

    Imported lazily (it pulls in provider packages) so config-only code paths
    stay dependency-free."""
    from pydantic_ai.models.openrouter import OpenRouterModelSettings
    from pydantic_ai.profiles import merge_profile
    from pydantic_ai.providers.openrouter import OpenRouterProvider

    _CostOpenRouterModel, _ = _make_cost_classes()

    provider = OpenRouterProvider(api_key=api_key)
    # MiniMax's native thinking tags aren't the OpenRouter profile default, so
    # point the profile at them: pydantic-ai then splits inline thinking into
    # ThinkingParts (in both the streaming and non-streaming paths) instead of
    # letting the tags leak into visible text.
    profile = None
    if "minimax" in model_id.lower():
        # ModelProfile is a TypedDict in 2.x: merge_profile replaces the old
        # dataclasses.replace, and handles a None base by building from scratch.
        base = provider.model_profile(model_id)
        profile = merge_profile(base, {"thinking_tags": MM_THINK_TAGS})
    settings = OpenRouterModelSettings(
        openrouter_usage={"include": True},
        openrouter_cache_instructions="5m",
        openrouter_cache_tool_definitions="5m",
        openrouter_cache_messages="5m",
    )
    return _CostOpenRouterModel(
        model_id, provider=provider, settings=settings, profile=profile
    )
