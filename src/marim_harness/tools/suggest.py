"""Fuzzy "did you mean" suggestions for unknown tool-name calls.

Weaker models occasionally call a tool by a plausible-but-wrong name. The case
that motivated this: a model called ``agents_memory_smart_search`` for the
registered ``agentmemory_memory_smart_search`` — the doubled ``memory`` reads as
a typo and collapses, and the unusual ``agentmemory`` token re-segments into the
more familiar English ``agents memory``. The name is reconstructed from meaning
rather than copied verbatim.

Pydantic AI already rejects an unknown call with a ``RetryPromptPart`` that lists
every available tool, and the model usually self-corrects on the retry. This
module makes that retry more reliable by naming the *single* nearest match
inline, so the model has a concrete target instead of re-scanning the list and
guessing again.

It plugs in as a Pydantic AI ``ProcessHistory`` capability: ``before_model_request``
runs on every request — including the retry that carries the rejection — so we
enrich the rejection text in place before the model sees it. This touches no
Pydantic AI internals; it only reads/rewrites the public message parts.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import replace

from pydantic_ai.messages import ModelMessage, ModelRequest, RetryPromptPart

# Matches the message Pydantic AI's ToolManager raises for an unknown tool:
#   "Unknown tool name: 'NAME'. Available tools: 'a', 'b', ..."
_UNKNOWN_RE = re.compile(r"Unknown tool name: '([^']*)'")
_QUOTED_RE = re.compile(r"'([^']*)'")
# Sentinel so re-running the processor over the same history is idempotent.
_HINT_PREFIX = " Did you mean "
_MIN_RATIO = 0.6


def _normalize(name: str) -> str:
    """Collapse a tool name for similarity scoring: lowercase and drop
    underscores. This is what lets ``agents_memory_smart_search`` and
    ``agentmemory_memory_smart_search`` score as near-neighbours despite the
    pluralisation and the dropped duplicate segment — the comparison is on the
    letters, not the (model-mangled) word boundaries."""
    return name.lower().replace("_", "")


def nearest_tool_name(
    unknown: str, available: list[str], *, min_ratio: float = _MIN_RATIO
) -> str | None:
    """Return the available tool whose name is closest to ``unknown``, or ``None``
    if nothing clears ``min_ratio``. Comparison is underscore-insensitive so a
    re-segmentation (``agentmemory`` → ``agents_memory``) doesn't drown out the
    real similarity. Ties keep the first candidate in ``available`` order."""
    if not available:
        return None
    target = _normalize(unknown)
    best_ratio = -1.0
    best_name: str | None = None
    for name in available:
        ratio = difflib.SequenceMatcher(None, target, _normalize(name)).ratio()
        if ratio > best_ratio:
            best_ratio, best_name = ratio, name
    if best_name is None or best_ratio < min_ratio:
        return None
    return best_name


def _enrich(content: str) -> str | None:
    """Given an unknown-tool rejection string, return it with a "Did you mean"
    hint appended, or ``None`` if there's nothing to add (not a rejection, hint
    already present, or no close match). Available names are parsed out of the
    message itself — self-contained, and a silent no-op if the format ever
    changes upstream."""
    if _HINT_PREFIX in content:
        return None
    m = _UNKNOWN_RE.search(content)
    if m is None:
        return None
    unknown = m.group(1)
    _, _, tail = content.partition("Available tools:")
    available = _QUOTED_RE.findall(tail) if tail else []
    suggestion = nearest_tool_name(unknown, available)
    if not suggestion or suggestion == unknown:
        return None
    return f"{content}{_HINT_PREFIX}{suggestion!r}?"


def suggest_unknown_tool_retry(messages: list[ModelMessage]) -> list[ModelMessage]:
    """History processor: append a "Did you mean 'X'?" hint to an unknown-tool
    rejection in the pending request, giving the model's retry a concrete target.

    Only the trailing ``ModelRequest`` is inspected — that's where the just-raised
    rejection lives. Rewrites are non-mutating (``dataclasses.replace`` on a copy)
    so the harness's stored history objects are left untouched."""
    if not messages:
        return messages
    last = messages[-1]
    if not isinstance(last, ModelRequest):
        return messages

    new_parts: list = []
    changed = False
    for part in last.parts:
        if isinstance(part, RetryPromptPart) and isinstance(part.content, str):
            enriched = _enrich(part.content)
            if enriched is not None:
                part = replace(part, content=enriched)
                changed = True
        new_parts.append(part)

    if not changed:
        return messages
    return [*messages[:-1], replace(last, parts=new_parts)]
