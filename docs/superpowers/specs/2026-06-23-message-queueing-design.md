# Message Queueing — Design

**Date:** 2026-06-23
**Status:** Approved (pending user spec review)
**Scope:** The editable, visible message **queue** only. Message **steering** (injecting into a running turn) is a separate, later spec — see "Out of Scope / Future" below.

## Goal

Let the user submit a message while a turn is already running. Instead of being blocked or cancelling the running turn, the message is buffered into a visible, editable FIFO queue and run automatically — as its own turn — once the current turn finishes cleanly. Queued items can be removed or edited before they run.

## Background (current behavior)

- The TUI submits a prompt in `interfaces/tui/app.py` via `on_prompt_input_submitted`, which mounts a `UserMessage` to the log and schedules `_run_turn(text, attachments)` through Textual's `self.run_worker(..., exclusive=True)`, storing the handle in `self._turn_worker`.
- `Harness.run_turn(text, event_stream_handler=..., attachments=...)` (agent.py) drives one turn to completion. **No harness change is needed for queueing** — the queue simply calls `run_turn` again for the next item.
- Esc → `action_cancel_turn()` → `self._turn_worker.cancel()`; the harness's `_flush_resumable` repairs dangling tool calls and persists, leaving the session resumable.
- After a turn, `_run_turn`'s `finally` calls `_maybe_wake()`, which can auto-spawn a follow-up empty-prompt worker when background jobs finished. This is the precedent the queue's drain step mirrors.
- There is **no** existing queue/pending-input mechanism today.

## Architecture

Everything lives in the TUI layer (`interfaces/tui/`). `Harness`, `Deps`, and the run loop are untouched.

### Components

- **`QueuedMessage`** — dataclass with `text: str`, `attachments: Optional[list[tuple[bytes, str]]]`, and a stable `id: str` (for the panel's remove/edit targeting). Defined in a new `interfaces/tui/queue.py`.
- **`HarnessApp` state** — `self._queue: list[QueuedMessage]` (single source of truth) and `self._queue_paused: bool`.
- **Queue panel widget** — a new widget under `interfaces/tui/widgets/` rendering the pending list with per-item `[edit]` / `[x]` affordances, modeled on the existing tasks/jobs panels (which render live lists). Shows a "paused" indicator when `_queue_paused` is set, and an attachment indicator (e.g. `📎 2`) for items carrying attachments.
- **`_start_turn(text, attachments)` helper** — extracted from the current submit handler. Mounts the `UserMessage` to the log and spawns the exclusive worker. Shared by a fresh submit and a drained queue item, so the drained item is mounted to the log identically to a typed one. (Today the submit handler mounts the message inline; this extraction lets the drain path reuse it.)
- **"Run queued" action + keybinding** — resumes a paused queue by clearing `_queue_paused` and draining the next item.

`Alt+Enter` (steer) is **reserved but not wired** in this spec.

## Data Flow

### Submit (Enter)
`on_prompt_input_submitted` runs the existing empty/whitespace validation, then branches on whether a turn is running (`self._turn_worker is not None`):
- **Idle** → `self._start_turn(text, attachments)` (today's behavior, via the extracted helper).
- **Busy** → append `QueuedMessage(text, attachments)` to `self._queue` and refresh the panel. The message is **not** mounted to the main log — it appears only in the pending panel until it runs.

### Drain
In `_run_turn`'s `finally`, after `self._turn_worker = None`, on a **clean** completion:
1. If `not self._queue_paused` and `self._queue` is non-empty → pop the first item and `self._start_turn(item.text, item.attachments)`.
2. Else fall through to the existing `_maybe_wake()`. **Queue drain takes priority over wake; wake fires only when the queue is empty.**

Items run strictly FIFO, each as its own independent `run_turn` call and history entry, never concurrently — the `exclusive=True` worker invariant holds because the next worker is spawned only after the previous finished.

### Pause / Resume
On cancel (`CancelledError`) or any turn error, set `self._queue_paused = True` and skip draining. Queued items stay intact; the panel shows "paused". The **run-queued action** clears the flag and drains the next item (same path as Drain step 1). A deliberate Esc or a crash never cascades into queued turns.

### Edit / Remove
- `[edit]` removes the item from `self._queue` and loads its text into the `PromptInput` (reusing the full editor). Re-submitting while still busy re-queues it — **appended at the end** (original position is not preserved; accepted behavior).
- `[x]` removes the item; it never runs.

## Error Handling & Edge Cases

- **Cancel/error → pause, not drop.** `_queue_paused` is the single gate the drain step checks.
- **Empty/whitespace queued text:** the existing submit guard rejects empty input before enqueue, so empty items never enter the queue.
- **Edit while paused:** allowed. Pop to input, edit; re-submit re-queues if busy, or runs immediately via `_start_turn` if idle.
- **Queue emptied by removals mid-turn:** drain finds an empty queue and falls through to `_maybe_wake()` — no special case.
- **App teardown / session switch / `/clear` with a non-empty queue:** the queue is **in-memory and process-scoped** (like `jobs`) and is **dropped** — not persisted across sessions. A one-line notice is shown if quitting with pending items, but it does not block.
- **Autonomous-wake depth cap:** a queued drain is user-initiated work and does **not** increment `_auto_turn_depth`; only wake-spawned turns do.
- **Attachments:** carried through `QueuedMessage.attachments` into `run_turn` unchanged.

## Testing

TUI-level via Textual's `Pilot` harness, following existing `test_app.py` patterns (Deps fixture + fake/echo model). Pure-logic helpers (`QueuedMessage`, queue mutation) get plain unit tests.

- **Enqueue while busy:** with a turn running, a second submit appends to `_queue` and does not start a second worker (assert `_turn_worker` unchanged, queue length 1, message in panel not main log).
- **FIFO drain after clean completion:** two messages queued during a turn run in order after it, each its own turn, never concurrently (second starts only after first finishes).
- **Pause on cancel:** cancelling a running turn with items queued sets `_queue_paused`, keeps the queue, and does not auto-drain.
- **Pause on error:** a turn that raises (use `_fail_once_then_echo_model` or equivalent) → same pause behavior.
- **Resume drains:** after a pause, the run-queued action clears the flag and runs the next item.
- **Remove:** `[x]` drops a pending item; it never runs.
- **Edit pops to input:** `[edit]` removes the item from the queue and populates `PromptInput` with its text.
- **Idle submit unchanged:** submitting with no turn running still runs immediately via `_start_turn` (regression guard on the extraction).
- **Wake interaction:** queue drain takes priority over `_maybe_wake`; a drained item does not increment `_auto_turn_depth`.

## Out of Scope / Future

- **Message steering** (inject a message into the *running* turn, via `Alt+Enter`) — its own brainstorm + spec. Two candidate mechanisms already identified: (a) interrupt-and-redirect (cancel + resume with the message, reusing existing machinery) vs. (b) cooperative injection via rewriting the run loop from `agent.run()` to `agent.iter()`. Deferred.
- **Persisting the queue** across sessions — deliberately dropped for now.
- **Headless support** — N/A; queueing is an interactive-TUI concern (headless has no mid-turn input).
