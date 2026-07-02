# Context Window vs. Context Budget — Design

**Date:** 2026-07-02
**Status:** Approved (brainstormed in-session; LM Studio field availability verified live)

## Problem

marim conflates two different numbers in one setting, `max_context_tokens`:

1. **The model's real context window** — a hard physical limit. Exceeding it is a
   provider rejection ("Context size has been exceeded."). Compaction and
   sub-agent masking must trigger safely *below* it.
2. **A spend budget** — an economic ceiling the user sets so long histories don't
   burn money on expensive models (e.g. cap Opus at 60k even though its window
   is 200k).

Today the single number defaults to 100k, the session compaction gate fires at
100% of it, and the sub-agent masker at 75% of it. That is only safe when the
number is set well below the real window — an assumption that silently broke on
the local provider: a model advertising 262k was *loaded* in LM Studio at
~101k while marim was configured to 180k, so no proactive layer could ever
fire and a six-spawn research fan-out died on provider overflow.

Verified live against LM Studio's `/api/v0/models`:

```json
{"id": "qwen/qwen3.5-9b", "state": "loaded",
 "max_context_length": 262144, "loaded_context_length": 101039}
```

## Decision summary

- Split the concepts: **window** (real limit, discovered per model, env
  override, conservative fallback) and **budget** (user cost cap, global +
  per-model overrides).
- Derive **one trigger** used everywhere:
  `threshold(model) = min(budget(model) or ∞, int(0.8 × window(model)))`.
- Consumers of the threshold: session compaction gate, sub-agent
  `ObservationMasker` (which drops its internal 0.75 ratio — ratios must not
  stack), and the TUI context gauge (100% keeps meaning "compaction imminent").
- Discovery scope: OpenRouter + Google from the catalogs already fetched;
  local = LM Studio `/api/v0/models` probe only; everything else falls back to
  the env override / default. (Chosen over probing llama.cpp/Ollama too —
  untestable here, more maintenance surface — and over no local probing, which
  would leave the motivating failure manual.)
- Budget scope: global default plus per-model overrides (chosen over
  global-only and per-model-only) so cheap/free models can run unbudgeted while
  Opus stays capped.

## Config surface

| Variable | Type | Default | Meaning |
|---|---|---|---|
| `MARIM_CONTEXT_WINDOW` | int, optional | unset | Manual window override when discovery can't determine it. |
| `MARIM_CONTEXT_BUDGET` | int, optional | **100 000** | Global economic cap. Defaulting to today's 100k preserves current cloud behavior — discovery finding a 200k window must not silently double anyone's Opus bill. |
| `MARIM_CONTEXT_BUDGETS` | string, optional | unset | Per-model overrides, comma-separated `pattern=tokens` pairs, fnmatch on the qualified model id (e.g. `anthropic/claude-opus*=60000,openrouter/*free*=0`). `0` (or empty value) means "no budget for this model" — window-only. First matching pattern wins. |
| `MARIM_MAX_CONTEXT_TOKENS` | int, deprecated | — | Alias for `MARIM_CONTEXT_BUDGET`, honored with a one-time startup deprecation warning. Ignored when `MARIM_CONTEXT_BUDGET` is also set. |

The settings TUI field currently bound to `max_context_tokens` becomes the
budget field (label updated). Precedence for the effective budget:
per-model override → global budget → deprecated alias → default 100k.

Window precedence: `MARIM_CONTEXT_WINDOW` (explicit user statement beats
discovery — the escape hatch when discovery lies) → discovered value →
100k fallback.

The safety ratio `0.8` is a module constant, not config (YAGNI).

## Components

### `ContextLimits` resolver (new module: `src/marim_harness/config/context_limits.py`)

One object owning both concepts. Interface (names indicative):

- `async window(model_qualified_id) -> int` — discovery-backed, cached,
  never raises.
- `budget(model_qualified_id) -> int | None` — pure config lookup
  (global + fnmatch overrides; `None` = unbudgeted).
- `async threshold(model_qualified_id) -> int` — `min(budget or ∞, int(0.8 × window))`.

Discovery sources:

- **OpenRouter**: `context_length` from the `/api/v1/models` catalog response
  (already fetched for the model picker; the field is currently parsed and
  discarded). `ModelEntry` grows `context_window: int | None = None`.
- **Google**: `inputTokenLimit` from `/v1beta/models` (same story).
- **Local**: GET `{base_url minus /v1}/api/v0/models` (LM Studio's enhanced
  REST API). Prefer `loaded_context_length` (the true serving window), fall
  back to `max_context_length`. `loaded_context_length` exists only on models
  with `"state": "loaded"`; a JIT-loaded model may load at a different size,
  so the cache is invalidated on model switch for the local provider.
- **claude-cli**: not applicable (Claude runs its own loop; marim's
  compaction/masking don't operate there).

Failure behavior: all probes best-effort with short timeouts; any failure →
env override → 100k default. Discovery must never fail a turn.

Caching: per qualified model id for the session; local-provider entries
re-resolved on `/model` switch (see above). Cloud catalog numbers are static
enough to cache for the session lifetime.

### Session compaction gate

`SessionController` receives a live threshold source instead of a fixed
`max_context_tokens` int. `maybe_compact` gates on
`estimate_tokens(history) > threshold(current model)` — the compare moves from
"100% of budget" to "100% of threshold", where the threshold already carries
the 0.8 window headroom. The forced-compaction-on-overflow path is unchanged.

### Sub-agent masker

`ObservationMasker` takes the trigger token count directly
(`trigger_tokens: int`) instead of `max_tokens` with an internal
`_TRIGGER_RATIO = 0.75`. `SubagentRunner` resolves the threshold for the
spawn's *own* model (per-spawn `model` overrides resolve their own window and
budget) during `_prepare_spawn` (async, so the resolver can await) and passes
it into `build`. The runner's current `max_context_tokens`/`mask_*`
constructor params: `mask_keep_recent`/`mask_min_chars`/`mask_observations`
stay; `max_context_tokens` is replaced by the resolver handle, threaded like
`get_model` is today.

### TUI gauge

`interfaces/tui/status.py` denominates the context gauge against the current
model's threshold (was: `session.max_context_tokens`). Semantics preserved:
100% ≈ compaction imminent. No new UI elements.

### Wiring

`build_collaborators` constructs one `ContextLimits` next to the `get_model`
closure and hands it to `SessionController`, `SubagentRunner`, and (via the
harness) the status bar. Resolution happens at harness build, on `/model`
switch, and per spawn — all already-async sites; the cache makes repeats free.

## What deliberately does not change

- The 0.8 ratio is not user-configurable.
- No llama.cpp / Ollama probes (env override covers them).
- The shed-and-resume backstop, overflow detection markers, and
  forced-compaction-on-overflow stay as-is — they remain the net under the
  proactive layers.
- `compaction.py`'s pure helpers keep their `max_tokens` parameter shape; only
  the *value* callers pass changes (the threshold).

## Testing

- **Resolver unit tests** (pure): catalog payload parsing (OpenRouter
  `context_length`, Google `inputTokenLimit`, LM Studio
  `loaded_context_length`/`max_context_length`/not-loaded shapes), precedence
  (env window override beats discovery; per-model budget beats global beats
  alias beats default), fnmatch matching incl. first-match-wins and the
  `=0` unbudgeted form, threshold math, failure fallback.
- **Regression test pinning the motivating failure**: discovered window
  (~101k) ≪ configured budget (180k) → threshold follows `0.8 × window`, so
  masking/compaction trigger below the real wall.
- **Wiring tests**: a spawn with a per-spawn model override masks against that
  model's threshold, not the session model's; session compaction gates on the
  threshold after a `/model` switch; deprecated `MARIM_MAX_CONTEXT_TOKENS`
  maps to the budget with a warning.
- **Masker**: existing `ObservationMasker` tests updated for the
  `trigger_tokens` signature (trigger values adjusted; behavior contracts —
  batch masking, cache stability, mutation-freedom — unchanged).
