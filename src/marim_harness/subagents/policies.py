"""Value objects bundling the ``SubagentRunner``'s masking and retry
configuration with the small behaviors that read them. The runner constructor
takes these two cohesive policies instead of six loose knobs, and the
masking-trigger resolution / retry backoff live next to the fields they use.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .masking import ObservationMasker

if TYPE_CHECKING:
    from ..config.context_limits import ContextLimits

# The masking trigger used when no ContextLimits resolver is wired (bare
# embedders / legacy constructions) — the historical default.
_FALLBACK_MASK_TRIGGER = 75_000


@dataclass(frozen=True)
class MaskingPolicy:
    """How a spawn masks its stale tool observations. A sub-agent does the
    read-heavy fan-out work, so its history is dominated by tool output; past a
    per-spawn token trigger those observations are masked per request by an
    ``ObservationMasker``. Bundles the resolver + knobs with the trigger
    resolution and masker construction that read them."""

    limits: ContextLimits | None = None
    enabled: bool = True
    keep_recent: int = 4
    min_chars: int = 200
    fallback_trigger: int = _FALLBACK_MASK_TRIGGER

    async def trigger_for(self, model_id: str | None) -> int:
        """The masking trigger for a spawn: the resolver's threshold for the
        spawn's OWN model (a per-spawn override budgets/windows as itself, not as
        the session model). Falls back to ``fallback_trigger`` when no resolver is
        wired — ``model_id`` is then irrelevant."""
        if self.limits is None:
            return self.fallback_trigger
        return await self.limits.resolve(model_id)

    def masker(self, trigger: int | None) -> ObservationMasker | None:
        """A fresh ``ObservationMasker`` for ONE spawn — it holds the run's
        committed mask set, so sharing an instance across spawns would leak one
        run's masked tool_call_ids into another's requests. ``None`` when masking
        is disabled."""
        if not self.enabled:
            return None
        return ObservationMasker(
            trigger if trigger is not None else self.fallback_trigger,
            keep_recent=self.keep_recent,
            min_chars=self.min_chars,
        )


@dataclass(frozen=True)
class RetryPolicy:
    """When and how a sub-agent run retries a *transient* model error.
    ``attempts`` is how many times the run is re-issued (resuming the captured
    conversation) after a gateway/timeout/rate-limit blip before the failure
    surfaces; a permanent error is never retried. ``request_limit`` caps model
    requests per run. Backoff is exponential from ``base_delay``, capped at
    ``max_delay``."""

    request_limit: int = 50
    attempts: int = 2
    base_delay: float = 0.5
    max_delay: float = 8.0

    async def backoff(self, attempt: int) -> None:
        """Sleep before the ``attempt``-th retry (1-based): exponential backoff,
        capped, so a brief upstream blip is ridden out without stalling long."""
        delay = min(self.base_delay * 2 ** (attempt - 1), self.max_delay)
        await asyncio.sleep(delay)
