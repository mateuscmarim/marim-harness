"""One compaction/masking threshold from two distinct numbers.

``max_context_tokens`` used to conflate the model's real context window (a
hard physical limit — exceed it and the provider rejects the request) with a
spend budget (an economic ceiling so long histories don't burn money on
expensive models). :class:`ContextLimits` keeps them separate and derives the
single number every proactive layer keys on:

    threshold(model) = min(budget(model), int(0.8 * window(model)))

with two deliberate wrinkles. The 0.8 ratio applies only when the window is
*known* (discovered from the provider, or stated via MARIM_CONTEXT_WINDOW):
the char/4 token estimate undershoots and history grows mid-turn, so a real
limit needs headroom — but a made-up fallback does not, and applying the ratio
to one would silently shift the long-standing 100k default down to 80k. And
the budget is taken literally, no ratio: there is no overflow at the budget
line, only money.

Discovery is async (catalog / probe HTTP) and cached; ``threshold()`` is sync
and never does I/O, so the status bar can call it every frame. Discovery is
strictly best-effort — any failure falls back to override-or-default and must
never break a turn.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from fnmatch import fnmatch

logger = logging.getLogger(__name__)

# Fraction of a KNOWN window the threshold may reach. Not config (YAGNI): it
# encodes the estimate's error margin, not a user preference.
WINDOW_SAFETY_RATIO = 0.8

# The threshold when nothing is known and no budget is set — the historical
# max_context_tokens default, preserved exactly.
DEFAULT_THRESHOLD = 100_000

# Injected discovery callables (built by build_context_limits per provider):
# a catalog fetch yielding ModelEntry-likes with .id/.context_window, and a
# local probe yielding {model_id: window}.
CatalogFetch = Callable[[], Awaitable[list]]
LocalFetch = Callable[[], Awaitable[dict[str, int]]]


def parse_budget_overrides(raw: str) -> list[tuple[str, int | None]]:
    """Parse ``MARIM_CONTEXT_BUDGETS``: comma-separated ``pattern=tokens``
    pairs, fnmatch patterns, first match wins. ``=0`` and ``=`` both mean
    "no budget for this model" (window-only). Malformed pairs are dropped —
    a config typo must not take the harness down."""
    overrides: list[tuple[str, int | None]] = []
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        pattern, _, value = pair.partition("=")
        pattern = pattern.strip()
        value = value.strip()
        if not pattern:
            continue
        if not value or value == "0":
            overrides.append((pattern, None))
            continue
        try:
            tokens = int(value)
        except ValueError:
            continue
        if tokens > 0:
            overrides.append((pattern, tokens))
    return overrides


def _bare_id(model_id: str) -> str:
    """Strip a ``provider:`` qualifier so overrides and catalog lookups match
    both ``local:qwen/qwen3.5-9b`` and ``qwen/qwen3.5-9b``. Only the FIRST
    colon-segment is a qualifier; model ids themselves don't contain colons."""
    head, sep, rest = model_id.partition(":")
    return rest if sep and "/" not in head else model_id


class ContextLimits:
    """Resolves per-model window/budget/threshold. One instance per harness,
    shared by the session controller, the sub-agent runner, and the gauge."""

    def __init__(
        self,
        *,
        budget: int | None = DEFAULT_THRESHOLD,
        budget_overrides_raw: str = "",
        window_override: int | None = None,
        fetch_catalog: CatalogFetch | None = None,
        fetch_local: LocalFetch | None = None,
    ) -> None:
        self._budget = budget
        self._overrides = parse_budget_overrides(budget_overrides_raw)
        self._window_override = window_override
        self._fetch_catalog = fetch_catalog
        self._fetch_local = fetch_local
        self._windows: dict[str, int] = {}
        # Discovery runs once per instance lifetime (catalog contents are
        # static enough for a session); invalidate() re-arms it — used on
        # /model switch because LM Studio JIT-loads models at possibly
        # different context sizes.
        self._discovered = False

    # -- budget ----------------------------------------------------------

    def budget_for(self, model_id: str) -> int | None:
        """The budget for ``model_id``: first matching per-model override
        (qualified or bare id), else the global budget. None ⇒ unbudgeted."""
        bare = _bare_id(model_id)
        for pattern, tokens in self._overrides:
            if fnmatch(model_id, pattern) or fnmatch(bare, pattern):
                return tokens
        return self._budget

    # -- window ----------------------------------------------------------

    def _window_for(self, model_id: str | None) -> int | None:
        """The KNOWN window for ``model_id``, or None. An explicit override is
        the user telling us discovery lies — it always wins."""
        if self._window_override is not None:
            return self._window_override
        if model_id is None:
            return None
        return self._windows.get(model_id) or self._windows.get(_bare_id(model_id))

    # -- threshold -------------------------------------------------------

    def threshold(self, model_id: str | None) -> int:
        """The compaction/masking trigger for ``model_id``, from whatever is
        currently known. Sync and I/O-free — safe to call every frame. Call
        :meth:`resolve` first (any async site) to warm discovery."""
        budget = self.budget_for(model_id) if model_id else self._budget
        window = self._window_for(model_id)
        if window is None:
            return budget if budget is not None else DEFAULT_THRESHOLD
        safe = int(window * WINDOW_SAFETY_RATIO)
        return min(budget, safe) if budget is not None else safe

    async def resolve(self, model_id: str | None) -> int:
        """Warm window discovery (once per instance, re-armed by
        :meth:`invalidate`) and return the threshold. Never raises."""
        if not self._discovered:
            self._discovered = True
            try:
                if self._fetch_local is not None:
                    self._windows.update(await self._fetch_local())
                elif self._fetch_catalog is not None:
                    for entry in await self._fetch_catalog():
                        window = getattr(entry, "context_window", None)
                        if isinstance(window, int) and window > 0:
                            self._windows[entry.id] = window
            except Exception as exc:  # noqa: BLE001 — discovery is best-effort
                logger.warning("context-window discovery failed: %s", exc)
        return self.threshold(model_id)

    def invalidate(self) -> None:
        """Drop discovered windows so the next resolve() re-probes. Called on
        /model switch: LM Studio JIT-loads the new model, possibly at a
        different context size than anything probed before."""
        self._windows.clear()
        self._discovered = False


def build_context_limits(
    provider: str,
    base_url: str | None,
    api_key: str | None,
    *,
    window_override: int | None,
    budget: int | None,
    budget_overrides_raw: str = "",
) -> ContextLimits:
    """Wire a ContextLimits to the right discovery source for ``provider``.
    Catalog imports are deferred to call time — this module is imported by
    config plumbing and must stay light."""
    from ..workspace.catalog import (
        fetch_google_models,
        fetch_lmstudio_windows,
        fetch_openrouter_models,
    )

    fetch_catalog: CatalogFetch | None = None
    fetch_local: LocalFetch | None = None
    if provider == "openrouter":
        async def _fetch_catalog():
            return await fetch_openrouter_models(api_key)
        fetch_catalog = _fetch_catalog
    elif provider == "google":
        async def _fetch_catalog():
            return await fetch_google_models(api_key)
        fetch_catalog = _fetch_catalog
    elif provider == "local":
        async def _fetch_local():
            return await fetch_lmstudio_windows(base_url, api_key)
        fetch_local = _fetch_local
    # claude-cli / unknown: no discovery — threshold rides on budget/override.
    return ContextLimits(
        budget=budget,
        budget_overrides_raw=budget_overrides_raw,
        window_override=window_override,
        fetch_catalog=fetch_catalog,
        fetch_local=fetch_local,
    )
