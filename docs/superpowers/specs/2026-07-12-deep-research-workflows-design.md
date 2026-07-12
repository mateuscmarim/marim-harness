# Deep-Research Workflows — Design

**Date:** 2026-07-12
**Branch:** `feat/deep-research-workflows` (off master `9cccd5a`)
**Status:** approved design, pending implementation plan

## Context

Dynamic workflows (the gated `run_workflow` tool, `workflows/engine.py`) shipped in
PR #58 and were polished in PR #64. Deep research is their driving use case: a
multi-researcher fan-out with schema-validated collection, adversarial verification,
and bounded retry loops is exactly the deterministic control flow `spawn_agent`
alone can't express.

Two builtins already exist on master (landed 2026-06-27/07-04):

- `src/marim_harness/builtin/agents/researcher.md` — a read-only research worker
  (`web_search`, `fetch_url`, workspace reads) with source-discipline rules and a
  CLAIM/source/type/quality findings format.
- `src/marim_harness/builtin/skills/deep-research/SKILL.md` — the orchestration
  skill: scope → decompose → fan out researchers → adversarial verify → synthesize.
  It orchestrates via plain `spawn_agent` calls.

Two things block using workflows for real research runs:

1. `_MAX_VM_DURATION_SECS = 300.0` (`workflows/engine.py`) hard-caps every workflow
   script at 5 minutes wall-clock. Monty's duration cap counts real elapsed time
   *including* host awaits, so a 4–6 researcher fan-out where each researcher takes
   2–5 minutes dies at the first control point past 300s.
2. `HarnessConfig.workflow_timeout_secs` (`runtime/harness.py`, default 1800) is
   wired to nothing — no env var, and `run_workflow` has no timeout parameter.

## Scope

**In this batch:**

1. **Duration knob** — model-requested per-call timeout on `run_workflow`, clamped
   to a config ceiling that is finally wired (env + builder).
2. **Deep-research skill upgrade** — rewrite `SKILL.md` workflow-first (a
   `run_workflow` reference script with schema-validated collection, a bounded
   coverage loop, and scripted adversarial verify), keeping a `spawn_agent`
   fallback section for installs without the `[workflows]` extra.

**Out of scope (next batch):** resumability journal, detachable/background
workflows, saved named workflows. Also carried forward from PR #64's review:
`resume_spawn` rebuilds without `output_schema` — fold into the resumability work.

## Decisions (from design review)

- Batch scope: knob + skill upgrade; resumability/detach deferred.
- Duration control: model-requested per call, clamped by a config-side ceiling.
- Skill shape: workflow-first with a `spawn_agent` fallback section (deep-research
  is a builtin shipped to everyone; workflows are an optional extra).
- Validation: full unit coverage plus one live smoke on the free local LM Studio
  model; no paid-model runs without explicit approval.

## Design

### 1. Duration knob

**Tool surface.** `run_workflow(ctx, script, args=None)` gains
`timeout_secs: float | None = None`. The docstring tells the model to size it to
the work — "a multi-researcher run may need 1800; omit it for ordinary sweeps" —
and states that values are clamped to a harness ceiling. When omitted, the
effective timeout is **300s**, so current behavior for short workflows is
unchanged.

**Clamping.** The engine computes `effective = min(requested or 300, ceiling)`.

- The ceiling is `WorkflowEngine.__init__`'s existing `timeout_secs`
  (`HarnessConfig.workflow_timeout_secs`, default 1800) — reinterpreted from
  "the timeout" to "the maximum a call may request".
- `bootstrap` reads a new `MARIM_WORKFLOW_TIMEOUT` env var (seconds, float) into
  `workflow_timeout_secs`; garbage or missing values fall back to 1800. Documented
  in `.env.example` next to `MARIM_WORKFLOWS`.
- The builder path keeps its explicit `workflow_timeout_secs` knob for embedders
  (no env reads in the builder, per the builder/bootstrap split).
- `timeout_secs <= 0` or non-finite is rejected with a tool-level error message
  (the model can correct and retry); the VM never starts.

**The VM cap.** `_MAX_VM_DURATION_SECS` is removed as a separate constant. Both
Monty's `max_duration_secs` resource limit and the outer `asyncio.wait` use
`effective`. The runaway-compute-loop guard becomes the effective timeout itself,
bounded by the ceiling. Accepted trade-off, documented in the engine comment: a
non-yielding spin loop can now burn up to the requested duration instead of 5
minutes; the user can abort, and the ceiling bounds the damage. The default-300s
behavior preserves fast failure for workflows that never asked for more.

**Plumbing.** One new keyword threads through the existing seam:
`services.run_workflow(script, args, tool_call_id, timeout_secs=None)` →
`WorkflowEngine.run(script, args, tool_call_id, timeout_secs=None)`. The tool
layer stays thin `ctx.deps` unwrapping. The timeout message reports the effective
value ("Workflow timed out after 1800s"), so a clamped request is visible.

### 2. Deep-research skill upgrade

Rewrite `builtin/skills/deep-research/SKILL.md`. The scope-then-decompose step
(inline scoping pass, 1–3 clarifying questions, 3–6 independent sub-questions)
survives nearly as-is — it is orchestration-agnostic.

**Primary path — one `run_workflow` script.** The skill instructs the model to
author a script (adapting a reference script shown in the skill) implementing:

1. **Fan out.** `asyncio.gather` of one
   `agent(type="researcher", schema=FINDINGS, ...)` call per sub-question.
   `FINDINGS` is a JSON Schema mirroring the researcher's existing
   CLAIM/source/type/quality report format (plus a `load_bearing` boolean and
   `open_questions` list), so collection is parsed data rather than prose.
   Each call is wrapped in `try/except` so one failed researcher degrades to a
   logged gap instead of killing the run.
2. **Coverage loop (bounded).** Sub-questions whose researcher returned no
   load-bearing findings (or failed) get exactly **one** follow-up researcher
   with a sharpened task. Hard cap of one extra round — research fan-outs must
   converge, not wander.
3. **Adversarial verify.** `asyncio.gather` of one
   `agent(type="explore", schema=VERDICT, ...)` refuter per load-bearing claim,
   tasked to refute it and confirm the cited source supports it. Verdicts
   downgrade or drop claims in the returned data.
4. **Return data, not prose.** The script's last expression is a dict:
   `{"findings": [...], "dropped": [...], "open_questions": [...]}`. Synthesis
   stays in the main turn — the model writes the cited report from the returned
   bundle. Scripts move data; the model writes prose.

The skill tells the model to pass `timeout_secs` sized to the fan-out (e.g. 1800
for 4–6 researchers) — the knob's first real consumer.

**Fallback path.** One short closing section: when `run_workflow` is unavailable,
orchestrate with `spawn_agent` directly — a condensed version of the current
fan-out/verify instructions. Same pipeline, weaker guarantees (no schema-validated
collection, no scripted loops).

**`researcher.md` is unchanged.** Schema enforcement happens at spawn level
(`output_schema` → StructuredDict native, prompt-contract fallback for
claude-cli), so the agent prompt needs no JSON instructions.

### 3. Error handling

- Invalid `timeout_secs` → tool-level error, correctable by the model.
- Clamped requests are visible in the timeout message.
- Per-researcher failures inside the script are caught; partial results survive.
- Schema-validation failures already retry once at the engine level (existing).
- The effective timeout flows into the existing `asyncio.wait` + drain machinery;
  the abort path is untouched. The never-cancel-the-VM invariant
  (engine.py module docstring) is unaffected.

## Testing

Unit tests only use TestModel/FunctionModel/fakes (standing constraint: no paid
models without explicit approval).

- **Tool → seam threading:** `run_workflow(timeout_secs=...)` reaches the engine.
- **Clamping matrix:** omitted → 300; requested < ceiling → requested;
  requested > ceiling → ceiling; ≤ 0 / non-finite → tool error, VM never starts.
- **Limit wiring:** Monty receives `max_duration_secs == effective` and the outer
  wait uses the same value (fakes/introspection with sub-second ceilings — no real
  long waits in tests).
- **Config wiring:** bootstrap reads `MARIM_WORKFLOW_TIMEOUT` (valid, missing,
  garbage → default 1800); builder knob still wins for embedders.
- **Timeout message** reports the effective (clamped) value.
- **Skill content checks:** the builtin skill still parses through discovery, and
  the reference script embedded in `SKILL.md` is extracted and compiled as Monty
  in a test — skill text with a broken script is a shipped bug.

**Live smoke (manual, end of batch):** one deep-research run on the free local
LM Studio model in tmux, with a question sized so researchers take a few minutes
each — proving a >5-minute workflow survives end-to-end (no 300s death) and the
workflow card and `log()` lines render.

## Batch mechanics

- Branch: `feat/deep-research-workflows` off master `9cccd5a`.
- Execution: subagent-driven development against the implementation plan.
- CI order before claiming done: ruff → pyright → pytest (3.10/3.12/3.14 matrix).
