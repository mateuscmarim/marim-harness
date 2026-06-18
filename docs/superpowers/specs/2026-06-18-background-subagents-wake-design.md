# Background Subagents: Wake-on-Completion + Gap Fixes — Design

**Date:** 2026-06-18
**Status:** Approved (pending spec review)

## Context

A "background subagent" capability already exists in marim-harness, wired
end-to-end. This project does **not** build it from scratch — it **hardens three
correctness gaps** in the existing implementation and **adds autonomous
wake-on-completion** (the one genuinely new capability).

What exists today:

- **Spawn:** `spawn_agent(type, task, background=True, …)`
  (`src/marim_harness/tools/provider.py:259–325`) registers a job and returns
  `Started <job_id> (agent) — …` immediately instead of blocking.
- **Detached execution:** `JobRegistry.register(coro)`
  (`src/marim_harness/jobs.py:86–122`) schedules the coroutine with
  `asyncio.ensure_future` and returns the id at once — genuinely detached on the
  event loop, not awaited inline. `SubagentRunner.run_background()`
  (`src/marim_harness/subagents.py:128–154`) is the coroutine; it streams no UI
  events and persists its spend immediately.
- **Poll (agent-facing tools):** `jobs()`, `job_output(id)`,
  `wait_for_job(id, timeout=60)`, and a cancel path
  (`provider.py:388–415`, plus the action-dispatch tool at `provider.py:433–440`).
- **Notify (passive):** `take_finished_digest()` (`jobs.py:199–218`) is injected
  into the next prompt via `_assemble_prompt()` (`agent.py:373–381`); the TUI
  jobs panel repaints via the `on_change` callback.

Key runtime facts that constrain the design:

- The TUI runs each turn in a single Textual worker:
  `self.run_worker(self._run_turn(text), exclusive=True)`
  (`interfaces/tui/app.py:595`). Only one turn runs at a time; background jobs run
  outside it on the event loop via `asyncio.ensure_future`.
- `Deps` (`src/marim_harness/deps.py:36–60`) is **shared** by the main agent and
  all sub-agents. `workspace_root`/`mode` are read-only and safe to share;
  `tasks: TaskList` and `jobs: JobRegistry` are **mutable** and unsafe under
  concurrent writers.
- `session.usage` is accumulated by both the main turn and background sub-agents;
  `session.persist()` writes the whole session (last-write-wins).
- Jobs are in-memory and process-scoped — lost on restart.

## Goal

Make background subagents production-correct and let a finished job **fire a turn
on its own** (autonomous wake), instead of only surfacing on the user's next
message. Scope is the **interactive TUI only**. The interaction model is
**poll + notify**, where "notify" is upgraded from passive (next-user-turn
digest) to autonomous (a turn fires when work completes).

## Decisions (resolved during brainstorming)

- **Interaction model:** poll + notify. Poll tools already exist; notify becomes
  autonomous.
- **Scope:** interactive TUI only. Headless (`-p`) keeps today's behavior. Not in
  scope.
- **Direction:** harden the known gaps **+** autonomous wake. Cross-restart
  persistence is **out** (YAGNI for TUI-only).
- **Wake trigger:** mirror Claude Code — backgrounding *is* the opt-in (no
  separate `wake=` flag); fire an autonomous turn when the turn-worker is idle;
  queue (do nothing) when busy; batch naturally.
- **Loop guard:** depth cap on consecutive autonomous turns **+** a kill switch
  (config default + `/jobs wake on|off`).
- **Defaults:** depth cap = **3**; autonomous wake **on** by default.
- **Task isolation:** background sub-agents get their **own** isolated
  `TaskList` (not the parent's), rather than having task tools stripped.

## Architecture

Four units, each with one responsibility:

1. **Wake scheduler** — decides when an autonomous turn fires and enforces the
   depth cap + kill switch. Lives in the TUI layer (it owns the turn worker).
   (Component A)
2. **Turn entry for autonomous turns** — a digest-only turn path reusing the
   existing prompt-assembly + digest plumbing. (Component B)
3. **Background sub-agent task isolation** — give each background sub-agent its
   own `TaskList`. (Component D)
4. **`/jobs` command** — human-facing mirror of the agent's job tools, plus the
   wake toggle. (Component E)

Component C (session persist serialization) was **dropped** — see its section
below for the rationale.

Everything reuses the existing `JobRegistry`, `take_finished_digest()`,
`_assemble_prompt()`, and `on_change` plumbing; the new code is the scheduler and
the targeted fixes.

## Component A — Wake scheduler (TUI)

Where: `interfaces/tui/app.py`, alongside the existing turn-worker management and
the `on_change` wiring.

State held on the app:
- `autonomous_wake: bool` — runtime toggle, initialized from config.
- `_auto_turn_depth: int` — count of consecutive autonomous turns.

Trigger evaluation — a single helper, call it `_maybe_wake()`, invoked from two
points:
1. The existing `jobs.on_change` callback (already fires on completion;
   `app.py:~321`), **after** the current repaint.
2. The turn-worker completion path (so a digest that arrived while a turn was
   running drains immediately after it ends).

`_maybe_wake()` fires an autonomous turn **iff all** hold:
- `self.autonomous_wake` is true;
- the turn worker is idle (no active `_turn_worker`);
- `self._auto_turn_depth < cap` (cap from config, default 3);
- `jobs.has_finished_pending()` is true — i.e. there is a non-empty digest
  waiting. (New tiny predicate on `JobRegistry`; see below. It must **not**
  consume the digest — only `take_finished_digest()` consumes, inside the turn.)

When it fires:
- `self._auto_turn_depth += 1`.
- Post a system marker into the log: `⏰ Resumed — background job(s) finished`.
- Start the turn worker on a digest-only turn (prompt `""`); `_assemble_prompt()`
  prepends `take_finished_digest()`, which consumes and clears the pending set.

Depth reset:
- Any **user-initiated** turn resets `self._auto_turn_depth = 0` (set in the
  user-submit path before starting the worker).

Queueing: when a turn is running, `_maybe_wake()` returns without acting; the
digest persists in the registry and is drained by whichever turn runs next (user
or auto). No preemption.

Batching: because one digest-only turn consumes the entire pending set, a burst of
completions collapses into one autonomous turn.

### New `JobRegistry` predicate

`has_finished_pending() -> bool` — true if there are finished-since-last-digest
jobs queued for `take_finished_digest()`. Read-only; does not mutate the
finished-pending set. Needed so the scheduler can check without consuming.

## Component B — Autonomous turn entry

No new turn engine. The autonomous turn is an ordinary `_run_turn("")`:
- `_assemble_prompt("")` returns the digest (already implemented at
  `agent.py:381`). If the digest is empty the turn must not start — the scheduler
  guarantees non-empty via `has_finished_pending()`.
- The turn runs in the same exclusive worker as user turns, so it cannot overlap a
  user turn.
- In `ask` mode, gated tools prompt the user normally (the user is present);
  no special handling.

## Component C — Usage-race fix — DROPPED

**Status: dropped during writing-plans (not implemented).**

The fix guarded a race that cannot occur. marim runs on a single-threaded
asyncio event loop. Tracing the read-modify-write of `session.usage` and the
persist that follows:

- Background sub-agent completion (`subagents.py:151–152`):
  `self.session.usage += result.usage` immediately followed by
  `self.session.persist()` — no `await` between them.
- Main-turn completion (`agent.py`): `self.session.usage += result.usage`
  then `self.session.persist()` — again no `await` between.
- `SessionController.persist()` and `SessionStore.save()` are **synchronous**
  (no `await` inside) — they run to completion without yielding the loop.

Because neither read-modify-write yields the event loop between the `+=` and the
`save()`, no second coroutine can interleave and clobber the increment. An
`asyncio.Lock` would serialize sections that are already atomic with respect to
each other — pure overhead, and a TDD test for it would assert nothing real
(there is no interleaving to provoke). YAGNI.

This becomes relevant only if marim ever moves `usage`/`persist` off the single
loop (threads, multiprocessing, or an `await` introduced mid-sequence). Recorded
in Out of Scope below so the deferral is not silently lost.

## Component D — Background sub-agent task isolation

Problem: background sub-agents share `Deps.tasks` with the main agent
(`deps.py`), so concurrent task-list writes race.

Fix: when `SubagentRunner.run_background()` builds the sub-agent's `Deps`, give it
its **own** empty `TaskList` instead of the parent's. The sub-agent can track its
own multi-step work without touching the parent's checklist. Foreground sub-agents
are unchanged (they run sequentially within the turn, no race).

`jobs: JobRegistry` stays shared (background sub-agents do not spawn further
sub-agents — `provider.py:531` — and only the main agent + the registry's own
done-callbacks mutate it, which already settle in order).

## Component E — `/jobs` command (TUI)

Where: `interfaces/tui/commands.py`, registered in `COMMANDS`.

```
/jobs                      → list jobs (same render as the panel / agent `jobs` tool)
/jobs output <id>          → print a job's output/result
/jobs cancel <id>          → cancel a running job
/jobs wake on|off          → toggle autonomous wake (sets app.autonomous_wake)
```

Implemented over the existing `JobRegistry` methods (`list`, `output`, `cancel`)
and `render_jobs()`. `wake` flips `app.autonomous_wake` and posts a confirmation.
Mirrors the agent-facing tools so the human has parity.

## Data Flow

```
spawn_agent(background=True)            [inside a turn]
  └─ jobs.register(run_background_agent(...))  → returns job_id immediately
       └─ coroutine runs detached on the event loop

job finishes
  └─ JobRegistry done-callback settles status, adds to finished-pending set
       └─ on_change fires → TUI repaints jobs panel
            └─ _maybe_wake():
                 wake on? worker idle? depth < cap? has_finished_pending()?
                   ├─ no  → digest stays pending (drained by next user/auto turn)
                   └─ yes → depth++, post "⏰ Resumed", run _run_turn("")
                              └─ _assemble_prompt("") consumes digest → agent reacts

user sends a message
  └─ depth = 0; _run_turn(text) (digest, if any, also drains here)
```

## Error Handling

| Situation | Behavior |
|-----------|----------|
| Background sub-agent raises | Job settles `failed`; digest reports the failure; an autonomous turn (if armed) surfaces it so the agent can react |
| Autonomous turn itself errors | Same recovery as any turn (existing `_repair_unanswered_tool_calls` / turn error handling); depth counter already incremented, so a failing wake loop is bounded by the cap |
| Depth cap reached | No further autonomous turns; digest waits for the user's next message; jobs panel still shows results |
| Wake disabled mid-flight | `_maybe_wake()` returns early; pending digest surfaces on the next user turn |
| Job cancelled (via tool or `/jobs cancel`) | Settles `cancelled`; reported in digest like any terminal job |
| Empty digest race (job finished but already drained) | `has_finished_pending()` false → no empty autonomous turn |

## Testing

- **Unit (jobs/scheduler logic):**
  - `has_finished_pending()` reflects the finished set without consuming it.
  - Depth counter increments per autonomous turn and resets on a user turn.
  - Wake gate: disabled ⇒ never fires; cap reached ⇒ stops.
- **Integration (FunctionModel-driven harness):**
  - Background job finishes while idle ⇒ one autonomous turn fires and consumes
    the digest.
  - Finishes while a turn is running ⇒ queued; drains on the following turn.
  - Several finish together ⇒ one batched autonomous turn.
  - Depth cap: a chain of wake→spawn→wake stops at the cap.
- **Isolation:** a background sub-agent mutating its task list does not change the
  parent's `Deps.tasks`.
- **Command:** `/jobs` resolves in `COMMANDS_BY_NAME`; `wake off` flips
  `app.autonomous_wake`; `output`/`cancel` reach the registry.

Gates: `uv run ruff check src tests`, `uv run pyright src`, `uv run pytest` all
green.

## Out of Scope (deferred)

- **Headless (`-p`) wake** — single-turn mode keeps today's blocking/passive
  behavior. Documented future extension; the scheduler is the only TUI-coupled
  piece.
- **Cross-restart job persistence** — jobs remain in-memory/process-scoped.
- **Autonomous wake without a present user** (e.g. cron/headless daemon) — not a
  TUI concern.
- **Per-job wake opt-out** — backgrounding implies wake; if a need for
  "background but don't wake me" appears, revisit then (YAGNI).
- **Usage-race lock (former Component C)** — no lock is added; the
  read-modify-write of `session.usage` + `persist()` is already atomic on the
  single-threaded asyncio loop (no `await` between the `+=` and the synchronous
  `save()`). Revisit only if `usage`/`persist` ever moves off the single loop.
- **Recursive sub-agent spawning** — unchanged; sub-agents still cannot spawn
  sub-agents.
