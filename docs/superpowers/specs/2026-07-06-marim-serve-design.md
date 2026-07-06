# `marim serve` — Server Mode Design

**Date:** 2026-07-06
**Status:** Approved (brainstorm), pending implementation plan

## Goal

A `marim serve` command that runs marim-harness as a long-lived HTTP daemon so
apps can manage and control agent sessions remotely. Target consumers, in
order: a web UI for marim, a mobile/remote companion for steering long-running
sessions, and headless automation clients (scripts, CI, other services).

Deployment model: **personal daemon with token auth**. Runs on the owner's dev
machine or personal server, binds `127.0.0.1` by default, and can be exposed
via reverse proxy / Tailscale using a bearer token. Single owner, full
filesystem access. Multi-user isolation is explicitly out of scope.

## Why the codebase is ready for this

- The CLI router dispatches management keywords (`sessions`, `mcp`, …) to
  lazily imported modules — `serve` slots in identically
  (`interfaces/cli/router.py`, `_MANAGEMENT`).
- There is **no `os.chdir` anywhere**; the workspace path is threaded
  explicitly through `Deps.workspace.root`. Multiple `Harness` instances on
  different workspaces can coexist in one process.
- The UI contract is already an interface: `Harness.bind_ui` takes async
  callbacks (`request_approval`, `ask_user`, sub-agent/stream hooks). The
  server is a third implementation of those callbacks, over the wire.
- Sessions are persistent JSON keyed by workspace path, with cross-process
  advisory file locking, cheap header-only listing, and resume support
  (`session/store.py`).
- One `Harness` = one workspace = **one turn at a time**. The TUI enforces
  this with an exclusive Textual worker; the server mirrors that discipline
  with a per-session turn queue.

## Architecture (chosen: in-process session supervisor)

One ASGI app (Starlette + uvicorn, optional `[serve]` extra) hosts a
`SessionSupervisor` that owns live `Harness` instances — one per **active**
session, each built through the existing `build_harness` seam. Alternatives
considered and rejected for v1:

- **Process-per-session** — real crash isolation, but requires building and
  maintaining an IPC protocol on top of the HTTP one, plus slower session
  start (interpreter + MCP/LSP boot per process). Because the supervisor's
  harness-per-session boundary is clean, process isolation can later replace
  the supervisor's internals without changing the client-facing API.
- **Shell out to headless (`marim -p --resume`)** — minimal code, but headless
  leaves `request_approval`/`ask_user` as `None` by design, so mid-turn
  interactivity (a hard requirement) is unmeetable.

### Package layout

The server core is transport-neutral; only the HTTP module knows about
REST/SSE. This is the "WebSocket later" boundary made physical. New top-level
package `src/marim_harness/server/` (not under `interfaces/`):

```
server/
  schema.py       # transport-neutral messages (pydantic): events out, commands in
  bus.py          # per-session EventBus: monotonic seq, ring buffer, subscriber fanout
  host.py         # SessionHost: one Harness + turn queue + pending asks
  supervisor.py   # SessionSupervisor: registry of live hosts, idle eviction
  workspaces.py   # WorkspaceRegistry (registered + managed dirs)
  auth.py         # bearer-token generation/verification
  http.py         # Starlette routes + SSE — the only transport-aware module
```

CLI entry: `interfaces/cli/serve.py` (added to `_MANAGEMENT`), parsing
`--host`, `--port`, `--workspaces-root`, then running uvicorn. Lazy import
keeps non-serve users from paying for starlette/uvicorn.

Dependencies: optional extra `marim-harness[serve] = starlette, uvicorn`
(pydantic is already a core dep).

## Workspaces

`WorkspaceRegistry` persists to
`$XDG_DATA_HOME/marim-harness/server/workspaces.json`. Record fields: `id`
(slug), `name`, `path`, `kind` (`registered` | `managed`), `created`.

Two creation flavors:

1. **Register** an existing host directory (like opening a project). Validates
   the directory exists.
2. **Create managed** under `--workspaces-root` (default
   `$XDG_DATA_HOME/marim-harness/server/workspaces/`), either empty or
   `git clone`d from a URL supplied at creation.

Deletion unregisters the workspace; for managed workspaces `?purge=true` also
removes the directory.

Sessions need no new storage: the existing `SessionManager(workspace_root)`
lists/creates/deletes per workspace. Session ids are only unique per
workspace, so all session URLs are workspace-scoped.

## SessionHost

One `SessionHost` per active session. It implements the `bind_ui` contract
server-side and owns:

### Turn execution

`POST …/messages` returns `202 {turn_id}` immediately. A per-host queue
(depth-limited, default 8; beyond → HTTP 429) feeds an asyncio task running
one turn at a time — the server's equivalent of the TUI's exclusive worker.
The turn's `event_stream_handler` maps pydantic-ai `AgentStreamEvent`s to
schema events and publishes them on the bus.

Refactor: extract headless's `_event_obj` mapping
(`interfaces/cli/headless.py`) into a shared module so headless `stream-json`
and the server speak the same event vocabulary.

### Pending asks (park and wait, no timeout)

`request_approval` and `ask_user` both park as a `PendingAsk` — `id`,
`session_id`, `kind` (`approval` | `question`), `payload`, `created` — an
unresolved asyncio future plus queryable session state. Lifecycle:

1. Turn hits a gated tool / `ask_user` → `PendingAsk` created, `ask.pending`
   event published, session status becomes `waiting_ask`.
2. Any authenticated client answers via POST → future resolves,
   `ask.resolved` published, turn continues.
3. No timeout. An interrupt resolves outstanding asks as denied/cancelled.

This rides an existing invariant for free: the dirty mid-approval history is
never persisted, so if the daemon dies with a parked ask, the session rolls
back to its last resumable baseline on next open.

### Interrupt & steer

- `POST …/interrupt` cancels the turn task; the controller's existing
  `_flush_resumable` path handles clean persistence (histories never end with
  an unanswered `ToolCallPart`).
- `POST …/steer {text}` feeds the controller's steer buffer, same as the
  TUI's steer keys.

### Lifecycle & eviction

Hosts are created lazily on the first prompt for a session. Idle eviction
(no running turn, no SSE subscriber, configurable N minutes, default 15)
persists the session and tears the harness down (`aclose`); the session stays
fully resumable from disk. Read-only endpoints (history, session info,
listing) never instantiate a host — they read `SessionStore`/`SessionManager`
directly.

## Wire protocol

### Envelope

Transport-neutral message: `{seq, ts, type, data}`. `seq` is monotonic per
session. Over SSE, `seq` is the event id, enabling `Last-Event-ID` resume
against a per-session ring buffer (default 1000 events). A reconnect that
falls off the buffer receives a `stream.gap` event telling the client to
re-sync via the history endpoint.

The schema is the contract; SSE + POST is merely the first transport. A
future WebSocket endpoint pipes the same messages both ways with zero changes
to supervisor, bus, or schema. Rationale for SSE-first: automation clients
consume it with bare `curl`; SSE has reconnect/replay built into the spec
(critical for mobile); inbound commands are rare and small, so duplex latency
buys nothing; and plain HTTP works with any proxy/auth middleware.

### Event types (v1)

`session.status` (`idle` | `running` | `waiting_ask`), `turn.started`,
`turn.finished` (final output + usage), `turn.error`, `text.delta`,
`tool.call`, `tool.result`, `ask.pending`, `ask.resolved`, `subagent.event`,
`tasks.changed`, `jobs.changed`, `session.renamed`, compaction notices.

### Endpoints (all `/v1`, JSON; errors as `{error: {code, message}}`)

| Method & path | Purpose |
|---|---|
| `GET /health` | unauthenticated liveness |
| `GET /workspaces` / `POST /workspaces` | list / register-or-create |
| `DELETE /workspaces/{ws}` (`?purge=true`) | unregister (and purge managed) |
| `GET /workspaces/{ws}/sessions` | list sessions |
| `POST /workspaces/{ws}/sessions` | create (name, mode) |
| `GET …/sessions/{sid}` | status incl. pending asks |
| `DELETE …/sessions/{sid}` | delete session |
| `POST …/sessions/{sid}/messages` | run a turn → 202 `{turn_id}` |
| `POST …/sessions/{sid}/interrupt` | cancel running turn |
| `POST …/sessions/{sid}/steer` | inject steering text |
| `GET …/sessions/{sid}/asks` | list pending asks |
| `POST …/sessions/{sid}/asks/{aid}` | answer an ask |
| `GET …/sessions/{sid}/events` | SSE stream (`Last-Event-ID`) |
| `GET …/sessions/{sid}/history` | paginated past transcript |

### Auth

Token generated on first run, stored at
`$XDG_DATA_HOME/marim-harness/server/token` (mode 0600), printed once. All
routes except `/health` require `Authorization: Bearer <token>`
(constant-time compare). Binds `127.0.0.1` by default. Exception: the SSE
endpoint also accepts `?access_token=` because browser `EventSource` cannot
set headers.

## Error handling & shutdown

- Provider failures mid-turn → `turn.error` event, session returns to `idle`;
  the existing actionable-error-note machinery still primes the next turn.
- Prompt while queue full → 429. Unknown workspace/session/ask → 404. Bad
  token → 401.
- Graceful shutdown: interrupt running turns (resumable flush), resolve
  parked asks as cancelled, persist all hosts, exit.

## Targeted changes to existing code

1. `runtime/bootstrap.py::build_harness` — add `session_id: str | None` so
   the server can open a specific session (today: latest-or-create only).
2. Extract headless's `_event_obj` event mapping into a shared module used by
   both headless `stream-json` and `server/schema.py`.
3. `interfaces/cli/router.py` — add `"serve"` to `_MANAGEMENT`.
4. Verify/expose public interrupt + steer entry points on
   `Harness`/`TurnController` (the TUI uses them; the server needs
   non-private access).
5. `pyproject.toml` — add the `[serve]` optional extra.

## Testing

- **Unit:** `EventBus` (seq, ring buffer, `Last-Event-ID` resume, gap),
  `WorkspaceRegistry` (register/create/purge), `PendingAsk` lifecycle
  (park → answer, park → interrupt), event-schema mapping from pydantic-ai
  events (shared with headless mapping tests), auth (valid/invalid/missing
  token, constant-time path).
- **Integration:** Starlette `TestClient` + pydantic-ai `FunctionModel`
  driving the full loop: create workspace → create session → prompt → SSE
  events observed → approval parks → answered → `turn.finished`. Plus
  interrupt mid-turn, steer, queue-full 429, SSE resume after disconnect,
  idle eviction + lazy revive.
- No live models in CI or local test runs (standing rule: free/local models
  only without explicit approval).

## Explicitly deferred (not v1)

WebSocket transport (schema is ready for it), mode-switch endpoint,
checkpoints/rewind over the API, multi-user auth, per-session resource
quotas, process-per-session isolation, push notifications.
