# Phase 3b — Turn completion inverts: the TUI submits to the host and reacts to events

**Status:** design, 2026-09-12. Parent: `2026-08-29-cross-process-session-events-design.md`
(§1 "Turn completion inverts", §3 "Asks and co-driving"). Predecessor:
`2026-09-01-tui-events-3a-design.md` (merged as #116, f4c0ad5d), whose
"Non-goals (all 3b or later)" list is this spec's scope.

## Context

After 3a the TUI renders every event from the bus, but it still *drives* the
turn the old way: `HarnessApp._run_turn` is an exclusive Textual worker that
awaits the provisional `SessionHost.run_turn`, and everything that happens at
the end of a turn — the elapsed stamp, the error card, pausing the queue, the
settle sweep, the wake chain — lives in that worker's
`try/except CancelledError/except Exception/finally`. The host publishes
`turn.finished`/`turn.error`/`session.status` for those same moments, and the
TUI ignores all three (`_WIRE_HANDLERS` comment: "belong to 3b").

Two things fall out of that. The TUI has to `_drain_pump` after the await
because the return value races the rendering it is supposed to follow. And a
turn started anywhere else (a second client, phase 4) has no worker in this
process, so none of the completion effects would run for it.

3b inverts control: the TUI **submits** a turn to the host and **reacts** to the
turn's lifecycle events like any other client. `SessionHost.run_turn` is
deleted; the host's queue worker is the only thing that calls
`Harness.run_turn` in-process, exactly as in the daemon.

Per the parent spec: **no compat flag.** The worker path is deleted, not gated.

## Goals

- The TUI drives turns through `host.submit()` and the host's queue worker;
  every turn-end effect runs from a `turn.finished` / `turn.error` /
  `session.status` handler, in bus order. `_drain_pump` and its seq barrier go
  away because the ordering problem they patched no longer exists.
- `HarnessApp.turn_busy` — the single guard against a second turn, a session
  switch, `/compact`, `/model`, `!` — is derived from the wire plus a local
  submitted-latch, the same inputs a remote client will have.
- Esc maps to `host.interrupt()`; the cancelled turn's UX runs from the
  interrupted `turn.finished`. A remote interrupt renders identically.
- An ask answered by *another* client dismisses this TUI's panel with a
  one-line notice (parent §3 step 3).
- The TUI queue stays a pre-submit staging area (edit / remove / pause-on-error)
  feeding the host one turn at a time.
- TUI teardown stops reaching into `host._worker`; the host gains a proper
  "stop the worker" method that both teardowns share.

## Non-goals (phase 4 or later)

- `RemoteSessionHost`, WebSocket-fed `Subscription`, daemon-owned sessions in
  the session picker, attach-time reconciliation (`GET .../asks`, `?after_seq`).
- Image sha references on `tool.result` (still additive, still remote-client
  work).
- Unifying TUI exit with `SessionHost.aclose()`. The TUI keeps its snappy exit
  (cancel autoname, persist, summary line); only the worker-stop half is shared.
- Moving autonomous wake onto the host's `WakeDriver`. It *would* be a
  simplification, but the host's turn-end trigger fires in its worker's
  `finally`, before the TUI's idle handler can drain a staged message — a wake
  digest turn would jump ahead of a message the user already typed. Today the
  user's message goes first and the digest rides its `<turn-context>`. That
  priority is kept; the driver stays in `ActivityMonitor`, submitting through
  the host. Phase 4 has to decide this for the remote case anyway.
- Publishing an approval round's run boundary (accepted residual from #116).
- `/compact`, `/model`, rewind, session switch still call the harness directly,
  guarded by `turn_busy`. They are not turns; phase 4 routes them through the
  command surface it adds.

## Architecture

### The turn lifecycle, as the TUI sees it

| Moment | Today (worker) | 3b (event) |
|---|---|---|
| User presses Enter | mount `UserMessage`; `run_worker(_run_turn)` | `host.submit(text, atts)` → latch `turn_id` |
| Turn begins | `_run_turn` head: `set_busy(True)`, `prune_completed()` | `turn.started`: same, plus mount `UserMessage(prompt)` when `trigger == "user"`, the "⏰ Resumed" notice when `"autonomous"` |
| Turn parks on an ask | (direct) | `session.status: waiting_ask` — no UI change in 3b |
| Turn completes | after the await: `_drain_pump`, `TurnMeta`, notify | `turn.finished` (not interrupted): `end_run()`, `TurnMeta(elapsed)`, notify |
| Esc | worker cancel → `except CancelledError` arm | `host.interrupt()`; `turn.finished{interrupted}`: `end_run()`, "turn cancelled" card, `settle_pending("cancelled")`, `queue.paused = True` |
| Provider error | `except Exception` arm | `turn.error`: `end_run()`, error card, notify, `settle_pending(detail)`, `queue.paused = True` |
| Host goes idle | `finally`: `set_busy(False)`, `CompactNotice` guard, `queue.after_turn()` | `session.status: idle`: same |

The rule: **turn events drive the transcript, status events drive state.**
`turn.finished`/`turn.error` mount things; `session.status` flips `busy`,
drains the staged queue, and runs the turn-end wake check. Splitting them this
way matters because the host publishes `turn.finished` from inside the turn
task and `session.status: idle` from the worker's `finally` — and when another
client has already queued the next turn, that status is `running`, not `idle`,
so the staged queue correctly waits.

`UserMessage` mounts from `turn.started.prompt` rather than at submit time so a
turn submitted by another client (phase 4) echoes here too. Locally it costs
three loop hops — imperceptible, but tests must wait for it.

### `TurnTracker` — the busy signal

A small pure object (`interfaces/tui/turn_state.py`, no Textual, no asyncio)
replacing `_turn_worker` + `_turn_starting`:

```python
class TurnTracker:
    submitted: str | None   # turn_id we submitted, no turn.started yet
    current: str | None     # turn_id of the running turn (any client's)
    status: str             # last session.status seen; "idle" initially

    def note_submitted(self, turn_id) -> None
    def on_started(self, turn_id) -> None      # current = id; clears submitted if it matches
    def on_status(self, status) -> Transition  # returns BECAME_IDLE / BECAME_BUSY / NONE
    @property
    def busy(self) -> bool:                    # submitted or current or status != "idle"
```

`submitted` is the phase-4 pattern: a remote `POST prompt` returns `202` with
the `turn_id` before the WebSocket delivers `turn.started`, and the client must
read busy in between. In-process it closes the same gap `_turn_starting` did.
`current` is cleared only by `session.status: idle`, never by `turn.finished`:
between the two the host's `_turn_task` is still set and an Esc must still
route to `interrupt()`. `Transition` lets the app run idle-edge effects once,
not on every `running → waiting_ask → running` bounce (which would reset the
status-bar timer).

`HarnessApp.turn_busy` becomes `self.turns.busy`. Every consumer
(`_route_submission`, `start_system_turn`, `_refuse_if_session_busy`,
`_cmd_compact`, `_cmd_model`, `_handle_bang`, `QueueController.resume`,
`ActivityMonitor.wake`) is unchanged at the call site.

### Submitting

`SessionHost.submit` grows a trigger:

```python
def submit(self, prompt, attachments=None, *, trigger: str = "user") -> str
```

`trigger ∈ {"user", "system", "autonomous"}`; `note_user_turn()` fires only for
`"user"` (a `/remember` prompt must not reset the wake chain — today's
`start_system_turn` contract). `"autonomous"` from outside is what the TUI's
own wake driver submits (the host's internal `_enqueue_autonomous_turn` keeps
using it too). The HTTP prompt route keeps submitting `"user"`; `turn.started`
already carries `trigger`, so this is a vocabulary widening, not a new event.

- `start_turn(text, atts)` → `activity.note_user_turn()`; `turn_id =
  host.submit(...)`; `turns.note_submitted(turn_id)`. No mount, no worker.
- `start_system_turn(prompt)` → refuse if busy (as today); `submit(...,
  trigger="system")`.
- `mount_wake_turn()` → `submit("", trigger="autonomous")`; the notice moves to
  the `turn.started` handler.
- `TurnQueueFull` (only possible when another client filled the host queue —
  phase 4): re-stage at the front, pause, post a notice. `HostClosed`: log,
  drop (we are exiting).

### Esc

`action_cancel_turn`: `if self.turns.busy: self.host.interrupt()`. A turn
submitted but not yet picked up cannot be interrupted (`interrupt()` returns
`False`, `_turn_task` is `None`); the window is one loop iteration in-process
and the turn then runs normally. The 3a `uncancel()` dance is gone with the
worker. `SessionHost.interrupt` returns `task.cancel()`'s result so an Esc
landing after `turn.finished` but before the worker's `finally` reports
`False` rather than claiming an interrupt it didn't perform.

### Steer

`SessionHost.steer(text, attachments=None)` widens to accept attachments and
publishes `steer.accepted {"text", "attachments": <count>}`. The TUI calls
`host.steer` instead of `harness.steer` and renders the "↪ steering" notice
from the event, so a steer from either client is visible in both. The HTTP
route is unchanged (text only).

Undelivered steers (`harness.take_buffered_steers()` at the idle edge, then
`queue.prepend`) stay a direct harness call in `QueueController.after_turn`.
It is the one turn-path harness reach left in the TUI, and it stays because
attachments are bytes: they cannot ride the wire until image references land
(phase 4). Flagged in code as the phase-4 seam.

### Asks: answered elsewhere

`_dismiss_ask` today unmounts on any `ask.resolved`. 3b distinguishes:

| `ask.resolved` shape | Panel state | Effect |
|---|---|---|
| `answer` present | `panel.result.done()` | local answer — silent (today) |
| `answer` present | not done | **another client answered** — unmount + notice ("Approval granted from another client" / "…denied…" / "Question answered from another client" / "Plan decided from another client") |
| `cancelled: true` | either | unmount, silent — the interrupted `turn.finished` card already explains it |

The approval wording reads `answer["approve"]`. No new wire fields.

### Teardown: `SessionHost.stop()`

`on_unmount` cancels `host._worker` directly today, which is wrong once the
worker owns turns: the worker's `except CancelledError` treats a cancel as an
interrupt unless `_closing` is set, swallows it, publishes an interrupted
`turn.finished`, and loops back to `queue.get()` — a cancel that lands while
the worker is awaiting `_turn_task` is eaten, and the second cancel is what
actually ends it. It works today only because the TUI never runs turns
through the worker.

3b factors the first half of `aclose()` out:

```python
async def stop(self) -> None:
    """Interrupt the running turn and stop the queue worker. No persist —
    aclose() layers the guarded teardown on top; the TUI keeps its own."""
    self._closing = True
    if self._turn_task is not None:
        self._turn_task.cancel()
    self._worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await self._worker
```

`aclose()` calls `stop()` then the guarded persist chain. `on_unmount` calls
`stop()` after cancelling the pump, then its own exit sequence unchanged.
`_closing` also disarms the wake triggers, which is what we want on exit.

### What is deleted

`HarnessApp._run_turn`, `_drain_pump`, `_pump_seq`, `_pump_advanced`,
`_turn_worker`, `_turn_starting`; `SessionHost.run_turn` and its four tests;
the `_WIRE_HANDLERS` "belong to 3b" comment. `_event_pump` keeps its loop but
loses the seq bookkeeping.

## Wire changes

Additive only; existing clients are unaffected.

- `turn.started.trigger` gains the value `"system"` (`serve-api.md` table:
  document all three values; the route table currently omits `trigger`).
- `steer.accepted` gains `attachments: int` (count, default 0).
- `SessionHost.submit(trigger=)`, `SessionHost.steer(attachments=)`,
  `SessionHost.stop()` are host API, not wire.

No new event types. `session.status`, `turn.finished`, `turn.error` are
consumed for the first time by the TUI; their shapes don't change.

## Error handling

- A handler raising (`NoMatches` mid-teardown, a widget mount failing) is
  caught and logged by the pump as today; the next event still dispatches. The
  old `finally` had to guard `query_one(CompactNotice)` so `after_turn` was
  reached — now they are separate events and a failure in one cannot strand
  the other. `after_turn` keeps its own try/except (draining starts a turn,
  which can fail).
- `turn.error` for a turn this TUI didn't submit renders the same card; the
  staged queue pauses the same way.
- `host.submit` raising inside `start_turn`: handled per the table above; no
  half-set state because the latch is set only after `submit` returns.
- A `session.status` that never arrives (host worker died) would leave `busy`
  true forever. The worker only dies on `stop()`; `_run_one_turn` swallows
  everything else into `turn.error`. Not guarded further.

## Testing

The blast radius is tests that call `app._run_turn(...)` or monkeypatch
`app.host.run_turn` (~10 sites in `test_app.py`, plus `test_app_present_plan`,
`test_app_turn_race`, `test_app_decomposition`, `test_approval`,
`test_steering`). They migrate to one pattern:

- Drive via `app.start_turn(...)` (or `host.submit`); fake the model, or
  monkeypatch `app.harness.run_turn` — the seam the host actually calls.
- Wait on `await app.turns_idle.wait()` — an `asyncio.Event` on the app, set
  on the idle edge, cleared on submit/`turn.started`. Never `pilot.pause()`
  (parent §6 trap). Timeouts via `wait_for`, and read the pytest-timeout
  stack before guessing.

New coverage:

1. `TurnTracker` unit tests: latch, foreign `turn.started`, bounce without an
   idle edge, idle edge exactly once.
2. Lifecycle from the wire: a scripted host publishes started → deltas →
   finished; assert `UserMessage`, reply, `TurnMeta` land in that order with no
   barrier. Same for `turn.error` and interrupted `turn.finished`.
3. Staged queue waits while `session.status` stays `running` after a
   `turn.finished` (a queued foreign turn) and drains on the idle edge.
4. Esc → `host.interrupt()` → interrupted card + settle + paused queue; Esc in
   the finished-but-not-idle window is a no-op.
5. Ask answered elsewhere: `host.answer_ask` from outside the panel → notice +
   unmount; local answer → silent; interrupt → silent.
6. `SessionHost.stop()`: a cancel landing while a turn is in flight ends the
   worker on the first try; `aclose()` still releases the claim (existing
   test).
7. Trigger plumbing: `system` doesn't reset the wake chain; `turn.started`
   mounts no `UserMessage` for `system`/`autonomous`; the resume notice renders
   from `autonomous`.
8. Live smoke (zen-go `glm-5.2`, ask first): text turn, gated approval, Esc
   mid-stream, `/remember`, steer mid-turn, background job → wake, `/exit`
   mid-turn exits promptly.

## Risks

- **Every turn-end effect changes trigger.** Mitigated by the table above
  being exhaustive against `_run_turn` on disk (b0a6a188): the plan must map
  each line of the old block to a handler, and the review must diff them.
- **Idle-edge ordering with the host's `finally`.** `session.status: idle` is
  the last event of a turn *only if the host queue is empty*. The TUI stages,
  so in-process it always is; the "foreign queued turn" test pins the other
  case for phase 4.
- **Test churn** (the largest item by line count). Contained by the single
  migration pattern; no new fixtures beyond `turns_idle`.
- **Timer reset on ask bounce** — handled by `Transition`; regression test 1.
- **Worker-cancel swallow** — the `stop()` test is the guard; without it the
  TUI could hang at exit with a parked approval.
