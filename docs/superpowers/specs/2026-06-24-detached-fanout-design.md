# Detached Fan-Out: Non-Blocking Parallel Sub-Agents — Design

**Date:** 2026-06-24
**Status:** Approved (pending spec review)

## Context

When the agent fans out several sub-agents in one turn (e.g. "review the whole
codebase" → 6 domain reviewers), every spawn runs **foreground**: each is a tool
call inside the orchestrator's turn, and the turn does not return until all of
them complete. On a slow or rate-limited route a fan-out can take minutes, during
which the session is frozen at `working… Nm`.

Two recent fixes (committed `151c82b`) already reduce the *cost* of that:

- **Concurrency cap** (`MARIM_SUBAGENT_CONCURRENCY`, `subagents.py:_slot`) bounds
  how many spawns hit the provider at once, so a wide fan-out queues instead of
  tripping a shared route's upstream rate limit (the actual cause of the
  multi-minute hangs observed on `xiaomi/mimo-v2.5`).
- **Resume-on-retry** (`subagents.py:_run_to_completion`) makes a transient 429
  cheap — the run resumes from its captured conversation instead of restarting.

This project addresses the remaining **UX** problem: the frozen session. It does
**not** build a background engine — the harness already has one. It routes
fan-outs through that engine by default and adds a synthesis-gating fix.

What exists today and is reused unchanged:

- **Background spawn:** `spawn_agent(..., background=True)`
  (`tools/provider.py:373–383`) registers a job via `jobs.register("agent", …)`
  (`jobs.py:92`) and returns `Started <id> (agent) — …` immediately.
- **Detached run:** `run_background_agent` → `SubagentRunner.run_background`
  (`subagents.py`), which already honors the concurrency cap and resume-retry.
- **Poll tools:** `jobs()`, `job_output(id)`, `wait_for_job(id, timeout=60)`
  (`provider.py:473–510`).
- **Notify + wake:** `take_finished_digest()` (`jobs.py:232`) is injected into the
  next prompt by `_assemble_prompt('')` (`agent.py:629`); autonomous wake fires an
  empty-prompt turn when idle with finished jobs pending (`interfaces/tui/app.py:
  _maybe_wake` → `WakeController.should_wake`, bounded by `wake_depth_cap`).

## Goal

A multi-spawn fan-out returns control to the user immediately, and its combined
results are synthesized in a follow-up turn — robustly, without depending on the
model to set a flag or remember to poll. The agent is *told* the spawns are
detached so it can choose to wait inline when that's better.

Non-goals: cross-restart job persistence; per-batch (vs. global) synthesis gating
(deferred — see Alternatives).

## Design

### 1. Trigger & config

New knob `MARIM_DETACH_FANOUT` (bool), **default on** (opt-out: `=0` restores
always-inline). Threaded `ModelConfig → HarnessConfig → Deps`, mirroring
`MARIM_SUBAGENT_CONCURRENCY`.

When **on**, **every** `spawn_agent` runs detached (background job + handoff
note) — the harness does not try to distinguish a fan-out from a single spawn. An
explicit `background=False` from the model still forces inline.

Why detach all, not just fan-outs of ≥2: a tool cannot see its sibling tool calls
in pydantic-ai 1.107 (`RunContext` exposes no message history; the parallel-call
list lives in the agent graph's internal scope), so a harness-side "≥2" test isn't
cleanly available. More importantly, it isn't needed: the informed handoff (§2)
delegates the wait-vs-end decision to the agent, which already knows both things a
threshold would proxy for — how many it just spawned and whether it needs a result
inline. A lone dependent spawn simply costs one `wait_for_job` round-trip; a
fan-out ends the turn. The harness detaches uniformly; the agent decides.

### 2. Launching turn — informed handoff

Each detached spawn registers a background job (existing `run_background_agent`
path → inherits cap + resume). Each `spawn_agent` call returns its own
**informational handoff** (the model sees one per call — N of them for an N-way
fan-out):

> Started detached sub-agent `<id>`, running in the background
> (concurrency-capped). End your turn to let it run — I'll deliver its report when
> it finishes and you act on it then — or `wait_for_job("<id>")` if you need the
> result in this turn. For a fan-out, ending the turn is usually better.

The agent then **chooses**: end the turn (control returns immediately — the UX
win, with wake as the synthesis path) or `wait_for_job` (inline; its 60s timeout
is a natural escape — a model that waits on a long batch is bounced with "still
running" and can then end the turn). The detach itself is harness-enforced; only
the wait-vs-end decision is the agent's, and wake guarantees synthesis either way.
For a single detached spawn the agent typically just `wait_for_job`s — one extra
round-trip vs. the old inline path, the cost of detaching uniformly.

### 3. Synthesis — wake turn

Reuses autonomous wake. The gap: `take_finished_digest()` carries only each job's
output **tail**, so a naive wake would make the model fire N `job_output` calls to
gather reports. For detached-fan-out jobs the wake turn instead **inlines the full
reports** (bounded by the existing `max_output_chars` spill-to-file backstop in
`run_background_agent`), framed as: *"Your detached sub-agents finished. Here are
their reports; produce the combined result the user asked for."* Session history
supplies the original request for context.

Two model-independent reasons inlining is preferred over pull-based collection
(not model capability — this holds regardless of how strong the model is):

- **Request economy.** Pull-based fires N sequential `job_output` requests in the
  synthesis turn; inlining adds zero extra provider requests. Since this whole
  feature exists because many requests trip a shared route's upstream rate limit,
  re-adding N requests at synthesis time partly re-creates the problem.
- **Completeness.** With the reports present, a synthesis cannot silently omit one
  because the model enumerated job ids wrong or dropped a `job_output` call —
  inlining removes the failure mode rather than betting the model avoids it.

Inlining costs nothing here: a synthesis needs all reports anyway (so pull-based
saves no context — the same N land in context either way), size is already bounded
by the spill cap, and `job_output` / `wait_for_job` remain available for the agent
to pull ad-hoc. Inlining is only the *default delivery* for the synthesis wake.

### 4. The premature-synthesis fix

Autonomous wake currently fires when **any** job finishes
(`WakeController.should_wake` via `jobs.has_finished_pending`). For a 6-way
fan-out that triggers synthesis after the *first* report — 6 premature turns,
burning `wake_depth_cap` and tokens.

Fix: gate the synthesis wake on **"no background jobs still running"** — synthesize
only when the in-flight set is terminal. Simpler and more robust than tracking
explicit batch ids (deferred as YAGNI), at the cost of also waiting on any
unrelated long-running job, which is rare and arguably correct (don't synthesize
mid-work). Implemented as an added predicate on the wake check; the per-job
finished digest is unchanged for the non-fan-out background path.

## Error handling

- A **failed** detached sub-agent (e.g. retries exhausted on a 429) settles
  `failed` and appears in the synthesis input as such; the model synthesizes from
  the successes and flags the gaps. It may re-spawn failed ones.
- A launching turn **cancelled** after dispatch is fine: jobs run on and wake
  later; the turn's history ends cleanly on the handoff note (resumable — no
  dangling tool call).
- `wake_depth_cap` continues to bound runaway wake chains (e.g. a synthesis turn
  that itself fans out).

## Interactions

Routing through the existing `run_background_agent` means detached fan-out
**composes automatically** with the concurrency cap (bounds the batch's request
load — the rate-limit protection) and resume-retry (cheap 429 recovery). No new
interaction code.

## Config wiring

`MARIM_DETACH_FANOUT` default-on: `_bool_env("MARIM_DETACH_FANOUT", True)` in
`config/model.py:load_config` → `ModelConfig.detach_fanout` → `bootstrap` →
`HarnessConfig.detach_fanout` → `Deps.detach_fanout` (read by `spawn_agent`).

## Testing (TDD)

- **Detach trigger:** with the mode on, `spawn_agent` registers a background job
  and returns the handoff note (not a report) — for both a fan-out and a single
  spawn; an explicit `background=False` still runs inline.
- **Default-on / opt-out:** no env → spawn detaches; `MARIM_DETACH_FANOUT=0` →
  inline.
- **Synthesis gate:** wake does **not** fire while any job runs; fires once all are
  terminal; a failed job still appears in the synthesis input.
- **Agent choice:** `wait_for_job` on a detached job still returns its report
  inline.
- **Config threading:** `load_config` → `Deps.detach_fanout`.

## Alternatives considered

- **Opt-in (default off):** rejected per owner preference; default-on is safe
  because the agent can choose to wait inline for fast fan-outs.
- **Per-batch synthesis gating** (track which job ids form a fan-out, wake when
  that set completes): more precise but more state; deferred — the global "no jobs
  running" gate is sufficient and simpler.
- **Dedicated `fan_out` batch tool / prompt-only:** both make the *trigger*
  model-dependent, failing the robustness bar on mimo.
- **Harness-side ≥2 fan-out detection (single spawn stays inline):** rejected — not
  cleanly implementable (a tool can't see its sibling calls in pydantic-ai 1.107),
  and unnecessary once the agent owns the wait-vs-end choice. Detach uniformly.
