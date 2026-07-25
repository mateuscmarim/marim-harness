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

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from fnmatch import fnmatch
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import ModelConfig

logger = logging.getLogger(__name__)

# Fraction of a KNOWN window the threshold may reach. Not config (YAGNI): it
# encodes the estimate's error margin, not a user preference.
WINDOW_SAFETY_RATIO = 0.8

# The threshold when nothing is known and no budget is set — the historical
# max_context_tokens default, preserved exactly.
DEFAULT_THRESHOLD = 100_000

# Injected discovery callables (built by build_context_limits per provider):
# a catalog fetch yielding ModelEntry-likes with .id/.context_window, a local
# probe yielding {model_id: window}, and the normalized form every source is
# reduced to — a window fetch yielding {model_id: window}.
CatalogFetch = Callable[[], Awaitable[list]]
LocalFetch = Callable[[], Awaitable[dict[str, int]]]
WindowFetch = Callable[[], Awaitable[dict[str, int]]]


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


# The provider names a colon prefix can qualify. Mirrors KNOWN_PROVIDERS in
# config/model.py (not imported: that module pulls in catalog/notification
# machinery and this one must stay light).
_PROVIDER_PREFIXES = frozenset(
    {"openrouter", "local", "google", "claude-cli", "zen", "zen-go"}
)


def _bare_id(model_id: str) -> str:
    """Strip a ``provider:`` qualifier so overrides and catalog lookups match
    both ``local:qwen/qwen3.5-9b`` and ``qwen/qwen3.5-9b``. Model ids CAN
    contain colons (Ollama tags like ``qwen2.5-coder:7b``), so only a known
    provider name before the first colon is treated as a qualifier."""
    head, sep, rest = model_id.partition(":")
    return rest if sep and head in _PROVIDER_PREFIXES else model_id


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
        fetchers: list[WindowFetch] | None = None,
    ) -> None:
        self._budget = budget
        self._overrides = parse_budget_overrides(budget_overrides_raw)
        self._window_override = window_override
        # Every discovery source is normalized to a WindowFetch and merged at
        # resolve time. The legacy single-source kwargs are folded in so older
        # call sites and tests keep working unchanged.
        self._fetchers: list[WindowFetch] = list(fetchers or [])
        if fetch_local is not None:
            self._fetchers.append(fetch_local)
        if fetch_catalog is not None:
            self._fetchers.append(_catalog_windows(fetch_catalog))
        self._windows: dict[str, int] = {}
        # Single-flight discovery: the first resolve() creates this task and
        # concurrent callers await the SAME task. That guarantee is what lets
        # a parallel spawn fan-out's spawns all see the discovered window —
        # each spawn freezes the returned threshold into its masker, so a
        # racing caller must never observe a budget-only threshold computed
        # from still-empty windows. A COMPLETED task also serves as the
        # "discovered" latch: a failed fetch still counts (discovery is
        # best-effort — stale/absent windows fall back to the budget), and
        # only invalidate() (a model switch) re-arms it. The generation
        # counter is what makes invalidate() safe mid-turn: a fetch started
        # before the invalidate discards its results instead of resurrecting
        # the windows invalidate() just cleared.
        self._discovery: asyncio.Task[None] | None = None
        self._generation = 0

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

    def window_for(self, model_id: str | None) -> int | None:
        """The KNOWN served window for ``model_id`` — override or discovered,
        raw (no safety ratio, no budget) — or None when nothing is known. This
        is the number the overflow-contention classifier compares a rejected
        request's size against; the derived :meth:`threshold` would understate
        it by the safety ratio. Sync and I/O-free like ``threshold``."""
        return self._window_for(model_id)

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
        :meth:`invalidate`; concurrent callers share one in-flight fetch)
        and return the threshold. Never raises."""
        if self._discovery is None:
            self._discovery = asyncio.create_task(self._discover(self._generation))
        # Hold a local reference: invalidate() may null self._discovery while
        # we are parked here, and we still want to await THIS fetch (its
        # results get discarded by the generation guard, not by us).
        discovery = self._discovery
        try:
            await discovery
        except Exception as exc:  # noqa: BLE001 — discovery is best-effort
            logger.warning("context-window discovery failed: %s", exc)
        return self.threshold(model_id)

    async def _discover(self, generation: int) -> None:
        """Fetch windows from ALL sources and merge them. One source failing
        (a provider catalog down, a non-LM-Studio local server 404ing the
        probe) must never poison the others — or break a turn."""
        results = await asyncio.gather(
            *(fetch() for fetch in self._fetchers), return_exceptions=True
        )
        merged: dict[str, int] = {}
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("context-window discovery source failed: %s", result)
                continue
            merged.update(result)
        if generation != self._generation:
            # invalidate() ran while this fetch was in flight: these windows
            # describe the world before the model switch, and committing them
            # would resurrect exactly what invalidate() cleared. Discard; the
            # next resolve() re-fetches under the new generation.
            return
        self._windows.update(merged)

    def invalidate(self) -> None:
        """Drop discovered windows so the next resolve() re-probes every
        source. Called on /model switch: the switch may land on another
        provider entirely (qualified ``local:...`` ids), and LM Studio
        JIT-loads the new model, possibly at a different context size than
        anything probed before. Bumping the generation makes an in-flight
        fetch stale, so its late results are discarded, not committed."""
        self._windows.clear()
        self._discovery = None
        self._generation += 1


def _catalog_windows(fetch_catalog: CatalogFetch) -> WindowFetch:
    """Normalize a catalog fetch (ModelEntry-likes) into a window fetch."""
    async def _windows() -> dict[str, int]:
        windows: dict[str, int] = {}
        for entry in await fetch_catalog():
            window = getattr(entry, "context_window", None)
            if isinstance(window, int) and window > 0:
                windows[entry.id] = window
        return windows
    return _windows


def _qualified(provider: str, windows: dict[str, int]) -> dict[str, int]:
    """Key each window under both the bare id and ``provider:id``: /model
    accepts qualified ids, and the qualified key keeps two providers serving
    the same bare id from clobbering each other on qualified lookups."""
    out = dict(windows)
    out.update({f"{provider}:{mid}": window for mid, window in windows.items()})
    return out


# A catalog-listing fetch (fetch_openrouter_models/fetch_google_models),
# called with just the api_key.
_KeyedCatalogFetch = Callable[[str | None], Awaitable[list]]

# A local-probe fetch (fetch_lmstudio_windows), called with base_url + api_key.
_LocalWindowFetch = Callable[[str | None, str | None], Awaitable[dict[str, int]]]


def _catalog_fetcher(provider: str, fetch: _KeyedCatalogFetch, api_key: str | None) -> WindowFetch:
    """A catalog-backed :data:`WindowFetch` for one active provider (``fetch``
    is ``fetch_openrouter_models``/``fetch_google_models``, called with
    ``api_key``). Hoisted to module scope (was nested in
    ``build_context_limits``) to keep that factory's McCabe count low; it
    captures only its explicit arguments, so the relocation is pure."""
    async def _windows() -> dict[str, int]:
        windows: dict[str, int] = {}
        for entry in await fetch(api_key):
            window = getattr(entry, "context_window", None)
            if isinstance(window, int) and window > 0:
                windows[entry.id] = window
        return _qualified(provider, windows)
    return _windows


def _local_fetcher(
    provider: str, fetch_local: _LocalWindowFetch, base_url: str | None, api_key: str | None
) -> WindowFetch:
    """A local-probe-backed :data:`WindowFetch` (LM Studio) for one active
    provider. Hoisted to module scope alongside :func:`_catalog_fetcher` for
    the same C901 reason; captures only its explicit arguments."""
    async def _windows() -> dict[str, int]:
        return _qualified(provider, await fetch_local(base_url, api_key))
    return _windows


def build_context_limits(
    configs: Mapping[str, ModelConfig],
    *,
    window_override: int | None,
    budget: int | None,
    budget_overrides_raw: str = "",
) -> ContextLimits:
    """Wire a ContextLimits with a discovery source for every ACTIVE provider
    in ``configs`` (provider name -> its ModelConfig). /model accepts
    qualified ids like ``local:qwen/...``, so a runtime switch can land on
    any active provider — discovery must cover them all, not just the
    default, or a cross-provider switch would silently fall back to the
    budget. Catalog imports are deferred to call time — this module is
    imported by config plumbing and must stay light."""
    from ..workspace.catalog import (
        fetch_google_models,
        fetch_lmstudio_windows,
        fetch_openrouter_models,
    )

    fetchers: list[WindowFetch] = []
    for provider, cfg in configs.items():
        if provider == "openrouter":
            fetchers.append(_catalog_fetcher(provider, fetch_openrouter_models, cfg.api_key))
        elif provider == "google":
            fetchers.append(_catalog_fetcher(provider, fetch_google_models, cfg.api_key))
        elif provider == "local":
            fetchers.append(
                _local_fetcher(provider, fetch_lmstudio_windows, cfg.base_url, cfg.api_key)
            )
        # claude-cli / unknown: no discovery — threshold rides on budget/override.
        # zen is deliberately included in that omission: OpenCode Zen's /models
        # endpoint carries no context-window metadata, so zen thresholds ride on
        # budget/override too, same as claude-cli.
    return ContextLimits(
        budget=budget,
        budget_overrides_raw=budget_overrides_raw,
        window_override=window_override,
        fetchers=fetchers,
    )
