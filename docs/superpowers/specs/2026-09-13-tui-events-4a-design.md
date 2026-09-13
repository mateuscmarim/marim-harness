# Phase 4a — Remote attach: the TUI drives a daemon-owned session

**Date:** 2026-09-13
**Status:** Draft for approval
**Parent spec:** `docs/superpowers/specs/2026-08-29-cross-process-session-events-design.md` (phase 4)
**Prerequisites (landed):** phase 2 claims (#106, #107), phase 3a (#116), phase 3b (#118)
**Branch:** `feat/tui-events-4a`

## Context

After 3b the TUI runs its turns on an in-process `SessionHost` and renders
only from bus events: the transcript, asks, busy state and every turn-end
effect come off the wire. The parent spec's phase 4 is "swap the transport":
a WebSocket-fed `Subscription`, a `RemoteSessionHost` over the REST routes,
daemon-owned sessions in the picker — "small if phases 1–3 landed correctly,
because by then the TUI cannot tell local from remote."

The turn path cannot tell. The rest of the app can: `HarnessApp` and its
collaborators still reach into `self.harness` about 150 times outside the
turn path (36 in `app.py`, 31 in `commands.py`, 24 in `settings.py`, 19 in
`session_view.py`, 12 in `pickers.py`, 10 in `status_bar.py`, the rest
scattered) — history replay, the status bar, MCP connect, session-start hooks,
the trust prompt, checkpoints, sessions ops, `/compact`, `/model`, skills,
plugins, worktrees, jobs, shell passthrough. A remote TUI has no `Harness`,
so every one of those is a decision, not plumbing.

This spec therefore splits phase 4 the way phase 3 was split:

- **4a (this spec):** the seam (`SessionLink`), `RemoteSessionHost`, the
  WebSocket subscription, launch-time attach to a session the daemon already
  owns, attach-time reconciliation, image sha references on `tool.result`,
  daemon-owned rows in the picker. Process-local features refuse cleanly on
  a remote session.
- **4b (later):** daemon-hosted by default (the parent's "daemon reachable →
  attach" row), workspace auto-register, switching between local and remote
  sessions inside a running TUI, the widened command surface (`/compact`,
  `/name`, rewind, jobs, settings over REST), takeover when the daemon dies.

## Decisions

**Attach only when the daemon already owns the session.** Today `marim
--resume` on a session the daemon is driving prints "already open in daemon
(pid N) at http://…" and exits 2. 4a turns that refusal into an attach. A
session nobody owns keeps opening in-process, exactly as today — the user's
`marim-serve.service` running in the background must not silently turn every
`marim` launch into a reduced-feature remote client. The parent's end state
(always daemon-hosted) is 4b, once the command surface is wide enough that
remote is not a regression. This is a deliberate, documented deviation from
the parent's decision table.

**One seam, async.** The TUI stops holding a `SessionHost` directly and holds
a `SessionLink`: the command surface plus an event feed plus a small read
model. `LocalSessionLink` wraps the in-process host and harness;
`RemoteSessionHost` is the same surface over HTTP + WebSocket. The command
methods are coroutines on both — a remote submit is an HTTP round trip, and
the local wrapper awaiting nothing costs nothing. `SessionHost` itself is
unchanged (its sync `submit` is what the HTTP route calls).

**`Subscription` is the transport seam, as the parent spec said.** The
remote feed implements `next_event(timeout)` / `close()` over a WebSocket and
nothing else; the pump does not change.

**The harness becomes optional on the app, and pyright does the triage.**
`HarnessApp.harness: Harness | None`. Every one of the ~150 reaches then
fails to type-check until it is sorted into one of three bins (table below):
read model, link command, or local-only. Nothing is guarded by a runtime
`if remote:` scattered through handlers; local-only code calls one helper.

Rejected: *keep a local `Harness` as a read mirror while the daemon drives.*
It would make the 150 reaches compile but reintroduce two sources of truth
(history, model, mode) that drift the moment the daemon runs a turn — the
exact thing this design exists to eliminate.

Rejected: *sync link with fire-and-forget commands.* `submit` must hand back
the `turn_id` for the 3b submit latch; a remote client only learns it from
the `202`.

## Goals

1. `marim --resume` / `marim --session <id>` on a daemon-owned session attaches
   as a client: transcript replayed from `GET history`, live events over the
   WebSocket, turns submitted with `POST messages`, Esc → `POST interrupt`,
   steer → `POST steer`, asks answered with `POST asks/{aid}`, ctrl+t →
   `POST mode`, `/model` → `POST model`. Two clients (the TUI and, say, the
   mobile app) co-drive one session: an ask answered on either side dismisses
   on both, with the 3b "answered from another client" notice.
2. Attach mid-turn is correct: history, then the in-flight tail from the
   replay ring, then `GET asks` reconciled against what the tail mounted.
3. `tool.result` carries image sha references (additive), closing the
   parent's phase-1 leftover.
4. The session picker marks daemon-owned rows and refuses them with a hint
   naming the command that attaches (4a has no in-app local↔remote switch).
5. Connection loss is visible and recoverable: the feed reconnects with
   `?after_seq`, the status bar says so, and a daemon that stays gone leaves
   the user with a one-line way out.
6. The local path is unchanged for the user: same rendering, same tests, one
   wrapper hop between `HarnessApp` and `SessionHost`.

## Non-goals (4b)

- Daemon-hosted by default; workspace auto-register (`POST /v1/workspaces`
  from the TUI).
- Switching a running TUI between a local and a remote session (the picker
  refuses instead).
- Remote `/compact`, `/name`, `/new`, `/rewind`, `/undo`, `/think`,
  `/advisor`, `/mcp`, the Settings screen, checkpoints, the jobs panel
  (`GET jobs` exists; rendering it is 4b), `/remember`, `/skill`, `/plugin`,
  `/worktree`, `/trust`, the shell passthrough (`!cmd`). All refuse with one
  notice on a remote session. Each needs either a route or a decision about
  which process it belongs to.
- Steer attachments over HTTP (`SteerIn` stays text-only; a remote steer
  drops pasted images with a notice).
- Automatic takeover when the daemon dies (today's dead-pid reclaim on a
  fresh `marim --resume` is the way out; 4a tells the user so).
- Rendering images in the TUI. The sha reference is for clients that can
  show them; the TUI keeps its `[image …]` placeholder.
- Remote-machine attach. 4a's discovery reads the claim sidecar and the
  daemon's token file, both local; the transport is already network-shaped,
  so a `--endpoint/--token` pair is a small 4b addition.

## Architecture

```
HarnessApp
 ├── link: SessionLink                     ← the seam (interfaces/tui/link.py)
 │    ├── LocalSessionLink(harness, host)  ← wraps SessionHost + Harness
 │    └── RemoteSessionHost(endpoint, token, ws_id, sid)   (server/client.py)
 │         ├── httpx.AsyncClient           ← REST commands + history + asks
 │         └── RemoteSubscription          ← WebSocket → Event queue
 └── pump task: link.attach(after_seq) → Subscription → parse_wire_event → handlers
```

### The seam: `SessionLink`

`interfaces/tui/link.py` (protocol + local implementation) and
`server/client.py` (remote implementation; imports httpx and websockets,
never starlette, so a TUI install without the `[serve]` extra can attach).

```python
class SessionLink(Protocol):
    kind: Literal["local", "remote"]
    info: LinkInfo                              # read model, see below
    def attach(self, after_seq: int | None = None) -> Subscription: ...
    async def submit(self, prompt, attachments=None, *, trigger="user") -> str: ...
    async def interrupt(self) -> bool: ...
    async def steer(self, text, attachments=None) -> None: ...
    async def answer_ask(self, ask_id, answer: dict) -> bool: ...
    async def pending_asks(self) -> list[dict]: ...
    async def set_mode(self, mode: str) -> None: ...
    async def set_model(self, model_id: str) -> None: ...
    async def history(self) -> list[ModelMessage]: ...   # replay source
    async def close(self) -> None: ...                    # host.stop() / sockets
```

`LinkInfo` is the read model the widgets and replay need and nothing more:
`workspace_root`, `session_id`, `session_name`, `mode`, `model_id`,
`model_label`, `model_source`, `advisor_model_id`, `thinking_level_id`,
`usage` (a `UsageSummary`), `history_tokens`, `message_count`,
`compact_threshold`. Local: a live view whose properties read the harness.
Remote: a dataclass filled from `GET session` at attach and refreshed from
events — `session.renamed`, `session.mode_changed`, `turn.finished.usage`
(accumulated), `compaction.finished`, and a `GET session` re-read on the
idle edge (cheap, `no-cache`, once per turn). The remote status bar's
context percentage is therefore "as of the last turn end", which is when it
changes anyway.

`TurnQueueFull` and `HostClosed` keep their meaning across the seam: the
remote maps `429 queue_full` and `404 host_closed` onto them, so
`_submit_turn`'s re-stage-and-pause behaviour is untouched. `409 claimed` on
submit (someone else took the session between attach and now) raises
`SessionClaimed` with the holder, rendered as a notice.

**Submit latch across the round trip.** `TurnTracker` gains a `pending` flag
set by `_submit_turn` before the await and cleared when `note_submitted`
lands; `busy` includes it. In-process the await never yields, so nothing
changes; over HTTP a second Enter during the round trip queues instead of
double-submitting — the same rule 3b established for the worker hop.

### `RemoteSessionHost`

One `httpx.AsyncClient` with the bearer header, base URL from discovery.
Methods are thin: `submit` → `POST messages` (attachments base64-encoded,
`trigger` in the body — see wire changes), `interrupt` → `POST interrupt`,
`steer` → `POST steer` (text only), `answer_ask` → `POST asks/{aid}` (`404`
→ `False`, exactly the local semantics: the loser is told plainly),
`pending_asks` → `GET asks`, `set_mode`/`set_model` → their routes,
`history` → `GET history` paged by `message_count` and deserialised with
pydantic-ai's `ModelMessagesTypeAdapter` — the on-disk shape is the
persisted message list, so `SessionView.replay_messages_into` renders it with
the same code as a local resume.

`attach(after_seq)` returns a `RemoteSubscription`: a task holding the
WebSocket (`websockets` client, `Authorization: Bearer` on the upgrade),
decoding each text frame into an `Event` and queueing it. `next_event`
reads the queue with the same timeout contract as the local one; `close`
cancels the task. Reconnect is internal: on socket loss the task reconnects
with `?after_seq=<last seq it delivered>` on a capped backoff (0.5s → 8s),
and reports state through an `on_state("reconnecting" | "connected" |
"lost")` callback the app wires to the status bar and a notice. After 60s
without a connection it reports `lost` and stops trying; the notice names
the way out: "daemon unreachable — `marim --session <id>` will take the
session over locally once the daemon's pid is gone." Bus seq restarting
from 1 after a daemon restart is detected as a `stream.gap`-equivalent (a
delivered seq below the last seen) and handled the same way.

`stream.gap` — unhandled by the local pump because it cannot happen there —
gets a handler: re-run `render_session` from `link.history()` (the existing
switch/rebuild path) and re-attach at the new `history_seq`. Events for an
in-flight turn older than the ring are gone; the notice says "resynced from
history; the running turn's earlier output is not shown."

### Discovery and the launch decision

`default_cmd._claim_target` today prints the refusal when `try_acquire`
returns `None`. 4a inserts one step before printing:

| Claim on the target session | Result |
|---|---|
| none / acquired | in-process, as today |
| `kind: daemon`, endpoint set, `GET /v1/health` answers within 1s, token file readable, workspace path registered on the daemon | **attach** |
| `kind: daemon` but unreachable, or workspace not registered, or no token | today's refusal, plus one line saying why the attach was not possible |
| `kind: tui` / `headless` | today's refusal |

Reachability is probed with `urllib` (stdlib; `default_cmd` deliberately
defers heavy imports) against the claim's `endpoint`, cross-checked with
`runtime.json`'s pid when present. The token is
`<XDG_DATA_HOME>/marim-harness/server/token` (`auth.load_or_create_token`'s
path; 4a only reads it and never creates it). The workspace id comes from
`GET /v1/workspaces` matched on resolved path. Target selection: `--resume`
keeps meaning "latest"; a new `--session <id>` names one explicitly (the
hint the picker and the lost-connection notice print). Both flags work for
the in-process path too — `--session` is just an explicit
`_resolve_target_session`.

`_launch_tui` grows a remote variant: `HarnessApp(harness=None,
remote=RemoteTarget(endpoint, token, ws_id, session_id))`. The link is built
in `on_mount` (the remote host opens sockets; the local host already had to
be loop-bound), before the pump attaches.

### Attach-time reconciliation

In `on_mount`, remote only:

1. `GET session` → seed `LinkInfo` and `TurnTracker.on_status(status)` so
   the status bar is right before the first event.
2. `link.history()` → `render_session` (header + replay), recording
   `history_seq`.
3. `link.attach(after_seq=history_seq)` → the pump starts on the in-flight
   tail: `turn.started` and the deltas of a running turn have seqs above the
   last persisted boundary, so the transcript catches up to the live cursor
   through the normal handlers.
4. `link.pending_asks()` → for each ask not already in `_ask_panels`, mount
   its panel via the same `_on_ask_pending` path. `GET asks` is the
   authority (an ask can outlive its `ask.pending` in a bounded ring);
   the tail is merely faster.

Order 2-3-4 is the parent's "attach → replay → reconcile". Step 3 before 4
means a panel is never mounted twice: the tail mounts it first and step 4
skips it by id.

### Triage of the harness reaches

Every `self.harness.…` becomes one of:

| Bin | Rule | Examples |
|---|---|---|
| **read model** | value the widgets or replay need → `app.link.info.*` | status bar (usage, history tokens, model label, mode, name), header, `sub_title`, `_announce_session_defaults`, replay's `model_label` fallback, `mount_header` |
| **link command** | a turn-path or session verb with a route → `await app.link.*` | `_submit_turn`, `action_cancel_turn`, steer, ask answers, `action_cycle_mode`, `/mode`, `/model`, the model picker's apply |
| **local-only** | needs the process that owns the harness → `app.require_local("…")` | MCP connect + `/mcp`, `session_start`/`session_end` hooks, trust prompt, `/compact`, `/name`, `/new`, `/sessions` switch, rewind/undo, `/remember`, `/skill`, `/plugin`, `/worktree`, `/trust`, `/jobs`, `/usage`'s cost detail, Settings screen, plan screen (`deps.plan`), sub-agent resume, `add_shell_result`, `take_buffered_steers` |

`require_local(what)` returns the `Harness` or raises `RemoteOnly`; the
command dispatcher and the key actions catch it and post one notice: "`what`
needs the session's own process — this TUI is attached to the daemon." No
handler checks `link.kind` itself. In-process, `require_local` is a
`Harness` attribute read; nothing else changes.

The jobs panel and `ActivityMonitor` need a decision, not a bin:

- **Wake.** The daemon's host runs its own `WakeDriver`
  (`autonomous_wake=True` is the daemon default). A remote TUI therefore
  constructs its `ActivityMonitor` disabled — two drivers would race for the
  same job-finished digests, the exact hazard the 3b non-goal deferred to
  phase 4. Decided: remote = the daemon wakes; local = the monitor wakes,
  unchanged.
- **Jobs.** `app.jobs` is process-scoped; remote gets an empty
  `JobRegistry` so the panel and the sub-agents screen render nothing
  instead of crashing. `jobs.changed` is delivered and ignored; `GET jobs`
  rendering is 4b.
- **Undelivered steers at the idle edge** (`take_buffered_steers`, flagged
  in 3b as the phase-4 seam): the daemon's harness holds them, and they are
  re-flushed on its next turn the way they always were headless. Remote
  skips the prepend; the `steer.accepted` echo already told the user it was
  taken.

### The picker

`SessionPickerModal` rows get a holder tag from `read_holder` on each
session's sidecar — a small JSON read per row, no lock taken: `· daemon`,
`· tui (pid N)`, or nothing. Choosing a daemon-owned row from a local TUI,
or any row from a remote TUI, posts the hint instead of switching:
"attached sessions can't be switched in place yet — run
`marim --session <id>`." Delete keeps today's `SessionClaimed` refusal.

### Exit

Remote `on_unmount`: `await link.close()` (cancel the feed, close the
client), print the summary line from `link.info`, no persist, no hooks —
the daemon owns all three. Local exit is byte-identical to 3b.

## Wire changes (all additive)

- `tool.result` gains `images: [{"sha", "media_type", "bytes"}]` when the
  return carried `BinaryContent`. Persist already externalises those bytes
  to the content-addressed sidecar (`images.externalize_images` →
  `store_image`, sha256 of the bytes). The wire mapper calls the same
  idempotent `store_image` when it builds the event, so the reference is
  fetchable from `GET .../images/{sha}` the moment it is published — a
  remote client would otherwise 404 until the turn's persist. `content`
  keeps its placeholder; `ToolResult` defaults `images` to `[]` so older
  servers parse.
- `MessageIn` gains `trigger: "user" | "system"` (default `"user"`), passed
  to `SessionHost.submit`. Without it a remote `/skill` or `/remember` would
  render on every client as a typed user message. `"autonomous"` is refused
  with `400` — only the daemon's own wake driver submits those.
- `docs/reference/serve-api.md`: the two fields, the `--session` flag, and a
  short "attaching a local TUI" section under Lifecycle.

`websockets` moves from a transitive dependency (via `google-genai`) to an
explicit entry in the `tui` extra; `httpx` is already core.

## Testing

Following the three-way split:

1. **`RemoteSessionHost` against the real app.** `create_app` under
   `httpx.ASGITransport` for every REST method (submit/429/404-host-closed/
   409 mapping, answer_ask 404 → False, history paging + deserialisation).
   The WebSocket feed runs against a real `uvicorn` server on an ephemeral
   port inside the test (the `websockets` client cannot speak ASGI
   directly): live delivery, `after_seq` replay, reconnect with the last
   seq after the server drops the socket, `lost` after the cap.
2. **`LocalSessionLink` parity.** The 3b app tests keep running unchanged
   against the local link; `_spy_submit` and friends patch `app.link`. One
   new test asserts the local link's `info` mirrors the harness live
   (rename, model switch, usage after a turn).
3. **Remote app tests.** `HarnessApp(harness=None, remote=…)` against an
   in-process daemon: attach mid-turn (history + tail + `GET asks`
   reconciliation mounts the parked approval exactly once); answer from the
   other client dismisses with the notice; `require_local` posts the notice
   for `/compact`; Esc reaches `POST interrupt`; queue-full re-stages.
4. **Launch decision.** `_claim_target` unit tests over a claim sidecar with
   `kind: daemon`: reachable → attach target; unreachable → refusal with
   the extra line; `tui` holder → today's refusal. Reachability is a
   injected probe, not a socket.
5. **Picker tags** and the refusal hint; **`TurnTracker.pending`**;
   **`tool.result.images`** sha equals the sidecar's; **`MessageIn.trigger`**
   validation.
6. **Live smoke (manual, model per approval):** `marim serve` on a scratch
   XDG dir; start a turn from `curl` so the daemon owns the session; `marim
   --resume` attaches, shows the running turn, approves an ask, sends a
   steer, Esc interrupts; kill the daemon → `lost` notice; restart it →
   reconnect.

Gates, in CI order: `ruff check` → `ruff format --check` → `pyright` →
`pytest` (coverage ≥ 90%), plus `uv run --python 3.10 pytest` on the new
files.

## Risks

- **Triage churn is the size risk** (~150 sites, 14 files). Contained by
  pyright doing the enumeration and the three-bin rule; no site gets a
  bespoke branch. Reviewed as a table in the PR body, same as 3b's turn-end
  effects.
- **Async submit changes the latch timing.** `pending` covers the round
  trip; the 3b race tests (`test_app_turn_race.py`) run unchanged against the
  local link and are the regression guard.
- **The WebSocket test needs a real port.** One helper starts `uvicorn` in
  the test loop; timeouts are on the clock, never `pilot.pause()`. Verified
  on 3.10 and 3.12.3 before pushing.
- **Two wake drivers.** Guarded by construction (the remote monitor is
  built disabled) and by a test that a remote app never submits
  `trigger="autonomous"`.
- **Ring overrun mid-turn** loses the running turn's earlier output on
  attach. Accepted; the notice says so; the history is intact.
- **The user's background daemon.** Because 4a attaches only to sessions the
  daemon already claims, a plain `marim` in a served workspace behaves
  exactly as it did yesterday. The behaviour change is confined to launches
  that used to exit 2.
