# Serve-mode Autonomous Wake — Design

**Date:** 2026-07-24
**Status:** Approved (brainstorm), pending implementation plan
**Repo:** marim-harness (server-only; mobile contract forward-compatible, no mobile change)

## Problem

A background job (a detached `bash` command or a spawned sub-agent) settles
asynchronously via `JobRegistry`. When it finishes, its result is queued as a
next-turn *digest* (`JobRegistry.take_finished_digest`, consumed inside
`controller._assemble_prompt`). For the model to actually *react* to that
result without the user prompting again, something must fire a **digest-only
autonomous turn** when the job settles and the session is idle.

Today that "wake" logic exists **only in the interactive TUI**
(`interfaces/tui/wake.py::WakeController` + `interfaces/tui/app.py::_maybe_wake`).
Under `marim serve` — the transport the Android app uses — `SessionHost`'s
`on_jobs_changed` callback does exactly one thing: `_publish("jobs.changed", {})`.
It never checks `has_finished_pending()` and never enqueues a turn.

**Result:** in serve mode a finished background agent never wakes the session.
The digest is not lost — it is prepended on the *next user-driven turn*
(`controller._assemble_prompt`, shared runtime) — but the session sits idle until
the user sends another message. The agent's spawn-then-end-turn pattern (spawn a
sub-agent, end the turn expecting the report to come back and be synthesized
autonomously) works in the TUI and stalls under serve.

## The two trigger points (correctness detail)

The TUI calls `_maybe_wake()` at **two** moments, and both are necessary:

1. **On job settle** (`_on_jobs_changed`, app.py:387).
2. **On turn finished** (app.py:605).

The second matters: if a job settles *while a turn is still running*,
`should_wake` returns false (turn busy) and the pending digest would never fire a
wake without a re-check when that turn ends. The server has **neither** trigger
today. The shared driver centralizes both.

## Goals

- In serve mode, a settled background job fires exactly one digest-only
  autonomous turn when the session is idle, honoring the existing
  `autonomous_wake` toggle, `wake_depth_cap`, and the "wake once after a whole
  fan-out settles" semantics.
- Extract the wake orchestration into a single shared path both the TUI and
  `SessionHost` consume — no second copy of the decide-and-enqueue effect.
- Emit a wire marker so a WebSocket client can distinguish an autonomous
  (job-completion) turn from a user-initiated one. No mobile UI work in this
  slice; the contract is additive and forward-compatible.

## Non-goals

- No mobile UI change (labeling the autonomous turn is a later slice; the wire
  marker exists so it needs no further server change).
- No new server API to toggle wake at runtime (the TUI's `/jobs wake on|off`
  stays TUI-only). Serve honors the config/env default.
- No change to the digest content, job lifecycle, or `WakeController` policy.

## Architecture

### 1. Shared policy + orchestrator — the single path

**Move** `interfaces/tui/wake.py` → `runtime/wake.py`. `WakeController` is
already Textual-free; relocating it makes it legitimately shared runtime rather
than something `runtime`/`server` reach into under `interfaces/`. Update the
two TUI import sites.

`WakeController` is unchanged — it remains the pure policy: the depth counter
and `should_wake(enabled, turn_busy, has_finished_pending, all_jobs_settled)`.

**New** `runtime/wake_driver.py`:

```python
class WakeDriver:
    """Owns the *effect* both interfaces duplicate: run the wake policy on a
    settle/turn-end signal and, if it fires, enqueue one digest-only turn.
    Textual-free and server-free — predicates and the enqueue are injected."""

    def __init__(
        self,
        controller: WakeController,
        *,
        is_enabled: Callable[[], bool],
        turn_busy: Callable[[], bool],
        has_finished_pending: Callable[[], bool],
        all_jobs_settled: Callable[[], bool],
        enqueue_digest_turn: Callable[[], None],
    ) -> None: ...

    def maybe_wake(self) -> bool:
        """Single decision point. Called on BOTH job-settle and turn-finished.
        Returns True iff it enqueued an autonomous turn."""
        if self._controller.should_wake(
            enabled=self._is_enabled(),
            turn_busy=self._turn_busy(),
            has_finished_pending=self._has_finished_pending(),
            all_jobs_settled=self._all_jobs_settled(),
        ):
            self._controller.record_auto_turn()
            self._enqueue_digest_turn()
            return True
        return False

    def note_user_turn(self) -> None:
        """Reset the depth chain — call when a USER-initiated turn is submitted."""
        self._controller.reset()
```

Rationale for the split: `WakeController` stays a pure, already-tested predicate
object; `WakeDriver` is the thin orchestrator that carries the two-trigger
discipline and the enqueue, injected per consumer. One decision path, two
adapters.

### 2. Server wiring (`SessionHost`)

- Construct `self._wake = WakeDriver(WakeController(harness.wake_depth_cap), ...)`
  with predicates:
  - `is_enabled` → `lambda: self.harness.autonomous_wake`
  - `turn_busy` → `lambda: self.status != "idle"`. **Not** `== "running"`: a turn
    parked on an ask reports `status == "waiting_ask"` while its task is still
    live (`_turn_task is not None`), so `!= "idle"` is the correct "a turn is in
    flight" predicate — `== "running"` would wrongly queue a wake turn behind a
    parked one.
  - `has_finished_pending` → `self.harness.deps.jobs.has_finished_pending`
  - `all_jobs_settled` → `lambda: not self.harness.deps.jobs.any_running()`
  - `enqueue_digest_turn` → `self._enqueue_autonomous_turn`
- `_on_jobs_changed`: keep `self._publish("jobs.changed", {})`, then
  `self._wake.maybe_wake()`.  *(Trigger 1: job settle.)*
- `_worker_loop` `finally` block (a turn just ended, `_turn_task` cleared,
  `_idle_since` reset): call `self._wake.maybe_wake()`.  *(Trigger 2: turn end.)*
- `submit(prompt, attachments)` (the user path): call
  `self._wake.note_user_turn()` inside `submit()` so a user turn resets the depth
  chain (order relative to the enqueue is irrelevant — `reset()` only zeroes the
  counter).
- **New** `_enqueue_autonomous_turn()`:
  ```python
  def _enqueue_autonomous_turn(self) -> None:
      turn_id = secrets.token_hex(8)
      try:
          self._queue.put_nowait((turn_id, "", None, "autonomous"))
      except asyncio.QueueFull:
          pass  # best-effort: a wake is never worth raising into a job callback
  ```
  The empty prompt makes it a digest-only turn (`_assemble_prompt("")` still
  prepends `take_finished_digest()`).

### 3. Turn tuple + WS marker

The queue item today is `(turn_id, prompt, attachments)`. Extend it to
`(turn_id, prompt, attachments, trigger)` where `trigger ∈ {"user","autonomous"}`.

- `submit()` enqueues `trigger="user"`.
- `_enqueue_autonomous_turn()` enqueues `trigger="autonomous"`.
- `_worker_loop` unpacks the 4-tuple and passes `trigger` to `_run_one_turn`.
- `_run_one_turn` publishes `turn.started` with the marker:
  ```python
  self.bus.publish("turn.started", {"turn_id": turn_id, "prompt": prompt,
                                    "trigger": trigger})
  ```

The marker is **additive**. The mobile client parses wire JSON with
`ignoreUnknownKeys`, so an unrecognized `trigger` field is tolerated with zero
change. A future client can render "⏰ Resumed — background job finished" off
`trigger == "autonomous"` without any further server work.

### 4. TUI migration (behavior-preserving)

Replace `app.py`'s `WakeController` field and the body of `_maybe_wake()` with
the shared `WakeDriver`:

- `is_enabled` → `lambda: self.autonomous_wake` (the runtime `/jobs wake on|off`
  flag).
- `turn_busy` → `lambda: self.turn_busy`.
- `has_finished_pending` / `all_jobs_settled` → the same `jobs` predicates.
- `enqueue_digest_turn` → the existing effect, wrapped:
  ```python
  def _mount_wake_turn(self) -> None:
      self._append_log(NoticeMessage("⏰ Resumed — background job(s) finished"))
      self._turn_worker = self.run_worker(self._run_turn(""), exclusive=True)
  ```
- The `is_running` guard currently at the top of `_maybe_wake` stays in the TUI
  adapter (it is a Textual-mount concern, not policy): the TUI wraps
  `self._wake.maybe_wake()` behind `if self.is_running`.
- `_on_jobs_changed` and the turn-end path call `self._wake.maybe_wake()`.
- The user-turn reset (previously `self._wake.reset()` at app.py:525) becomes
  `self._wake.note_user_turn()`.

Behavior is unchanged; the existing wake unit tests and app tests are the guard.

### 5. Config / safety (unchanged semantics)

- Serve honors existing `autonomous_wake` (default `True`) and `wake_depth_cap`
  (default `8`), already surfaced on `Harness` (`harness.py:533-534`).
- The all-jobs-settled guard (an N-way detached fan-out wakes once, after the
  whole batch) and the depth cap (bounds runaway wake→spawn→wake chains) are
  preserved by reusing `WakeController` verbatim.
- `note_user_turn()` resets the chain on every user turn, exactly as the TUI's
  `reset()` does today.

## Data flow (serve mode)

```
sub-agent job settles
  → JobRegistry._settle → on_change()
    → SessionHost._on_jobs_changed
      → _publish("jobs.changed", {})          (existing: pokes the jobs view)
      → _wake.maybe_wake()                     (NEW)
        → should_wake? (idle, pending, all settled, under cap, enabled)
          → yes → _enqueue_autonomous_turn()   → _queue: (tid,"",None,"autonomous")
            → _worker_loop → _run_one_turn
              → publish turn.started {trigger:"autonomous"}
              → _assemble_prompt("") prepends take_finished_digest()
              → model synthesizes the result
              → turn.finished
                → finally: _wake.maybe_wake()  (NEW trigger 2: catch a digest
                                                 that arrived mid-turn)
```

## Testing

**`WakeDriver` unit tests** (no Textual, no server — pure injected predicates):
- fires `enqueue` exactly once when the policy says wake;
- suppresses when disabled / turn busy / a job still running / cap reached /
  no pending digest;
- `note_user_turn()` resets depth so a fresh chain can wake again;
- `maybe_wake()` returns the boolean it acted on.

**Server-host tests** (`test_server_host` / `test_server_http`):
- settle a background job on an *idle* host → an autonomous turn is enqueued and
  its `turn.started` carries `trigger == "autonomous"`;
- settle a job *while a turn runs* → no wake during that turn; when the turn ends
  the turn-end trigger fires exactly one wake;
- a user `submit()` carries `trigger == "user"`;
- depth cap: a wake→spawn→wake chain halts at `wake_depth_cap`.

**TUI**: the existing wake suite (`WakeController` tests + any app-level wake
tests) must stay green — the migration's guard. No new TUI assertions required.

**Gates**: `ruff check`; `uv run pytest -q` (≥90% coverage, enforced).

## Files

- Move: `src/marim_harness/interfaces/tui/wake.py` → `src/marim_harness/runtime/wake.py`
  (+ fix imports in `interfaces/tui/app.py`, `interfaces/tui/settings.py`,
  `interfaces/tui/commands.py`, and any test importing it).
- Create: `src/marim_harness/runtime/wake_driver.py`
- Create: `tests/test_wake_driver.py`
- Modify: `src/marim_harness/server/host.py` (driver field, two triggers,
  `_enqueue_autonomous_turn`, 4-tuple queue, `turn.started` marker,
  `note_user_turn` in `submit`).
- Modify: `src/marim_harness/interfaces/tui/app.py` (migrate onto `WakeDriver`).
- Modify: `tests/test_server_host.py` / `tests/test_server_http.py` (wake coverage).
