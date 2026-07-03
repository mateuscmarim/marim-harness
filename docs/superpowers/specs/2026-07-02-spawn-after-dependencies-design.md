# spawn_agent `after=` dependencies — design

**Date:** 2026-07-02
**Status:** approved design, pre-implementation

## What this is

Harness-enforced ordering between detached sub-agent spawns. `spawn_agent`
gains `after: list[str] | str | None` naming background jobs that must settle
before this spawn starts. The dependent job waits, fails fast if a
prerequisite failed, and otherwise starts its sub-agent with the
prerequisites' reports injected into its prompt.

Motivating failure (observed in a scraper-gen plugin dogfood run): an
orchestrating model spawned a merge-script generator in parallel with the
generators producing its inputs. Prose-level "spawn in waves" instructions
are advisory; this makes the ordering mechanical.

## Decisions (with reasons)

- **Skip + fail loudly on prerequisite failure.** A dependent whose
  prerequisite failed/cancelled never starts its sub-agent; it settles
  `failed` with a clear message. No tokens burned on doomed work; the
  orchestrator sees the failure in the next-turn digest and can re-plan.
- **Inject prerequisite reports into the dependent's prompt.** Appended under
  a "Results of prerequisite jobs" heading. This is what makes `after=` more
  useful than prose ordering — no file-based side channel required. Injection
  never truncates: report size is controlled by `max_output_chars` on the
  *dependency* spawns (auto-detached spawns already default to the detach
  budget).
- **Background-only.** `after` requires the spawn to end up detached
  (explicit `background=True` or auto-detach). Foreground + `after` is just
  `wait_for_job` then spawn, and background spawning is already
  main-agent-only — keeping the whole dependency graph in one place (the
  session's `JobRegistry`).
- **Approach A — wrapper coroutine at the spawn-tool layer** (over
  first-class registry dependencies with a new `waiting` status). Reuses the
  registry exactly as designed ("an awaitable that yields final text" — ours
  waits first). No status-enum changes, no TUI changes; can graduate to
  first-class later if panel UX warrants it.

## Tool semantics & validation

`spawn_agent(after=...)` — coerced like `mcp` (string or list of job ids).

- Every id must name an **existing** job at call time. Unknown ids → the tool
  returns an error naming them and registers nothing. Ids can only reference
  already-registered jobs, so the graph is **acyclic by construction** — no
  cycle detection needed.
- If the spawn would run foreground (`background=False`, or unset outside
  detached-fanout mode, or any spawn at depth > 0), the tool refuses with a
  message telling the model to drop `after` and use `wait_for_job`, or spawn
  detached.
- Dependencies may be any job kind (agent or bash); a bash dependency's
  output buffer injects the same way.
- Returns the usual immediate handoff (`Started job-N …` / detach-handoff);
  the job shows as waiting until deps settle.
- The `spawn_agent` docstring (model-facing product surface) documents
  `after` with both usage notes: reports are injected, and their size is set
  via `max_output_chars` on the dependency spawns.

## The wrapper coroutine

When `after` is set, the registered coroutine is a wrapper:

1. **Wait** — `JobRegistry.await_settled(ids)` (new): like `wait()` but no
   timeout, takes a list, shield-awaits each dependency's task, returns the
   settled `Job` objects. Shielding: cancelling the waiter must not cancel
   the job being waited on.
2. **Check** — any dependency settled `failed`/`cancelled` → raise
   `PrerequisiteFailed("prerequisite job-2 failed — <one-line tail of its
   result>")`. The registry's done-callback formats exceptions as `failed`
   with `"{type}: {message}"`, so the digest reads honestly and the sub-agent
   is never built.
3. **Compose** — append to the already-composed task prompt:

       ## Results of prerequisite jobs
       ### job-1 — <label>
       <full result>
       ### job-3 — <label>
       <full result>

4. **Run** — only now call `ctx.deps.services.run_background_agent(...)` with
   the augmented task and await it. The inner coroutine is created lazily so
   a cancel-before-start never leaks an unawaited coroutine (same concern
   `jobs.register`'s docstring guards).

**Waiting visibility, no new status:** the wrapper closes over a mutable flag
and registers an `output_fn` returning `(waiting on job-1, job-3)` until step
4 begins, then `(still running)`. Jobs panel and `job_output` show it with
zero TUI changes.

## Failure, cancellation, wake

- **Cancel the dependent while waiting** → task cancelled, settles
  `cancelled`; dependencies untouched (the shield's guarantee).
- **Cancel a dependency** → every dependent fails fast with
  `PrerequisiteFailed`; a downstream chain collapses loudly, never hangs.
- **Shutdown `cancel_all`** iterates every running job, wrappers included;
  chains die cleanly in any order (`_settle` is idempotent, so the
  cancelled-directly vs failed-fast race is harmless).
- **Wake & digest:** `await_settled` marks dependencies wake-consumed exactly
  like `wait()` — the chain is their consumer, so intermediate completions
  don't fire redundant autonomous turns. Digest entries are preserved (full
  chain history still visible next turn). The chain's terminal job is
  consumed by nobody and wakes a turn normally — the "synthesize when the
  pipeline finishes" behavior.
- **Edge cases that just work:** `after=` on an already-finished dependency
  proceeds immediately (results persist in the registry); `/clear` keeps
  running jobs, and the wrapper holds direct `Job` references, so a mid-chain
  `/clear` can't yank results from a waiter.

## Files touched

- `src/marim_harness/jobs.py` — `await_settled`, `PrerequisiteFailed`.
- `src/marim_harness/tools/provider.py` — `after` parameter, validation,
  wrapper, docstring.
- Tests (below). No TUI, session, or runner changes.

## Testing

Registry-level (alongside existing `JobRegistry` tests):
- `await_settled` returns settled jobs in the order the ids were given and
  marks them wake-consumed (digest preserved; `has_finished_pending()` false).
- Resolves immediately for already-terminal jobs.
- Cancelling the waiter leaves the awaited job running.

Tool-level (new `tests/test_subagent_after.py`, stubbing
`run_background_agent` in the style of the existing subagent tool tests):
1. B spawned `after=job-A` doesn't start until A resolves; A's report appears
   in B's task under "Results of prerequisite jobs".
2. Multiple deps: both reports injected, in order.
3. Failed dep → dependent settles `failed` with `PrerequisiteFailed`; inner
   runner never called.
4. Cancelled dep → same fail-fast; cancelling the dependent while waiting →
   `cancelled`, dep unaffected.
5. Validation: unknown id → error return, nothing registered;
   forced-foreground + `after` → refusal; depth > 0 + `after` → refusal.
6. Chain A→B→C runs strictly in order; C receives B's (not A's) report.
7. `output_fn` shows `(waiting on job-…)` before start, `(still running)`
   after.
8. Bash job as dependency: its output buffer injects like an agent report.

## Follow-up folded into the same implementation plan

Update the `examples/scraper-gen` plugin prompts to use the feature and close
the previously-identified gaps, as one final plan task:
- SKILL.md Step 3: replace prose wave-ordering with `after=` chains driven by
  a new optional `depends_on: [<task-name>, ...]` plan field; add the
  same-host politeness cap (2–3 concurrent generators per host, sequential on
  429s; different hosts and no-network tasks don't count).
- planner.md: `depends_on` field + `strategy: derive` (pure post-processing:
  inputs are dependency tasks' `samples/*.jsonl`, no network); sharpen the
  Step 0 browser-server detection heuristic (prefer a general-purpose server
  with `browser_navigate`/`browser_snapshot` over the `playwright_test`
  run-test server).
- generator.md: `depends_on`/`derive` join the task-block fields; a derive
  script reads dependency sample files, never re-fetches, and exits 2 loudly
  if an input is missing/empty.
- healer.md: run scripts in dependency order (dependencies before
  dependents).
- Zero-risk polish from the last review: bare `specs/plan.md` read paths in
  healer.md step 1 and SKILL.md Step 2; Step 5's "Step 2 snapshot" label.
