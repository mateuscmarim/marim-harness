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
| `--wide`            | off                      | Draw `--qr` with half-blocks, not sextants     |
| `--sixel`           | auto-detected            | Draw `--qr` as a sixel image (`--no-sixel` off)|

The state dir is `$XDG_DATA_HOME/marim-harness/server` (default
`~/.local/share/marim-harness/server`). It holds the bearer token file, the
workspace registry (`workspaces.json`), and by default the managed-workspaces
root.

While the daemon is running it also holds `runtime.json` —
`{"host": ..., "port": ..., "pid": ..., "started": ...}` — written at startup
and removed on clean exit, so a client on the same machine can find the daemon
without being told the port. A killed daemon leaves the file behind, so treat
it as a hint and let the connection attempt be the authoritative answer.

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
               [--wide] [--sixel | --no-sixel]
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

**Size and shape.** There are three renderings, and the terminal picks between
them:

- **Sixel**, when the terminal advertises it in reply to a `DA1` query. The
  code is drawn in pixels rather than characters, so it comes out square, at
  whatever resolution the display can show, with no font involved. This is the
  default wherever it works. `--no-sixel` forces characters; `--sixel` forces
  the image, for terminals that render sixel without advertising it. The probe
  is a single escape sequence with a 0.35s timeout, and a terminal that doesn't
  answer just gets the character rendering.
- **Sextants** (Unicode 13's Symbols for Legacy Computing), the character
  default: 2×3 modules per cell puts a typical pairing payload in 27 columns by
  18 rows, and the whole block inside a 25-line terminal. Each module ends up a
  third taller than it is wide; scanners correct for it, since the finder
  patterns give them a perspective transform and a uniform stretch is exactly
  what that undoes.
- **Half-blocks**, via `--wide`: 1×2 per cell, square modules, five rows
  taller. For fonts that predate Unicode 13 and draw the sextants as empty
  boxes — the code prints that hint under itself, because a QR made of tofu is
  a puzzle otherwise.

The four-module quiet zone is not adjustable in any of them — scanners need it.

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
`busy` (409), `claimed` (409), `not_running` (409), `queue_full` (429),
`host_closed` (404), `unreadable` (500), `trust_store_error` (500).

## Safe mutation retries (opt-in)

Every HTTP response advertises `X-Marim-Idempotency: v1`. Clients can learn this
passively from an ordinary read; no extra capability request is required. After
observing it, use `/v1/idempotent/workspaces...` in place of `/v1/workspaces...`
for **any POST or DELETE** in the endpoint table. The protected aliases use the
same body, query parameters, response JSON and status codes as the original
routes, with one mandatory request header:

```http
Idempotency-Key: 4ea1b17f-6cdf-4e71-9b62-9db82d4f4e5d
```

Use a fresh canonical lowercase UUID for each logical operation. Retry that
operation using exactly the same key, method, target, query bytes and body bytes.
All keys share one daemon-wide namespace; changing any of these inputs while
reusing a key returns `409 idempotency_conflict` without another effect. Missing,
malformed or repeated key headers return `400 bad_request`. Authentication is
checked before reading the request body or consulting the operation ledger,
including on replay; token rotation does not invalidate recorded operations.

The server commits a claim before invoking the handler, then records its status,
body and relevant response headers durably before sending the result. Both
successful responses and ordinary handler rejections are recorded. A recorded
response carries `Idempotency-Status: completed` and echoes `Idempotency-Key`.
Repeating a completed request returns its recorded result with those headers and
`Idempotency-Replayed: true`, even if its session or workspace has since been
deleted. The response describes the original outcome, not the current state.
Pre-claim failures, authentication failures and unresolved claims never carry
`Idempotency-Status: completed`; a later authentication rejection alone cannot
settle an earlier lost response. A recorded server error is still an error:
`completed` says its outcome is recorded, not that the requested effect succeeded.

Additional standard-envelope errors are:

| Status / code | Meaning |
| --- | --- |
| `503 idempotency_in_progress` | The same operation is executing in this app instance. `Retry-After: 1`; retry with the same key. |
| `503 idempotency_unknown` | A claim exists without a durable response, following interruption, restart, another app instance, or a failure to save the result. It will **never execute again** with that key. Inspect server state before deciding on another logical operation. |
| `503 idempotency_unavailable` | The ledger could not be consulted or its claim committed. This request did not start the handler. A retry must still use the same key. |

Clients should bound automatic retries (for example, two retries on transport
errors or HTTP 5xx), retain uncertainty when a durable result is unavailable, and
never switch a protected request to the original route. The distinct alias is
intentional: an older server that ignores unknown headers returns 404 for the
protected path instead of executing a supposedly protected mutation. Clients
that have not observed the capability should retain conservative legacy behavior.
The original routes retain their existing semantics and do not deduplicate keys.

The ledger lives in `<state-dir>/idempotency/operations.sqlite3`, with directory
permissions 0700 and database permissions 0600. It stores request fingerprints
and response data, never raw request bodies or bearer tokens. Responses are
capped at 1 MiB; a larger result remains unresolved. Records are retained without
automatic expiry or pruning, including unresolved claims. Back up the ledger
with server state; deleting it removes retry protection for previously used keys.
This is not an offline mutation queue or an exactly-once guarantee: a process
can stop between applying an effect and recording its response. Keeping that
claim unresolved prevents a retry from repeating an effect that may have occurred.
WebSocket and streaming read behavior are unchanged.

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
| GET    | `/v1/workspaces/{ws}/sessions/{sid}/jobs`         | Background jobs (live + persisted) |
| GET    | `/v1/workspaces/{ws}/sessions/{sid}/jobs/{id}`    | One job with its prompt and result |
| POST   | `/v1/workspaces/{ws}/sessions/{sid}/jobs/{id}/cancel` | Cancel a running job           |
| POST   | `/v1/workspaces/{ws}/sessions/{sid}/subagents/{stream_id}/resume` | Resume an interrupted spawn |
| GET    | `/v1/workspaces/{ws}/sessions/{sid}/images/{sha}` | Cached image bytes                 |

## Health

### GET /v1/health

No auth. Returns `200`:

```json
{"status": "ok", "version": "0.9.1"}
```

`version` is the installed `marim-harness` package version captured when this
server app starts, without a `v` prefix. It is `null` when package metadata is
unavailable; older servers omit it. Upgrading the package requires a server
restart before the reported version changes. The response retains
`Cache-Control: no-cache`. Version is informational; use capabilities for feature
availability.

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
              "advisor_model": null, "thinking": null, "mode": null,
              "model_label": "anthropic/claude-sonnet-4-6"},
  "status": "idle",
  "queued": 0,
  "pending_asks": [],
  "trust_prompt_pending": false,
  "workspace_path": "/home/me/proj",
  "usage": {"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500,
            "cache_read_tokens": 0, "cost": 0.0042},
  "compact_threshold": 160000,
  "context": {"used": 27516, "window": 200000},
  "quota": "quota 11% (5h) · 59% (1w)"
}
```

`queued` is the number of prompts waiting behind the running turn.

The response is what an attaching client seeds its view from. When the
session's live host is loaded, the `session` object's `mode`, `model`, and
`thinking` reflect its live values (a mode cycled in the TUI is never persisted,
so the listing's value may lag). `advisor_model` is the persisted next-turn
selection, which can differ from the active turn's snapshot. The response also
gains `model_label` (the display form of the model, provider-aware).
`workspace_path` is the workspace's root on disk (what an attached TUI
shows as its subtitle and resolves relative paths against). `usage` is
the session-cumulative token split with cost, and `compact_threshold`
the token budget the context gauge is denominated against; both are
`null` while no host is loaded (a cold session has nothing live to
report — the listing's `tokens` is the persisted estimate).

`context` and `quota` exist for the CLI backends. Under `claude-cli` or
`codex-cli` the conversation lives inside the CLI and marim's history is a
mirror of it, so `context` is the backend's own reading: `used` is the
prompt size of its most recent model request (cache-inclusive input tokens,
what the CLI's own `/context` counts) and `window` the model's context
window, or `null` until the backend has said (the first Claude turn learns
it at its `result`). A client that shows a context gauge should prefer
this pair over `compact_threshold` and the estimate whenever it is
present. `quota` is the rendered subscription rate-limit hint the status
bar shows (`quota 11% (5h) · 59% (1w)`), refreshed once per turn. Both are
`null` under marim's own providers, while no host is loaded, and — for
`context` — on a session whose backend has not reported yet.

### DELETE /v1/workspaces/{ws}/sessions/{sid}

Refuses while a turn is running: `409 busy` ("interrupt it first"). Also
refuses a session owned by another live process: `409 claimed` (see below).
Otherwise closes the live host (if any), deletes the session file, and
reclaims all server state for the session (event bus included).

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
  ],
  "trigger": "user"
}
```

`attachments` is optional. Invalid base64 returns `400`.

`trigger` (optional, default `user`) is who is speaking, and is echoed on
the turn's `turn.started` event: `user` is a typed prompt; `system` is a
client-side slash command's own prompt (`/remember`, `/skill`), which
clients render without a user bubble — the same as the in-process TUI.
`autonomous` is **not** accepted (`400`): the daemon's own wake driver is
the only thing that may wake a session.

`202`: `{"turn_id": "9f3ab1c2d4e5f607"}` — the turn is queued; follow
progress on the WebSocket stream. Errors:

- `429 queue_full` — the per-session turn queue is at capacity.
- `404 host_closed` — the host was torn down mid-submit; retry.
- `409 claimed` — another live process (a local TUI or headless run) owns this
  session. The code is returned by `POST /messages` and `DELETE
  /workspaces/{wid}/sessions/{sid}` when the session is owned elsewhere. For
  headless and daemon runs, a claim is held for its holder's lifetime, so
  unlike `busy` this is not transient and retrying will not clear it; the
  message names the holder. Close the session there first.

  Claims follow the active view: an in-TUI switch (/sessions or the picker)
  claims the target session and releases the one being left; switching onto a
  session owned by another process is refused with a notice naming the holder
  (kind, pid, endpoint). /new releases the outgoing claim and claims the fresh
  session. The claim lifecycle for headless and daemon paths is unchanged
  (held until exit; the daemon releases through SessionHost teardown).

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
echoes a `steer.accepted` event (its `attachments` count is always `0` for a
steer sent over HTTP; the TUI can attach pasted images to a steer).

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

## Jobs

Background work the agent started (detached `bash` commands, background
sub-agent spawns) lives on the session's job registry, which the daemon
owns. These routes are the authority an attached TUI reads its jobs panel
and sub-agent cards from; the `jobs.changed` stream event is only the
trigger to re-read them.

Agents spawned internally by a `claude-cli` or `codex-cli` session also appear
here, with `backend_owned: true`. Their lifecycle is observed at the backend
reader, including completions between parent turns. The CLI retains execution
and result delivery; these rows never trigger marim's autonomous job wake.
Clients refreshing after `jobs.changed` should send `Cache-Control: no-cache`
so a fresh cached empty list cannot hide a newly started agent.

### GET /v1/workspaces/{ws}/sessions/{sid}/jobs

`200 {"jobs": [JobDto, ...]}`, running jobs first, then settled ones by
`finished_at` descending. With a live host the list is its registry (live
rows plus the settled history the session file carried when it loaded);
with no host loaded it is the settled history persisted in the session
file, so a spawn that finished in an earlier daemon life is still listed.
`Cache-Control: max-age=30`.

```json
{
  "id": "job-3",
  "kind": "agent",
  "label": "explore: find the flaky test",
  "status": "running",
  "started_at": "2026-09-13T10:00:00+00:00",
  "finished_at": null,
  "stream_id": "sg-7f3a",
  "duration_secs": null,
  "usage": null,
  "tool_count": null,
  "result_tail": null,
  "prompt": null
}
```

- `kind` is `bash` or `agent`; `status` is `running`, `done`, `failed`,
  `timeout` or `cancelled`.
- `stream_id` is set on agent jobs and matches the `subagent.*` events and
  the transcript sidecar of that spawn.
- `duration_secs`, `usage` (`{"input", "output"}`) and `tool_count` come
  from the spawn's sidecar meta once written; `null` for bash jobs and for
  a spawn still running.
- `result_tail` is the settled result's whitespace-collapsed tail (the
  last ~200 characters, the same verdict-carrying tail the persisted
  history keeps); `null` while the job runs.
- `prompt` is always `null` on the list (see the detail route).
- `backend_owned` is `true` for observed CLI agents, `false` for native jobs.
  Older servers omit it. Prompts and metrics unavailable on the backend's
  public event stream remain `null`; no values are invented. For example,
  Codex's `subAgentActivity` spawn ping names the agent but carries no prompt.
  Live detail returns the completion report; persisted history retains the
  same bounded result tail as other jobs. If backend tracking ends before a
  terminal report, the row fails with an explicit status-unavailable message.

### GET /v1/workspaces/{ws}/sessions/{sid}/jobs/{id}

The same `JobDto` plus `prompt` (the spawn's task or the bash command)
and the full `result` (the job's output so far, or its final output).
`404 job_not_found` for an unknown id, and for every id while no host is
loaded (a cold session has no readable job output).

### POST /v1/workspaces/{ws}/sessions/{sid}/jobs/{id}/cancel

Stops a running job (kills its process if any, cancels its task).
`200 {"ok": true, "message": "cancelled job-3"}` — `message` is the
registry's own wording, so an attached TUI's `/jobs cancel` reads exactly
as the in-process one. `409 already_settled` when the job already
finished; `404 job_not_found` for an unknown id, or for any id while no
host is loaded (history rows are settled by definition).
Running backend-owned jobs return `409 backend_managed`: cancellation must
be requested through their owning CLI, and this route does not pretend to stop them.

### POST /v1/workspaces/{ws}/sessions/{sid}/subagents/{stream_id}/resume

Resumes an interrupted sub-agent spawn from its persisted transcript as a
new background job on the daemon (what `r` does in the TUI's sub-agents
screen). Loads the host when cold, through the same `409 claimed` /
`404 not_found` mapping `POST messages` uses. `201 {"job_id": "job-4"}`
when the resume started — watch `jobs.changed` and the spawn's
`subagent.*` events from there. `409 resume_refused` with a readable
`message` when the runner declines: no resumable transcript, the spawn
already finished, a resume already in flight, or a session without the
resume seam.

## Workspace files

### GET /v1/workspaces/{ws}/sessions/{sid}/files?path={path}

Downloads a regular file from the session's workspace. Requires the same bearer
token and existing workspace/session pair as history and images; a running host
is not required. `path` is one URL-encoded filesystem path, relative to the
workspace or absolute inside it. Clients remove link-only decorations such as
`file://` and source-line suffixes before sending the path. Spaces and Unicode
filenames are supported.

`200`: streamed file bytes (at most **50 MiB**) with the extension-derived
`Content-Type` (fallback `application/octet-stream`), `Content-Length`,
`Content-Disposition: attachment; filename*=UTF-8''...`,
`Cache-Control: private, no-store`, and `X-Content-Type-Options: nosniff`.
The filename is the basename, never an absolute host path. Downloads are bounded
to the length observed when opened; files are not snapshotted, so edits during
a download can change its contents or interrupt it. The client should retry
an incomplete download. Range requests are not supported.

`400 bad_request`: missing, empty, duplicate or malformed path, or a file over
the limit. `404 not_found`: unknown workspace/session, missing or inaccessible
file, a path outside the workspace, any `..` traversal component, a directory,
or another nonregular file. All symlinks are refused, including links to files
inside the workspace; directory traversal and the final file open are anchored
to descriptors with no symlink following to prevent replacement races. File
errors do not reveal the requested host path.

This initial route exposes workspace files only. External scratchpad artifacts
are not downloadable; copy an artifact into the workspace before linking it.
Stable artifact IDs and artifact retention are not part of this contract.

## Images

### GET /v1/workspaces/{ws}/sessions/{sid}/images/{sha}

Serves bytes from the session's image cache (images the harness ingested,
referenced internally as `marim-image-cache://` refs, and every image a
tool returned during a turn — the `images` references on `tool.result`
events point here). `sha` must be 64 lowercase hex chars (the content
SHA-256); anything else is `404`.

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

Turn stream (model output and display-only backend activity):

| Type             | `data`                                        |
| ---------------- | --------------------------------------------- |
| `text.delta`     | `{"text": "<chunk>"}`                         |
| `thinking.delta` | `{"text": "<chunk>"}`                         |
| `tool.call`      | `{"name": "...", "args": {...}, "id": "..."}` |
| `tool.result`    | `{"id": "...", "content": "<stringified>", "status": "done"\|"failed"\|"denied", "images": [{"sha": "...", "media_type": "image/png", "bytes": 4096}]}` — `images` lists any image the tool returned (a `read_file` on a PNG, an MCP image block); `content` carries a text placeholder in its place, and the bytes are served at `GET .../images/{sha}` the moment the event is published |

Backend lifecycle events use the same ordered session bus:

| Type | `data` |
| --- | --- |
| `session.notice` | Required `message`; optional `id`, `backend`, `kind`, `severity`, `data`. Older message-only events remain valid. |
| `session.backend_state` | `inventory` and `telemetry` snapshots, replacing previous backend display state. |
| `backend.task` | `id`, `backend`, `description`, `status` (`running`, `completed`, `failed`, `interrupted`); update one card by id. |

Session GET also exposes `backend_inventory` and `backend_telemetry` objects
(empty when unavailable). These are observations, never turn-completion or
approval-resolution signals. Backend compaction produces `session.notice`,
not `compaction.finished`; clients must preserve cached mirror history.

Persisted lifecycle notices are blank text parts with
`provider_details.backend_notice`, or ordered `metadata.backend_notices_after`
records on a preceding tool return. Restore these as system notices at that
position and deduplicate live/history overlap by `id` (not message text).
See [lifecycle capabilities](cli-lifecycle-capabilities.md) for field sources.

Under the `claude-cli` and `codex-cli` providers the CLI runs its own tools,
so those calls never enter the model loop's stream; the daemon publishes them
as the same `tool.call` / `tool.result` events (with `status`), so a client
renders a CLI provider's tool calls with no special case. They are persisted
the same way: once the turn settles, `GET .../history` carries them as
ordinary `tool-call` / `tool-return` parts (the prose is split around them,
exactly as with marim's own tools), so a transcript rebuilt from history
matches what streamed. A tool result longer than 16k characters is cut with
a `…[truncated N chars]` marker in the persisted copy; a call the CLI never
answered (the turn was interrupted mid-tool) is persisted with an
`interrupted` return so the history stays resumable.

Both CLIs can spawn sub-agents of their own (Claude's Agent/Task tool,
Codex's collab `spawn_agent`). marim demuxes them out of the stream and
publishes each as a `spawn_agent` `tool.call` on the parent's stream (its
`id` is the child's `stream_id`) followed by the child's own traffic as the
`subagent.*` family below — `subagent.model` (`codex-cli:<model>` /
`claude-cli:<model>`), `subagent.event` for its text and tool calls,
`subagent.usage`, `subagent.notice` for collab follow-ups (`wait`,
`send_input …`) — and a `tool.result` when the agent settles. A client
that renders marim's own nested spawns needs no special case.

Turn lifecycle:

| Type            | `data`                                                       |
| --------------- | ------------------------------------------------------------ |
| `turn.started`  | `{"turn_id": "...", "prompt": "...", "trigger": "user"\|"system"\|"autonomous"}` — `user` is a typed prompt (clients render it as the user's message), `system` a slash command's own prompt (`/remember`, `/skill`; nothing to show), `autonomous` a wake-on-job-completion turn with an empty prompt |
| `turn.usage`    | `{"turn_id": "...", "total_tokens": <n>}` — the running total of the turn's current model run, republished whenever it changes (≈ once per model response); the live in-flight counter, not the per-turn summary |
| `turn.finished` | `{"turn_id": "...", "output": "...", "usage": {...}}` — or `{"turn_id": "...", "interrupted": true}` for an interrupted turn |
| `turn.error`    | `{"turn_id": "...", "error": "<detail>"}`                    |
| `steer.accepted`| `{"text": "...", "attachments": <n>}` — `n` image attachments rode along with the text |

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
| `session.ttft`        | `{"seconds": <float>}` — time to first token of the latest streamed request |
| `session.mode_changed`| `{"mode": "plan" \| "ask" \| "auto"}`     |
| `session.notice`      | `{"message": "..."}` — a system notice line for the transcript |
| `tasks.changed`       | `{}` (re-fetch task state out of band)    |
| `jobs.changed`        | `{}` (re-read `GET .../jobs`; the registry changed — a job registered, settled or cancelled) |
| `compaction.started`  | `{}`                                      |
| `compaction.finished` | `{"before": <n>, "after": <n>}`           |
| `subagent.event`      | `{"stream_id": "...", "event": {...}}` — `event` is a stream-event dict with an inner `"type"` of `text`/`thinking`/`tool_call`/`tool_result` |
| `subagent.notice`     | `{"stream_id": "...", "message": "..."}`  |
| `subagent.model`      | `{"stream_id": "...", "model": "..."}`    |
| `subagent.thinking`   | `{"stream_id": "...", "level": "..."}`    |
| `subagent.usage`      | `{"stream_id": "...", "usage": {...}}` — `usage` is a `usage_summary` dump |
| `stream.gap`          | `{"resync": "history"}`                   |

Workflow orchestration (`run_workflow`):

| Type                     | `data`                                                                         |
| ------------------------ | ------------------------------------------------------------------------------ |
| `workflow.spawned`       | `{"stream_id": "...", "spawn_type": "...", "task": "...", "parent_tool_call_id": "..."}` |
| `workflow.started`       | `{"tool_call_id": "...", "title": "..."}`                                      |
| `workflow.logged`        | `{"tool_call_id": "...", "message": "..."}`                                    |
| `workflow.finished`      | `{"tool_call_id": "...", "outcome": "...", "failed": <bool>}`                  |
| `workflow.spawn_finished`| `{"stream_id": "...", "report": "..."}`                                        |

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

**Attaching a local TUI.** `marim --session <id>` (or `marim --resume`
when the latest session is the daemon's) does not take a daemon-owned
session over: the launch reads the session's claim sidecar, and when the
holder is a `marim serve` daemon whose endpoint answers, whose pid matches
the daemon's runtime record, whose token is readable from the daemon's
state directory (`$XDG_DATA_HOME/marim-harness/server/token`) and which
lists this workspace, the TUI starts as a client of this API instead —
`GET session` seeds its status bar, `GET jobs` seeds its jobs panel
(re-read on every `jobs.changed`, with `GET jobs/{id}` fetched for a
spawn card that settles while the TUI is watching), `GET history` replays
the transcript, the WebSocket (from `history_seq`) streams the live tail,
`GET asks` reconciles the panels, and every action (`POST messages` with
the attachments base64-encoded, `interrupt`, `steer`, `asks/{aid}`, `mode`,
`model`, `jobs/{id}/cancel`, `subagents/{stream_id}/resume`) goes through
the routes above. The daemon's jobs list is what decides whether a replayed
spawn card is still running or finished; only when that read fails does
the TUI fall back to leaving every running card pending. Prompts sent from any other
client (a phone, curl) show up in the attached TUI and vice versa, since
they are the same session on the same host. The status bar shows `daemon`
(`daemon · reconnecting…` while the socket is re-established with backoff;
`daemon · lost` after 60 s or an auth/not-found rejection). On
`stream.gap` the TUI re-renders from `GET history` and re-attaches at its
boundary. Commands that need the session's own process (`/clear`, `/new`, `/compact`, `/rewind`, `/name`, `/switch`, `/skill`, `/mcp`, `/jobs wake`, `/worktree`, `/plugin`, `/trust`, `/advisor`, `/think`, `!` shell passthrough,
steering with an image, switching sessions in place) are refused with a notice
while attached; `/jobs` (list, `output`, `cancel`) and resuming a sub-agent
act on the daemon's jobs, and autonomous wake stays the daemon's to drive. When any probe fails the launch prints why
(`not attaching: …`) and falls back to the usual "already open" refusal;
headless runs never attach. The daemon keeps the claim throughout — an
attached TUI holds no claim of its own — so the session is still
`409 claimed` for a second local process, and it stays alive on the
daemon after the TUI quits.

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
