# Design: spawn_agent after= dependencies in the TUI

**Date:** 2026-07-02
**Status:** Approved (brainstormed with user)
**Scope:** `src/marim_harness/interfaces/tui/` only, plus a two-line fail-prefix
addition in `stream_render.py`. No changes to `tools/`, `jobs.py`, `runtime/`,
or headless behavior.

## Problem

`spawn_agent(after=[job-…])` (landed 2026-07-02) gates a background spawn on
earlier background jobs. The TUI renders such a spawn identically to a running
one: the card spins, the Ctrl+X list shows `▸`, and the summary counts it as
running. The user can't tell a blocked spawn from an executing one, and when a
prerequisite fails the dependent's card shows a generic error (and a tool-level
`after=` rejection even renders a green ✓).

**Primary goal (user-chosen):** answer "why isn't this running?" at a glance.
Explicitly out of scope for v1 (user-chosen): DAG/pipeline visualization and
jump-to-blocker navigation.

## Mechanism (approach A — renderer-derived)

The TUI derives waiting state from information it already receives; nothing new
is threaded through the tool or job layers.

- **Prerequisite ids:** `spawn_agent`'s tool args carry `after`.
  `StreamRenderer.mount_spawn_widget(args)` normalizes it (str → 1-list, list →
  stripped non-empty strings; a tiny local helper, not an import from
  `tools/provider`) into `widget.after_ids`.
- **Own job id:** `note_detached_spawn` already parses the detach handoff
  ("Started <id> …"); it additionally stores `widget.job_id = job_id` (today the
  id lives only in the transient `_detached_cards` map).
- **Waiting predicate:** a card is waiting while `status == "pending"`,
  `after_ids` is non-empty, and any prerequisite job in the registry is still
  `running`. A missing/pruned job id counts as settled, so a card can never
  block forever on a forgotten id.
- **Flip signal:** `fill_finished_detached_cards(jobs)` — already invoked from
  the job-registry change hook on every settle — additionally re-evaluates every
  card with `waiting=True` and flips it off (repaint + viewer refresh) once all
  prerequisites settled. This mirrors `await_settled`'s semantics; the ~3-line
  re-derivation in the renderer is accepted duplication (same pattern as
  detached-card filling).

Rejected alternatives: a `Job.waiting` field (cross-layer plumbing for the same
observable result) and a new `deps.ui.on_subagent_waiting` callback (most
plumbing; no non-TUI consumer exists).

## Widget state

`SubAgentWidget.__init__` gains:

- `after_ids: list[str]` — prerequisite job ids (empty for normal spawns)
- `job_id: str | None` — this card's own background job id (None until handoff)
- `waiting: bool` — derived display state; `status` stays `"pending"`
- `blocked_by: str | None` — culprit job id once a prerequisite failed

`waiting` is display-only: nothing that switches on `status` changes behavior.

## Card rendering (`SubAgentWidget`)

- **Header, pending+waiting:** static `⧗` glyph instead of the spinner, plus a
  dim `after job-3, job-4` tag rendered next to the existing `bg` tag.
- **Activity line, pending+waiting:** `↳ waiting on job-3, job-4` instead of
  `working…`.
- **Failure attribution:** a pure helper `blocked_by_id(report) -> str | None`
  extracts the culprit from the report head (`PrerequisiteFailed` produces
  `prerequisite job-N failed — …`; the report may prefix it with the exception
  class name). On a failed fill it sets `widget.blocked_by`; the header tag then
  reads `blocked by job-3` (dim red) so the cause survives the one-line clip.
  The red `↳` reason line already shows the full message verbatim — unchanged.

## Sub-agents screen (`subagent_stats.py`, `subagent_viewer.py`, `subagents_view.py`)

- `status_glyph` gains a waiting variant: pending+waiting rows show `⧗` instead
  of `▸` (signature grows a `waiting: bool = False` parameter; `row_cells`
  passes `getattr(agent, "waiting", False)` so `FakeAgent`-style stand-ins
  without the field keep working).
- `SummaryStats` gains `waiting: int`; `aggregate` splits waiting out of
  `running` (an agent is counted waiting when non-terminal and
  `getattr(a, "waiting", False)`). The summary bar inserts an `N waiting`
  segment when the count is non-zero:
  `4 sub-agents · 1 waiting · 1 running · 2 done · …`.
- The tests' `FakeAgent` gains a `waiting: bool = False` field (precedent: the
  `stream_id`/`parent_id` additions for tree ordering).

## Fail-prefix fix (`stream_render.py`)

`_SUBAGENT_FAIL_PREFIXES` gains the two `after=` tool-rejection texts so those
returns render ✕ instead of ✓:

- `"Cannot spawn with after="`
- `"after= requires a detached spawn"`

## Edge cases

- **Unknown/pruned prerequisite id** → treated as settled; never blocks a card.
- **Foreground spawn with after=** → tool rejects before any handoff; `waiting`
  is never set; the card renders failed via the prefix fix.
- **Prerequisite fails** → the dependent's job raises `PrerequisiteFailed`; the
  existing fill path marks the card failed; `blocked_by` attribution applies.
- **Session resume / process restart** → jobs are process-scoped; replayed
  cards never claim waiting (their `after_ids` re-derive from args on live
  spawns only; historical cards rebuild without waiting state).
- **/clear, /new, /switch** → cards and job history are already dropped
  together; no new lifecycle concern.
- **Headless** → untouched; everything lives in `interfaces/tui/`.

## Testing

- **Pure:** `status_glyph`/`row_cells`/`aggregate` with waiting `FakeAgent`s;
  `blocked_by_id` parser (with and without the exception-class prefix, and on
  non-prerequisite reports → None); `subagent_failed` round-trips for the two
  new prefixes.
- **Widget:** card paints `⧗` header + `after …` tag + `↳ waiting on …` line
  while waiting; flips to spinner/`working…` when `waiting` clears; failed card
  shows the `blocked by job-N` tag (existing `test_subagent_card.py` style).
- **Renderer:** `mount_spawn_widget` populates `after_ids` from args (str and
  list forms); `note_detached_spawn` stores `job_id` and sets `waiting` against
  a fake registry with a running prerequisite; `fill_finished_detached_cards`
  flips `waiting` once the prerequisite settles and leaves it set while one is
  still running.

## Follow-ups deliberately deferred

- Jump-to-blocker navigation (needs a durable job-id → card map).
- DAG/pipeline visualization on the sub-agents screen.
- Surfacing the waiting phase in the jobs panel beyond the existing
  `(waiting on …)` output_fn text.
