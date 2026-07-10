# CodeMode Experiment — Design

**Date:** 2026-07-10
**Status:** Approved (pending implementation)
**Scope:** Contained, deletable experiment. Keep-or-delete decided by benchmark.

## Summary

Integrate `pydantic-ai-harness`'s `CodeMode` capability into marim's main agent behind a
`MARIM_CODEMODE=1` flag. CodeMode wraps selected tools behind a single `run_code` tool:
the model writes Python (executed in the Monty sandbox) that calls those tools as
functions, batching N tool round-trips into one model request.

Only **ungated** tools (read/search/LSP/MCP — everything not behind
`requires_approval=True`) are sandboxed, in **all modes**. Gated tools
(`write_file`/`edit_file`/`bash`/forge create/checkout/`web_search`) stay native, so the
existing approval loop is untouched. This split is forced by upstream mechanics, not just
caution: `CodeModeToolset.dispatch_tool_call` converts `ApprovalRequired`/`CallDeferred`
into an error — the sandbox cannot round-trip a deferral, so a gated tool inside
`run_code` would fail even in auto mode.

Main agent only. Sub-agents, `claude-cli` provider, and inline-approval of gated tools
are all out of scope for this iteration.

## Why

- Models orchestrate multi-step reads better in code (loops, `asyncio.gather`,
  local variables) than through turn-by-turn tool calls.
- Wide read/search sweeps currently cost one model round-trip per call and push every
  intermediate result through the context window. CodeMode collapses both.
- Risk is contained: the sandboxed set is read-only, the sandbox itself is fully closed
  (no `mount`, no `os_access` — marim's tool functions run on the host as always; the
  sandbox only orchestrates calls), and the whole experiment is ~30 lines + one optional
  extra to delete.

## Key upstream facts (verified against pydantic-ai-harness `master` @ 2026-07-10; re-verify against the pinned release tag during implementation — latest PyPI release is 0.6.0)

- `CodeMode(tools=<selector>)` accepts a ctx-aware callable `(ctx, tool_def) -> bool`;
  matching tools are sandboxed, the rest stay native tool calls.
- Inner sandbox calls dispatch through a real `ToolManager.handle_call` that inherits the
  agent's `root_capability` → **capability hooks fire per inner call** (live TUI
  rendering is wireable without upstream changes).
- Every nested call/return is attached to `run_code`'s `ToolReturnPart.metadata`
  (`{'code_mode': True, 'tool_calls': …, 'tool_returns': …}`) → persisted with history,
  so resumed sessions re-render inner calls without the live path.
- `run_code` carries `metadata={'code_arg_name': 'code', 'code_arg_language': 'python'}`
  so renderers can treat the argument as code. It registers `sequential=True`.
- Framework tools (`tool_kind` set, `defer_loading`, `unless_native`) stay native
  automatically. Tool names are sanitized to Python identifiers; collisions warn + hide.
- Nested `tool_call_id`s follow `{parent_id}__{n}`.
- Deps: `pydantic-ai-harness` requires `pydantic-ai-slim>=2.1.0` (marim is on 2.8);
  the `code-mode` extra adds `pydantic-monty`.

## Design

### 1. Dependency & flag

- `pyproject.toml`: new optional extra `codemode` pinning
  `pydantic-ai-harness[code-mode]==0.6.0` (exact pin: 0.x minors break; bumps are
  deliberate).
- `HarnessConfig.code_mode: bool = False`; `HarnessBuilder.with_code_mode()` for
  embedders. `bootstrap.build_harness` reads `MARIM_CODEMODE=1` and sets the field —
  env reads stay in bootstrap, wiring in the builder, per the repo convention.
- Flag on + extra missing → fail fast at build time with an actionable message
  (`uv sync --extra codemode`), never silently off.
- `claude-cli` provider: not applicable (marim's tools don't run there); doc note only.

### 2. Capability wiring & tool selection

- In `build_collaborators` (`runtime/harness.py`), when `cfg.code_mode` is set, append
  `CodeMode(tools=_code_mode_selector)` to the existing `capabilities=[…]` list. Lazy
  import inside the branch so default installs never import the package.
- Selector: `True` only for tools **not** in the gated name-sets maintained in
  `tools/names.py` — consulted, not copied, so it cannot drift. If a gated name is
  missing from `names.py`, extend `names.py` (the designated leaf module), never inline
  names in the selector.
- `dynamic_catalog=False` (default): marim's ungated toolset is fixed at build time (no
  ToolSearch), so the static `run_code` description is the cache-friendlier choice.
- No `mount`, no `os_access`. MCP tools sandbox too (they're ungated); tools without a
  return schema render `-> Any` (upstream warns once) — accepted for an experiment.
- Sub-agents untouched: `SubagentRunner.build` composes its own agents and never sees
  this capability.

### 3. TUI rendering

- **Code card:** extend the tool-call card renderer in
  `interfaces/tui/stream_render.py` to check `code_arg_name` tool metadata and render
  that argument syntax-highlighted (Rich `Syntax`) instead of as JSON. Generic — covers
  any future code-carrying tool.
- **Live inner calls:** a marim-side capability (`CodeModeObserver`, in `runtime/`)
  implements `before_tool_execute`/`after_tool_execute` and pushes nested call/return
  info to the TUI through a **new optional callback on `Deps`** (None in headless; bound
  via `Harness.bind_ui`, never field-poked).
  - Nested-call detection: hooks also fire for top-level calls (already rendered from
    the stream), so the observer renders only calls whose `tool_call_id` matches the
    upstream nested scheme (`{parent_id}__{n}`). Heuristic keyed to the pinned version;
    guarded by a unit test against the real toolset so an upstream change fails loudly.
  - Presentation: compact lines appended inside the `run_code` card (name, short args,
    ✓/✗/denied) — a flat mini-transcript.
- **Persistence/resume:** transcript rendering of past turns uses
  `ToolReturnPart.metadata` (persisted with history); live rendering is display-only
  sugar, nothing load-bearing.
- Interrupts unchanged: `run_code` is one `ToolCallPart` + one `ToolReturnPart`, so
  `_flush_resumable` / `_repair_unanswered_tool_calls` treat it like any tool.

### 4. Benchmark, testing, error handling

**Benchmark** (`scripts/bench_codemode.py`, deleted with the experiment):

- 4 fixed read-heavy prompts against this repo (summarize `runtime/` package; find all
  callers of `resolve_approvals`; list every tool registered on the main agent and
  where; which tests cover session resumability).
- Headless one-shot runs, flag off vs on, N=3 each, `MARIM_PROVIDER=local`
  (LM Studio model — no paid models without explicit approval).
- Per run: model requests, input/output tokens, wall-clock (from `RunUsage` / session
  record), and `run_code` retry count (Monty syntax/type errors surface as `ModelRetry`)
  — small-local-model code quality is a first-class failure mode to quantify.
- Output: markdown table. **Keep rule: ≥2× fewer model requests on average with no
  judged quality loss; otherwise delete the experiment.**

**Testing** (CI runs the default sync):

- Add the `codemode` extra to the dev dependency group so CI exercises it; every
  codemode test starts with `pytest.importorskip("pydantic_monty")`.
- Unit tests:
  1. selector sandboxes ungated names and excludes every gated name — asserted against
     the `names.py` sets, not a copy;
  2. flag off → no `run_code` on the agent; flag on → present, gated tools still native;
  3. builder knob + bootstrap env read;
  4. nested `tool_call_id` pattern against the real upstream toolset (version-pin
     tripwire);
  5. `stream_render` renders a `code_arg_name` tool call as syntax-highlighted code.

**Error handling:**

- Build-time fail-fast on missing extra (see §1).
- `run_code` retries: upstream default `max_retries=3` unchanged.
- Denied/deferred inside the sandbox is impossible by construction (gated tools are
  never sandboxed); the selector test guards the invariant.
- `.env.example`: `MARIM_CODEMODE=` with a one-line comment. CLAUDE.md: one sentence in
  the env-knobs paragraph.

## Out of scope (explicitly)

- Sub-agent CodeMode (second iteration, only if main-agent numbers justify it).
- Inline-approving gated tools inside the sandbox (`HandleDeferredToolCalls`).
- `dynamic_catalog`, `mount`, `os_access`.
- Any change to the approval loop, session store, or checkpoint machinery.

## Deletion plan

Remove: the `codemode` extra, `HarnessConfig.code_mode` + builder knob + bootstrap read,
the capability append + selector, `CodeModeObserver` + the `Deps` callback, the
`stream_render` code-card branch (optional keep — it's generic), the tests, the bench
script, `.env.example`/CLAUDE.md lines.
