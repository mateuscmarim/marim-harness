# Cross-process session events: TUI ↔ serve daemon

Date: 2026-08-29
Status: design approved, not implemented

## Problem

Session files are keyed by workspace path, not by front-end, so the serve
daemon and the TUI already read and write the same store
(`SessionManager(workspace)`, `session/store.py:47`). A session created in the
TUI is visible and resumable over the serve API, and vice versa — this is
documented behaviour (`docs/reference/serve-api.md:360`).

Two things do not cross, and both are gaps worth closing:

1. **Events.** The `EventBus` is in-process. A turn driven over HTTP does not
   stream into a running TUI, and a turn driven in the TUI is invisible to
   every WebSocket client. There is no fan-out seam: `bind_ui`
   (`runtime/harness.py:623`) is a *single-owner* contract, and `SessionHost`
   is simply the server-side implementation of the same contract the TUI fills
   interactively.

2. **Ownership.** Nothing prevents the same session being live in both
   processes. `SessionStore.save` takes an advisory `file_lock`
   (`store.py:260`), but that only serializes the writes so the file is never
   torn. Both processes hold the whole history in memory and write it back
   wholesale, so whichever turn finishes second silently overwrites the
   other's turn entirely.

## Goals

- One session can be live in the TUI and on a remote client at once, with a
  turn started from either appearing live in the other.
- Approvals, `ask_user` questions, and plan cards can be answered from either
  side.
- The clobbering window is closed: concurrent ownership becomes an explicit
  refusal rather than silent data loss.

## Non-goals

- **Live ownership handoff.** If the TUI claimed a session because no daemon
  was running, and a daemon starts afterwards, that session stays TUI-owned
  until the TUI exits. Migrating a live harness — with its parked asks and its
  dirty mid-approval history — across processes is a lot of machinery for a
  narrow case. The claim file (below) makes the refusal honest rather than
  dangerous. Revisit if it bites.
- Changing headless. It has its own path and stays as-is.

## Decisions

**The daemon owns; the TUI attaches as a client.** For any session on a
workspace a running daemon knows about, the TUI stops building its own harness
and drives the existing serve API instead. This reuses the whole protocol as-is
— the bus already has monotonic sequence numbers, a bounded replay ring,
`stream.gap` resync, and a `history_seq` watermark (`server/bus.py`).

**With no daemon running, the TUI hosts in-process.** It runs a `SessionHost`
and `EventBus` internally and renders from *bus events* rather than `bind_ui`
callbacks. A remote session is then literally the same host reached over a
socket — one rendering path, with transport as a detail.

Rejected: *auto-spawning a daemon and always being a client* requires the same
renderer rewrite (a pure client never touches pydantic-ai event objects) plus
daemon lifecycle, port allocation and orphan reaping — strictly more work for
the same end state. Rejected: *keeping today's direct-harness path for local
sessions* leaves two renderers for the same content, and every new event type
has to be taught to both.

## 1. The seam: one host, two transports

The same contract is implemented twice today. `bind_ui` is filled by
`HarnessApp.__init__` (`interfaces/tui/app.py:166`) and by
`SessionHost.__init__` (`server/host.py:90`). `event_stream_handler` is filled
by `stream.on_events` (`app.py:523`, raw pydantic-ai objects) and by the host's
local handler (`host.py:296`, `event_to_dict` → bus).

`SessionHost` becomes the only implementation of both.

The transport seam already exists and is already the right shape.
`Subscription` (`server/bus.py:22`) exposes exactly two operations —
`next_event(timeout=...)` and `close()` — with backlog replay handled
internally. A remote session swaps the local `Subscription` for a
WebSocket-fed object implementing the same two operations. There is no new
protocol type to design: the `Subscription` interface *is* the abstraction.

The client side is therefore a pair of small protocols:

- **Command surface** — `submit` / `interrupt` / `steer` / `answer_ask` /
  `set_model` / `set_mode`. `SessionHost` already has all of these, and the
  existing REST routes (`server/http.py:782-788`) already cover every one, so
  `RemoteSessionHost` needs no new endpoints.
- **Event feed** — `Subscription`, local or WebSocket-backed.

### Turn completion inverts

The largest single change: **turn completion stops being a return value and
becomes an event.** `app.py:518-560` currently wraps `await
harness.run_turn(...)` in a try/except/finally that stamps `TurnMeta` with the
elapsed time, fires the desktop notification, pauses the queue on error, calls
`stream.settle_pending(...)` on cancel and failure, and drives the wake chain.

All of it moves to handlers for `turn.finished`, `turn.error`, and the
interrupted-`turn.finished` variant (`host.py:266`). The information is already
carried — `turn.finished` has `output` and `usage`, `turn.error` has the
formatted detail — but the control flow inverts, and the `CancelledError` arm
has no direct event analogue today beyond `{"interrupted": true}`.

Net effect: one `bind_ui` implementation instead of two, one
`event_stream_handler` instead of two, and the TUI render layer no longer
imports from `pydantic_ai.messages`.

## 2. Widening the wire vocabulary

`SessionHost` binds **8 of the 22** `bind_ui` callbacks (`host.py:90-103`). The
other 14 are dropped — acceptable for a status-oriented phone client, not
acceptable for a client rendering a first-class session.

| Group | Callbacks | New event family |
|---|---|---|
| Sub-agent detail | `on_subagent_notice`, `on_subagent_model`, `on_subagent_thinking`, `on_subagent_usage`, `on_cli_activity` | `subagent.*` |
| Workflow cards | `on_workflow_start`, `on_workflow_spawn`, `on_workflow_log`, `on_workflow_spawn_done`, `on_workflow_done` | `workflow.*` |
| Status | `on_ttft`, `on_mode_change`, `on_notice` | `session.*` |
| Plan | `on_present_plan` | third `PendingAsk` kind |

Only `on_subagent_event` is published today (`host.py:232`); the sub-agents
screen needs per-spawn model, thinking, usage/cost and the nesting tree.

`on_present_plan` is the one that is not a notification: `OnPresentPlanFn`
returns a `PlanDecision` (`runtime/deps.py:107`), so like `request_approval`
and `ask_user` it becomes a third `PendingAsk` kind (`kind: "plan"`), answered
over the existing `POST .../asks/{aid}` with a new arm in `AskAnswerIn`
(`server/schema.py:78`, which today knows only approve/reason, answers,
cancel). No new route.

### Images: reference, not bytes

`_jsonify_tool_content` (`stream_events.py:24`) deliberately replaces
`BinaryContent` with a placeholder so a remote client is not sent ~20MB of
base64 per image read. The TUI renders `read_file` images today, so routing
local rendering through the wire unchanged would be a visible regression.

Fix: `tool.result` carries a sha reference. The image sidecar already exists
and already has an endpoint (`GET .../images/{sha}`, `http.py:793`). Local
resolves it from disk, remote over HTTP — same event shape, same renderer, no
placeholder on either side.

### The rule that prevents re-drift

After this change there is no consumer of `bind_ui` other than `SessionHost`,
so **every new callback must be published, or it is invisible to every
front-end including the local one.** The anti-drift property is structural, not
a convention someone has to remember.

The widening is purely additive: existing clients ignore unknown event types
and keep working throughout the migration.

## 3. Asks and co-driving

The concurrency model is already correct. `answer_ask` (`host.py:169`)
pops-then-checks and returns `False` for an already-answered ask; HTTP maps
that to `404 unknown or already-answered ask` (`http.py:640`). Two clients
racing to approve the same tool call resolve deterministically — first wins,
second is told plainly. All remaining work is client-side.

`_request_approval` (`app.py:788`) is a direct await today. It becomes:

1. `ask.pending` (kind `approval` / `question` / `plan`) → mount the matching
   panel. The existing panels (`interactions/approval.py`, `ask_user.py`,
   `plan_card.py`) are already async and already mounted above the status bar
   rather than modal, so none of them assume they own the call stack.
2. Panel result → `host.answer_ask(...)` locally, `POST .../asks/{aid}`
   remotely.
3. `ask.resolved` → **dismiss the panel regardless of who answered.**

Step 3 is new behaviour: an approval panel can vanish because it was answered
on another client. The event already exists (`host.py:174`), including the
cancelled-on-interrupt variant (`host.py:285`). The dismissal must leave a
one-line notice in the log ("approved from another client") — a prompt
evaporating with no trace reads as a bug.

**Attach-time reconciliation.** A client attaching mid-turn must not miss an
ask parked before it connected. `?after_seq` replay plus `stream.gap` covers
the event side; `GET .../asks` (`http.py:787`) lists what is actually pending.
`GET /asks` is the authority — the replay ring is bounded at 1000, so an ask
can outlive its own `ask.pending` event. Attach → replay → reconcile.

**The turn queue.** `SessionHost` has a bounded queue (limit 8); the TUI has
its own (`interfaces/tui/queue.py`) with pause-on-error and pre-send editing.
These compose if the TUI queue stays a pre-submit staging area feeding the host
queue one at a time. But because a remote client can submit while messages are
staged locally, the TUI's busy signal must come from `session.status`
(`host.py:246`) rather than local state — `status.set_busy` (`app.py:519`) is
driven by the local turn worker today.

## 4. Ownership and discovery

**Discovery.** The daemon persists a token and `workspaces.json` under its state
dir but records nothing about where it is listening (`interfaces/cli/serve.py:371`
binds host/port and stops there). It should write `server/runtime.json`
(`{host, port, pid, started}`) on startup and remove it on clean exit. The TUI
reads that plus the existing token file (mode 0600, same user — no new secret
handling).

**Auto-register the workspace.** Today a local project must be registered by
hand (`POST /v1/workspaces` with a path) before the daemon can see it. The TUI
knows its own cwd and registers on attach if needed (`workspaces.py:86`), so the
registry never has to be thought about.

**Ownership is a claim, not an inference.** The daemon builds hosts lazily on
first prompt (`supervisor.host_for`), so an idle session in the store has no
owner at all. If the TUI opened that session in-process and the daemon later
received a prompt for it, `host_for` would build a second harness — exactly the
clobbering this design exists to eliminate. `supervisor.peek` cannot detect
this; absence of a host does not mean absence of an owner.

So: a **claim file beside the session file**, holding
`{pid, kind: "tui"|"daemon", endpoint}`. The primitive already exists —
`atomic_io.file_lock` is what `SessionStore.save` uses (`store.py:260`).
`host_for` consults it before building and refuses with `409` when another live
process holds it. A claim whose pid is dead is reclaimed, cross-checked against
`runtime.json`'s pid for the daemon case.

Decision table at session-open time:

| Situation | Result |
|---|---|
| Daemon reachable | TUI asks it to host, attaches as client. Daemon claims. Co-driving works. |
| No daemon | TUI hosts in-process and claims. Fully functional, not remotely visible. |
| Claim held by another live process | Refuse, naming the holder. |

**Daemon death mid-turn.** The TUI is a pure client, so a crash drops the
WebSocket. A `SIGKILL` skips `SessionHost.aclose`'s guarded persist, but
`_flush_resumable` and `_repair_unanswered_tool_calls` are precisely the
machinery for a history that ends badly, and the session reloads from its last
clean baseline. The TUI shows "connection lost", reconnects with `?after_seq`
(the daemon rebuilds the host from disk on the next prompt), and if the daemon
is gone for good, offers to take over locally — the claim path with a dead pid
to reclaim.

## 5. Phasing

Four phases, each independently mergeable and individually valuable.

**Phase 1 — Wire widening (server only).** Publish the 14 unbound callbacks,
add `plan` as a third ask kind, switch `tool.result` images to sha references.
No TUI changes. Ships value immediately to existing remote clients. Near-zero
risk.

**Phase 2 — Claim and discovery.** `runtime.json`, the claim file, workspace
auto-register, the 409 refusal path. Small, and it closes the silent clobbering
bug on its own — worth landing early regardless of what follows.

**Phase 3 — TUI renders from events.** In-process `SessionHost`;
`stream_render.py` moved from pydantic-ai objects to wire dicts; turn
completion inverted; asks driven by `ask.pending`/`ask.resolved`. The bulk of
the work (the front half of 1199 lines plus the `app.py:518-560` turn worker)
and the highest risk. Deletes the duplicate `bind_ui` implementation.

**Phase 4 — Remote attach.** WebSocket-fed `Subscription`,
`RemoteSessionHost` over the existing REST routes, daemon-owned sessions in the
session picker. Small if phases 1-3 landed correctly, because by then the TUI
cannot tell local from remote.

**No compatibility flag.** Keeping the old callback path behind a `MARIM_*`
switch during phase 3 would recreate exactly the two-renderer drift this design
exists to eliminate, and an unexercised fallback rots. Delete it in phase 3.

## 6. Testing

The highest-value test, written first: **assert every `bind_ui` parameter is
published by `SessionHost`** — introspect the signature and diff it against
what the host binds. Cheap, permanent, and it makes the anti-drift rule
mechanical rather than aspirational. It fails today, 14 times over.

Following the repo's three-way split:

- Callback → wire-dict mapping lives in pure functions, unit-tested directly.
- `SessionHost` publishing is tested against a real `EventBus`.
- The two-client case gets an explicit test: two subscriptions, one answers an
  ask, assert the other observes `ask.resolved` and the loser's answer 404s.
- The phase-3 renderer is driven by a scripted turn using pydantic-ai's
  `TestModel`, asserting on the emitted event sequence rather than widget
  internals.

Two known traps in this repo, baked into the plan rather than rediscovered:

- **`pilot.pause()` is not a wait.** TUI tests must wait on the clock and on
  focus, or they flake and break master.
- **The 3.12 CI leg is 3.12.3 specifically.** Anything stdlib-timing-sensitive
  gets verified with `uv run --python 3.12.3` before pushing.

Gates, in order: `uv run ruff check src tests` → `uv run pyright` →
`uv run pytest`, on Python 3.10, 3.12 and 3.14.
