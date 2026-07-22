"""Thinking level (reasoning effort): the vocabulary and the pure helpers that
turn a chosen level into a pydantic-ai ``ModelSettings.thinking`` value.

marim exposes one ordered vocabulary — ``off`` plus pydantic-ai's five
``ThinkingEffort`` steps — as a single source of truth for the env parser, the
session store, the TUI picker, and the sub-agent resolver. ``off`` is a
first-class member that means "no reasoning effort": ``settings_for`` OMITS the
key entirely for it (never ``thinking=False``), so an unset or explicitly-off
session is byte-identical to marim's pre-thinking behavior.

Everything here is side-effect-free and unit-tested directly (see
coding-guidelines.md's pure-helper split): ``settings_for`` never mutates its
base, and ``resolve_thinking`` mirrors ``subagents/tiers.resolve_tier`` — an
unrecognized candidate falls through to the next precedence level instead of
erroring, so a fat-fingered override degrades gracefully."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic_ai.settings import ModelSettings

# The ordered thinking vocabulary. ``off`` is FIRST and disables reasoning
# effort (settings_for omits the key). The rest are pydantic-ai's
# ``ThinkingEffort`` steps, ascending. Persisted verbatim into session JSON and
# read by the config parser + TUI — changing a spelling orphans saved sessions.
THINKING_LEVELS: tuple[str, ...] = ("off", "minimal", "low", "medium", "high", "xhigh")


def parse_thinking_level(value: str | None) -> str | None:
    """Coerce a raw string (env var, CLI flag, /think arg) to a canonical level,
    or ``None`` when it is blank or unrecognized. Case-insensitive and
    whitespace-tolerant. ``None`` means "unset" — the caller then falls back to
    its own default (the env default, or the inherited session level)."""
    if value is None:
        return None
    candidate = value.strip().lower()
    return candidate if candidate in THINKING_LEVELS else None


def settings_for(level: str | None, base: ModelSettings) -> ModelSettings:
    """Fold ``level`` into a copy of ``base`` as ``ModelSettings.thinking``.

    ``off`` and ``None`` return ``base`` unchanged (the key is OMITTED, never
    set to ``False``) — that is what keeps an unset/disabled session
    byte-identical to today's per-run settings and preserves prompt caching.
    Any other level returns a NEW mapping with ``thinking`` set; ``base`` is
    never mutated (a per-round settings object must not accumulate state).

    ``ModelSettings`` is imported LAZILY here (not at module top) so that
    importing this module for its pure string helpers — ``parse_thinking_level``
    is pulled in by ``config/model.py``, which sits on the CLI-router import
    path — does not drag in ``pydantic_ai``. That eager import would break the
    ``cli.router`` lazy-load guard (test_cli_startup.py) and cost every
    ``config``/``models``/``--help`` invocation ~1s for an agent it never
    builds. Only ``settings_for`` needs the real class at runtime."""
    from pydantic_ai.settings import ModelSettings

    if not level or level == "off":
        return base
    return ModelSettings({**base, "thinking": level})  # type: ignore[arg-type]


def resolve_thinking(
    override: str | None, spec: str | None, inherited: str | None
) -> str | None:
    """Resolve a sub-agent's thinking level by precedence: the spawn-call
    ``override`` first, then the spec's ``thinking:`` frontmatter, then the
    ``inherited`` session level. Returns the first candidate that is a known
    level (``off`` counts — an explicit off wins), else ``None``.

    An unrecognized candidate (a raw model slug in the override slot, a typo'd
    label) falls through to the next level rather than erroring — the same
    graceful-degrade contract as ``subagents.tiers.resolve_tier``."""
    for candidate in (override, spec, inherited):
        if candidate in THINKING_LEVELS:
            return candidate
    return None
