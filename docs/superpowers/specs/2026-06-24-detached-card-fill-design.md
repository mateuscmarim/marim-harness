# Fill-on-Finish Detached Sub-Agent Cards — Design

**Date:** 2026-06-24
**Status:** Approved (pending spec review)

## Context

The detached fan-out feature (`docs/superpowers/specs/2026-06-24-detached-fanout-design.md`)
runs a sub-agent spawn as a background job by default and lets the agent end its
turn; the autonomous-wake turn then synthesizes the reports. Two follow-ups
already landed: resumed `spawn_agent` calls render as `SubAgentWidget` cards
(`session_view.py`), and a `wait_for_job` on a finished agent job renders the
sub-agent's card inline (`stream_render.py`).

The remaining gap is the *common* detach path — the agent **ends its turn** rather
than waiting. There, the live card never shows the real report:

- An auto-detached spawn (`background=None` under detach mode) still builds a
  `SubAgentWidget` in `intercept_tool` (`stream_render.py`), because the gate is
  `not args.get("background")` and `background` is absent.
- But the spawn's `ToolReturnPart` is the **handoff note** ("Started detached
  sub-agent `<id>`…"), so the result handler finishes the card with that text as
  its report. The card reads ✓ + handoff note and never updates.

The actual report *is* already visible — the wake digest inlines it into the
synthesis turn (`jobs.take_finished_digest`). So this is **card-UI parity**, not a
missing capability: connect the finished report back to a card per sub-agent so a
fan-out reads as N cards that fill in, instead of the reports appearing
disconnected in a later turn.

Confirmed runtime facts:

- The TUI wires `on_jobs_changed=self._on_jobs_changed` into the `JobRegistry`
  (`app.py:102`); `_on_jobs_changed` (`app.py:285`) fires on every job state
  change (including settle).
- Background spawns do **not** forward their steps to the UI
  (`run_background` passes `handler(None)` in `subagents.py`), so there is no live
  nested transcript to show — only a final report.
- The handoff note carries the `job_id` (`_detach_handoff`, `provider.py`).

## Goal

When a detached sub-agent's background job finishes, fill its existing live card
with the real report and final status — driven by the existing `on_jobs_changed`
hook. No server-side changes, no live streaming, no resume support.

## Design

### Components

1. **`_detached_job_id(content) -> str | None`** (`stream_render.py`). Parses the
   `job_id` out of a handoff note; returns `None` for any other return. Lives
   next to `subagent_failed` and is pinned by a test against a real
   `_detach_handoff(...)` string so the producer and parser can't drift.

2. **`StreamRenderer._detached_cards: dict[str, SubAgentWidget]`** — the
   `job_id → pending card` map. Initialised empty; cleared on session reset/switch
   (the cards are destroyed with the log).

3. **`note_detached_spawn(content, widget, jobs) -> bool`** (renderer method),
   called from the `FunctionToolResultEvent` handler when `widget` is a
   `SubAgentWidget`. If `content` is a handoff (`_detached_job_id` non-None):
   record `job_id → widget`, set the card's activity to "running in background…",
   **fill immediately if the job is already terminal** (a fast job can settle
   before its handoff return renders), and return `True` so the caller does not
   finish the card. Otherwise return `False` → the caller finishes normally
   (foreground spawns and `wait_for_job` cards are unaffected).

4. **`fill_finished_detached_cards(jobs)`** (renderer method). For each mapped
   `(job_id, card)` whose job is now terminal, call `card.finish(report,
   status=status)` and drop it from the map. `report` is `job.result`; `status` follows the same rule
   as the live/resume/wait paths: a `failed`/`cancelled` job → `failed`; a `done`
   job whose result is runner-failure text (`subagent_failed`) → `failed`; else
   `done`.

### Wiring

`app._on_jobs_changed` calls `self.stream.fill_finished_detached_cards(
self.harness.deps.jobs)` (alongside its existing repaint/wake work). The
session-reset path that rebuilds the log also clears `_detached_cards`.

### Data flow

```
spawn (background=None) → card built (intercept_tool)
  → ToolReturn = handoff → note_detached_spawn: keep pending, map job_id→card
  → job settles → on_jobs_changed → fill_finished_detached_cards → card: report + ✓/✗
```

## Error / edge handling

- **Fast job (race):** `note_detached_spawn` fills immediately when the job is
  already terminal at handoff time, so completion is never missed.
- **Failed / cancelled job:** filled via `job.status` (✗), same as other paths.
- **Session switch:** `_detached_cards` is cleared so a stale `job_id` can't fill
  a card in a different session's log.
- **Resume:** out of scope — jobs are process-scoped (gone on restart) and the
  report lives in a later turn's digest, not the spawn's return. The resumed card
  keeps today's handoff-note resting state; the report still shows in the resumed
  wake synthesis.

## Scope boundaries

- **In:** auto-detached spawns (those that already build a `SubAgentWidget`).
- **Out:** explicit `background=True` (stays a generic tool row — the model chose
  raw background); live nested streaming of background sub-agent steps; resume
  parity.

## Testing (TDD)

- `_detached_job_id` round-trips against a real `_detach_handoff(job_id)` output;
  returns `None` for a normal report.
- `note_detached_spawn`: handoff + running job → returns `True`, card stays
  `pending`, mapped; handoff + already-done job → fills immediately; non-handoff →
  returns `False` (foreground card finishes normally).
- Integration via `on_events` + a `JobRegistry` (mirroring the wait-for-job test):
  a spawn returns its handoff while the job runs → card pending; settle the job +
  `fill_finished_detached_cards` → card `done` with the report; a failed job →
  card `failed`.
