# Context Window vs. Context Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the conflated `max_context_tokens` into a discovered per-model **window** (real limit, 0.8 safety ratio) and a user-set **budget** (cost cap, global + per-model), deriving one threshold that drives session compaction, sub-agent masking, and the TUI gauge.

**Architecture:** A new `ContextLimits` resolver owns both numbers and exposes `threshold(model_id)` = `min(budget, 0.8 × window)` (budget-only when the window is unknown — see the deviation note below). Window discovery reuses the model catalogs already fetched (OpenRouter `context_length`, Google `inputTokenLimit`) plus a new LM Studio `/api/v0/models` probe preferring `loaded_context_length`. The resolver is threaded like `get_model`: `SessionController` gates compaction on it, `SubagentRunner` resolves per spawn (per-spawn model overrides get their own threshold), the status gauge denominates against it.

**Tech Stack:** Python ≥3.10, httpx (already a dependency, lazily imported in catalog fetchers), pytest + anyio, fnmatch.

**Spec:** `docs/superpowers/specs/2026-07-02-context-window-budget-design.md`

## Global Constraints

- Use `uv` for everything: `uv run pytest …`, `uv run ruff check src tests`, `uv run pyright`. Never bare `python`/`pip`/`pytest`.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM` (import sorting enforced).
- `requires-python = ">=3.10"` — no 3.11+-only syntax.
- CI order ruff → pyright → pytest; run all three before claiming a task done.
- The *why* comments in code blocks below are deliverables — keep them.
- Discovery must never fail a turn: every probe is best-effort with a short timeout and a silent fallback.
- The safety ratio `0.8` is a module constant (`WINDOW_SAFETY_RATIO`), not config.
- Another session may commit unrelated TUI work to this checkout concurrently: `git add` only the files your task names; if uncommitted edits vanish, re-check `git log`, re-apply, commit promptly.
- Commit messages end with the two trailer lines shown in each commit step.

**Deviation from the spec (agreed refinement):** the spec says "window fallback 100k" feeding the ratio. Applying a safety ratio to a *made-up* fallback is theater and would silently move the default cloud trigger from 100k to 80k. Instead: the `0.8 ×` term participates only when the window is **known** (discovered or `MARIM_CONTEXT_WINDOW`); with an unknown window the threshold is the budget alone (default 100k) — exactly today's behavior. When both are unknown, 100k.

## Background you need (read once)

- `workspace/catalog.py` — `ModelEntry` (id, name, supports_images, provider; `qualified` property), `parse_models` (OpenRouter shape, currently drops `context_length`), `parse_google_models` (drops `inputTokenLimit`), `fetch_openrouter_models` / `fetch_google_models` / `fetch_local_models` (all return `[]` on failure, lazy httpx import).
- `config/model.py` — `ModelConfig` dataclass (`max_context_tokens: int = 100_000` at line ~66), `_common_kwargs()` reads `MARIM_MAX_CONTEXT_TOKENS` at line ~136, `_provider_config` has provider/base_url/api_key.
- `runtime/harness.py` — `HarnessConfig` (max_context_tokens at line ~89), `build_collaborators` constructs `SessionController(cfg.store, cfg.manager, deps, cfg.max_context_tokens, cfg.keep_last_messages, …)` and `SubagentRunner(…, max_context_tokens=cfg.max_context_tokens, …)`; `Harness.set_model` (line ~431) is **sync** and sets `self.current_model`.
- `runtime/bootstrap.py` — `build_harness` maps env `ModelConfig` → `HarnessConfig` (line ~119).
- `session/ctrl.py` — `SessionController.__init__(store, manager, deps, max_context_tokens, keep_last_messages, summarizer=None, titler=None, mask_observations=False, mask_keep_recent=4, mask_min_chars=200)`; `maybe_compact` (async) uses `self.max_context_tokens` at lines ~296/322/327.
- `subagents/masking.py` — `ObservationMasker(max_tokens, keep_recent=4, min_chars=200)` with internal `_TRIGGER_RATIO = 0.75`; method `mask`.
- `subagents/runner.py` — constructor params `…, max_context_tokens: int = 100_000, mask_observations: bool = True, mask_keep_recent: int = 4, mask_min_chars: int = 200`; `build()` creates one masker per spawn; `_prepare_spawn` is **async** and calls `self.build(type, max_output_chars, model, work_root, defn=defn, depth=depth)`.
- `interfaces/tui/status.py` line ~86 reads `session.max_context_tokens` for the gauge; `interfaces/tui/settings.py` line ~286 binds a settings field to `env_cfg.max_context_tokens`; `interfaces/cli/config.py` lines ~63/77 print it.
- pydantic-ai `Model` objects expose `.model_name` (the provider-native id, e.g. `qwen/qwen3.5-9b`, `anthropic/claude-sonnet-4-6`) — that string matches catalog ids.
- LM Studio's `/api/v0/models` rows (verified live): `{"id": "qwen/qwen3.5-9b", "state": "loaded", "max_context_length": 262144, "loaded_context_length": 101039}`; not-loaded rows lack `loaded_context_length`; the plain `/v1/models` has neither field.

## File Structure

- **Modify** `src/marim_harness/workspace/catalog.py` — `ModelEntry.context_window`, keep the context fields in both parsers, add LM Studio parser + fetcher. (Task 1)
- **Create** `src/marim_harness/config/context_limits.py` — `ContextLimits`, `parse_budget_overrides`, `build_context_limits`. (Task 2)
- **Modify** `src/marim_harness/config/model.py` — `context_window`/`context_budgets` fields, budget env resolution + deprecation warning. (Task 3)
- **Modify** `src/marim_harness/interfaces/cli/config.py`, `src/marim_harness/interfaces/tui/settings.py` — surface the new knobs. (Task 3)
- **Modify** `src/marim_harness/session/ctrl.py`, `src/marim_harness/runtime/harness.py`, `src/marim_harness/runtime/bootstrap.py`, `src/marim_harness/interfaces/tui/status.py` — threshold-driven compaction gate, wiring, gauge, invalidate-on-switch. (Task 4)
- **Modify** `src/marim_harness/subagents/masking.py`, `src/marim_harness/subagents/runner.py` — trigger-direct masker, per-spawn resolution. (Task 5)
- **Tests:** `tests/test_catalog.py` (extend), `tests/test_context_limits.py` (new), `tests/test_config.py` (extend), `tests/test_session.py` + `tests/test_app.py`-adjacent wiring tests as named per task, `tests/test_subagent_masking.py` (update).

---

### Task 1: Catalog carries context windows

**Files:**
- Modify: `src/marim_harness/workspace/catalog.py`
- Test: `tests/test_catalog.py`

**Interfaces:**
- Consumes: existing `ModelEntry`, `parse_models`, `parse_google_models`.
- Produces: `ModelEntry.context_window: int | None = None`; `parse_models`/`parse_google_models` fill it; `parse_lmstudio_models(payload: dict) -> dict[str, int]`; `async fetch_lmstudio_windows(base_url: str | None, api_key: str | None = None, timeout: float = 10.0) -> dict[str, int]`. Task 2 calls the fetchers and reads `context_window`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_catalog.py` (match its existing import style — it already imports the parsers):

```python
def test_parse_models_keeps_openrouter_context_length():
    payload = {"data": [
        {"id": "anthropic/claude-opus-4-8", "name": "Opus", "context_length": 200000},
        {"id": "some/other", "name": "Other"},                    # field absent
        {"id": "bad/ctx", "name": "Bad", "context_length": "big"},  # non-int ignored
    ]}
    entries = {e.id: e for e in parse_models(payload)}
    assert entries["anthropic/claude-opus-4-8"].context_window == 200000
    assert entries["some/other"].context_window is None
    assert entries["bad/ctx"].context_window is None


def test_parse_google_models_keeps_input_token_limit():
    payload = {"models": [
        {"name": "models/gemini-2.5-pro", "displayName": "Gemini",
         "supportedGenerationMethods": ["generateContent"],
         "inputTokenLimit": 1048576},
    ]}
    (entry,) = parse_google_models(payload)
    assert entry.context_window == 1048576


def test_parse_lmstudio_models_prefers_loaded_context_length():
    """The exact shape LM Studio's /api/v0/models returns (verified live):
    a loaded model carries loaded_context_length — the true serving window,
    which can be far below max_context_length — while a not-loaded model
    only advertises its max."""
    payload = {"data": [
        {"id": "qwen/qwen3.5-9b", "state": "loaded",
         "max_context_length": 262144, "loaded_context_length": 101039},
        {"id": "ornith-1.0-35b", "state": "not-loaded",
         "max_context_length": 262144},
        {"id": "junk", "max_context_length": "nope"},
    ]}
    windows = parse_lmstudio_models(payload)
    assert windows["qwen/qwen3.5-9b"] == 101039   # loaded beats max
    assert windows["ornith-1.0-35b"] == 262144    # max is the only signal
    assert "junk" not in windows


def test_parse_lmstudio_models_tolerates_garbage():
    assert parse_lmstudio_models({}) == {}
    assert parse_lmstudio_models({"data": "nope"}) == {}
```

Add `parse_lmstudio_models` to the test file's `from marim_harness.workspace.catalog import …` block.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_catalog.py -k "context_length or lmstudio or token_limit" -v`
Expected: ImportError on `parse_lmstudio_models`; after commenting nothing out, the two parser tests fail with `AttributeError: context_window` — either failure mode confirms RED.

- [ ] **Step 3: Implement**

In `src/marim_harness/workspace/catalog.py`:

Add the field to `ModelEntry` (after `provider: str | None = None`):

```python
    # The model's context window in tokens, when the catalog states it
    # (OpenRouter context_length, Google inputTokenLimit); None when the
    # source doesn't say. Consumed by config.context_limits to derive the
    # compaction/masking threshold.
    context_window: int | None = None
```

In `parse_models`, before the `entries.append(...)` call, extract the window, and pass it:

```python
        ctx = row.get("context_length")
        context_window = ctx if isinstance(ctx, int) and ctx > 0 else None
        entries.append(ModelEntry(id=model_id, name=display,
                                  supports_images=supports_images,
                                  context_window=context_window))
```

In `parse_google_models`, same pattern with `inputTokenLimit`:

```python
        limit = row.get("inputTokenLimit")
        context_window = limit if isinstance(limit, int) and limit > 0 else None
        entries.append(ModelEntry(id=model_id, name=display, supports_images=None,
                                  context_window=context_window))
```

(Keep each parser's existing comments; only the constructor calls grow.)

Add after `fetch_local_models`:

```python
def parse_lmstudio_models(payload: dict) -> dict[str, int]:
    """Model id → context window from LM Studio's enhanced ``/api/v0/models``.

    Prefers ``loaded_context_length`` — the window the model is *actually
    serving* — over ``max_context_length`` (what the weights support). The two
    can differ wildly: a model advertising 262k loaded at ~101k is exactly the
    mismatch that let requests overflow while the token gauge read 12%.
    ``loaded_context_length`` only exists on rows with ``state: "loaded"``;
    for everything else the max is the best available signal."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    windows: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        ctx = row.get("loaded_context_length")
        if not (isinstance(ctx, int) and ctx > 0):
            ctx = row.get("max_context_length")
        if isinstance(ctx, int) and ctx > 0:
            windows[model_id] = ctx
    return windows


async def fetch_lmstudio_windows(
    base_url: str | None, api_key: str | None = None, timeout: float = 10.0
) -> dict[str, int]:
    """Probe LM Studio's ``/api/v0/models`` for per-model context windows.

    The OpenAI-compatible ``/v1/models`` carries no context information, so
    this hits the enhanced REST API instead, derived from the same base_url
    (``…:1234/v1`` → ``…:1234/api/v0/models``). Returns ``{}`` on any failure
    — a non-LM-Studio local server 404s here and that must never break a turn.
    httpx is imported lazily to keep the import chain light."""
    if not base_url:
        return {}
    import httpx

    root = base_url.rstrip("/")
    root = root.removesuffix("/v1")
    url = f"{root}/api/v0/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return parse_lmstudio_models(response.json())
    except Exception as exc:
        logger.warning("failed to fetch LM Studio windows from %s: %s", url, exc)
        return {}
```

- [ ] **Step 4: Run the catalog tests**

Run: `uv run pytest --no-cov tests/test_catalog.py -v`
Expected: ALL PASS (pre-existing parser tests must keep passing — the new field defaults to None).

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check src tests && uv run pyright`
Expected: clean.

```bash
git add src/marim_harness/workspace/catalog.py tests/test_catalog.py
git commit -m "$(cat <<'EOF'
feat(catalog): carry per-model context windows; LM Studio window probe

OpenRouter and Google catalogs already state the window — stop
discarding it. LM Studio's /api/v0/models exposes the *loaded* context
length, which is the real serving limit.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0111Y8LPyWyXqC9tv7MSonPt
EOF
)"
```

---

### Task 2: `ContextLimits` resolver

**Files:**
- Create: `src/marim_harness/config/context_limits.py`
- Test: `tests/test_context_limits.py`

**Interfaces:**
- Consumes: Task 1's `fetch_openrouter_models`, `fetch_google_models`, `fetch_lmstudio_windows`, `ModelEntry.context_window` (imported lazily inside `build_context_limits`, NOT at module top — catalog pulls httpx-adjacent code and this module is imported by config).
- Produces (Tasks 4/5 depend on these exact names):
  - `WINDOW_SAFETY_RATIO = 0.8`, `DEFAULT_THRESHOLD = 100_000`
  - `parse_budget_overrides(raw: str) -> list[tuple[str, int | None]]`
  - `class ContextLimits` with `budget_for(model_id: str) -> int | None`, `threshold(model_id: str | None) -> int` (sync, cached), `async resolve(model_id: str | None) -> int`, `invalidate() -> None`
  - `build_context_limits(provider: str, base_url: str | None, api_key: str | None, *, window_override: int | None, budget: int | None, budget_overrides_raw: str = "") -> ContextLimits`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_context_limits.py`:

```python
"""The ContextLimits resolver: one threshold from two concepts.

window = the model's real limit (discovered / env override); budget = the
user's cost cap (global + per-model fnmatch overrides). threshold =
min(budget, 0.8 * window) when the window is KNOWN; budget alone when it
isn't (applying a safety ratio to a made-up fallback would silently shift
the long-standing 100k default). Discovery is async and cached; the sync
threshold() never does I/O, so the status bar can call it every frame.
"""

import pytest

from marim_harness.config.context_limits import (
    DEFAULT_THRESHOLD,
    ContextLimits,
    parse_budget_overrides,
)
from marim_harness.workspace.catalog import ModelEntry


def test_parse_budget_overrides_patterns_and_unbudgeted_forms():
    parsed = parse_budget_overrides(
        "anthropic/claude-opus*=60000, openrouter/*free*=0, local/*="
    )
    assert parsed == [
        ("anthropic/claude-opus*", 60000),
        ("openrouter/*free*", None),   # 0 ⇒ unbudgeted
        ("local/*", None),             # empty ⇒ unbudgeted
    ]
    assert parse_budget_overrides("") == []
    assert parse_budget_overrides("garbage-no-equals, x=notanum") == []


def test_budget_precedence_first_match_wins_then_global():
    limits = ContextLimits(
        budget=100_000,
        budget_overrides_raw="anthropic/claude-opus*=60000,anthropic/*=90000",
    )
    assert limits.budget_for("anthropic/claude-opus-4-8") == 60_000  # first match
    assert limits.budget_for("anthropic/claude-sonnet-5") == 90_000
    assert limits.budget_for("qwen/qwen3.5-9b") == 100_000           # global


def test_override_matches_qualified_and_bare_ids():
    limits = ContextLimits(budget=None, budget_overrides_raw="qwen/*=5000")
    assert limits.budget_for("local:qwen/qwen3.5-9b") == 5000  # prefix stripped
    assert limits.budget_for("qwen/qwen3.5-9b") == 5000


def test_threshold_unknown_window_is_budget_alone():
    assert ContextLimits(budget=100_000).threshold("m") == 100_000
    assert ContextLimits(budget=None).threshold("m") == DEFAULT_THRESHOLD
    assert ContextLimits(budget=100_000).threshold(None) == 100_000


def test_threshold_known_window_applies_safety_ratio():
    limits = ContextLimits(budget=None, window_override=200_000)
    assert limits.threshold("m") == 160_000                 # 0.8 * window
    capped = ContextLimits(budget=60_000, window_override=200_000)
    assert capped.threshold("m") == 60_000                  # budget wins when lower


@pytest.mark.anyio
async def test_resolve_discovers_windows_from_catalog_once():
    calls = {"n": 0}

    async def fake_catalog():
        calls["n"] += 1
        return [ModelEntry(id="anthropic/claude-opus-4-8", name="Opus",
                           context_window=200_000)]

    limits = ContextLimits(budget=None, fetch_catalog=fake_catalog)
    assert await limits.resolve("anthropic/claude-opus-4-8") == 160_000
    assert limits.threshold("anthropic/claude-opus-4-8") == 160_000  # cached, sync
    await limits.resolve("anthropic/claude-opus-4-8")
    assert calls["n"] == 1                                   # fetched once


@pytest.mark.anyio
async def test_resolve_lmstudio_loaded_window_beats_large_budget():
    """The motivating failure: model advertises 262k, LM Studio loaded it at
    ~101k, user budget was 180k — the trigger MUST follow the loaded window."""
    async def fake_local():
        return {"qwen/qwen3.5-9b": 101_039}

    limits = ContextLimits(budget=180_000, fetch_local=fake_local)
    assert await limits.resolve("qwen/qwen3.5-9b") == int(0.8 * 101_039)


@pytest.mark.anyio
async def test_invalidate_forces_a_fresh_probe():
    windows = {"m": 8_192}
    calls = {"n": 0}

    async def fake_local():
        calls["n"] += 1
        return dict(windows)

    limits = ContextLimits(budget=None, fetch_local=fake_local)
    assert await limits.resolve("m") == int(0.8 * 8_192)
    windows["m"] = 32_768                                    # user reloads the model
    limits.invalidate()
    assert await limits.resolve("m") == int(0.8 * 32_768)
    assert calls["n"] == 2


@pytest.mark.anyio
async def test_env_window_override_beats_discovery():
    async def fake_local():
        return {"m": 500_000}

    limits = ContextLimits(budget=None, window_override=10_000,
                           fetch_local=fake_local)
    assert await limits.resolve("m") == 8_000  # 0.8 * override, discovery ignored


@pytest.mark.anyio
async def test_discovery_failure_falls_back_silently():
    async def broken():
        raise RuntimeError("boom")

    limits = ContextLimits(budget=100_000, fetch_local=broken)
    assert await limits.resolve("m") == 100_000  # budget alone; never raises
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_context_limits.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'marim_harness.config.context_limits'`.

- [ ] **Step 3: Implement**

Create `src/marim_harness/config/context_limits.py`:

```python
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
        async def fetch_catalog():  # type: ignore[no-redef]
            return await fetch_openrouter_models(api_key)
    elif provider == "google":
        async def fetch_catalog():  # type: ignore[no-redef]
            return await fetch_google_models(api_key)
    elif provider == "local":
        async def fetch_local():  # type: ignore[no-redef]
            return await fetch_lmstudio_windows(base_url, api_key)
    # claude-cli / unknown: no discovery — threshold rides on budget/override.
    return ContextLimits(
        budget=budget,
        budget_overrides_raw=budget_overrides_raw,
        window_override=window_override,
        fetch_catalog=fetch_catalog,
        fetch_local=fetch_local,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_context_limits.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check src tests && uv run pyright`
Expected: clean. (If ruff flags the conditional inner-function redefinitions, keep the `# type: ignore[no-redef]` pattern but satisfy ruff by renaming to two distinct inner names and assigning, e.g. `async def _catalog(): …` / `fetch_catalog = _catalog` — behavior identical.)

```bash
git add src/marim_harness/config/context_limits.py tests/test_context_limits.py
git commit -m "$(cat <<'EOF'
feat(config): ContextLimits — window vs. budget, one derived threshold

threshold = min(budget, 0.8 * window) when the window is known; budget
alone when it isn't. Async cached discovery, sync I/O-free reads.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0111Y8LPyWyXqC9tv7MSonPt
EOF
)"
```

---

### Task 3: Env/config plumbing + deprecation

**Files:**
- Modify: `src/marim_harness/config/model.py` (ModelConfig fields ~line 66; `_common_kwargs` ~line 136)
- Modify: `src/marim_harness/interfaces/cli/config.py` (~lines 63, 77)
- Modify: `src/marim_harness/interfaces/tui/settings.py` (~line 286 — label only)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `ModelConfig.context_window: int | None`, `ModelConfig.context_budgets: str`; `ModelConfig.max_context_tokens` KEEPS its name (persisted-settings and TUI compat) but is now documented as **the global context budget**, resolved from `MARIM_CONTEXT_BUDGET` → deprecated `MARIM_MAX_CONTEXT_TOKENS` (one-time warning) → 100 000, with `0` meaning "unbudgeted". Task 4 reads all three fields.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py` (it already exercises env loading — follow its monkeypatch idiom for setting/clearing `MARIM_*` vars; use its existing loader entry point, e.g. the function the file already calls to build a `ModelConfig` from env):

```python
def test_context_budget_env_resolution(monkeypatch):
    monkeypatch.setenv("MARIM_CONTEXT_BUDGET", "60000")
    monkeypatch.setenv("MARIM_MAX_CONTEXT_TOKENS", "111111")  # ignored when new var set
    cfg = _load()  # the file's existing helper for env -> ModelConfig
    assert cfg.max_context_tokens == 60000


def test_deprecated_max_context_tokens_still_honored(monkeypatch, caplog):
    monkeypatch.delenv("MARIM_CONTEXT_BUDGET", raising=False)
    monkeypatch.setenv("MARIM_MAX_CONTEXT_TOKENS", "70000")
    with caplog.at_level("WARNING"):
        cfg = _load()
    assert cfg.max_context_tokens == 70000
    assert any("MARIM_MAX_CONTEXT_TOKENS" in r.message and "deprecated" in r.message
               for r in caplog.records)


def test_context_window_and_budgets_env(monkeypatch):
    monkeypatch.setenv("MARIM_CONTEXT_WINDOW", "32768")
    monkeypatch.setenv("MARIM_CONTEXT_BUDGETS", "anthropic/claude-opus*=60000")
    cfg = _load()
    assert cfg.context_window == 32768
    assert cfg.context_budgets == "anthropic/claude-opus*=60000"


def test_context_defaults(monkeypatch):
    for var in ("MARIM_CONTEXT_BUDGET", "MARIM_MAX_CONTEXT_TOKENS",
                "MARIM_CONTEXT_WINDOW", "MARIM_CONTEXT_BUDGETS"):
        monkeypatch.delenv(var, raising=False)
    cfg = _load()
    assert cfg.max_context_tokens == 100_000
    assert cfg.context_window is None
    assert cfg.context_budgets == ""
```

Adapt `_load()` to whatever loader helper `tests/test_config.py` actually uses (it exists — the file already tests `_common_kwargs`-driven fields like `MARIM_SUBAGENT_CONCURRENCY`). Do not invent a new loader.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_config.py -k context -v`
Expected: FAIL (`context_window` attribute missing; budget resolution not implemented).

- [ ] **Step 3: Implement**

In `src/marim_harness/config/model.py`:

Replace the `max_context_tokens: int = 100_000` field (line ~66) and its comment with:

```python
    # The GLOBAL context budget in tokens — an economic ceiling, not the
    # model's window. Compaction/masking trigger at min(budget, 0.8 × the
    # discovered window); see config/context_limits.py. Kept under its
    # historical name because persisted settings and the TUI field bind to
    # it. 0 ⇒ unbudgeted (window-only).
    max_context_tokens: int = 100_000
    # Manual context-window override for servers discovery can't read
    # (a non-LM-Studio local server, an offline box). None ⇒ discover.
    context_window: int | None = None
    # Per-model budget overrides: comma-separated pattern=tokens pairs,
    # fnmatch on the model id (e.g. "anthropic/claude-opus*=60000"); "=0"
    # means unbudgeted for that model. Raw string; parsed by ContextLimits.
    context_budgets: str = ""
```

In `_common_kwargs` (line ~136), replace the `max_context_tokens=…` line with budget resolution (add a module-level helper next to the other `_*_env` helpers):

```python
def _context_budget_env() -> int:
    """MARIM_CONTEXT_BUDGET, falling back to the deprecated
    MARIM_MAX_CONTEXT_TOKENS (same meaning, old name) with a one-time
    warning, else the historical 100k default."""
    if os.getenv("MARIM_CONTEXT_BUDGET") is not None:
        return _int_env("MARIM_CONTEXT_BUDGET", 100_000)
    if os.getenv("MARIM_MAX_CONTEXT_TOKENS") is not None:
        logger.warning(
            "MARIM_MAX_CONTEXT_TOKENS is deprecated; rename it to "
            "MARIM_CONTEXT_BUDGET (same meaning: the global context budget)."
        )
        return _int_env("MARIM_MAX_CONTEXT_TOKENS", 100_000)
    return 100_000
```

(If `config/model.py` has no module `logger`, add `logger = logging.getLogger(__name__)` with an `import logging` in its import block.)

and in the `return dict(…)`:

```python
        max_context_tokens=_context_budget_env(),
        context_window=(
            _int_env("MARIM_CONTEXT_WINDOW", 0) or None
        ),
        context_budgets=os.getenv("MARIM_CONTEXT_BUDGETS", ""),
```

In `src/marim_harness/interfaces/cli/config.py`: the dict entry at ~line 63 and the print at ~line 77 keep `max_context_tokens` and gain two lines mirroring the existing style:

```python
            "max_context_tokens": cfg.max_context_tokens,
            "context_window": cfg.context_window,
            "context_budgets": cfg.context_budgets,
```

```python
    print(f"max_context_tokens:  {cfg.max_context_tokens}", file=out)
    print(f"context_window:      {cfg.context_window}", file=out)
    print(f"context_budgets:     {cfg.context_budgets}", file=out)
```

In `src/marim_harness/interfaces/tui/settings.py` (~line 286): the field keeps binding `env_cfg.max_context_tokens`; update only its visible label/description string to say "Context budget (tokens, 0 = unbudgeted)" — find the label argument adjacent to the `value=str(self.env_cfg.max_context_tokens)` binding and reword it. No behavioral change.

- [ ] **Step 4: Run the config tests**

Run: `uv run pytest --no-cov tests/test_config.py tests/test_config_cli.py -q`
Expected: ALL PASS (the CLI-config tests may assert on printed output — if one pins the exact line set, extend its expectation with the two new lines).

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check src tests && uv run pyright`
Expected: clean.

```bash
git add src/marim_harness/config/model.py src/marim_harness/interfaces/cli/config.py \
        src/marim_harness/interfaces/tui/settings.py tests/test_config.py
git commit -m "$(cat <<'EOF'
feat(config): MARIM_CONTEXT_BUDGET / _WINDOW / _BUDGETS env knobs

max_context_tokens keeps its name (persisted-settings compat) but now
means the global budget; MARIM_MAX_CONTEXT_TOKENS becomes a deprecated
alias with a one-time warning.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0111Y8LPyWyXqC9tv7MSonPt
EOF
)"
```

(If `tests/test_config_cli.py` needed the two-line extension in Step 4, include it in the `git add`.)

---

### Task 4: Threshold-driven session gate, wiring, gauge, invalidate-on-switch

**Files:**
- Modify: `src/marim_harness/session/ctrl.py` (constructor ~line 106; `maybe_compact` ~lines 296/322/327)
- Modify: `src/marim_harness/runtime/harness.py` (`HarnessConfig` ~line 89; `build_collaborators` SessionController construction ~line 228; `Harness.set_model` ~line 431)
- Modify: `src/marim_harness/runtime/bootstrap.py` (`HarnessConfig(…)` ~line 119)
- Modify: `src/marim_harness/interfaces/tui/status.py` (~line 86)
- Test: `tests/test_session.py`

**Interfaces:**
- Consumes: `ContextLimits`, `build_context_limits` (Task 2); config fields (Task 3).
- Produces: `SessionController` gains keyword params `limits: ContextLimits | None = None, get_model_id: Callable[[], str | None] | None = None` and a property `compact_threshold -> int`; `HarnessConfig.context_limits: ContextLimits | None = None`; `Harness.set_model` invalidates. Task 5 reuses the same `limits` object off `build_collaborators`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session.py` (reuse its existing controller-construction idiom — it builds `SessionController` directly; mirror the surrounding tests' fixtures for `deps`):

```python
@pytest.mark.anyio
async def test_maybe_compact_gates_on_the_resolved_threshold(tmp_path):
    """With a ContextLimits attached, the compaction gate follows the
    discovered window (0.8 ratio), not the raw budget: a tiny discovered
    window must force compaction even when the budget says there's room."""
    from marim_harness.config.context_limits import ContextLimits

    async def fake_local():
        return {"tiny": 100}      # threshold = 80 tokens — anything compacts

    limits = ContextLimits(budget=1_000_000, fetch_local=fake_local)
    ctrl = _make_controller(tmp_path)          # the file's existing helper
    ctrl.limits = limits
    ctrl.get_model_id = lambda: "tiny"
    ctrl.history = _long_history()             # existing helper/fixture pattern
    assert await ctrl.maybe_compact() is True


@pytest.mark.anyio
async def test_maybe_compact_without_limits_keeps_legacy_budget_gate(tmp_path):
    ctrl = _make_controller(tmp_path)          # max_context_tokens as before
    assert ctrl.compact_threshold == ctrl.max_context_tokens


def test_compact_threshold_reads_the_warm_cache(tmp_path):
    from marim_harness.config.context_limits import ContextLimits

    ctrl = _make_controller(tmp_path)
    ctrl.limits = ContextLimits(budget=42_000)
    ctrl.get_model_id = lambda: "m"
    assert ctrl.compact_threshold == 42_000    # sync, no resolve needed
```

Adapt helper names (`_make_controller`, `_long_history`) to what `tests/test_session.py` actually provides — it has established patterns for building a controller with a small `max_context_tokens` and an over-budget history (the masking tests around line 497 use them). Do not duplicate fixtures; reuse.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_session.py -k "threshold or legacy_budget" -v`
Expected: FAIL — `SessionController` has no `limits`/`compact_threshold`.

- [ ] **Step 3: Implement `SessionController` changes**

In `src/marim_harness/session/ctrl.py`:

Constructor — add after `mask_min_chars: int = 200`:

```python
        limits: ContextLimits | None = None,
        get_model_id: Callable[[], str | None] | None = None,
```

with the import added under `TYPE_CHECKING` if the file uses that pattern, else a plain `from ..config.context_limits import ContextLimits` in its import block, and store them after `self.mask_min_chars = …`:

```python
        # The window/budget resolver, when the harness wires one (headless and
        # TUI both do via build_collaborators; embedders may leave it None and
        # keep the fixed max_context_tokens gate). get_model_id reads the LIVE
        # model so a /model switch re-keys the threshold without rewiring.
        self.limits = limits
        self.get_model_id = get_model_id
```

Add the property next to `total_tokens`:

```python
    @property
    def compact_threshold(self) -> int:
        """The compaction trigger: the resolver's threshold for the current
        model when one is wired (already min(budget, 0.8 × window)), else the
        legacy fixed budget. Sync and I/O-free — the status gauge reads this
        every frame; maybe_compact warms discovery before comparing."""
        if self.limits is not None:
            model_id = self.get_model_id() if self.get_model_id else None
            return self.limits.threshold(model_id)
        return self.max_context_tokens
```

In `maybe_compact`, before the `_plan_tail_start` call, warm discovery:

```python
        # Warm window discovery before gating: this is an async site, and the
        # resolver caches, so all later sync reads (the gauge, the property
        # above) see the discovered window. Never raises — discovery is
        # best-effort by contract.
        if self.limits is not None:
            model_id = self.get_model_id() if self.get_model_id else None
            await self.limits.resolve(model_id)
        threshold = self.compact_threshold
```

then replace `self.max_context_tokens` with `threshold` in the `_plan_tail_start(...)` call (~line 296) and in both `compact_history_with_summary(...)` / `compact_history(...)` calls (~lines 322/327).

- [ ] **Step 4: Wire through `HarnessConfig`, `build_collaborators`, bootstrap, `set_model`, and the gauge**

In `src/marim_harness/runtime/harness.py`:

`HarnessConfig` — add after `mask_min_chars: int = 200`:

```python
    # The window/budget resolver. None ⇒ build_collaborators constructs a
    # discovery-less one from max_context_tokens, preserving the legacy
    # fixed-budget behavior for embedders that never touch the new knobs.
    context_limits: ContextLimits | None = None
```

(add `from ..config.context_limits import ContextLimits` to the import block).

In `build_collaborators`, before the `SessionController(…)` construction:

```python
    limits = cfg.context_limits or ContextLimits(budget=cfg.max_context_tokens or None)
    # The live model id for threshold resolution: reads the current model each
    # call, so a runtime /model switch re-keys thresholds without rewiring —
    # the same closure trick get_model itself uses.
    get_model_id = lambda: getattr(get_model(), "model_name", None)  # noqa: E731
```

then pass `limits=limits, get_model_id=get_model_id` to `SessionController(…)`.

In `Harness.set_model` (line ~431), after `self.current_model = model`:

```python
        # A model switch invalidates discovered windows: on the local provider
        # the new model JIT-loads, possibly at a different context size than
        # anything probed before. Re-discovery happens lazily at the next
        # async site (maybe_compact / spawn prep) — set_model stays sync.
        if self.session.limits is not None:
            self.session.limits.invalidate()
```

In `src/marim_harness/runtime/bootstrap.py`, in the `HarnessConfig(…)` construction (~line 119), add:

```python
            context_limits=build_context_limits(
                cfg.provider, cfg.base_url, cfg.api_key,
                window_override=cfg.context_window,
                budget=cfg.max_context_tokens or None,
                budget_overrides_raw=cfg.context_budgets,
            ),
```

(import `from ..config.context_limits import build_context_limits`).

In `src/marim_harness/interfaces/tui/status.py` line ~86, replace:

```python
        max_ctx = getattr(self.app.harness.session, "max_context_tokens", 0) or 0
```

with:

```python
        # Denominate against the resolved threshold (min(budget, 0.8×window)),
        # not the raw budget: 100% keeps meaning "compaction imminent" even
        # when a small discovered window, not the budget, is the binding limit.
        max_ctx = getattr(self.app.harness.session, "compact_threshold", 0) or 0
```

- [ ] **Step 5: Run the affected suites**

Run: `uv run pytest --no-cov tests/test_session.py tests/test_app.py tests/test_provider_errors.py -q`
Expected: ALL PASS — existing compaction/masking session tests construct the controller without `limits`, so the legacy gate is unchanged; the overflow force-compaction test is unaffected (force path bypasses the gate).

- [ ] **Step 6: Lint, type-check, full suite, commit**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: clean; full suite green.

```bash
git add src/marim_harness/session/ctrl.py src/marim_harness/runtime/harness.py \
        src/marim_harness/runtime/bootstrap.py src/marim_harness/interfaces/tui/status.py \
        tests/test_session.py
git commit -m "$(cat <<'EOF'
feat(session): compaction gates on the resolved window/budget threshold

SessionController takes a ContextLimits + live model-id closure;
maybe_compact warms discovery then gates on min(budget, 0.8×window).
The TUI gauge denominates against the same threshold, and /model
switches invalidate discovered windows (LM Studio JIT reloads).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0111Y8LPyWyXqC9tv7MSonPt
EOF
)"
```

---

### Task 5: Masker takes the trigger directly; per-spawn resolution; motivating-failure regression

**Files:**
- Modify: `src/marim_harness/subagents/masking.py` (constructor + docstring trigger note)
- Modify: `src/marim_harness/subagents/runner.py` (constructor, `build`, `_prepare_spawn`)
- Modify: `src/marim_harness/runtime/harness.py` (`SubagentRunner(…)` call in `build_collaborators`)
- Test: `tests/test_subagent_masking.py`

**Interfaces:**
- Consumes: the `limits` object built in `build_collaborators` (Task 4).
- Produces: `ObservationMasker(trigger_tokens: int, keep_recent: int = 4, min_chars: int = 200)` (internal `_TRIGGER_RATIO` deleted — ratios must not stack with the threshold's 0.8); `SubagentRunner.__init__` replaces `max_context_tokens: int = 100_000` with `limits: ContextLimits | None = None`; `build(…)` gains `mask_trigger: int | None = None`.

- [ ] **Step 1: Update the masker unit tests (failing first)**

In `tests/test_subagent_masking.py`, change every `ObservationMasker(max_tokens=N, …)` construction to pass the trigger directly — the tests' documented trigger values are already `0.75 × max_tokens`, so substitute those numbers and the behavior contracts stay identical:

- `ObservationMasker(max_tokens=100_000)` → `ObservationMasker(trigger_tokens=75_000)`
- `ObservationMasker(max_tokens=1000, keep_recent=2, min_chars=100)` → `ObservationMasker(trigger_tokens=750, keep_recent=2, min_chars=100)` (all four occurrences)
- `ObservationMasker(max_tokens=1000, keep_recent=1, min_chars=100)` → `ObservationMasker(trigger_tokens=750, keep_recent=1, min_chars=100)`

Update inline comments that mention "trigger = 0.75 * 1000 = 750" to say "trigger = 750" (the ratio no longer exists here).

In the integration test `test_built_subagent_masks_stale_observations_in_requests`, replace the three `runner._max_context_tokens = 400 / _mask_keep_recent / _mask_min_chars` pokes with:

```python
    from marim_harness.config.context_limits import ContextLimits

    # threshold = 0.8 * 400 = 320 tokens; each blob is ~500 tokens.
    runner._limits = ContextLimits(budget=None, window_override=400)
    runner._mask_keep_recent = 1
    runner._mask_min_chars = 100
```

And add the motivating-failure regression test at the end of the file:

```python
@pytest.mark.anyio
async def test_spawn_trigger_follows_the_loaded_window_not_the_budget(tmp_path):
    """The failure that motivated the split: LM Studio loads a 262k model at
    ~101k while the configured budget said 180k — the spawn's mask trigger
    must follow 0.8 × the LOADED window, resolved per spawn."""
    from marim_harness.config.context_limits import ContextLimits

    async def fake_local():
        return {"qwen/qwen3.5-9b": 101_039}

    deps = _make_deps(tmp_path)
    runner = _make_harness(_text_model(), deps).subagents
    runner._limits = ContextLimits(budget=180_000, fetch_local=fake_local)
    trigger = await runner._mask_trigger_for("qwen/qwen3.5-9b")
    assert trigger == int(0.8 * 101_039)
```

(`_text_model` is in `tests/conftest.py` alongside `_make_deps`/`_make_harness`; import it with them.)

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest --no-cov tests/test_subagent_masking.py -v`
Expected: FAIL — `ObservationMasker` rejects `trigger_tokens`; `runner._limits`/`_mask_trigger_for` don't exist.

- [ ] **Step 3: Implement the masker signature change**

In `src/marim_harness/subagents/masking.py`:

- Delete the `_TRIGGER_RATIO = 0.75` constant and its comment.
- Constructor becomes:

```python
    def __init__(self, trigger_tokens: int, keep_recent: int = 4,
                 min_chars: int = 200) -> None:
        # The trigger arrives pre-derived (min(budget, 0.8 × window) — see
        # config/context_limits.py). No internal ratio on top: stacking one
        # would silently move masking to 0.6 of the window.
        self._trigger_tokens = trigger_tokens
        self._keep_recent = keep_recent
        self._min_chars = min_chars
```

(keep the `_masked_ids` initialization and its comment exactly as they are). Update the class docstring's parameter sentence: "``trigger_tokens`` is the masking trigger, already carrying any window safety ratio". The module docstring's saturated-regime paragraph stays.

- [ ] **Step 4: Implement the runner threading**

In `src/marim_harness/subagents/runner.py`:

Constructor: replace the `max_context_tokens: int = 100_000,` parameter with `limits: ContextLimits | None = None,` (import `from ..config.context_limits import ContextLimits` — under `TYPE_CHECKING` if pyright is satisfied with the runtime use via `Optional`; a plain import is also fine and cycle-free: `config.context_limits` imports only stdlib at module level). Replace the `self._max_context_tokens = max_context_tokens` line with:

```python
        self._limits = limits
```

and update the surrounding knobs comment: the masking knobs sentence now says the trigger is resolved per spawn from the runner's ContextLimits (per-spawn model overrides resolve their own window and budget), falling back to the historical 75k when no resolver is wired. Keep the existing sentence about the reactive backstop covering late triggers.

Add a helper next to `build`:

```python
    # The historical default trigger (0.75 × the old 100k budget), used only
    # when no ContextLimits is wired (bare embedders / legacy constructions).
    _FALLBACK_MASK_TRIGGER = 75_000

    async def _mask_trigger_for(self, model_id: str | None) -> int:
        """The masking trigger for a spawn: the resolver's threshold for the
        spawn's OWN model (a per-spawn override budgets/windows as itself, not
        as the session model), warmed here because spawn prep is async."""
        if self._limits is None:
            return self._FALLBACK_MASK_TRIGGER
        if model_id is None:
            model_id = getattr(self._get_model(), "model_name", None)
        return await self._limits.resolve(model_id)
```

`build(…)`: add a keyword param `mask_trigger: int | None = None` (document in the docstring: "the masking trigger resolved by the caller; None falls back to the legacy default") and change the masker construction to:

```python
            masker = ObservationMasker(
                mask_trigger if mask_trigger is not None else self._FALLBACK_MASK_TRIGGER,
                keep_recent=self._mask_keep_recent,
                min_chars=self._mask_min_chars,
            )
```

`_prepare_spawn`: before the `self.build(…)` call, resolve the trigger and pass it through:

```python
        mask_trigger = await self._mask_trigger_for(model)
        sub, err = self.build(type, max_output_chars, model, work_root, defn=defn,
                              depth=depth, mask_trigger=mask_trigger)
```

In `src/marim_harness/runtime/harness.py`, in the `SubagentRunner(…)` call, replace `max_context_tokens=cfg.max_context_tokens,` with `limits=limits,` (the object built in Task 4's Step 4 — it is in scope in `build_collaborators`).

- [ ] **Step 5: Run the affected suites**

Run: `uv run pytest --no-cov tests/test_subagent_masking.py tests/test_subagent_retry.py tests/test_subagent_tool.py -q`
Expected: ALL PASS.

- [ ] **Step 6: Lint, type-check, full suite, commit**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: clean; full suite green (this is the last task — the gate is the release gate).

```bash
git add src/marim_harness/subagents/masking.py src/marim_harness/subagents/runner.py \
        src/marim_harness/runtime/harness.py tests/test_subagent_masking.py
git commit -m "$(cat <<'EOF'
feat(subagents): mask trigger resolved per spawn from ContextLimits

ObservationMasker takes the trigger directly (no internal 0.75 —
ratios must not stack with the threshold's 0.8×window). A per-spawn
model override resolves its own window and budget.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0111Y8LPyWyXqC9tv7MSonPt
EOF
)"
```

---

## Notes for the implementer

- **Don't touch** `compaction.py` (its pure helpers keep their `max_tokens` parameter — only the values callers pass change) or the overflow backstop in `runner.py:_run_to_completion`/`errors.py` (they remain the net under the proactive layers).
- Test helpers referenced by name from `tests/conftest.py`: `_make_deps`, `_make_harness`, `_text_model`. Session tests: reuse the file's existing controller/history fixtures rather than inventing new ones.
- The claude-cli provider gets a discovery-less `ContextLimits` (threshold = budget); that's correct — marim's compaction/masking don't operate in that provider.
- If a wiring step's surrounding code moved a few lines from the `~line` hints, anchor on the quoted code, not the line number.
