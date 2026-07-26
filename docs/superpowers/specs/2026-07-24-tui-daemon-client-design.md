# TUI-as-Client / Daemon-Always Sessions — Design

**Date:** 2026-07-24
**Status:** Approved (brainstorm complete)

## Goal

One session controllable from N clients simultaneously — TUI, mobile, web — with
full parity. The TUI becomes just another client of the `marim serve` daemon:
actions taken on any client (approve a tool, steer, interrupt, switch model)
take immediate effect on the live session and are visible on every other
attached client in real time.

## Decisions (from brainstorm)

| Question | Decision |
|---|---|
| Primary experience | Any client, any session — full multi-client parity |
| Deployment model | **Daemon-always** (docker→dockerd model): `marim` auto-starts/connects to a local daemon and runs as a client |
| Rollout | **Flag until parity**: opt-in via `MARIM_DAEMON=1`; flip the default only when TUI parity over the wire is reached; master stays releasable throughout |
| TUI reach | **Same-machine only**: the TUI client requires filesystem affinity with the daemon (direct file reads for diffs/images/completions stay legal). Remote access remains the job of mobile/web clients |
| Approach | **B — extract a real client protocol + driver seam** (rejected: A `RemoteHarness` facade — calcifies the TUI's implicit ~25-member harness surface; C server-embedded-in-TUI — sessions die with the TUI, doesn't meet the any-client goal) |

## Non-goals

- Headless one-shot (`marim -p`) stays in-process — ephemeral by nature.
- Remote TUI attach (over the network). The filesystem-affinity invariant is
  stated explicitly so a future effort knows exactly what to lift.
- Multi-user auth. One bearer token = one user, as today.

## Architecture

### Daemon lifecycle (auto-spawn)

On startup with `MARIM_DAEMON=1`, the TUI:

1. Reads the daemon state dir (existing serve state: token file, plus a new
   `endpoint` file recording host:port + PID).
2. Probes `GET /v1/health` (gains a `version` field). Healthy → connect.
3. Not running → spawns `marim serve` detached (defaults unchanged:
   `127.0.0.1:8642`, bearer token 0600), waits for health with a short
   deadline, connects. Spawn is guarded by a lockfile so two TUIs starting at
   once race safely.
4. Client/daemon version mismatch → warn and offer a daemon restart, allowed
   only when `busy_sessions` is empty.

The daemon outlives TUI exits — quitting the TUI is a *detach*; sessions keep
running. Idle-eviction reclaims idle hosts (existing). The daemon process
stays up until explicitly stopped; `marim serve` remains runnable in the
foreground for systemd-style supervision.

Failure modes: port taken by a foreign process, stale endpoint file, health
timeout → precise operator-facing errors. Stale endpoint files are detected by
health-probe failure + PID check and overwritten under the spawn lock.

### Workspace mapping

On connect the TUI resolves its cwd to a daemon workspace: find an existing
`WorkspaceRecord` by real path, else `register(name=dirname, path=cwd)`.
Session pickers list that workspace's sessions from the daemon, not from local
`SessionManager`.

### The `SessionClient` seam

A new `client/` package defines `SessionClient` — the typed interface the TUI
consumes instead of `Harness`. Its vocabulary is deliberately the wire
vocabulary (nothing the daemon can't serve):

**Commands** (async): `submit(prompt, attachments) -> turn_id`, `interrupt()`,
`steer(text)`, `answer_ask(id, answer)`, `set_model(id)`, `set_mode(mode)`,
`set_thinking(level)`, `set_advisor(model|off)`, `rename(name)`, `compact()`,
`rewind(checkpoint_id)`; reads: `history(offset, limit)`, `state()`
(status/queued/pending_asks/model/mode), `checkpoints()`, `tasks()`, `jobs()`,
`models()`.

**One event stream** (async iterator of typed events): the existing envelope —
`text.delta`, `thinking.delta`, `tool.call/result`, `turn.started/finished/
error`, `ask.pending/resolved`, `session.status/renamed`, `tasks.changed`,
`jobs.changed`, `compaction.*`, `subagent.event`, `steer.accepted`,
`stream.gap` — with `seq` for resume.

Approvals and questions flip from *callback* (`bind_ui`) to *event + command*:
observe `ask.pending`, post an answer. This inversion is what makes N
concurrent clients coherent — an `ask.resolved` event dismisses the panel on
every client, including the ones that didn't answer.

### Two drivers

- **`DaemonDriver`** — HTTP for commands, one WebSocket for the stream,
  `after_seq` reconnect built in. Thin: transport + typed (de)serialization,
  no business logic.
- **`LocalDriver`** — wraps the in-process `Harness` **by reusing
  `server/host.py`'s `SessionHost` + `EventBus` in-process**, not by emulating
  them. Commands call `SessionHost` methods; the event iterator attaches to
  the bus. Both drivers exercise the same host semantics (single-turn queue,
  parked asks, status derivation). When daemon-always flips, `LocalDriver` is
  glue to delete, not logic to migrate.

### Data flow (happy path)

TUI `submit()` → host queues turn → worker runs `harness.run_turn` → stream
events publish on the bus → every attached client renders deltas → gated tool
defers → `ask.pending` → any client `answer_ask()` → future resolves → turn
continues → `turn.finished` with usage.

### TUI refactor shape

`interfaces/tui/app.py` (1213 lines) currently owns a harness and wires ~10
`bind_ui` callbacks; it becomes a `SessionClient` consumer with one
event-dispatch loop feeding the existing widgets. Widgets (transcript, panels,
status bar) keep their APIs — the change is who feeds them. This is the
natural line along which `app.py` splits: event dispatch vs. widget
orchestration.

### Parity roadmap = protocol PRs

Each missing capability lands as one vertical slice: `SessionHost` method +
HTTP route + event (if any) + `SessionClient` method + TUI wiring. Order:

1. mode / thinking / advisor / rename (trivial setters)
2. tasks / jobs reads
3. compact (manual trigger)
4. checkpoints / rewind
5. sub-agent detail feed

Mobile/web gain each capability the day it merges.

## Error handling

- **WS drops** → reconnect with `?after_seq=<last seen>`; ring replay or
  `stream.gap` → full resync (`/history` + `state()`, reconciled via
  `history_seq`). Commands during the outage fail fast with a visible
  "reconnecting…" status; no silent client-side queueing.
- **Daemon dies mid-turn** → existing harness resumability rolls the session
  back to the last clean baseline; parked asks die with the process (dirty
  mid-approval history is never persisted). TUI re-runs the auto-spawn
  handshake, resyncs, shows the turn as interrupted. Nothing new server-side.
- **TUI dies mid-turn** → nothing happens to the turn (detach). Next launch
  attaches into the live stream (`running` / `waiting_ask`).
- **Command races between clients** are modeled server-side and stay that way:
  second `answer_ask` → 404, `set_model` during a turn → 409, queue full →
  429. `DaemonDriver` maps these to typed exceptions; the TUI renders toasts,
  not crashes.

## Concurrency rule (the invariant)

The daemon is the **only writer** of session state. Clients hold no
authoritative state — only a render cache keyed by `seq`. Anything a client
shows must be reconstructible from `/history` + `state()` + events since.

## Testing

1. **Host/protocol** (extends `test_server_*`): every new endpoint + event,
   `FunctionModel`-driven, including multi-client scenarios (two subscribers,
   one answers, both see `ask.resolved`).
2. **Driver contract:** one scripted scenario (submit → deltas → ask → answer
   → finish → resync after forced gap) run against **both** drivers, asserting
   identical event sequences and command outcomes — the parity lock against
   driver drift.
3. **TUI with `FakeSessionClient`:** Pilot tests drive widgets from scripted
   event streams; TUI tests stop needing a harness.
4. **End-to-end smoke:** real `marim serve` spawn, TUI attach, one turn with
   an approval answered "remotely" via raw HTTP — the couch-approval loop.

No live/paid models in any test; the e2e smoke uses `FunctionModel` via a test
harness factory.
