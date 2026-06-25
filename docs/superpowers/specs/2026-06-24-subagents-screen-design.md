# Sub-agents Screen — Design

**Date:** 2026-06-24
**Status:** Approved (brainstorm), pending implementation plan
**Area:** `src/marim_harness/interfaces/tui/`

## Problem

The current `ctrl+x` sub-agent viewer is hard to use and barely discoverable. Concretely:

1. **Navigation feels clumsy.** It's not a real "place" — it toggles docked widgets (`SubAgentList`
   left rail + `SubAgentFooter` bottom bar) on top of the main log and reveals a transcript in
   place via a `.viewing` CSS class. Arrow-key cycling competes with normal scrolling and the
   focus chain is shared with the live log.
2. **Discoverability is poor.** The viewer is so unobvious that, in practice, the stats and
   transcripts were not findable at all. Surfacing how to get in — and what you can see — is a
   first-class goal, not a nice-to-have.
3. **Background jobs are missing.** Only foreground spawns (`stream.subagents`) appear. Detached/
   background sub-agents live in a separate `JobPanel` (`panels.py`), so there is no single
   "all my agents" view.
4. **Stats are scattered.** Tool count + duration sit on the breadcrumb's activity line, tokens/
   cost on a separate usage line, type/index/spend in the footer — there is no clean per-agent
   stats summary or session roll-up.

## Goals

- A real, discoverable, full-bleed sub-agents view: agent list on the left, selected agent's
  stats + live transcript on the right (two-pane master-detail).
- **Fully live:** the list and the open transcript update in real time as agents stream.
- One unified list including background/detached agents (live streaming for those is Phase 2).
- A clean per-agent stats header + a session-total roll-up: status, duration, tool count,
  current activity, tokens (input/output split), cost, model, type/title.
- Keep a compact breadcrumb card in the main log where each sub-agent ran, clickable to jump
  into the view at that agent.

## Non-goals

- Replacing the inline breadcrumb cards (they stay, slimmed).
- A literal Textual `push_screen` route (see Approach decision below).
- Cross-process re-architecture of how background jobs run (Phase 2 spike decides the minimal
  viable streaming path).

## Approach decision

A transcript is a live Textual **widget tree**, and a widget lives in exactly one place in the
DOM. That constraint drives the mechanism choice:

- **A — Full-bleed view + relocate transcript bodies (CHOSEN).** Move each transcript `body` out
  of its inline card into a persistent, hidden `ContentSwitcher` ("detail host") that lives in the
  main screen. The view is a container that hides the log and shows `[list | detail host]`. Because
  bodies live in the always-mounted host, the live stream keeps mounting into them whether the view
  is open or not — fully live for free, no reparenting, no mirroring. Same DOM tree → clean focus/
  key handling. Cost: one real refactor (pull `body` out of `SubAgentWidget`, repoint the sink).
- **B — Real pushed `Screen` + mirror data.** A true `Screen` can't hold the live widgets, so every
  transcript event is captured into a serializable model and rendered twice. Most plumbing, most
  drift risk, worst fit for the "fully live" requirement. Rejected.
- **C — Real pushed `Screen` + reparent on select.** Move the live `body` onto the screen on
  select, back on close. Textual doesn't cleanly support cross-parent reparenting of mounted
  widgets (remove/remount loses scroll/state and re-runs mount) — fragile exactly on live, growing
  transcripts. Rejected.

The full-bleed view delivers the same UX as a pushed screen (takeover, Esc to return, own
bindings); "not literally `push_screen`" is an accepted internal trade for liveness + low risk.

## Architecture & components

### Changed

- **`SubAgentWidget` (`widgets/subagent.py`) — slimmed to a pure breadcrumb.**
  Keeps the compact header + activity line + status glyph and the cheap scalar updates
  (`note_tool()`, `set_usage()`, `finish()`). **Loses ownership of `body`.** Gains a click handler
  that opens the view focused on this agent.

- **`StreamRenderer` (`stream_render.py`) — sink retargeted.**
  Still owns the `subagents` registry and per-stream state. Gains a reference to the detail host.
  The per-stream `_SubAgentSink` mounts transcript content into `detail_host.pane(stream_id)`
  instead of `widget.body`. `_TopLevelSink.intercept_tool()` additionally creates the agent's
  detail pane and a list row at spawn time.

- **`SubAgentList` (rework of `subagent_viewer.py`) — a `DataTable`.**
  Columns: status · type/title · tools · tokens · cost · duration. One live-updated row per agent.
  Selection drives the detail host. Replaces today's plain glyph list + separate footer.

### New

- **`SubAgentDetailHost` (`widgets/subagent_detail.py`) — a `ContentSwitcher`.**
  Lives permanently in the main screen, `display:none` until the view opens. One transcript pane
  per sub-agent keyed by `stream_id`; exactly one visible. This is the right-hand pane. Transcript
  events mount here regardless of view state → liveness is free.

- **`SubAgentsView` (`widgets/subagents_view.py`) — the full-bleed container.**
  Lays out a top `SubAgentSummary`, `[ SubAgentList | SubAgentDetailHost ]`, and a bottom hint bar.
  Toggling hides the main log and shows this; Esc reverses. Owns the view's key bindings and focus.

- **`SubAgentSummary` (small widget) — session roll-up.**
  Top bar: total agents (running/done/failed), summed tokens, summed cost.

- **`HarnessApp` (`app.py`)** owns `SubAgentsView` and the `ctrl+x` toggle
  (`action_toggle_subagents`, rewired from today's docked-widget toggle).

## Data flow & liveness

One event fans out to three cheap sinks, all already on the app event loop (callbacks fire on the
UI loop — confirmed — so direct widget mutation is safe; no new marshalling).

**Spawn (foreground):**
1. `spawn_agent` → `_TopLevelSink.intercept_tool()` creates the `SubAgentWidget` breadcrumb in the
   log **and** `detail_host.add_pane(stream_id)` (empty, hidden).
2. Agent registered in `stream.subagents`; a `SubAgentList` row is appended.

**Live streaming (hot path):**
3. `on_subagent_event()` routes each event through `_SubAgentSink`; the only change is it mounts
   transcript content into `detail_host.pane(stream_id)` instead of `widget.body`.
4. The same event updates scalars: breadcrumb one-liner (`note_tool`/`set_usage`) **and** the
   agent's list row **and** `SubAgentSummary` totals.
5. Panes live in the always-mounted host, so step 3 runs whether or not the view is open — opening
   `ctrl+x` mid-run shows an already-current transcript that keeps ticking.

**Finish:** `FunctionToolResultEvent` → `widget.finish()` freezes the breadcrumb; the row flips its
glyph and freezes duration; the pane keeps the final transcript + report.

## Navigation & UX

**Opening / closing:**
- `ctrl+x` toggles `SubAgentsView`. With no sub-agents yet, show a clear notice instead of an empty
  view.
- Clicking an inline breadcrumb opens the view focused on that agent (jump-to-context).
- The main-screen hint bar always shows `ctrl+x Sub-agents` for discoverability.

**Inside the view:**
- Two focus zones — list (left) and detail pane (right) — with `Tab` / `Shift+Tab` between them;
  the focused zone is visibly highlighted.
- List focused: `↑/↓` select an agent (detail pane updates live via the `ContentSwitcher`),
  `Enter` drops focus into the detail pane.
- Detail focused: `↑/↓ / PgUp/PgDn` scroll the transcript (no contention with the main log, which
  is out of this DOM path while the view is open).
- `Esc` (or `ctrl+x` again) returns to the main log, restoring prior focus.
- Persistent bottom hint bar: `Esc back · ↑↓ select · Tab switch pane`.

**Edge states:**
- Detached agents (pre-Phase 2): pane shows `detached — ran in background, no live transcript`;
  row still carries status/duration/final stats.
- Failed/denied agents: error shown prominently in both the row and the pane.

## Phasing

**Phase 1 — the screen (Approach A), foreground agents.**
All of the above for foreground spawns. Detached agents appear in the list with status/duration/
final report but their pane shows the placeholder. Delivers all four pain points for the common
case (foreground).

**Phase 2 — live streaming for background jobs.**
First task is a **feasibility spike**, not code, because how detached jobs carry output across the
job boundary is not yet verified. Branches:
- Jobs already emit structured events to a subscribable sink/registry → route into a detail pane
  like foreground (clean win).
- Jobs only persist output to a file/buffer → tail/poll it into the pane (live-ish, simple).
- Output opaque until completion → keep the placeholder, surface only the final report, and state
  that as the ceiling rather than build something fragile.

The spike outcome decides Phase 2 scope. Until then, Phase 2 cost is explicitly unknown and must
not block Phase 1.

### Open question (to resolve in Phase 2 spike)

How does a detached/background sub-agent's output and stats become available to the TUI process?
(Subscribable event stream vs. file/buffer tail vs. final-report-only.) Determines whether live
background streaming is cheap, moderate, or out of reach.

## Testing

Repo split: pure logic unit-tested directly; thin widget/IO layer via Textual `Pilot`; one
live-terminal caveat.

**Pure unit tests (no app):**
- Stats aggregation: sub-agent records → `SubAgentSummary` totals (counts by status, summed
  tokens/cost) and per-row `DataTable` cells (duration formatting, glyph selection).
- Title derivation / detached-vs-foreground labeling.

**Pilot widget tests:**
- Spawn → breadcrumb in log + hidden pane in `SubAgentDetailHost` + list row.
- Streaming events mount into the correct pane by `stream_id`; breadcrumb scalar + row update;
  assert "live while view closed, then open and it's current."
- Toggle: `ctrl+x` shows view / hides log; Esc reverses and restores focus; breadcrumb click opens
  at that agent.
- `Tab` moves focus between zones; `↑/↓` on the list changes the visible pane.
- Detached agent → placeholder pane.

**Live-terminal caveat:** `Pilot` key simulation does not prove the real terminal delivers
`ctrl+x` (Kitty keyboard protocol can swallow/remap it). The plan includes a **manual tmux
verification** of the keypress. Pilot covers the action (`action_toggle_subagents`); tmux covers
delivery.

## Affected files (indicative)

- `interfaces/tui/widgets/subagent.py` — slim breadcrumb, drop `body`, add click-to-open.
- `interfaces/tui/widgets/subagent_detail.py` — **new** `SubAgentDetailHost` (`ContentSwitcher`).
- `interfaces/tui/widgets/subagents_view.py` — **new** full-bleed `SubAgentsView` + summary + hints.
- `interfaces/tui/widgets/subagent_viewer.py` — rework `SubAgentList` into a `DataTable`; retire
  `SubAgentFooter` (folded into the view's hint/summary bars).
- `interfaces/tui/stream_render.py` — retarget `_SubAgentSink` to the detail host; create pane +
  row at spawn.
- `interfaces/tui/app.py` — own `SubAgentsView`; rewire `action_toggle_subagents`; main-screen hint.
- `interfaces/tui/styles.tcss` — layout for the two-pane view, focus highlight.
- `tests/` — new pure + Pilot tests as above.
