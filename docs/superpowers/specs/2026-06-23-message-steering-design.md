# Message Steering — Design

**Date:** 2026-06-23
**Status:** Approved (pending user re-review — simplified after a feasibility spike)
**Scope:** Mid-turn message **steering** — injecting a user message (with optional attachments) into a turn that is already running so the agent adjusts course without cancelling and losing in-flight work. Builds on the message **queue** (already shipped); reuses the queue as the fallback for one edge case.

## Goal

Let the user, while a turn is running, press `Alt+Enter` to inject their typed message (and any attachments) into the *current* turn. The message reaches the model at the next request boundary — losslessly: a mid-flight tool finishes, nothing is cancelled or re-run. The agent sees the steer and adjusts.

## Background (verified against pydantic-ai 1.107.0, with spikes)

- The harness drives a turn in `Harness._run_with_approval` (`src/marim_harness/agent.py`) via `await self.agent.run(...)` per approval round, passing `event_stream_handler=` for live rendering, wrapped in `capture_run_messages()` with `_flush_resumable` recovery, `DeferredToolRequests` approval reruns, history/usage updates, and persistence.
- `event_stream_handler` is invoked as `handler(run_ctx, events)`, where `run_ctx` is a `RunContext`. The harness already wraps the TUI's handler in `_build_hooked_handler` (to fire Pre/PostToolUse hooks on tool events).
- pydantic-ai exposes **`RunContext.enqueue(*content, priority='asap')`** — documented "safe to call from anywhere a `RunContext` is available." `'asap'` content is drained in `before_model_request` and prepended to the next model request. `EnqueueContent` accepts `UserContent`, so a `str` plus `BinaryContent` items coalesce into one `UserPromptPart`.
- **Feasibility spike (throwaway, decisive):** with plain `agent.run(event_stream_handler=handler)`, capturing the `run_ctx` the handler receives and calling `run_ctx.enqueue("STEER", BinaryContent(...), priority='asap')` mid-run (from a concurrent task) injected the steer — text **and** image — into the next model request. **No `agent.iter()` rewrite is needed.** `agent.run()` already drives the capability-hooked loop that drains the enqueued content.

This supersedes an earlier draft of this design that planned to rewrite `_run_with_approval` over `agent.iter()`. That rewrite is unnecessary: the supported `RunContext.enqueue` path works through the existing `agent.run()` call, so the harness's most delicate code is left untouched.

## Architecture

**No change to the run loop.** `_run_with_approval` keeps calling `agent.run(...)` exactly as today. Steering is additive:

1. **Capture the live `RunContext`.** In `_build_hooked_handler`'s wrapper (which already intercepts the event stream), stash the `run_ctx` it receives onto the harness: `self._active_run_ctx = stream_ctx`. It is refreshed on every streamed node, so it stays current within a run. Cleared to `None` in `run_turn`'s `finally` (turn over → no live ctx).
2. **`Harness.steer(text, attachments=None)`** (`attachments: Optional[list[tuple[bytes, str]]]`): append `(text, attachments)` to `self._steer_buffer`; if `self._active_run_ctx is not None`, flush the buffer immediately — for each `(t, atts)`, call `self._active_run_ctx.enqueue(t, *(BinaryContent(data=d, media_type=m) for d, m in (atts or [])), priority='asap')` — then clear the buffer.
3. **TUI trigger:** an `Alt+Enter` action calls `Harness.steer`.

**Buffer rationale:** in `ask` mode a turn spans multiple `agent.run()` calls (one per approval round); between rounds there is briefly no live `run_ctx`. Buffering lets a steer typed during an approval prompt apply to the next round's run (its handler re-captures a fresh `run_ctx`, then the loop/flush delivers the buffered steer). The buffer is cleared as it flushes.

**Loop safety:** the `Alt+Enter` action and the turn worker share the app's event loop, so `enqueue` is an on-loop call (no `call_soon_threadsafe`). `RunContext.enqueue` is explicitly documented safe to call from such contexts.

**Scope:** a `run_ctx` capture in the existing handler wrapper + `Harness.steer` + a thin TUI action. No `Deps` shape change, no run-loop rewrite.

## Data Flow

### Trigger (TUI)
`Alt+Enter` with text and/or attachments. The app branches on its busy signal (`_turn_worker is not None`):
- **Turn in progress** → `self.harness.steer(text, attachments)`; mount a visible steer marker in the log (`↪ <text>` + `📎 N` when attachments present); clear the input. Not queued; starts no turn.
- **Idle** → fall back to `_start_turn(text, attachments)` (run normally).
- **Empty text AND no attachments** → no-op. (Image-only steer with empty text is valid.)
- The existing `_image_block_reason(attachments)` check runs before a steer with attachments, so images aren't injected into a text-only model.

### Injection
`steer()` enqueues on the live `run_ctx` (or buffers, then flushes when the next round captures a ctx). pydantic-ai drains `'asap'` content before the next model request → the model sees the steer at the next request boundary. Lossless: an in-flight tool finishes; nothing is cancelled or re-run. Multiple `Alt+Enter`s enqueue multiple messages, drained in order.

### Stranded-steer fallback (finishing-gap race)
If `steer()` is called in the tiny window where the turn is finishing (busy signal still set, but the run ends before the buffer flushes), the leftover buffered steer(s) are kept at the **front of the message queue** on ANY finish — clean or paused (cancel/error) — so no input is ever lost. They run as the very next turn: immediately on a clean finish, or on resume after a paused (cancel/error) finish.

### Interactions
Steering never touches the message queue directly (Enter still queues/runs; Esc still cancels). After a steered turn completes, the queue drains normally. Steering composes with the queue only via the stranded-steer fallback above.

## Error Handling & Edge Cases

- **`_active_run_ctx` lifecycle:** refreshed on every streamed node; cleared in `run_turn`'s `finally` so it is `None` on exception/cancel/normal-exit — a steer can never enqueue into a finished run. Single-threaded loop → no await-gap concerns.
- **Cancel (Esc) mid-steer:** unchanged — `_flush_resumable`/`_repair_unanswered_tool_calls` run as today; a steer already folded into history stays; an un-flushed buffer is cleared.
- **Turn error:** caught as today; session preserved; buffer cleared.
- **`enqueue` near End:** a steer landing as the run ends triggers pydantic-ai's redirect (terminal `End` → one more model request), so the steer still gets a response. Benign.
- **Plan/auto/ask modes:** steering works in all three (it injects a user message; tool denial still governs side-effects). The buffer covers `ask`'s between-round gaps.

## Testing

**Harness unit/integration (`tests/test_steering.py`):**
- `steer(text)` with `_active_run_ctx` set (a fake `RunContext` with an `enqueue` spy) → `enqueue(text, priority='asap')` called.
- `steer(text, attachments=[(b"..","image/png")])` → `enqueue` called with `text` + a `BinaryContent`.
- `steer()` with no active ctx → buffers (`_steer_buffer` holds it; enqueue not called); flushes when a ctx is next captured.
- **End-to-end injection** (the real proof, mirrors the spike): drive a real turn through the harness with a streaming `FunctionModel` that records received messages and emits a tool call on the first request; call `harness.steer("STEER")` mid-run (concurrently); assert a later request's messages contain the steer text (and a `BinaryContent` for the attachment variant).

**TUI Pilot:**
- `Alt+Enter` with a turn running → calls `harness.steer`, mounts the marker, starts no worker, adds nothing to the queue.
- `Alt+Enter` idle → runs normally via `_start_turn`.
- `Alt+Enter` empty text + no attachments → no-op; empty text + attachments → steers.
- Stranded steer → lands at the front of the message queue (assert order).

**Regression:** the existing suite must stay green. Because the run loop is unchanged, the only harness change is the additive `run_ctx` capture in the handler wrapper + the clear in `finally`; tests confirm streaming/rendering behavior is unaffected.

## Out of Scope / Future

- **`agent.iter()` run-loop rewrite** — considered and rejected: the spike showed `RunContext.enqueue` works through the existing `agent.run()` call, so the rewrite's risk isn't warranted.
- **Interrupt-and-redirect** (cancel + resume with the steer) — the other rejected alternative; the lossless `enqueue` path was chosen.
- **`'when_idle'` priority** — only `'asap'` is used (steer should reach the next request, not wait for idle).
- **Headless support** — N/A; steering is an interactive-TUI concern.
