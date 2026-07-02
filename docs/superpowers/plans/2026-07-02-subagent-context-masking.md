# Sub-agent Context Masking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sub-agents no longer die on context overflow: stale tool observations are masked proactively per-request (JetBrains-style context masking), and an overflow that slips through anyway is recovered once by shedding observations from the captured history and resuming.

**Architecture:** Two layers. (1) *Proactive:* a stateful per-spawn `ObservationMasker` registered as a `ProcessHistory` capability on every built sub-agent — when the request's token estimate crosses a trigger it masks stale `ToolReturnPart` payloads in one batch, then re-applies exactly that set on later requests so the request prefix stays byte-stable (cache-friendly) between trigger events. (2) *Reactive backstop:* `SubagentRunner._run_to_completion` catches a provider context-overflow rejection, aggressively masks the captured conversation, and resumes once — mirroring the main turn's compact-and-retry-once pattern in `runtime/controller.py:497-551`. Both layers reuse the existing pure helpers in `compaction.py` (`mask_stale_observations`, `MASKED_OBSERVATION`, `estimate_tokens`).

**Tech Stack:** Python ≥3.10, pydantic-ai (`ProcessHistory` capability, `capture_run_messages`, `FunctionModel` for tests), pytest + anyio.

## Global Constraints

- Use `uv` for everything: `uv run pytest …`, `uv run ruff check src tests`, `uv run pyright`. Never bare `python`/`pip`/`pytest`.
- Ruff line length is 100; lint set `E,F,I,UP,B,SIM` (import sorting enforced).
- `requires-python = ">=3.10"` — no 3.11+-only syntax.
- CI order is ruff → pyright → pytest; run all three locally before claiming a task done.
- The codebase favors long *why* comments around invariants (resumability, caching). The comments in the code blocks below are part of the deliverable — keep them.
- Commit messages end with the two trailer lines shown in each commit step.

## Background you need (read once)

- **Where masking already exists:** `src/marim_harness/compaction.py:151-204` defines `MASKED_OBSERVATION` and `mask_stale_observations(history, keep_recent=4, *, min_chars=200) -> tuple[list, int]`. It is pure (never mutates input; rebuilds via `dataclasses.replace`), idempotent (skips already-masked returns), keeps the newest `keep_recent` `ToolReturnPart`s intact, and only masks returns whose rendered length ≥ `min_chars`. `estimate_tokens(history) -> int` (same file, line 51) is the char/4 heuristic. The *session* uses these only at compaction time; nothing touches sub-agents today.
- **Why the masker must be stateful:** whether a `ProcessHistory` rewrite persists is an upstream implementation detail — pydantic-ai 1.107 writes the processed messages back into run state (`ctx.state.message_history[:] = messages` in `_agent_graph._prepare_request`, verified against the installed source), but that semantics has differed across versions. Under a request-only semantics, a stateless "mask everything but the newest N" masks relative to a boundary that moves every request → the request prefix changes every request → provider prompt cache busts every request. The masker therefore remembers which `tool_call_id`s it masked and only *extends* that set when the estimate crosses the trigger again — byte-stable between triggers under either upstream semantics. Side effect of the current write-back semantics: masking persists into `all_messages()` and saved transcripts (pinned by the Task 3 integration test).
- **Where sub-agents are built/run:** `src/marim_harness/subagents/runner.py`. `SubagentRunner.build` constructs the pydantic-ai `Agent` with `capabilities=[ProcessHistory(_drop_nameless_tool_calls)]` (line ~321). `_run_to_completion` (line ~393) is the retry loop: it retries only `is_transient_model_error(exc)` with backoff, resuming from `_resumable_history(captured)`. A context overflow is a permanent 400 → today it raises immediately and the foreground containment in `_execute_foreground_spawn` returns `"Sub-agent 'X' failed: …"`.
- **Overflow detection gap:** `is_context_overflow_error` (`src/marim_harness/runtime/errors.py:215`) only looks for an `openai.APIError` in the exception chain. A sub-agent's model layer raises pydantic-ai `ModelHTTPError` (tests construct these directly; some providers never chain an openai error), so detection must also read `ModelHTTPError`. The helper `_find_model_http_error` already exists in the same file (line 134).
- **Test helpers:** `tests/conftest.py` provides `_make_deps(tmp_path)`, `_make_harness(model, deps)` (→ `.subagents` is a real `SubagentRunner`), `_text_model()`. `tests/test_subagent_retry.py` shows the house pattern: drive `_run_to_completion` with a pydantic-ai `Agent(FunctionModel(fn))` whose `fn(messages, info)` inspects history and returns `ModelResponse` parts or raises.

## File Structure

- **Create** `src/marim_harness/subagents/masking.py` — `ObservationMasker` (pure logic + tiny per-run state; no I/O). New leaf module: imports only `pydantic_ai.messages` and `..compaction`, so no import cycle (compaction imports nothing from marim).
- **Modify** `src/marim_harness/runtime/errors.py` — extend `is_context_overflow_error` to also classify `ModelHTTPError`.
- **Modify** `src/marim_harness/subagents/runner.py` — masker knobs on the constructor, masker capability in `build`, overflow shed-and-resume in `_run_to_completion`, overflow-specific containment message in `_execute_foreground_spawn`.
- **Modify** `src/marim_harness/runtime/harness.py` — thread the four knobs from `HarnessConfig` into the `SubagentRunner(...)` call in `build_collaborators` (reusing the existing `max_context_tokens` / `mask_observations` / `mask_keep_recent` / `mask_min_chars` config fields — no new config).
- **Create** `tests/test_subagent_masking.py` — masker unit tests + built-sub-agent integration test.
- **Modify** `tests/test_provider_errors.py`, `tests/test_subagent_retry.py` — detection + backstop tests.

---

### Task 1: Overflow detection sees `ModelHTTPError`

**Files:**
- Modify: `src/marim_harness/runtime/errors.py:215-232` (`is_context_overflow_error`)
- Test: `tests/test_provider_errors.py`

**Interfaces:**
- Consumes: existing `_find_api_error`, `_find_model_http_error`, `_OVERFLOW_MARKERS` (same file).
- Produces: `is_context_overflow_error(exc: BaseException) -> bool` now returns True for a `pydantic_ai.exceptions.ModelHTTPError` whose message/body names an overflow. Task 4 depends on this exact behavior.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_provider_errors.py` (it already imports `is_context_overflow_error`; add `from pydantic_ai.exceptions import ModelHTTPError` to its imports if not present — check first, `test_subagent_retry.py` imports it that way):

```python
def test_is_context_overflow_detects_model_http_error_body():
    """A sub-agent's model layer surfaces the provider rejection as a pydantic-ai
    ModelHTTPError (no openai.APIError in the chain). The detector must classify
    it, or the runner's shed-and-resume backstop never fires."""
    err = ModelHTTPError(
        400, "m",
        body={"message": "This model's maximum context length is 8192 tokens."},
    )
    assert is_context_overflow_error(err) is True


def test_is_context_overflow_model_http_error_plain_400_is_false():
    """A genuine bad request must NOT read as an overflow — the backstop would
    mask-and-resume a request that will fail identically."""
    err = ModelHTTPError(
        400, "m", body={"message": "invalid request: unsupported parameter"}
    )
    assert is_context_overflow_error(err) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_provider_errors.py -k model_http_error -v`
Expected: `test_is_context_overflow_detects_model_http_error_body` FAILS (`assert False is True`); the plain-400 test passes already (that's fine — it pins the negative).

- [ ] **Step 3: Extend the detector**

In `src/marim_harness/runtime/errors.py`, replace the body of `is_context_overflow_error` (keep the function where it is):

```python
def is_context_overflow_error(exc: BaseException) -> bool:
    """True when ``exc`` is a provider rejection for exceeding the context window.

    The harness's token estimate is a coarse char/4 heuristic, so it can undershoot
    the real window and let a too-large request through; the caller uses this to
    force a compaction (main turn) or an observation shed (sub-agent) and retry
    instead of failing outright.

    Two shapes are recognized: an ``openai.APIError`` in the chain (the main
    turn's shape — OpenRouter nests the detail in the body), and a pydantic-ai
    ``ModelHTTPError`` (the shape a sub-agent's model layer raises, which may not
    chain an openai error at all)."""
    api = _find_api_error(exc)
    if api is not None:
        err = _error_dict(api) or {}
        if err.get("code") == "context_length_exceeded":
            return True
        haystack = [str(api), str(err.get("message") or "")]
        meta = err.get("metadata")
        if isinstance(meta, dict):
            haystack.append(str(meta.get("raw") or ""))
        if any(m in " ".join(haystack).lower() for m in _OVERFLOW_MARKERS):
            return True
    http = _find_model_http_error(exc)
    if http is None:
        return False
    blob = f"{http} {getattr(http, 'body', '') or ''}".lower()
    return any(marker in blob for marker in _OVERFLOW_MARKERS)
```

- [ ] **Step 4: Run the whole provider-errors file**

Run: `uv run pytest --no-cov tests/test_provider_errors.py -v`
Expected: ALL PASS (the pre-existing openai-shape tests must still pass — the openai branch is unchanged except falling through instead of returning False early).

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check src tests && uv run pyright`
Expected: clean.

```bash
git add src/marim_harness/runtime/errors.py tests/test_provider_errors.py
git commit -m "$(cat <<'EOF'
feat(runtime): detect context overflow on pydantic-ai ModelHTTPError

is_context_overflow_error only read openai.APIError, so a sub-agent's
overflow (surfaced as ModelHTTPError) was never classified. Needed by
the sub-agent shed-and-resume backstop.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0111Y8LPyWyXqC9tv7MSonPt
EOF
)"
```

---

### Task 2: `ObservationMasker` — stateful, cache-stable per-spawn masking

**Files:**
- Create: `src/marim_harness/subagents/masking.py`
- Test: `tests/test_subagent_masking.py`

**Interfaces:**
- Consumes: `mask_stale_observations`, `MASKED_OBSERVATION`, `estimate_tokens` from `marim_harness.compaction`; `ToolReturnPart`, `ModelMessage` from `pydantic_ai.messages`.
- Produces: `ObservationMasker(max_tokens: int, keep_recent: int = 4, min_chars: int = 200)` with method `mask(self, messages: list[ModelMessage]) -> list[ModelMessage]`. Task 3 registers `ProcessHistory(masker.mask)`; the constructor signature above is exactly what `SubagentRunner.build` will call.

- [ ] **Step 1: Write the failing unit tests**

Create `tests/test_subagent_masking.py`:

```python
"""Per-spawn context masking for sub-agents.

The masker rides every outgoing sub-agent request (a ProcessHistory capability).
Its contract has three parts these tests pin: (1) below the trigger it changes
nothing; (2) crossing the trigger masks stale tool observations in one batch,
sparing the newest keep_recent; (3) between triggers it re-applies EXACTLY the
committed set — a return spared at trigger time stays unmasked even after newer
returns arrive, so the request prefix is byte-stable and the provider prompt
cache survives. A stateless newest-N mask would fail (3).
"""

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from marim_harness.compaction import MASKED_OBSERVATION
from marim_harness.subagents.masking import ObservationMasker


def _round(i: int, size: int) -> list:
    """One tool round: the assistant calls tool ``t{i}``; it returns ``size`` chars."""
    return [
        ModelResponse(parts=[
            ToolCallPart(tool_name="read_file", args={}, tool_call_id=f"t{i}")
        ]),
        ModelRequest(parts=[
            ToolReturnPart(tool_name="read_file", content="x" * size,
                           tool_call_id=f"t{i}")
        ]),
    ]


def _history(rounds: int, size: int) -> list:
    history: list = [ModelRequest(parts=[UserPromptPart(content="task")])]
    for i in range(rounds):
        history += _round(i, size)
    return history


def _returns(history) -> dict[str, str]:
    """tool_call_id -> content for every ToolReturnPart in ``history``."""
    return {
        p.tool_call_id: str(p.content)
        for m in history
        for p in getattr(m, "parts", [])
        if isinstance(p, ToolReturnPart)
    }


def test_below_trigger_masks_nothing():
    masker = ObservationMasker(max_tokens=100_000)
    history = _history(rounds=3, size=400)
    view = masker.mask(history)
    assert all(c == "x" * 400 for c in _returns(view).values())


def test_crossing_trigger_masks_stale_keeps_recent():
    # trigger = 0.75 * 1000 = 750 tokens; 4 rounds x 1200 chars ≈ 1200 tokens.
    masker = ObservationMasker(max_tokens=1000, keep_recent=2, min_chars=100)
    view = masker.mask(_history(rounds=4, size=1200))
    returns = _returns(view)
    assert returns["t0"] == MASKED_OBSERVATION
    assert returns["t1"] == MASKED_OBSERVATION
    assert returns["t2"] == "x" * 1200
    assert returns["t3"] == "x" * 1200


def test_never_mutates_the_input_history():
    masker = ObservationMasker(max_tokens=1000, keep_recent=2, min_chars=100)
    history = _history(rounds=4, size=1200)
    masker.mask(history)
    assert all(c == "x" * 1200 for c in _returns(history).values())


def test_mask_set_is_stable_between_triggers():
    """After a trigger, a spared return stays unmasked even once newer returns
    arrive — until the NEXT trigger. This is the cache-stability property; a
    stateless newest-N mask would re-mask t2 here and bust the prefix cache."""
    masker = ObservationMasker(max_tokens=1000, keep_recent=2, min_chars=100)
    history = _history(rounds=4, size=1200)
    masker.mask(history)                       # trigger 1: masks t0, t1
    history += _round(4, size=200)             # small growth: stays under trigger
    view = masker.mask(history)
    returns = _returns(view)
    assert returns["t2"] == "x" * 1200         # spared at trigger 1, STILL spared
    assert returns["t4"] == "x" * 200


def test_second_trigger_extends_the_mask_set():
    masker = ObservationMasker(max_tokens=1000, keep_recent=2, min_chars=100)
    history = _history(rounds=4, size=1200)
    masker.mask(history)                       # trigger 1: masks t0, t1
    history += _round(4, size=1200)            # big growth: crosses trigger again
    view = masker.mask(history)
    returns = _returns(view)
    assert returns["t2"] == MASKED_OBSERVATION  # newly stale, masked at trigger 2
    assert returns["t3"] == "x" * 1200          # newest 2 spared
    assert returns["t4"] == "x" * 1200


def test_small_returns_are_never_masked():
    masker = ObservationMasker(max_tokens=1000, keep_recent=1, min_chars=100)
    history = [ModelRequest(parts=[UserPromptPart(content="task")])]
    history += _round(0, size=50)              # tiny: below min_chars
    history += _round(1, size=4000)
    history += _round(2, size=4000)
    view = masker.mask(history)
    returns = _returns(view)
    assert returns["t0"] == "x" * 50           # small stays, masking it buys nothing
    assert returns["t1"] == MASKED_OBSERVATION
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_subagent_masking.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'marim_harness.subagents.masking'`.

- [ ] **Step 3: Implement the masker**

Create `src/marim_harness/subagents/masking.py`:

```python
"""Per-run context masking for sub-agents.

A sub-agent's context is dominated by tool observations (file reads, grep dumps,
command output) whose useful lifespan is short: once the model has acted on an
observation, the raw payload is dead weight. ``ObservationMasker`` watches the
outgoing request size and, past a trigger, swaps stale observation payloads for
:data:`~marim_harness.compaction.MASKED_OBSERVATION` — the model keeps the
*trace* of what it did and can re-run a tool if it still needs a masked output.

The masker is deliberately **stateful, one instance per spawn**. Whether a
``ProcessHistory`` rewrite persists between requests is an upstream
implementation detail: pydantic-ai currently writes the processed history back
into the run's state (``ctx.state.message_history[:] = messages`` in
``_agent_graph._prepare_request``), but that semantics has differed across
versions, and under a request-only semantics a stateless "mask everything older
than the newest N" would mask against a boundary that moves every request —
rewriting the request prefix every time and busting the provider prompt cache
on each call. The masker therefore remembers which returns it masked (by
``tool_call_id``) and re-applies exactly that set, only *extending* it when the
estimate crosses the trigger again. Between trigger events the request prefix
is byte-stable under either upstream semantics, so masking costs one cache miss
per trigger — the same bargain session compaction makes. A side effect of the
current write-back: the run's ``all_messages()`` and saved transcripts carry
the masked placeholders — i.e. what the model actually saw.
"""

from __future__ import annotations

import dataclasses

from pydantic_ai.messages import ModelMessage, ToolReturnPart

from ..compaction import MASKED_OBSERVATION, estimate_tokens, mask_stale_observations

# Fraction of the context budget at which masking kicks in. Below it the history
# rides untouched; above it, stale observations are masked in one batch. Kept
# comfortably under 1.0 because estimate_tokens is a char/4 heuristic that can
# undershoot the provider's real tokenizer.
_TRIGGER_RATIO = 0.75


class ObservationMasker:
    """Masks stale tool observations in a sub-agent's outgoing requests.

    Build one per spawn and register its :meth:`mask` as a ``ProcessHistory``
    capability. ``max_tokens`` is the model's context budget (the same value the
    session compactor uses); ``keep_recent``/``min_chars`` have the semantics of
    :func:`marim_harness.compaction.mask_stale_observations`.
    """

    def __init__(self, max_tokens: int, keep_recent: int = 4,
                 min_chars: int = 200) -> None:
        self._trigger_tokens = int(max_tokens * _TRIGGER_RATIO)
        self._keep_recent = keep_recent
        self._min_chars = min_chars
        # tool_call_ids whose returns are masked. Monotonic — ids are only ever
        # added — which is what keeps the request prefix stable between triggers.
        self._masked_ids: set[str] = set()

    def mask(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        """The ProcessHistory hook: re-apply the committed mask set, then extend
        it (sparing the newest ``keep_recent`` returns) if the request would still
        run past the trigger. Never mutates ``messages`` or its parts."""
        view = self._apply(messages)
        if estimate_tokens(view) <= self._trigger_tokens:
            return view
        view, masked = mask_stale_observations(
            view, self._keep_recent, min_chars=self._min_chars
        )
        if masked:
            self._commit(view)
        return view

    def _apply(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        """Rebuild ``messages`` with every return in the committed set masked.
        Returns a fresh list; untouched messages are shared, not copied."""
        out = list(messages)
        if not self._masked_ids:
            return out
        for idx, message in enumerate(out):
            parts = getattr(message, "parts", None)
            if not parts:
                continue
            new_parts = list(parts)
            changed = False
            for pidx, part in enumerate(parts):
                if (
                    isinstance(part, ToolReturnPart)
                    and part.tool_call_id in self._masked_ids
                    and part.content != MASKED_OBSERVATION
                ):
                    new_parts[pidx] = dataclasses.replace(
                        part, content=MASKED_OBSERVATION
                    )
                    changed = True
            if changed:
                out[idx] = dataclasses.replace(message, parts=new_parts)
        return out

    def _commit(self, view: list[ModelMessage]) -> None:
        """Record every masked return in ``view`` so later requests re-apply the
        exact same set."""
        for message in view:
            for part in getattr(message, "parts", []):
                if (
                    isinstance(part, ToolReturnPart)
                    and part.content == MASKED_OBSERVATION
                ):
                    self._masked_ids.add(part.tool_call_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_subagent_masking.py -v`
Expected: ALL PASS. If `test_mask_set_is_stable_between_triggers` fails because the small growth still crossed the trigger, the token arithmetic drifted — the masked view after trigger 1 is ≈ 2 masked placeholders (~20 tokens each) + 2 × 300-token returns ≈ 645 tokens against a 750 trigger, so a 200-char (50-token) round must not re-trigger. Fix the test sizes, not the masker.

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check src tests && uv run pyright`
Expected: clean.

```bash
git add src/marim_harness/subagents/masking.py tests/test_subagent_masking.py
git commit -m "$(cat <<'EOF'
feat(subagents): ObservationMasker — cache-stable stale-observation masking

Stateful per-spawn masker: on crossing a token trigger it masks stale
tool returns in one batch (reusing compaction.mask_stale_observations)
and re-applies exactly that set on later requests, so the request
prefix stays byte-stable between triggers and prompt caching survives.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0111Y8LPyWyXqC9tv7MSonPt
EOF
)"
```

---

### Task 3: Wire the masker into every built sub-agent

**Files:**
- Modify: `src/marim_harness/subagents/runner.py` (constructor ~line 112-155; `build` ~line 299-322)
- Modify: `src/marim_harness/runtime/harness.py` (the `SubagentRunner(...)` call, ~line 241-257)
- Modify: `CLAUDE.md` (the `subagents/` bullet under "Supporting subsystems")
- Test: `tests/test_subagent_masking.py` (append the integration test)

**Interfaces:**
- Consumes: `ObservationMasker` from Task 2 (constructor `(max_tokens, keep_recent=, min_chars=)`, method `mask`).
- Produces: `SubagentRunner.__init__` gains keyword params `max_context_tokens: int = 100_000, mask_observations: bool = True, mask_keep_recent: int = 4, mask_min_chars: int = 200`, stored as `self._max_context_tokens`, `self._mask_observations`, `self._mask_keep_recent`, `self._mask_min_chars` (Task 4's tests poke these attribute names directly). Every `build()` output carries the masker capability when `mask_observations` is on.

- [ ] **Step 1: Write the failing integration test**

First update the import block at the top of `tests/test_subagent_masking.py`: add `TextPart` to the existing `from pydantic_ai.messages import (...)` group, and add these imports (ruff's import sorting will dictate the order — `uv run ruff check --fix tests` settles it):

```python
import pytest
from pydantic_ai.models.function import FunctionModel

from tests.conftest import _make_deps, _make_harness
```

Then append the test:

```python
@pytest.mark.anyio
async def test_built_subagent_masks_stale_observations_in_requests(tmp_path):
    """End-to-end through SubagentRunner.build: with a tiny context budget, older
    bulky tool returns are masked in the request the model actually sees — and,
    because pydantic-ai writes the processed history back into run state
    (``ctx.state.message_history[:] = messages``), the masking persists into
    ``all_messages()``. The second property is pinned so an upstream semantics
    change is caught here instead of silently altering transcript content."""
    seen: dict = {}
    calls = {"n": 0}

    def fn(messages, info):
        calls["n"] += 1
        if calls["n"] <= 3:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="blob", args={}, tool_call_id=f"t{calls['n']}")])
        seen["messages"] = messages
        return ModelResponse(parts=[TextPart(content="done")])

    deps = _make_deps(tmp_path)
    runner = _make_harness(FunctionModel(fn), deps).subagents
    runner._max_context_tokens = 400   # trigger ≈ 300 tokens; each blob is ~500
    runner._mask_keep_recent = 1
    runner._mask_min_chars = 100
    sub, err = runner.build("general")
    assert err is None, err
    assert sub is not None

    @sub.tool_plain
    def blob() -> str:
        return "x" * 2000

    result = await runner._run_to_completion(sub, "go", deps, None, None)
    assert result.output == "done"

    request_returns = [
        str(p.content) for m in seen["messages"]
        for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)
    ]
    assert MASKED_OBSERVATION in request_returns        # stale observations masked
    assert any("x" * 100 in c for c in request_returns)  # newest spared

    stored_returns = [
        str(p.content) for m in result.all_messages()
        for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)
    ]
    # Write-back semantics: the processed (masked) history IS the stored history.
    assert MASKED_OBSERVATION in stored_returns
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest --no-cov tests/test_subagent_masking.py::test_built_subagent_masks_stale_observations_in_requests -v`
Expected: FAIL — `AttributeError` on `runner._max_context_tokens` (or, if you set attributes leniently, `MASKED_OBSERVATION in request_returns` asserts False).

- [ ] **Step 3: Add the knobs to `SubagentRunner.__init__`**

In `src/marim_harness/subagents/runner.py`, extend the constructor signature (after `max_depth: int = 3`):

```python
                 max_depth: int = 3,
                 max_context_tokens: int = 100_000,
                 mask_observations: bool = True,
                 mask_keep_recent: int = 4,
                 mask_min_chars: int = 200) -> None:
```

and at the end of `__init__` (after `self._max_depth = max_depth`):

```python
        # Context masking for spawned sub-agents. A sub-agent does the read-heavy
        # fan-out work, so its history is dominated by tool observations; past a
        # token trigger those are masked per-request by an ObservationMasker (one
        # per spawn — see masking.py for why the state matters). The knobs are the
        # same user-facing settings the session compactor reads.
        self._max_context_tokens = max_context_tokens
        self._mask_observations = mask_observations
        self._mask_keep_recent = mask_keep_recent
        self._mask_min_chars = mask_min_chars
```

- [ ] **Step 4: Register the masker capability in `build`**

Add the import at the top of `runner.py` with the other relative imports:

```python
from .masking import ObservationMasker
```

In `build`, replace the `capabilities=[ProcessHistory(_drop_nameless_tool_calls)]` argument of the `Agent(...)` construction. First, just above the `sub = Agent(` line, build the list:

```python
        capabilities: list = [ProcessHistory(_drop_nameless_tool_calls)]
        if self._mask_observations:
            # One masker PER SPAWN: it holds the run's committed mask set, and
            # sharing an instance across spawns would leak one run's masked
            # tool_call_ids into another's requests.
            masker = ObservationMasker(
                self._max_context_tokens,
                keep_recent=self._mask_keep_recent,
                min_chars=self._mask_min_chars,
            )
            capabilities.append(ProcessHistory(masker.mask))
```

then in the `Agent(...)` call pass `capabilities=capabilities` (keep the existing long comment about the nameless-tool-call scrub attached to the list construction above).

- [ ] **Step 5: Thread the knobs through `build_collaborators`**

In `src/marim_harness/runtime/harness.py`, in the `SubagentRunner(...)` call (~line 241), add after `max_depth=SUBAGENT_MAX_DEPTH,`:

```python
        # Sub-agents reuse the session's context budget and masking knobs — one
        # user-facing setting governs both the main history and spawned runs.
        max_context_tokens=cfg.max_context_tokens,
        mask_observations=cfg.mask_observations,
        mask_keep_recent=cfg.mask_keep_recent,
        mask_min_chars=cfg.mask_min_chars,
```

- [ ] **Step 6: Run the masking tests and the sub-agent suite**

Run: `uv run pytest --no-cov tests/test_subagent_masking.py -v`
Expected: ALL PASS (the integration test may re-trigger masking more than once — assertions only require *some* masked and the newest spared, which holds).

Run: `uv run pytest --no-cov tests/test_subagent_retry.py tests/test_subagent_tool.py tests/test_agent_subagents.py -q`
Expected: ALL PASS (existing spawns are unaffected: default budget 100k means the trigger never fires in those tests).

- [ ] **Step 7: Update CLAUDE.md's subagents bullet**

In `CLAUDE.md`, extend the `subagents/` line under "Supporting subsystems":

```markdown
- `subagents/` — `runner.py` (`SubagentRunner`: spawns and drives isolated
  sub-agents), `masking.py` (per-spawn context masking of stale tool
  observations), and `cli_backend.py` (the optional `claude -p` CLI backend it
  delegates to). Re-exported as `marim_harness.subagents.SubagentRunner`.
```

- [ ] **Step 8: Lint, type-check, commit**

Run: `uv run ruff check src tests && uv run pyright`
Expected: clean.

```bash
git add src/marim_harness/subagents/runner.py src/marim_harness/runtime/harness.py \
        tests/test_subagent_masking.py CLAUDE.md
git commit -m "$(cat <<'EOF'
feat(subagents): mask stale observations on every built sub-agent

Each spawn gets its own ObservationMasker as a ProcessHistory
capability, gated and sized by the same config knobs the session
compactor uses (mask_observations, max_context_tokens, ...).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0111Y8LPyWyXqC9tv7MSonPt
EOF
)"
```

---

### Task 4: Reactive backstop — shed and resume once on overflow

**Files:**
- Modify: `src/marim_harness/subagents/runner.py` (`_run_to_completion` ~line 393-437; new helpers `_shed_context`, `_notice_overflow` next to `_notice_retry`)
- Test: `tests/test_subagent_retry.py`

**Interfaces:**
- Consumes: `is_context_overflow_error` (Task 1), `mask_stale_observations` from `..compaction`, existing `_resumable_history`, `_notice_retry` pattern, `self.deps.ui.on_subagent_notice`.
- Produces: `_run_to_completion` recovers from ONE overflow per run by resuming from a shed history; `_shed_context(messages: list) -> list | None` (None ⇒ nothing to shed). Task 5 relies on overflow exceptions still propagating when recovery is impossible or exhausted.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_retry.py`:

```python
def _overflow() -> ModelHTTPError:
    return ModelHTTPError(
        400, "m", body={"message": "This model's maximum context length is "
                                   "8192 tokens; your request used more."}
    )


@pytest.mark.anyio
async def test_overflow_sheds_stale_observations_and_resumes(tmp_path: Path):
    """A context overflow mid-run is recovered ONCE: the captured conversation is
    resumed with stale tool observations masked, so the run finishes instead of
    dying — and without re-running the tools (same resume contract as the
    transient path)."""
    runner, sleeps = _runner(tmp_path)
    state = {"raised": False}
    seen: dict = {}

    def fn(messages, info):
        parts = [p for m in messages for p in getattr(m, "parts", [])]
        returns = [p for p in parts if type(p).__name__ == "ToolReturnPart"]
        if len(returns) < 2:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="blob", args={}, tool_call_id=f"t{len(returns)}")])
        if not state["raised"]:
            state["raised"] = True
            raise _overflow()
        seen["messages"] = messages
        return ModelResponse(parts=[TextPart(content="done")])

    sub = Agent(FunctionModel(fn))

    @sub.tool_plain
    def blob() -> str:
        return "x" * 500

    result = await runner._run_to_completion(sub, "go", None, None, None)
    assert result.output == "done"
    assert sleeps == []  # overflow recovery resumes immediately, no backoff

    from marim_harness.compaction import MASKED_OBSERVATION
    contents = [
        str(p.content) for m in seen["messages"]
        for p in getattr(m, "parts", [])
        if type(p).__name__ == "ToolReturnPart"
    ]
    assert contents[0] == MASKED_OBSERVATION   # stale observation shed
    assert contents[1] == "x" * 500            # newest spared (keep_recent=1)


@pytest.mark.anyio
async def test_overflow_with_nothing_to_shed_raises(tmp_path: Path):
    """When masking can free nothing (only one observation, which is spared), a
    resume would fail identically — the overflow must surface, not loop."""
    runner, _ = _runner(tmp_path)
    calls = {"n": 0}

    def fn(messages, info):
        calls["n"] += 1
        parts = [p for m in messages for p in getattr(m, "parts", [])]
        if not any(type(p).__name__ == "ToolReturnPart" for p in parts):
            return ModelResponse(parts=[ToolCallPart(
                tool_name="blob", args={}, tool_call_id="t0")])
        raise _overflow()

    sub = Agent(FunctionModel(fn))

    @sub.tool_plain
    def blob() -> str:
        return "x" * 500

    with pytest.raises(ModelHTTPError):
        await runner._run_to_completion(sub, "go", None, None, None)
    assert calls["n"] == 2  # tool round + the failing request; no resume attempt


@pytest.mark.anyio
async def test_overflow_gives_up_after_one_shed(tmp_path: Path):
    """A second overflow after a successful shed-and-resume surfaces: masking
    already freed everything it could."""
    runner, _ = _runner(tmp_path)
    calls = {"n": 0}

    def fn(messages, info):
        calls["n"] += 1
        parts = [p for m in messages for p in getattr(m, "parts", [])]
        returns = [p for p in parts if type(p).__name__ == "ToolReturnPart"]
        if len(returns) < 2:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="blob", args={}, tool_call_id=f"t{len(returns)}")])
        raise _overflow()

    sub = Agent(FunctionModel(fn))

    @sub.tool_plain
    def blob() -> str:
        return "x" * 500

    with pytest.raises(ModelHTTPError):
        await runner._run_to_completion(sub, "go", None, None, None)
    # 2 tool rounds + overflow, then exactly ONE resumed request that overflows
    # again and surfaces: 4 model calls total.
    assert calls["n"] == 4


@pytest.mark.anyio
async def test_overflow_shed_emits_a_ui_notice_for_a_foreground_spawn(tmp_path: Path):
    runner, _ = _runner(tmp_path)
    notices: list[tuple[str, str]] = []

    async def _notice(stream_id: str, message: str) -> None:
        notices.append((stream_id, message))

    runner.deps.ui.on_subagent_notice = _notice
    state = {"raised": False}

    def fn(messages, info):
        parts = [p for m in messages for p in getattr(m, "parts", [])]
        returns = [p for p in parts if type(p).__name__ == "ToolReturnPart"]
        if len(returns) < 2:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="blob", args={}, tool_call_id=f"t{len(returns)}")])
        if not state["raised"]:
            state["raised"] = True
            raise _overflow()
        return ModelResponse(parts=[TextPart(content="done")])

    sub = Agent(FunctionModel(fn))

    @sub.tool_plain
    def blob() -> str:
        return "x" * 500

    await runner._run_to_completion(sub, "go", None, None, None, "sid-1")
    assert len(notices) == 1
    stream_id, message = notices[0]
    assert stream_id == "sid-1"
    assert "overflow" in message.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_subagent_retry.py -k overflow -v`
Expected: the first, third, and fourth FAIL (the overflow raises straight through — permanent error). `test_overflow_with_nothing_to_shed_raises` may pass already (it pins today's behavior plus the call count); confirm the call count assertion holds after Step 3.

- [ ] **Step 3: Implement shed-and-resume**

In `src/marim_harness/subagents/runner.py`:

Add to the compaction import block near the top (there is no existing compaction import — add one with the other relative imports):

```python
from ..compaction import mask_stale_observations
```

and extend the errors import:

```python
from ..runtime.errors import is_context_overflow_error, is_transient_model_error
```

Add the helpers next to `_notice_retry`:

```python
    # Shed settings for the overflow backstop: spare only the newest observation
    # (the model may still be acting on it) and mask anything else remotely bulky.
    # Deliberately more aggressive than the proactive masker — by the time we're
    # here the provider has already rejected the request for size.
    _SHED_KEEP_RECENT = 1
    _SHED_MIN_CHARS = 64

    def _shed_context(self, messages: list) -> list | None:
        """The overflow-recovery lever: repair the captured conversation the same
        way a transient resume does, then aggressively mask stale observations.
        Returns the shrunk history to resume from, or None when masking freed
        nothing — the overflow is then unrecoverable here and must surface."""
        repaired = _resumable_history(messages)
        if not repaired:
            return None
        masked, count = mask_stale_observations(
            repaired, self._SHED_KEEP_RECENT, min_chars=self._SHED_MIN_CHARS
        )
        return masked if count else None

    async def _notice_overflow(self, stream_id: str | None) -> None:
        """Surface an overflow recovery on a foreground spawn's card. A no-op for
        a background spawn (no card) or when no UI is listening."""
        cb = self.deps.ui.on_subagent_notice
        if cb is None or not stream_id:
            return
        await cb(stream_id, "context overflow — masked stale tool output, resuming…")
```

In `_run_to_completion`, add `overflow_shed = False` next to `attempt = 0`, and insert the recovery at the TOP of the `except Exception as exc:` block (before the transient check):

```python
            except Exception as exc:  # noqa: BLE001
                # Context overflow is a permanent 4xx, so the transient path below
                # would re-raise it — but unlike a genuine bad request it IS
                # recoverable: shed the bulky old observations from the captured
                # conversation and resume once. Unlike the proactive masker (which
                # rewrites only the outgoing request), the shed is folded into the
                # resume history itself, so the freed tokens stay freed. One shot
                # only: a second overflow means masking already gave all it had.
                if not overflow_shed and is_context_overflow_error(exc):
                    shed = self._shed_context(list(captured))
                    if shed is not None:
                        overflow_shed = True
                        resume_history = shed
                        logger.info(
                            "sub-agent overflowed its context; masked stale "
                            "observations and resuming"
                        )
                        await self._notice_overflow(stream_id)
                        continue
                if attempt >= self._retry_attempts or not is_transient_model_error(exc):
                    raise
```

Also extend the method's docstring with one sentence after the transient-retry paragraph:

```
        A context-overflow rejection (a permanent 4xx the transient path would
        surface) gets one recovery attempt of its own: the captured conversation
        is resumed with stale tool observations masked (see ``_shed_context``);
        a repeat overflow, or one with nothing left to shed, surfaces normally.
```

- [ ] **Step 4: Run the retry suite**

Run: `uv run pytest --no-cov tests/test_subagent_retry.py -v`
Expected: ALL PASS — including every pre-existing transient/permanent test (the overflow branch sits before the transient check and matches only overflow shapes).

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check src tests && uv run pyright`
Expected: clean.

```bash
git add src/marim_harness/subagents/runner.py tests/test_subagent_retry.py
git commit -m "$(cat <<'EOF'
feat(subagents): recover a sub-agent context overflow by shedding and resuming

Mirrors the main turn's compact-and-retry-once: on a provider overflow
rejection the captured conversation is resumed with stale observations
masked (keep newest only). One shot; a repeat overflow surfaces.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0111Y8LPyWyXqC9tv7MSonPt
EOF
)"
```

---

### Task 5: Overflow-specific containment message for foreground spawns

**Files:**
- Modify: `src/marim_harness/subagents/runner.py` (`_execute_foreground_spawn`, the `except Exception` branch ~line 574-582)
- Test: `tests/test_subagent_retry.py`

**Interfaces:**
- Consumes: `is_context_overflow_error` (imported in Task 4); the existing containment contract (foreground errors become strings, background re-raises).
- Produces: an unrecovered overflow returns an actionable report to the orchestrating agent instead of the raw exception rendering.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_subagent_retry.py`:

```python
@pytest.mark.anyio
async def test_foreground_overflow_failure_tells_orchestrator_to_split(tmp_path: Path):
    """When the shed-and-resume backstop is exhausted, the contained foreground
    error must tell the orchestrator what to DO (split/narrow the task) — the
    tool result is model-facing product surface, not a stack trace."""
    runner, _ = _runner(tmp_path)

    async def _boom(*args, **kwargs):
        raise ModelHTTPError(
            400, "m", body={"message": "maximum context length exceeded"}
        )

    runner._run_to_completion = _boom
    out = await runner.run("general", "task", "sid-1")
    assert "overflowed its context window" in out
    assert "split the task" in out.lower()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest --no-cov tests/test_subagent_retry.py::test_foreground_overflow_failure_tells_orchestrator_to_split -v`
Expected: FAIL — output is the generic `"Sub-agent 'general' failed: ModelHTTPError: …"`.

- [ ] **Step 3: Add the tailored message**

In `_execute_foreground_spawn`'s `except Exception as exc:` branch, replace the final `return` line:

```python
            await self.hooks.subagent_stop(type, task, f"error: {exc}")
            if is_context_overflow_error(exc):
                # The shed-and-resume backstop already ran and it still
                # overflowed: tell the orchestrator what to DO, not just what
                # broke — this string is what the model reads and acts on.
                return (
                    f"Sub-agent {type!r} overflowed its context window even after "
                    "masking stale tool output. Split the task into smaller "
                    "spawns, or narrow the scope so this sub-agent reads less."
                )
            return f"Sub-agent {type!r} failed: {exc.__class__.__name__}: {exc}"
```

- [ ] **Step 4: Run the retry suite**

Run: `uv run pytest --no-cov tests/test_subagent_retry.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Full local CI gate, then commit**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: all three clean, full suite green with coverage on.

```bash
git add src/marim_harness/subagents/runner.py tests/test_subagent_retry.py
git commit -m "$(cat <<'EOF'
feat(subagents): actionable report when a spawn's overflow is unrecoverable

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0111Y8LPyWyXqC9tv7MSonPt
EOF
)"
```

---

## Notes for the implementer

- **Don't touch `compaction.py`** — `mask_stale_observations` is reused as-is; if a test seems to demand changing it, the test is wrong.
- **Background spawns** intentionally keep re-raising on unrecovered overflow (the job registry marks the job failed); only the *foreground* containment message changes. The shed-and-resume in Task 4 protects both paths because both go through `_run_to_completion`.
- **The `claude-cli` backend is out of scope** — the CLI runs its own loop and context management; nothing here applies to it.
- Token numbers in tests are chosen with slack against the char/4 estimate; if an assertion is off by a hair, re-derive: `estimate_tokens` ≈ (sum of content chars + args chars) // 4, `MASKED_OBSERVATION` is ~80 chars ≈ 20 tokens, trigger = `int(max_tokens * 0.75)`.
