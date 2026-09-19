"""Value objects bundling the ``SubagentRunner``'s masking and retry
configuration with the small behaviors that read them. The runner constructor
takes these two cohesive policies instead of six loose knobs, and the
masking-trigger resolution / retry backoff live next to the fields they use.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_ai.usage import UsageLimits

from ..config.model import DEFAULT_SUBAGENT_REQUEST_LIMIT
from ..session.compaction import SafeClearToolResults, safe_tool_result_clearer

if TYPE_CHECKING:
    from ..config.context_limits import ContextLimits

# The masking trigger used when no ContextLimits resolver is wired (bare
# embedders / legacy constructions) — the historical default.
_FALLBACK_MASK_TRIGGER = 75_000


@dataclass(frozen=True)
class MaskingPolicy:
    """How a spawn clears stale tool results through the upstream capability."""

    limits: ContextLimits | None = None
    enabled: bool = True
    keep_recent: int = 4
    fallback_trigger: int = _FALLBACK_MASK_TRIGGER

    async def trigger_for(self, model_id: str | None) -> int:
        """The masking trigger for a spawn: the resolver's threshold for the
        spawn's OWN model (a per-spawn override budgets/windows as itself, not as
        the session model). Falls back to ``fallback_trigger`` when no resolver is
        wired — ``model_id`` is then irrelevant."""
        if self.limits is None:
            return self.fallback_trigger
        return await self.limits.resolve(model_id)

    def clearer(self, trigger: int | None) -> SafeClearToolResults[object] | None:
        """Build a fresh upstream clearing capability for one spawn."""
        if not self.enabled:
            return None
        return safe_tool_result_clearer(
            max_tokens=trigger if trigger is not None else self.fallback_trigger,
            keep_pairs=self.keep_recent,
        )


@dataclass(frozen=True)
class RetryPolicy:
    """When and how a sub-agent run retries a *transient* model error.
    ``attempts`` is how many times the run is re-issued (resuming the captured
    conversation) after a gateway/timeout/rate-limit blip before the failure
    surfaces; a permanent error is never retried. ``request_limit`` caps model
    requests per run — a runaway guard, not a task budget: a run that reaches
    it is asked for a final report from the work so far rather than discarded
    (see ``SpawnRunDriver.run_to_completion``); ``0`` (or negative) removes
    the cap, leaving the context window as the only bound. Backoff is
    exponential from ``base_delay``, capped at ``max_delay``."""

    request_limit: int = DEFAULT_SUBAGENT_REQUEST_LIMIT
    attempts: int = 2
    base_delay: float = 0.5
    max_delay: float = 8.0

    @property
    def bounded(self) -> bool:
        """Whether a request cap applies at all (``0``/negative opts out)."""
        return self.request_limit > 0

    def usage_limits(self) -> UsageLimits:
        """The per-run pydantic-ai limits: the request cap, or none at all."""
        return UsageLimits(request_limit=self.request_limit if self.bounded else None)

    async def backoff(self, attempt: int) -> None:
        """Sleep before the ``attempt``-th retry (1-based): exponential backoff,
        capped, so a brief upstream blip is ridden out without stalling long."""
        delay = min(self.base_delay * 2 ** (attempt - 1), self.max_delay)
        await asyncio.sleep(delay)
