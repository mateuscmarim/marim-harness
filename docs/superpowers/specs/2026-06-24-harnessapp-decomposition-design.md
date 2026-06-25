# HarnessApp decomposition — design

**Date:** 2026-06-24
**Status:** approved (pending written-spec review)
**Scope:** Extract the clean-cut, self-contained state machines out of
`HarnessApp` into Textual-free collaborator objects, mirroring the existing
`WakeController` / `StatusPresenter` pattern. Input-event glue (prompt input
routing, command autocomplete) deliberately stays on the App.

## Problem

`HarnessApp` (`src/marim_harness/interfaces/tui/app.py`) is 875 lines / 63
methods / 20 attributes — 92% of its file. It is *not* a true god class: the
heavy lifting (AI execution, streaming render, session replay, status, wake
policy) is already delegated to collaborators. What remains is drift — several
**independent** state machines implemented inline on the App that have nothing
to do with each other and never share state:

- Turn queue: 6 methods + `_queue`, `_queue_seq`, `_queue_paused`.
- Finished-job notification dedup: `_notify_finished_jobs` + `_notified_jobs`.
- Sub-agent viewer: 8 methods + `subagent_viewer_open`, `subagent_index`.

The codebase already establishes the cure. `WakeController` (`wake.py`) owns the
*decision + state* and stays free of Textual so it is unit-testable without an
App; the App keeps the *effect* (mounting widgets, spawning workers). `queue.py`
already holds the `QueuedMessage` dataclass and a pure `render_queue` helper —
only the stateful coordination was left behind on the App.

## Principle (the seam)

Every extracted object follows the house pattern:

- **Object owns:** state + decisions. **No Textual imports.** Returns data.
- **App keeps:** the effect — widget queries/mounts, worker spawning, focus,
  desktop notifications, panel repaints.
- **Testability:** each object gets a plain `pytest` unit test with no App
  harness. Existing App-level tests are the behavior-preserving regression net.

## Components

### 1. `TurnQueue` (into existing `queue.py`) — strong extraction

Owns `_items: list[QueuedMessage]`, `_seq: int`, and `paused: bool`. Each method
is lifted from an existing App body:

| Method | Lifted from | Logic |
|---|---|---|
| `enqueue(text, atts)` | `_enqueue` (app.py:511) | `_seq += 1`; append a `QueuedMessage` |
| `prepend(text, atts)` | `_after_turn` (app.py:535) | `_seq += 1`; `insert(0, …)` — the leftover-steer front-insert |
| `pop_next() -> QueuedMessage` | `_drain_next` (app.py:521) | `_items.pop(0)` |
| `remove(id)` | `action_remove_queued` (app.py:570) | filter the item out |
| `take(id) -> QueuedMessage \| None` | `action_edit_queued` (app.py:578) | find + remove + return; `None` if absent |
| `items` (read), `__bool__` | the `if self._queue` / render calls | read access for rendering |

`paused` is a plain attribute the App flips. The App keeps `_render_queue`
(queries `QueuePanel`), `_drain_next` / `action_run_queued` / `action_*`
(worker + widget effects), and the whole `_after_turn` orchestration — they call
into the queue, then `_render_queue()`. The panel is fed `queue.items,
paused=queue.paused`.

**Subtlety to preserve:** `_after_turn` re-inserts leftover buffered steers at
the **front** of the queue (so they run next), even on a paused finish. That is
why `prepend` exists separately from `enqueue`; `reversed()` iteration order in
the current loop must be preserved so multiple leftovers keep their order.

### 2. `FinishedJobNotifier` — small but clean extraction

Owns `notified: set[str]`. One pure method:

- `newly_finished(jobs) -> list[Job]` — lifted from `_notify_finished_jobs`
  (app.py:314): returns each job whose status is `done`/`failed` and whose id is
  not yet in `notified`, adding those ids to the set. Cancelled jobs are skipped
  (agent-initiated or shutdown teardown — a ping would be noise).

The App's `_notify_finished_jobs` shrinks to a loop over the returned list
calling `self._notify(...)` — the off-event-loop desktop notification effect,
which stays on the App. The dedup *decision* becomes testable without a
notifier daemon.

Home: a new `notify.py`, a small standalone class kept for symmetry with
`WakeController`. Not folded into `wake.py` — wake *policy* and notification
*dedup* are separate concerns that never share state.

### 3. Sub-agent viewer — thinnest extraction (scoped down)

The viewer's 8 methods are ~90% Textual widget show/hide/focus/mount that
**cannot** leave the App. Only two clean pieces are extracted:

- **`spend_tag(tokens, max_ctx) -> str`** — a free function lifted from
  `_subagent_spend` (app.py:449): empty when `tokens` is falsy; `{human_tokens}`
  with no `max_ctx`; `{human_tokens} ({pct}%)` when `max_ctx` is known. Pure
  formatting, trivially testable.
- **`SubAgentViewer`** — a tiny value object owning `open: bool` and
  `index: int`, with `clamp(count)` (`max(0, min(index, count-1))`),
  `prev()`, and `next()`. The App's `action_subagent_prev/next`,
  `_open_subagents`, `_close_subagents`, and `_apply_subagent_view` keep all the
  widget effects (`display`, `add_class("viewing")`, focus, `stream.viewing_sid`,
  `flush_streams`) and call the viewer for index state.

This step is mostly cosmetic symmetry plus a genuinely testable `spend_tag`. It
is included but explicitly the lowest-value of the three.

## What is explicitly NOT changing

- Command autocomplete routing (9 input-handler methods) — Textual message glue;
  belongs on the App.
- Prompt-input submit/steer/slash handlers — same.
- Turn driving, approvals, session/conversation lifecycle, compaction, modals —
  already delegated or legitimately App-owned.
- No behavior changes anywhere. This is a pure structural refactor.

## Data flow (unchanged externally)

User submit / steer / job-completion callbacks fire on the App as today. The App
now delegates the *state* mutation to the new objects and performs the same I/O
effects (panel repaint, worker spawn, desktop notify) it always did. No new
async paths, no new threads, no new Textual messages.

## Error handling

No new failure modes. The existing `_after_turn` guard (pause the queue and
surface "failed to start next turn" on exception) is preserved verbatim — only
the queue mutation inside it moves behind `TurnQueue`. The new objects are pure
data structures and raise nothing the App doesn't already handle.

## Testing

New unit tests (no App harness — Textual-free objects):

- `TurnQueue`: enqueue ordering; `prepend` front-insert preserves multi-leftover
  order; `pop_next`; `remove`/`take` by id; `take` of an absent id → `None`;
  `_seq` monotonic across enqueue + prepend.
- `FinishedJobNotifier`: `newly_finished` yields each done/failed job exactly
  once across repeated calls; cancelled jobs never appear.
- `spend_tag`: the three branches (no tokens / no max_ctx / both).
- `SubAgentViewer`: clamp into range; clamp after a shrunk count; prev/next at
  bounds.

Regression net: existing App-level tests (`test_app_decomposition.py`, queue and
sub-agent tests) stay green **untouched** — proof the extraction is
behavior-preserving.

## Sequencing

Strict TDD, smallest blast radius first; each step an independent commit gated by
`ruff check` → `pyright` → `pytest` (CI order from CLAUDE.md):

1. `TurnQueue` — tests, extract, rewire `_enqueue` / `_drain_next` /
   `_after_turn` / `action_*`, green.
2. `FinishedJobNotifier` — tests, extract, shrink `_notify_finished_jobs`, green.
3. `spend_tag` + `SubAgentViewer` — tests, extract, rewire `action_subagent_*`
   and `_apply_subagent_view` index handling, green.

## Outcome

`HarnessApp` ends ~30–40 lines lighter with four pieces of logic (queue ops,
job-notify dedup, spend formatting, viewer index) now testable in isolation,
following the same pattern as the collaborators already extracted. It remains a
coordinator — by design — but stops carrying unrelated state machines inline.
