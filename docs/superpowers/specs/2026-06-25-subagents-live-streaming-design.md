# Phase 2 — Live-streaming background sub-agents (design)

**Status:** approved (design)
**Date:** 2026-06-25
**Predecessor:** [2026-06-24-subagents-screen-design.md](2026-06-24-subagents-screen-design.md) (Phase 1). This
document is the Phase 2 the Phase 1 design deferred to "a feasibility spike, then a separate plan."

## Goal

A detached/background sub-agent streams its transcript, tool activity, tokens, and cost **live** into
the sub-agents screen (its `ContentSwitcher` pane) and its inline log card — identical to a foreground
spawn — instead of showing the `detached — ran in background, no live transcript` placeholder until it
finishes.

## Spike outcome (resolves the Phase 1 open question)

The Phase 1 design left one open question: *how does a detached/background sub-agent's output and stats
become available to the TUI process?* — with three branches (subscribable event stream → clean win;
file/buffer tail → moderate; opaque-until-completion → keep placeholder). The spike resolved it to the
**clean-win branch, decisively**:

- A background sub-agent is an **in-process `asyncio` task**, not a separate OS process. There is no
  process boundary to cross. (The earlier deferral assumed there might be.)
- The streaming machinery foreground spawns use is **already built and already runs for background
  jobs** — its output is just deliberately discarded:
  - `SubagentRunner._execute_spawn` runs both foreground and background spawns with the same
    `event_stream_handler` (`subagents.py:413–421`).
  - `SubagentRunner.handler(stream_id)` forwards every streamed event + usage to the UI via
    `cb = deps.on_subagent_event(stream_id, event, usage)` — **but only when `stream_id` is set**
    (`subagents.py:198–220`).
  - For a background run it passes `None if background else stream_id` (line 413), so `forward=False`:
    hooks still fire, the UI never sees the events. `run_background` is called with `stream_id=""`
    (`subagents.py:516`), and `spawn_agent` drops `ctx.tool_call_id` when it registers the job
    (`provider.py:412–417`).
  - The TUI consumer already exists and is keyed by `stream_id`: `on_subagent_event` looks up
    `tool_widgets[stream_id]` and dispatches the event into that widget's pane, ticking usage through
    the same call (`stream_render.py:580–598`). The pane is keyed by `stream_id` (= `tool_call_id`),
    which a background spawn already has.

**Conclusion:** live-streaming a background sub-agent is **wiring + lifecycle, not a re-architecture**.
Thread the spawn's `tool_call_id` through `run_background → _execute_spawn → handler` instead of
nulling it, and on the TUI side let the card stream live instead of marking it detached and showing
the placeholder.

## Hard boundary (unchanged by Phase 2)

The **model-facing** background-job contract is untouched. Streaming is a pure UI overlay on the
existing `JobRegistry` lifecycle. None of these change:

- the finished-job digest (`take_finished_digest`, `has_finished_pending`),
- `job_output` / `wait_for_job` / `cancel`,
- the autonomous wake scheduler and its `_wake_consumed` bookkeeping,
- output spill files and the soft `max_output_chars` budget,
- `jobs.py` semantics generally.

A background spawn's report still reaches the model the same way it does today. Phase 2 only adds a
live UI view of work that was already happening.

## Design

### Engine wiring

1. `spawn_agent` (the background / auto-detach branch) threads its `ctx.tool_call_id` into the job
   rather than dropping it (`provider.py:412–417`). This id becomes the spawn's `stream_id` — the same
   id its inline tool-call card and detail pane are already keyed on.
2. `run_background` and `_execute_spawn` accept that id and pass it to `self.handler(stream_id)` for
   background runs. The `None if background else stream_id` suppression at `subagents.py:413` is
   removed; the gate reverts to what `handler()` already enforces — *stream when a UI listener exists
   and a `stream_id` is present* — so a headless background run (no `on_subagent_event`) still streams
   nothing, unchanged.
3. Headless mode: `deps.on_subagent_event is None` → `forward=False` → no-op. No behavior change.

### TUI side

1. The spawn's tool-call card (keyed by `tool_call_id`) **is** the live card; its detail-host pane is
   created on the first streamed event, exactly as a foreground spawn's is (`ensure_pane`). The
   `note_detached_spawn` placeholder path is **not** used for in-process agent jobs.
2. On job settle, the existing `_fill_detached_card` work collapses into the ordinary card settle:
   set the final usage/cost on the row, then `widget.finish(report, status)`. Because events have
   streamed, a successful finish just flips the status glyph and freezes the duration; a failed finish
   appends the returned error to the pane (failures are returned, not streamed — existing `finish()`
   behavior, `subagent.py:291–304`). The settle is idempotent with what already streamed.
3. **Subtle background marker.** A detached agent's row and card carry a quiet `bg` tag (or dimmed
   status glyph) so an off-turn agent is tellable at a glance from one running inside the current turn.
   The row format is otherwise identical to a foreground agent's.
4. **Quiet settle.** No new notifications are introduced. The existing finished-job digest and current
   notifier behavior are preserved as-is; on settle the card simply stops moving.

### The one genuinely new concern: off-turn streaming

A background card streams across **later** turns, from the scroll position in `#log` where it was
created. This is safe — the event loop is single-threaded, and the card and its pane live in the
persistent `#log` / detail host, which survive turn boundaries. The card stays "live" until its job
settles, regardless of how many turns pass.

Edge case to honor: `/clear` keeps a still-running background job (`JobRegistry.clear_history` retains
`running` jobs) but the card is gone from the cleared log. Streaming must therefore tolerate a missing
`tool_widgets[stream_id]` — which it already does: `on_subagent_event` no-ops when the parent widget is
absent (`stream_render.py:588`). The spec requires this to remain true.

## Testing

- **Pure:** `stream_id` (the `tool_call_id`) is threaded through `run_background` / `_execute_spawn` and
  reaches `handler`; a background run with a UI listener forwards events, a headless one does not.
- **Pilot (Textual):** a background spawn streams text/tool activity into its pane and ticks its list
  row's tokens/cost live; it carries the `bg` marker; on settle it flips to done/failed via `finish()`
  with the final stats frozen. A `/clear` mid-stream leaves the running job streaming into nothing
  without error.
- **Live caveat:** one tmux/real-terminal pass (Kitty-protocol delivery isn't proven by Pilot key
  tests), per the Phase 1 testing note.

## Out of scope

- **Bash background jobs.** They are OS processes with a growing output buffer (`output_fn`), not
  sub-agents, and do not appear in the sub-agents screen. Their buffer-tail behavior is unchanged.
- **Any cross-process mechanism.** The spike showed none is needed; agent jobs are in-process.
- **Changes to the model-facing job contract** (see the Hard boundary section).
