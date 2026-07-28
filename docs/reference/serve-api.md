# marim serve HTTP API

`marim serve` runs marim as a long-lived HTTP daemon: workspaces and sessions
are managed over REST, turns stream over a per-session WebSocket, and approval
prompts ("asks") are parked server-side until any authenticated client answers
them. Each session runs the same full harness the TUI and headless modes use
(models, MCP, LSP, hooks — wired via `build_harness`).

Sources of truth: `src/marim_harness/interfaces/cli/serve.py` (CLI),
`src/marim_harness/server/{http,schema,auth,bus,host,supervisor,workspaces}.py`.

## Installation and startup

The server is an optional extra (Starlette + uvicorn):

```
uv add 'marim-harness[serve]'    # or: pip install 'marim-harness[serve]'
marim serve --port 8642
```

Flags (all optional):

| Flag                | Default                  | Meaning                                        |
| ------------------- | ------------------------ | ---------------------------------------------- |
| `--host`            | `127.0.0.1`              | Bind address                                   |
| `--port`            | `8642`                   | Bind port                                      |
| `--workspaces-root` | `<state-dir>/workspaces` | Directory for *managed* workspaces             |
| `--idle-ttl`        | `900.0`                  | Seconds before an idle session host is evicted |
| `--no-banner`       | off                      | Skip the startup wordmark                      |
| `--qr`              | off                      | Also print a pairing QR at startup             |
| `--advertise`       | auto-detected            | Address `--qr` encodes (`HOST[:PORT]`)         |

The state dir is `$XDG_DATA_HOME/marim-harness/server` (default
`~/.local/share/marim-harness/server`). It holds the bearer token file, the
workspace registry (`workspaces.json`), and by default the managed-workspaces
root.

The daemon binds loopback by default; to reach it remotely, front it with a
reverse proxy or a tailnet.

### Startup output

On a terminal, startup prints the MARIM wordmark (the same art as the TUI intro
header) over the four facts you need to drive the daemon:

```
 ███╗   ███╗ █████╗ ██████╗ ██╗███╗   ███╗
 ████╗ ████║██╔══██╗██╔══██╗██║████╗ ████║
 ██╔████╔██║███████║██████╔╝██║██╔████╔██║
 ██║╚██╔╝██║██╔══██║██╔══██╗██║██║╚██╔╝██║
 ██║ ╚═╝ ██║██║  ██║██║  ██║██║██║ ╚═╝ ██║
 ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝╚═╝     ╚═╝
   · · ·   s e r v e   v0.2.0

  listening     http://127.0.0.1:8642
  bearer token  ~/.local/share/marim-harness/server/token
  workspaces    ~/.local/share/marim-harness/server/workspaces
  idle ttl      900s
```

Only the token's *path* is ever printed, never its value — startup output ends
up in scrollback and screenshots.

When stdout isn't a terminal (systemd, Docker, `nohup`, a pipe) the art is
skipped automatically and the same facts print one per line, led by the
long-standing `marim serve <version> listening on <url>` line, with absolute
paths. `--no-banner` or `MARIM_NO_BANNER=1` forces that plain form on a
terminal too; `NO_COLOR` (or `TERM=dumb`) keeps the art but drops the color.

### Pairing a client (QR)

```
marim serve qr [--port N] [--advertise HOST[:PORT]] [--name NAME]
marim serve --qr            # print one at startup, then serve
```

Prints a QR encoding a pairing URI, so a client (e.g. `marim-mobile`) can
provision a server profile in one scan instead of retyping a 43-character
bearer token:

```
marim://pair?v=1&url=http%3A%2F%2F192.168.0.3%3A8642&token=<token>&name=workstation
```

That format is the cross-repo contract. `v=1` lets a client reject a future
shape rather than mis-parse it; `url` keeps its scheme so a reverse-proxy or
tailnet address round-trips; `name` defaults to the machine's hostname.

The subcommand doesn't talk to a running daemon — the token file *is* the
contract, so it works before the first `marim serve` and creates the token if
it doesn't exist yet. It can't discover a running daemon's port, so pass
`--port` if it isn't the default.

The encoded address comes from the source address of the default route (the
address a client on the same network should use — never a `docker0` or bridge
address), and always prints in plain text beneath the code so a wrong guess is
visible. `--advertise` overrides it and is the only way to encode a tailnet
name or a proxy domain. With no default route and no `--advertise`, the command
exits 1 and says so.

**The code is a credential.** It prints only on an explicit `marim serve qr` or
`--qr`, never as part of normal startup output, and it is **refused when stdout
isn't a terminal** (exit 1, no token bytes emitted) — a QR in a log file is
both useless and a leak. `NO_COLOR` is also refused rather than honored: a code
drawn in the terminal's own colors renders inverted on a dark theme and may not
scan. Under `--qr`, a refusal is a note on stderr and the daemon still starts.

Rendering needs `segno` (in the `serve` extra). Without it the command prints
the URI as text plus an install hint and exits 0 — a typed URI still pairs.

## Authentication

Single-owner bearer token, generated once and persisted with mode `0600` at
`<state-dir>/token` (`server/auth.py`). There is no CLI flag to set a token;
delete or edit the file to rotate it. Comparison is constant-time.

Every request except `GET /v1/health` must send:

```
Authorization: Bearer <token>
```

This includes the WebSocket upgrade request. Failures:

- REST: `401` with the standard error body, code `"unauthorized"`.
- WebSocket: the connection is closed with code `4401` before accept.

## Conventions

- Base path: `/v1`. All request/response bodies are JSON.
- Error responses share one shape:

```json
{"error": {"code": "not_found", "message": "unknown session"}}
```

Codes used: `unauthorized` (401), `bad_request` (400), `not_found` (404),
`busy` (409), `not_running` (409), `queue_full` (429), `host_closed` (404),
`unreadable` (500), `trust_store_error` (500).

## Endpoint summary

| Method | Path                                              | Purpose                            |
| ------ | ------------------------------------------------- | ---------------------------------- |
| GET    | `/v1/health`                                      | Liveness check (no auth)           |
| GET    | `/v1/workspaces`                                  | List workspaces                    |
| POST   | `/v1/workspaces`                                  | Register or create a workspace     |
| DELETE | `/v1/workspaces/{ws}`                             | Delete a workspace                 |
| GET    | `/v1/workspaces/{ws}/sessions`                    | List sessions (with live status)   |
| POST   | `/v1/workspaces/{ws}/sessions`                    | Create a session                   |
| GET    | `/v1/workspaces/{ws}/trust`                       | Workspace trust state + surface    |
| POST   | `/v1/workspaces/{ws}/trust`                       | Grant/revoke project trust         |
| GET    | `/v1/workspaces/{ws}/sessions/{sid}`              | Session detail + live status       |
| DELETE | `/v1/workspaces/{ws}/sessions/{sid}`              | Delete a session                   |
| POST   | `/v1/workspaces/{ws}/sessions/{sid}/messages`     | Submit a prompt (enqueue a turn)   |
| POST   | `/v1/workspaces/{ws}/sessions/{sid}/interrupt`    | Cancel the running turn            |
| POST   | `/v1/workspaces/{ws}/sessions/{sid}/steer`        | Steer the running turn             |
| GET    | `/v1/workspaces/{ws}/sessions/{sid}/asks`         | List pending asks                  |
| POST   | `/v1/workspaces/{ws}/sessions/{sid}/asks/{aid}`   | Answer an ask                      |
| POST   | `/v1/workspaces/{ws}/sessions/{sid}/mode`         | Switch the session's approval mode |
| WS     | `/v1/workspaces/{ws}/sessions/{sid}/ws`           | Live event stream                  |
| GET    | `/v1/workspaces/{ws}/sessions/{sid}/history`      | Persisted message history          |
| GET    | `/v1/workspaces/{ws}/sessions/{sid}/images/{sha}` | Cached image bytes                 |

## Health

### GET /v1/health

No auth. Returns `200`:

```json
{"status": "ok"}
```

## Workspaces

A workspace is a named directory sessions run in. Two kinds:

- **registered** — points at an existing directory on the host; never removed
  from disk by the server.
- **managed** — created by the server under the workspaces root, either empty
  or `git clone`d (clone timeout: 600 s); may be purged on delete.

Workspace ids are slugs derived from the name (`my project` → `my-project`),
suffixed `-2`, `-3`, … on collision. The registry persists to
`<state-dir>/workspaces.json`.

Workspace record shape (returned by list/create):

```json
{
  "id": "my-project",
  "name": "my project",
  "path": "/home/user/.local/share/marim-harness/server/workspaces/my-project",
  "kind": "managed",
  "created": "2026-07-23T12:00:00+00:00"
}
```

### GET /v1/workspaces

`200`: `{"workspaces": [<record>, ...]}`

### POST /v1/workspaces

Request body (`WorkspaceIn`):

```json
{"name": "my project", "path": null, "git_url": null}
```

- `name` (required) — display name; also the basis of the slug id.
- `path` — register an existing directory (kind `registered`). Must exist and
  be a directory, else `400`.
- `git_url` — for a managed workspace, clone this URL into the new directory.
  Ignored when `path` is set. Clone failure returns `400` with git's stderr.

`201`: the workspace record. `400 bad_request` on validation/clone errors.

### DELETE /v1/workspaces/{ws}

Query: `?purge=true` also removes the directory from disk — allowed only for
`managed` workspaces (`400` otherwise). `404` for an unknown id.

Refuses while any session in the workspace has a running turn: `409 busy`
("interrupt them first"). On success every live host, event bus, and cached
mode for the workspace is torn down, so nothing keeps streaming from the
deleted (possibly purged) directory.

`200`: `{"deleted": true}`

## Trust

Project trust is a property of the *workspace* (its directory on disk), not of
any one session — every session sharing a workspace shares one trust
decision. See `docs/guides/trust.md` for the full model (the trust store, the
gated surface it decides over, and the TUI/CLI front-ends); this section
covers only the serve-specific shape.

`marim serve` never prompts interactively — it consults the same persistent
store (`$XDG_STATE_HOME/marim-harness/trusted-projects.json`) `marim trust`
and the TUI's first-open dialog write to, so a decision made in any front-end
is honored by the others. `GET .../sessions` and `GET/POST
.../sessions/{sid}` surface `"trust_prompt_pending"` (see below) so a remote
client knows when to render its own trust dialog for a workspace.

### GET /v1/workspaces/{ws}/trust

`200`, `Cache-Control: no-cache` (decision state, like `get_session`):

```json
{
  "trusted": false,
  "source": "default",
  "fingerprint_fresh": false,
  "surface": {
    "hook_events": ["SessionStart"],
    "mcp_servers": ["docs-server"],
    "skills": ["deploy"],
    "agents": [],
    "plugins": [],
    "summary": "hooks: 1 (SessionStart) · mcp: 1 (docs-server) · skills: 1"
  }
}
```

- `source` — which layer decided: `"config"` (an explicit caller decision),
  `"env"` (`MARIM_TRUST_PROJECT_HOOKS`), `"store"` (a persisted decision whose
  fingerprint still matches), or `"default"` (no usable decision — untrusted).
- `fingerprint_fresh` — whether a stored decision exists AND its fingerprint
  still matches the workspace's current gated surface. `false` either when
  nothing is stored, or the executable surface changed since the last
  decision (hooks/MCP/plugin config edited) — in that case the stored
  decision is ignored and the resolution falls through to `"default"`.
- `surface` — everything a grant would enable, straight from
  `ProjectSurface`: hook event names, MCP server names, skill/agent/plugin
  names, and `summary` (the same one-line readout the TUI panel and `marim
  trust status` show).

`404 not_found` for an unknown workspace.

### POST /v1/workspaces/{ws}/trust

Request body:

```json
{"trusted": true}
```

Persists the decision against the workspace's *current* fingerprint (so a
grant/revoke always applies to what's on disk right now, not some earlier
scan), then hot-applies it to every **live** session host of the workspace:

- `trusted: true` — each live host's `apply_project_trust()` reloads hooks,
  connects project MCP servers, and rebuilds the LSP registry, live, no
  restart.
- `trusted: false` — each live host's `revoke_project_trust()` flips its
  trust state immediately (lazy readers stop seeing project content on their
  next read), but already-running MCP server processes / LSP providers for
  that host keep running until the daemon restarts — `restart_note` reports
  this.

`200`:

```json
{"trusted": true, "applied_sessions": 2, "restart_note": null, "failed_sessions": []}
```

- `applied_sessions` — how many live hosts the decision was hot-applied to
  successfully (0 when the workspace has no live sessions right now, or when
  every host's apply failed).
- `restart_note` — non-null only on a revoke that reached at least one live
  host; `null` on a grant, or when nothing was live to apply to.
- `failed_sessions` — one entry per host whose hot-apply raised (LSP registry
  rebuild, hooks/MCP config load can all fail independently per host):
  `{"session_id": ..., "error": "<str(exc)>"}`. A per-host failure never
  aborts the loop or changes the HTTP status of an otherwise-successful
  persist — the rest of the workspace's live hosts still get applied and are
  reported normally.

`400 bad_request` for a malformed body (missing/non-boolean `trusted`).
`404 not_found` for an unknown workspace. `500 trust_store_error` if the
decision could not be persisted (disk full, permissions) — the live
TrustState still flips on every reached host (the caller already consented;
only durability failed), so the error is informational, not a rollback.

**Honest limit (documented, not mitigated here):** `POST /v1/workspaces/{ws}/trust`
lets a remote client enable startup code execution. Serve already exposes
turn execution (bash in auto mode) to whoever can reach it, so trust adds no
new exposure class.

## Sessions

Session files persist under the workspace's session store (the same store the
TUI uses), so server sessions are resumable from the terminal and vice versa.

Live status values: `"idle"` (no host, or host with nothing running),
`"running"` (a turn is executing or queued), `"waiting_ask"` (a turn is
blocked on an unanswered ask).

### GET /v1/workspaces/{ws}/sessions

`200`:

```json
{
  "sessions": [
    {
      "id": "a1b2c3",
      "name": "fix the login bug",
      "updated": "2026-07-23T12:34:56+00:00",
      "message_count": 12,
      "tokens": 45678,
      "duration_seconds": 321.5,
      "model": "anthropic/claude-sonnet-4-6",
      "advisor_model": null,
      "thinking": null,
      "mode": "ask",
      "status": "idle",
      "pending_asks": [],
      "trust_prompt_pending": false
    }
  ]
}
```

Each row carries the same `status`/`pending_asks` a per-session GET would
return, so list consumers don't need one detail request per session. The
status lookup is in-memory only — no harness is spawned.

`trust_prompt_pending` mirrors the TUI's first-open mount check
(`Harness.trust_prompt is not None`): `true` means this workspace ships a
non-empty gated surface with no usable trust decision yet, and a client
should offer its own trust dialog (`GET/POST .../trust` above). It reflects
the *workspace's* trust state, so it reads the same whether or not this
particular session has a live host.

### POST /v1/workspaces/{ws}/sessions

Request body (`SessionIn`, both fields optional):

```json
{"name": "fix the login bug", "mode": "ask"}
```

- `mode` — `"auto"`, `"ask"`, or `"plan"`; omitted/`null` means the
  configured default. Unknown values return `400`.
- The mode is persisted on the session file header, so it survives daemon
  restarts and idle evictions; it also appears as `"mode"` in session
  list/detail responses.

`201`: `{"id": "<session-id>", "name": "<name>", "trust_prompt_pending": false}`

The session file is written immediately (empty history), so list/history/
message endpoints see it before its first turn.

### GET /v1/workspaces/{ws}/sessions/{sid}

`200`:

```json
{
  "session": {"id": "...", "name": "...", "updated": "...", "message_count": 0,
              "tokens": 0, "duration_seconds": null, "model": null,
              "advisor_model": null, "thinking": null, "mode": null},
  "status": "idle",
  "queued": 0,
  "pending_asks": [],
  "trust_prompt_pending": false
}
```

`queued` is the number of prompts waiting behind the running turn.

### DELETE /v1/workspaces/{ws}/sessions/{sid}

Refuses while a turn is running: `409 busy` ("interrupt it first"). Otherwise
closes the live host (if any), deletes the session file, and reclaims all
server state for the session (event bus included).

`200`: `{"deleted": true}`

### POST /v1/workspaces/{ws}/sessions/{sid}/mode

Switches the session's approval mode. Request body (`SetModeIn`):

```json
{"mode": "plan"}
```

`mode` must be `"auto"`, `"ask"`, or `"plan"`; anything else returns `400
bad_request`.

A loaded (live) host switches immediately, mid-session. A session with no
live host (idle-evicted, or never yet prompted) persists the mode straight
to the session file header — the same field the create-session `mode` and
list/detail `"mode"` responses read — so the switch survives daemon restarts
and idle evictions either way.

`200`: `{"ok": true, "mode": "plan"}`

`409 busy` while a turn is running:

```json
{"error": {"code": "busy", "message": "cannot switch modes while a turn is running"}}
```

The mode is left untouched in this case — retry after the turn finishes or
interrupt it first.

## Messages (turns)

### POST /v1/workspaces/{ws}/sessions/{sid}/messages

Submits one user prompt. This is the call that lazily creates the session's
live host (building the full harness) if none is mounted. Turns run strictly
one at a time per session; extra submissions queue (bounded, capacity 8).

Request body (`MessageIn`):

```json
{
  "prompt": "run the tests and fix any failures",
  "attachments": [
    {"data_b64": "<base64 bytes>", "media_type": "image/png"}
  ]
}
```

`attachments` is optional. Invalid base64 returns `400`.

`202`: `{"turn_id": "9f3ab1c2d4e5f607"}` — the turn is queued; follow
progress on the WebSocket stream. Errors:

- `429 queue_full` — the per-session turn queue is at capacity.
- `404 host_closed` — the host was torn down mid-submit; retry.

### POST /v1/workspaces/{ws}/sessions/{sid}/interrupt

Cancels the running turn (empty request body). The session rolls back to its
last cleanly persisted history (the harness's resumable-flush machinery);
pending asks belonging to the interrupted turn are cancelled and announced as
`ask.resolved` events with `"cancelled": true`.

`200`: `{"interrupted": true}` — or `false` when nothing was running (this is
not an error).

### POST /v1/workspaces/{ws}/sessions/{sid}/steer

Injects steering text into the running turn (`SteerIn`):

```json
{"text": "prefer the smaller refactor"}
```

`200`: `{"ok": true}`. `409 not_running` when no turn is active. The stream
echoes a `steer.accepted` event.

## Asks (approvals and questions)

When a turn hits a gated tool (in `ask` mode) or the agent calls `ask_user`,
the turn parks: an ask is created with a fresh id, published on the stream as
`ask.pending`, and held with **no timeout** until answered, cancelled, or the
turn is interrupted. Session status becomes `waiting_ask`. Any authenticated
client may answer.

Ask shape (`PendingAsk.as_dict()`):

```json
{"id": "7d2f9a3b1c4e5d60", "kind": "approval", "payload": {...},
 "created": "2026-07-23T12:35:00+00:00"}
```

`kind` is `"approval"` or `"question"`.

Approval payload:

```json
{"tool_name": "bash", "args": {"command": "rm -rf build"},
 "tool_call_id": "call_abc123"}
```

Question payload (from `ask_user`):

```json
{
  "questions": [
    {
      "question": "Which database should I target?",
      "header": "database",
      "multi": false,
      "options": [
        {"label": "postgres", "description": "the production default"},
        {"label": "sqlite", "description": null}
      ]
    }
  ]
}
```

### GET /v1/workspaces/{ws}/sessions/{sid}/asks

`200`: `{"asks": [<ask>, ...]}` — empty when no host is live.

### POST /v1/workspaces/{ws}/sessions/{sid}/asks/{aid}

Request body (`AskAnswerIn`). Fields are interpreted in this precedence:

1. `answers` present → an `ask_user` answer.
2. `cancel: true` → cancel the question (the tool returns "no answer").
3. otherwise → an approval verdict from `approve` (+ optional `reason`).

Approve a tool call:

```json
{"approve": true}
```

Deny with a reason (the reason is surfaced to the model):

```json
{"approve": false, "reason": "don't delete build artifacts"}
```

Answer a question — keys are each question's `header` (the question text when
`header` is blank); values are the chosen label, or a list of labels for
`multi` questions (free-text answers are also accepted):

```json
{"answers": {"database": "postgres"}}
```

`200`: `{"ok": true}`. `404 not_found` when there is no live host, or the ask
id is unknown / already answered.

## History

### GET /v1/workspaces/{ws}/sessions/{sid}/history

Reads the persisted session file. Query params: `offset` (default 0,
clamped >= 0) and `limit` (default 100, clamped >= 1); non-integers → `400`.

`200`:

```json
{
  "id": "a1b2c3",
  "name": "fix the login bug",
  "model": "anthropic/claude-sonnet-4-6",
  "message_count": 42,
  "offset": 0,
  "history_seq": 137,
  "messages": [ ... ]
}
```

`messages` are the raw persisted Pydantic AI message dicts as stored in the
session file — their internal structure is Pydantic AI's serialization, not
defined by the server schema.

`history_seq` is the stream sequence number up to which this on-disk snapshot
is consistent. It advances only when a `turn.finished`, `turn.error`, or
`compaction.finished` event is published (each published *after* the
corresponding persist completes). Clients use it to reconcile stream and
history: a stream event with `seq <= history_seq` is already reflected in
these messages (a resync echo); `seq > history_seq` is an in-flight tail not
yet persisted. A session with no live bus reports `history_seq: 0`.

`500 unreadable` if the session file cannot be parsed.

## Images

### GET /v1/workspaces/{ws}/sessions/{sid}/images/{sha}

Serves bytes from the session's image cache (images the harness ingested,
referenced internally as `marim-image-cache://` refs). `sha` must be 64
lowercase hex chars (the content SHA-256); anything else is `404`.

`200`: raw image bytes with the detected `Content-Type` (fallback
`application/octet-stream`) and
`Cache-Control: public, max-age=31536000, immutable`. `404` when no cached
file matches.

## Streaming: the WebSocket

```
GET /v1/workspaces/{ws}/sessions/{sid}/ws
Authorization: Bearer <token>
```

- Auth is the `Authorization: Bearer` header on the upgrade request. Invalid
  token → close code `4401`; unknown workspace/session → close code `4404`.
- One JSON **text frame per event**, in the envelope (`Event.as_dict()`):

```json
{"seq": 42, "ts": "2026-07-23T12:35:01.123456+00:00",
 "type": "text.delta", "data": {"text": "Sure — "}}
```

- `seq` is monotonic per session and is the resume cursor.
- The stream is server→client only; clients send nothing (received frames are
  used solely to detect disconnect).
- This WebSocket is the only streaming transport — there is no
  `text/event-stream` (SSE) endpoint.

### Resume and gaps

Reconnect with `?after_seq=<last seen seq>` to replay missed events from the
session's replay ring (last 1000 events). If the resume point has fallen off
the ring, the first frame is a synthetic gap marker:

```json
{"seq": 120, "ts": "...", "type": "stream.gap", "data": {"resync": "history"}}
```

On `stream.gap`, re-sync via `GET .../history` and use its `history_seq`
watermark to dedupe against subsequent live events.

The event bus (and its ring) outlives host eviction: it lives for the
daemon's lifetime, or until the session is deleted. A connected subscriber
also *blocks* idle eviction, so a watching client never sees its stream
silently reset. Across a daemon restart, `seq` restarts from 1 — treat a
reconnect after daemon restart as a full resync.

### Event types

Turn stream (from the model loop; only these four stream-event kinds are
surfaced):

| Type             | `data`                                        |
| ---------------- | --------------------------------------------- |
| `text.delta`     | `{"text": "<chunk>"}`                         |
| `thinking.delta` | `{"text": "<chunk>"}`                         |
| `tool.call`      | `{"name": "...", "args": {...}, "id": "..."}` |
| `tool.result`    | `{"id": "...", "content": "<stringified>"}`   |

Turn lifecycle:

| Type            | `data`                                                       |
| --------------- | ------------------------------------------------------------ |
| `turn.started`  | `{"turn_id": "...", "prompt": "..."}`                        |
| `turn.finished` | `{"turn_id": "...", "output": "...", "usage": {...}}` — or `{"turn_id": "...", "interrupted": true}` for an interrupted turn |
| `turn.error`    | `{"turn_id": "...", "error": "<detail>"}`                    |
| `steer.accepted`| `{"text": "..."}`                                            |

The `usage` object on `turn.finished` (`usage_summary`):

```json
{"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
 "uncached_input_tokens": 0, "cache_read_tokens": 0,
 "cache_write_tokens": 0, "cost_usd": 0.0123, "cost_is_exact": false}
```

Asks and status:

| Type             | `data`                                                      |
| ---------------- | ----------------------------------------------------------- |
| `ask.pending`    | the ask object (`{"id", "kind", "payload", "created"}`)     |
| `ask.resolved`   | `{"id": "...", "answer": {...}}` — or `{"id": "...", "cancelled": true, "reason": "interrupted"}` |
| `session.status` | `{"status": "idle" \| "running" \| "waiting_ask"}`          |

Session and housekeeping:

| Type                  | `data`                                    |
| --------------------- | ----------------------------------------- |
| `session.renamed`     | `{"from": "<old>", "to": "<new>"}`        |
| `tasks.changed`       | `{}` (re-fetch task state out of band)    |
| `jobs.changed`        | `{}`                                      |
| `compaction.started`  | `{}`                                      |
| `compaction.finished` | `{"before": <n>, "after": <n>}`           |
| `subagent.event`      | `{"stream_id": "...", "event": {...}}` — `event` is a stream-event dict with an inner `"type"` of `text`/`thinking`/`tool_call`/`tool_result` |
| `stream.gap`          | `{"resync": "history"}`                   |

## Lifecycle semantics

**Host creation.** A session's live host (a full harness) is created lazily
on the first `POST .../messages` after daemon start or eviction. Creation
runs the same connect + session-start lifecycle headless uses; resumed
sessions reload their persisted history.

**One turn at a time.** Each host has a single worker draining a bounded
queue (8). `submit` returns immediately with a `turn_id`; results arrive on
the stream.

**Idle eviction (`--idle-ttl`).** A background sweep (interval:
`min(idle_ttl, 60)` s) tears down a host that has, for at least `idle_ttl`
seconds: no running turn, an empty queue, no pending asks, and **no
WebSocket subscriber**. Teardown persists the session and closes the harness
cleanly; the session remains resumable from disk, and its event bus (with
replay ring) stays alive so a returning client can resume with `after_seq`.
Eviction and host creation are serialized per session, so they cannot race.

**Interrupts.** `POST .../interrupt` cancels the turn task. Rollback is the
harness's standard resumable-flush: the dirty mid-approval history is never
persisted, so the session returns to its last clean baseline. Any asks left
by the interrupted turn are cancelled (`ask.resolved` with
`"cancelled": true`), and the stream sees `turn.finished` with
`"interrupted": true`.

**Graceful shutdown.** On daemon shutdown, every live host is interrupted
(resumable flush), parked asks are cancelled, and each session is persisted.

**Daemon crash with a parked ask.** The un-persisted mid-approval history is
lost by design; on restart the session resumes from its last clean baseline.

## curl walkthrough

```sh
TOKEN=$(cat ~/.local/share/marim-harness/server/token)
BASE=http://127.0.0.1:8642/v1
AUTH="Authorization: Bearer $TOKEN"

# 1. Create a managed workspace (clone a repo)
curl -s -X POST "$BASE/workspaces" -H "$AUTH" -H 'content-type: application/json' \
  -d '{"name": "demo", "git_url": "https://example.com/repo.git"}'
# -> 201 {"id":"demo","name":"demo","path":"...","kind":"managed","created":"..."}

# 2. Create a session in ask mode
curl -s -X POST "$BASE/workspaces/demo/sessions" -H "$AUTH" \
  -H 'content-type: application/json' -d '{"name": "first run", "mode": "ask"}'
# -> 201 {"id":"<SID>","name":"first run"}

# 3. Submit a prompt (starts the live host, queues a turn)
curl -s -X POST "$BASE/workspaces/demo/sessions/$SID/messages" -H "$AUTH" \
  -H 'content-type: application/json' -d '{"prompt": "summarize this repo"}'
# -> 202 {"turn_id":"..."}

# 4. Follow the stream (websocat; any WS client works)
websocat -H="Authorization: Bearer $TOKEN" \
  "ws://127.0.0.1:8642/v1/workspaces/demo/sessions/$SID/ws"
# frames: turn.started, text.delta..., tool.call, ask.pending, ...

# 5. When an ask.pending arrives (kind "approval", id <AID>), approve it
curl -s -X POST "$BASE/workspaces/demo/sessions/$SID/asks/$AID" -H "$AUTH" \
  -H 'content-type: application/json' -d '{"approve": true}'
# -> {"ok":true}; the turn resumes and eventually emits turn.finished

# Reconnect later without losing events:
#   ws://.../ws?after_seq=<last seq you saw>
```
