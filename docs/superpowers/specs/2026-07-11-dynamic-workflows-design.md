# Dynamic Workflows — Design

**Date:** 2026-07-11
**Status:** Approved (brainstorm complete, pending implementation plan)

## Problem

Marim's spawn system (`spawn_agent` + detached jobs + `after=` chaining) is a
model-driven orchestrator: every fan-out step round-trips through the main
model, job ids must be transcribed correctly turn after turn, and dynamic
control flow (loops until convergence, data-dependent fan-out width) is
impractical. Orchestrating a large sweep also floods the main context with
job handles and reports.

A *dynamic workflow* is a model-authored orchestration **script**: the model
writes Python that spawns sub-agents with loops, conditionals, and
`asyncio.gather`, executed deterministically in one tool call. Intermediate
results live in script variables; only the final value returns to the main
context.

## Decision summary

| Decision | Choice |
| --- | --- |
| Build path | Bespoke `run_workflow` tool executed by [pydantic-monty](https://github.com/pydantic/monty) (not pydantic-ai-harness CodeMode, not pydantic-graph) |
| MVP scope | `agent()` with `schema=`, `asyncio.gather`, `log()`, `args` — no phases/pipeline/budget/resume/named workflows |
| Approval | Gated (`requires_approval=True`) like `bash`; ask mode shows the full script |
| Acceptance | Parallel review sweep: N schema'd `explore` spawns fanned out, findings merged |

Why Monty: deny-by-default Rust interpreter purpose-built for LLM-authored
code — no `open`/`import`/`eval`, host controls the only reachable functions,
`ResourceLimits` for runaway scripts, and VM snapshot (`dump`/`load`) leaves a
future resume path. Spike verified on Python 3.10: async external functions
work and `asyncio.gather` genuinely runs `agent()` calls concurrently.

Why not CodeMode: it wraps the agent's *entire toolset* into a `run_code`
capability — invasive to marim's `DeferredToolRequests` approval loop — and
lives in a separate experimental package. Why not pydantic-graph: it is
developer-authored graphs compiled at dev time; it does not address
model-authored scripts.

## Architecture

Three pieces, following the repo's pure / effectful / thin-tool split:

- **`workflows/engine.py`** (effectful core) — `WorkflowEngine`: compiles the
  script with `Monty(code)`, runs `run_async` with the host-function table and
  `ResourceLimits`, shapes the last-expression value into the tool result.
  Sub-agents are spawned through an **injected async callable** (the same
  `Deps.services` seam `spawn_agent` uses) — the engine never imports
  `SubagentRunner`, so tests use a fake.
- **`workflows/schema.py`** (pure) — JSON-schema → Pydantic model construction
  for `agent(schema=...)`; result JSON serialization and size-capping. Unit
  tested directly, no I/O.
- **`tools/workflow_tools.py`** (thin tool layer) — `run_workflow(ctx, script,
  args=None)`; unwraps `ctx.deps` and delegates. Registered on the **main
  agent only** (like spawn tools), gated `requires_approval=True`. New
  `"workflow"` entry in `TOOL_GROUPS` (`names.py`).

Wiring:

- `pydantic-monty` is an **optional extra** `marim-harness[workflows]`. The
  tool attaches only when the package imports (forge-style
  attach-when-available), behind `MARIM_WORKFLOWS` (default on). Env read in
  `bootstrap`, composition in `HarnessBuilder`.
- Workflow spawns run at `subagent_depth + 1`: the existing
  `SUBAGENT_MAX_DEPTH` ceiling and the runner's concurrency semaphore apply
  unchanged.
- **TUI cards need one new seam** *(amended: "spawns render as today's cards
  with no TUI work" was wrong)*. Cards are created only when a literal
  `spawn_agent` tool call renders (`stream_render.py` gates on the tool name);
  events for synthesized stream ids are silently dropped. MVP adds an optional
  `UIHooks.on_workflow_spawn(stream_id, type, task, parent_tool_call_id)`
  callback: the engine fires it before launching each child so the TUI claims
  a card for that stream id (headless leaves it `None` and loses nothing).
  Child stream ids are synthesized as `"<run_workflow tool_call_id>::wf<n>"`.
- API note from the spike: `run_monty_async` is deprecated; use
  `Monty.run_async(external_functions={...})`.

## Model-facing script API

The script is plain Python in Monty's subset (no classes, no `match`, only a
small stdlib subset — `asyncio`, `json`, `re`, `datetime` are the useful ones). The **last expression is the
tool result**, JSON-serialized and size-capped.

```python
import asyncio

async def review(dim):
    return await agent(
        "Review the diff for " + dim + " issues",
        type="explore",
        schema={"type": "object", "properties": {"findings": {"type": "array"}}},
    )

results = await asyncio.gather(*[review(d) for d in ["bugs", "perf", "style"]])
log(str(len(results)) + " dimensions reviewed")
{"findings": [f for r in results for f in r["findings"]]}
```

- `await agent(task, *, type="general", model=None, schema=None,
  max_output_chars=None, isolation=None)` — mirrors `spawn_agent`'s vocabulary
  (same `type`/`model`/`isolation` semantics). No `background`/`after`: the
  script is the scheduler. With `schema`, the output contract is appended to
  the sub-agent's task (respond with only a JSON object matching the schema),
  and the engine parses + validates the report against the schema, re-spawning
  once with the validation errors on failure; `agent()` returns a dict.
  Without `schema`, the freeform report string. *(Amended from "built with a
  Pydantic `output_type`": the runner's report pipeline — streaming,
  transcripts, spill-capping — is string-typed end to end, and threading a
  structured output through it is disproportionate for MVP. Validation lives
  in the engine, above the runner seam.)*
- `log(msg)` — progress line via the existing optional UI callback (headless:
  DEBUG log).
- `args` — the tool call's optional `args` value, injected via Monty `inputs`.
- Parallelism is plain `asyncio.gather`; no bespoke `parallel()` primitive.
- The `run_workflow` docstring is the model-facing documentation of all of
  the above (docstrings are the product).

## Error handling

- **Parse failure** (`MontySyntaxError`) → tool error carrying the syntax
  message; the model fixes the script and retries.
- **`agent()` failure** (spawn error, budget exhaustion, cancelled child) →
  raised *into the script*, so `try/except` lets a workflow degrade
  gracefully. Uncaught → the tool call fails with the Monty traceback, which
  names the script line.
- **Schema validation** → handled below `agent()` by pydantic-ai `output_type`
  retries; the script sees a validated dict or an exception.
- **Result too big / not JSON-serializable** → serializer caps size with the
  `cap_subagent_output` lossless-spill pattern (head + pointer to file);
  non-serializable last expression is a tool error telling the model to
  return plain data.

## Limits and cancellation

- `ResourceLimits` on the Monty run: an infinite loop in the script dies
  deterministically (`max_duration_secs` counts VM compute only — spike
  verified it does not tick while awaiting host functions — so it caps
  runaway compute without limiting long workflows; `max_memory` /
  `max_allocations` bound the heap).
- One overall wall-clock timeout via `asyncio` (knob on `HarnessConfig`,
  generous default).
- **Ctrl-C / turn abort** *(amended: the spike ran early and the "fallback"
  is now the primary design)*: cancelling the `run_async` task directly
  crashes Monty v0.0.18 (GIL fatal error), so the engine NEVER cancels the VM
  task. Instead, the tool wrapper intercepts `CancelledError`, sets an abort
  event, and every in-flight `agent()` host call cancels its child spawn and
  raises `WorkflowCancelled` into the VM — host exceptions are catchable
  in-script (spike verified), and an uncaught one ends the script promptly.
  The engine drains the VM task with a short deadline, then abandons it
  (it holds no external resources once host calls have raised), and re-raises
  `CancelledError` so the session's `_flush_resumable` invariants hold.
- Spend: each `agent()` uses the normal spawn path, so per-spawn token
  accounting and cards work unchanged; no separate ledger.

## Testing

- **Pure** (`workflows/schema.py`): schema→model construction, capping, error
  shapes — direct unit tests.
- **Engine**: real Monty + fake runner callable — gather fan-out, exception
  propagation into scripts, sandbox denial (`open`, `import socket`),
  `ResourceLimits` kill, `args` injection, result shaping.
- **Tool layer**: registration/gating assertions; `TestModel`/`FunctionModel`
  end-to-end through the approval loop.
- **Acceptance**: the parallel review sweep against `FunctionModel`. Live
  smoke is manual against local LM Studio only (no paid models without
  approval).
- **Cancellation** integration test per the spike above.

## Out of scope (future work, in likely order)

1. `phase()` grouping in the sub-agents screen; `pipeline()` helper.
2. Snapshot resume: Monty `dump()` at `agent()` boundaries into the spawn
   sidecar (mirrors `claude-cli` session-id checkpointing).
3. Budget object backed by per-spawn spend tracking.
4. Saved named workflows (`.marim/workflows/`).

## Risks

- **Monty is experimental** (v0.0.18, May 2026): subset gaps or behavior
  changes may bite. Mitigations: optional extra, pinned version, engine seam
  keeps Monty behind one module.
- **Cancellation propagation** unverified — dedicated spike, fallback defined.
- **Model ergonomics**: models may write CPython idioms Monty rejects
  (classes, `match`). The docstring must state the subset explicitly; parse
  errors round-trip cheaply.
