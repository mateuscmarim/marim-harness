# Phase 3a — TUI renders from events: in-process host + wire-dict renderer

**Date:** 2026-09-01
**Status:** Approved design (brainstormed 2026-09-01 with Mateus)
**Parent spec:** `docs/superpowers/specs/2026-08-29-cross-process-session-events-design.md` (phase 3)
**Prerequisite (landed):** phase 2 claims (PR #106), claim hygiene (PR #107)
**Branch:** `feat/tui-events-3a`

## Context

Phase 3 of the cross-process session events design makes the TUI an
event-driven client of a `SessionHost`, so the local front-end and a future
remote one run the same render code against the same wire events. The parent
spec scopes phase 3 as one large, high-risk landing. This spec splits it:

- **3a (this spec):** in-process `SessionHost`; renderer converted from
  pydantic-ai objects to wire events; asks ported to
  `ask.pending`/`ask.resolved`. Single-client behavior parity — nothing the
  user can see changes.
- **3b (later spec):** turn-completion inversion (the `app.py` turn worker's
  `try/except/finally` becomes `turn.finished`/`turn.error` handlers,
  including the `CancelledError`/interrupt mapping), busy signal from
  `session.status`, cross-client ask semantics ("answered on another client"
  dismissal), queue staging feeding `host.submit()`.

Rationale for the split: the renderer conversion (1,224 lines changing input
format) and the completion inversion are the two riskiest changes in phase 3;
landing them in separate PRs keeps each failure attributable. The ask
mechanics could not wait for 3b — once the TUI's local `bind_ui` duplicate is
deleted, `SessionHost`'s ask machinery (`ask.pending` → `answer_ask`) is the
only path approvals have.

Per the parent spec: **no compat flag.** The old callback path is deleted in
3a, not gated behind a `MARIM_*` switch — an unexercised fallback rots and
recreates the two-renderer drift this design exists to eliminate.

## Goals

1. `SessionHost` becomes the **only** `bind_ui` consumer. The TUI's local
   `bind_ui` call in `HarnessApp.__init__` is deleted.
2. `stream_render.py` no longer imports `pydantic_ai.messages`; it renders
   from wire events.
3. Approvals, ask-user, and plan cards flow through
   `ask.pending`/`ask.resolved` with `host.answer_ask` as the answer path —
   UX byte-identical to today in the single-client case.
4. Every `Harness.bind_ui` parameter is published by `SessionHost`
   (the spec's headline anti-drift test goes fail → green; 22 parameters,
   8 published today, 14 missing).
5. Full behavioral parity: same rendering, same approval flow, same claim
   lifecycle, same interrupt behavior.

## Non-goals (all 3b or later)

- Turn-completion inversion; `CancelledError` → event mapping.
- Cross-client awareness: answered-elsewhere dismissal notices, `session.status`-driven busy signal.
- `RemoteSessionHost`, WebSocket-fed `Subscription`, daemon-owned sessions in the picker (phase 4).
- Image sha-on-`tool.result` + `GET /images/{sha}` resolution. The parent spec
  (§"Images: reference, not bytes") warned that routing local rendering through
  the wire unchanged would regress TUI image display. That claim does not match
  current master: `stream_render.py` already renders `read_file` tool returns via
  `binary_safe.render_binary_safe` — the same `[image media_type, N KB]`
  placeholder the wire already emits. Prompt-side attachments (`[Image #N]`
  markers in `widgets/prompt.py`) are unrelated to `tool.result` and are
  unchanged. So 3a keeps the placeholder path with **no visible regression**.
  Shipping sha references so either client can show the actual image is still
  the right end-state; it is additive and belongs with remote-client work
  (3b or phase 4), not the renderer conversion.

## Architecture

```
HarnessApp
 ├── harness (built by bootstrap as today; OWNS the session claim)
 ├── bus = EventBus()
 ├── host = SessionHost(harness, bus, claim=None)   ← sole bind_ui consumer
 └── pump task (started on mount)
      └── Subscription → parse_wire_event() → renderers / handlers
```

### In-process host

`HarnessApp` wraps the bootstrap-built harness in an `EventBus` +
`SessionHost` exactly as the daemon's supervisor does, with one difference:
**the host is constructed with `claim=None`.**

Claim ownership stays on the `Harness`, where claim hygiene put it:
`default_cmd` adopts the claim onto the harness, `/sessions` swaps move it via
`Harness.switch_session`, `Harness.aclose()` releases it as the last teardown
action. `SessionHost` *can* own a claim (the daemon's mode — the host releases
it in `aclose()` after the harness closes), but both cannot hold one: flock is
per open-file-description, and a second acquire in the same process is denied
even by the process that already holds the lock. The in-process host is
therefore a pure event adapter. The daemon's host-owned mode is untouched.

### Provisional `SessionHost.run_turn`

`SessionHost` grows one public method for in-process use, mirroring the
harness signature:

```python
async def run_turn(
    self,
    prompt: str,
    attachments: list[tuple[bytes, str]] | None = None,
) -> TurnOutcome: ...
```

It awaits `harness.run_turn` through the same publish wrapper
`_run_one_turn` uses — `turn.started` → stream events →
`turn.finished`/`turn.error` on the bus — but bypasses the host's internal
queue. The TUI awaits it directly, keeping its current
`try/except(CancelledError)/except(Exception)/finally` completion block
**byte-identical** (elapsed stamp, desktop notification, settle-pending,
queue-pause-on-error, wake chain). It stops passing
`event_stream_handler=self.stream.on_events`; rendering arrives only through
the pump.

This method is **provisional**: 3b deletes it when completion inverts into
event handlers and the TUI switches to `host.submit()`-driven turns.

### The pump

Started in `HarnessApp.on_mount`, cancelled on app exit (before the
unchanged teardown sequence):

- Owns one `Subscription` (backlog-then-live, ordered by `seq`) — the same
  object a phase-4 WebSocket client receives.
- Loop: `next_event(timeout=...)` → `parse_wire_event(dict)` → dispatch to the
  renderer / activity / ask handlers by event type.
- Ordering is inherited from the bus; the pump never reorders.
- A synthetic `stream.gap` (resume past the ring) cannot occur for the local
  subscriber; the handler treats it as a logged no-op defensively.

### Typed wire models (renderer contract)

The bus stays dict-typed (transport; phase-1 decision unchanged; the daemon
and HTTP paths never parse). At the renderer boundary, one parse hop:

- New module `server/wire_events.py` beside `schema.py`: pydantic v2 models as
  a discriminated union on `type` — one model per wire event in the §2
  vocabulary (`TextDelta`, `ThinkingDelta`, `ToolCall`, `ToolResult`,
  `TurnStarted`, `TurnFinished`, `TurnError`, `AskPending`, `AskResolved`,
  `SubagentEvent`, `WorkflowLog`, `SessionStatus`, …) — plus
  `parse_wire_event(d: dict) -> WireEvent | None`.
- Unknown `type` → log once + return `None` (forward-compatible; the pump
  skips it).
- The models are the canonical, reviewable definition of the §2 vocabulary.
- Cost: low-microsecond validation per event (hundreds of deltas/second in a
  heavy turn ⇒ sub-millisecond/second). Acceptable.

`stream_render.py` and the activity/ask handlers consume only these models.

## Publish completeness

`SessionHost` binds all of `Harness.bind_ui` (today: 8 of 22). New emissions,
wire names per the parent spec's §2 table:

| Callback | Wire event |
|---|---|
| `on_workflow_spawn` / `_start` / `_log` / `_done` / `_spawn_done` | `workflow.*` (5) |
| `on_subagent_notice` / `_model` / `_thinking` / `_usage` | `subagent.*` (4; `subagent.event` exists) |
| `on_cli_activity` | `subagent.cli_activity` (same family as the other `subagent.*` callbacks) |
| `on_present_plan` | `ask.pending` with `kind: "plan"` (third `PendingAsk` kind) |
| `on_ttft` | `session.ttft` |
| `on_mode_change` | `session.mode_changed` |
| `on_notice` | `session.notice` |
| `on_compact_start` / `on_compact` | exist (`compaction.started/finished`) |
| `on_rename`, `on_tasks_changed`, `on_jobs_changed` | exist |
| `request_approval` / `ask_user` | exist (`ask.pending` kinds `approval` / `question`) |

Anti-drift test (lands in 3a, fail → green): **assert every `Harness.bind_ui`
parameter is published by `SessionHost`** — fails 14× on master.

After 3a the rule holds: every new callback must be published, or it is
invisible to every front-end including the local one.

## Ask mechanics (single-client parity)

- `ask.pending` (kinds `approval` | `question` | `plan`) → pump mounts the
  matching interaction panel (approval / ask-user / plan card), exactly as the
  current direct callbacks do.
- User action → `host.answer_ask(ask_id, answer)` → the parked future resolves
  → `ask.resolved` → pump dismisses the panel.
- Cancel/interrupt of a parked ask keeps today's semantics
  (`ask.resolved` cancelled variant already exists).
- No cross-client behavior in 3a: the answerer is always this TUI. The
  dismissal-notice path ("approved from another client") is 3b.

Plan-card answers travel through `answer_ask` with a new answer arm for the
`plan` kind (approve/reject); the exact payload shape is fixed in the plan.

## Teardown order

**3a keeps today's teardown byte-identical.** The TUI exit sequence
(`app.py:330-358`: `cancel_autoname` → `persist(force=True)` →
`session_end("exit")` → `harness.aclose()`) and `default_cmd`'s
finally-block `harness.release_claim()` are unchanged. The in-process host
adds no teardown of its own: the pump task is cancelled and its subscription
closed at app exit, and that is all.

Deliberately *not* done in 3a: routing TUI exit through `host.aclose()`.
`SessionHost.aclose` waits for in-flight autoname (right for the daemon's
idle-eviction context); the TUI deliberately cancels it to keep exit snappy.
Unifying the two teardowns is a behavior decision, not plumbing, and belongs
to 3b if it's wanted at all.

Interrupt: Ctrl-C path unchanged in 3a. The `CancelledError` arm's event
analogue is a 3b problem.

## Error handling

- Renderer receives a malformed event → `parse_wire_event` returns `None` →
  logged, skipped; the turn continues.
- Pump task crash → logged and surfaced as a TUI notice; the app degrades to
  "no live rendering" rather than dying (the awaited `run_turn` still
  completes and the completion block still runs).
- `host.run_turn` exceptions propagate to the existing `app.py` handler
  unchanged (3a does not move them).

## Testing

1. **Renderer scripted-turn tests:** wire-dict sequences driven through the
   render layer, asserting widget output — not widget internals (parent spec
   §6).
2. **Publish-coverage test:** every `bind_ui` parameter published
   (fail → green headline test).
3. **Parity proof:** the same scripted `TestModel` turn renders identically
   pre-conversion (pydantic-ai objects) and post-conversion (wire dicts):
   capture the final transcript state from the current renderer as a fixture,
   then assert the wire-dict renderer reaches the same state.
4. **App-level pump tests:** `TestModel` scripted turns through the pump;
   respect the spec's traps (`pilot.pause()` is not a wait; CI 3.12 leg is
   3.12.3).
5. **Ask round-trip tests:** `ask.pending` mounts panel → `answer_ask` →
   `ask.resolved` dismisses, for all three kinds.
6. **Live smoke (manual, free model `zen/mimo-v2.5-free`):** TUI turn,
   approval round, `/sessions` switch; claim still `kind: tui`, ownership
   behaves as on master.

## Risks

- **Renderer drift bugs** (the big one — 1,224 lines change input format).
  Mitigation: parity fixture (test 3) + mechanical conversion driven by
  `event_to_dict()`'s existing output shapes.
- **Provisional API churn:** `SessionHost.run_turn` exists for one release
  interval. Accepted cost of the split; documented as provisional in its
  docstring.
- **Pump/handler latency:** one extra hop vs direct callbacks. Bounded by the
  parse cost above; no user-visible impact expected (verified in the live
  smoke).
- **Two turn-driving styles coexist** during 3a (direct-await TUI,
  queue-driven daemon). Contained: they share `_run_one_turn`'s publish logic.
- **Do not call `host.aclose()` from the TUI.** That method waits for in-flight
  autoname and then `harness.aclose()`s — the opposite of the TUI's snappy-exit
  path. The in-process host is an event adapter; its `aclose` stays unused in 3a.
