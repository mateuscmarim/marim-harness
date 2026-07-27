# Live session mode switch — design

**Date:** 2026-07-27
**Status:** approved (discussed in conversation 2026-07-27)

## Problem

marim-mobile's transcript screen ships a mode chip + picker (`ask`/`auto`/
`plan`) that already calls `TranscriptRepository.setMode`, which POSTs
`.../sessions/{sid}/mode`. That route doesn't exist on the server yet — the
mobile client's own spec
(`marim-mobile/docs/superpowers/specs/2026-07-26-session-mode-switch-design.md`)
names the gap. That mobile spec also asks for `mode` to be added to
`GET .../sessions/{sid}` and `GET .../sessions` — **that part is already
done**: `SessionInfo.mode` (`session/store.py:154`) has existed since the
create-session route learned to accept a `mode` field, and both
`list_sessions` and `get_session` serialize it via `asdict(info)` with
nothing stripping the key. This doc corrects that assumption and scopes only
the real gap: the POST write path for changing mode on a session that already
exists.

Mode-switching machinery already exists for the TUI (`Harness.set_mode`,
`.cycle_mode()`, the `/mode` command, the settings screen) and one adjacent
route family already does exactly the persist-vs-live-host branch this needs
(`set_session_model` / `SessionSupervisor.set_model`). The design is almost
entirely "mirror `/model`," with one deliberate divergence called out below.

## Decision

Mirror the `/model` route's supervisor-mediated pattern exactly, reusing
`SessionBusy`/409 for a running turn. The one divergence: `Harness.set_mode`
gets a `persist` parameter **defaulting to `False`** — the opposite of
`set_model`'s `persist: bool = True` default — because `set_mode`/`cycle_mode`
already have four call sites in the TUI
(`interfaces/tui/app.py:535`, `interfaces/tui/commands.py:201,204`,
`interfaces/tui/settings.py:603`) that today never persist (mode is
documented in `session/store.py` as "a live, per-launch toggle" for the TUI).
Giving `persist` a default of `True` would silently make every TUI mode
cycle/switch durable — a behavior change nobody asked for. Only the new
supervisor code path passes `persist=True` explicitly.

Rejected alternative: leave `set_mode`/`cycle_mode` untouched and have the
supervisor write `deps.workspace.mode` and the store directly, bypassing
`Harness`. Rejected because it duplicates the "write point" `Harness.set_mode`
already documents itself as ("the single write point for `deps.mode`"), and
would need its own persistence logic anyway — mirroring `set_thinking_level`'s
existing `persist` keyword (`runtime/harness.py:857`) is less code and stays
consistent with how the other two runtime toggles (model, thinking level)
already do live-vs-persist.

## Design

### 1. Route + DTO (`server/schema.py`, `server/http.py`)

New DTO next to `SetModelIn`:

```python
class SetModeIn(BaseModel):
    mode: str
```

New route, registered next to `/model`:

```python
Route(f"{base}/mode", set_session_mode, methods=["POST"]),
```

New handler `set_session_mode`, structurally identical to
`set_session_model` (auth → workspace lookup → session-existence check → DTO
parse → supervisor call → `SessionBusy` → 409), plus the same mode-value
validation `create_session` already does:

```python
async def set_session_mode(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    session_id = request.path_params["sid"]
    if not _session_exists(record, session_id):
        return _error(404, "not_found", "unknown session")
    try:
        body = await _json_body(request, SetModeIn)
    except _BadBody as exc:
        return _error(400, "bad_request", str(exc))
    if body.mode not in (m.value for m in Mode):
        return _error(400, "bad_request", f"unknown mode: {body.mode}")
    try:
        _supervisor(request).set_mode(record, session_id, Mode(body.mode))
    except SessionBusy:
        return _error(409, "busy", "cannot switch modes while a turn is running")
    return JSONResponse({"ok": True, "mode": body.mode})
```

This satisfies the mobile spec's contract exactly: `200 {}` shape (mobile
only checks status, but the body mirrors `/model`'s `{"ok": True, ...}`) and
`409` on a running turn via the same `Busy` semantics the model route uses.

### 2. `SessionSupervisor.set_mode` — signature change to match `set_model`

Today: `set_mode(self, ws_id: str, session_id: str, mode: Mode) -> None` is a
bare in-memory cache write, called once, from `create_session`, with a bare
`record.id` (not the full record) — because at creation time there is never a
live host yet.

The live-switch path needs the full `WorkspaceRecord` for the same reason
`set_model` does: to build a `SessionManager` for the idle (no in-memory
host) branch. New signature and body, mirroring `set_model` line for line:

```python
def set_mode(self, record: WorkspaceRecord, session_id: str, mode: Mode) -> None:
    """Apply a mode to a session. A loaded host switches live (persisted)
    unless it is busy, in which case SessionBusy is raised so the API can
    reject with 409. An idle session persists straight to its store. The
    in-memory cache is kept warm either way, for host_for's next build."""
    self._modes[(record.id, session_id)] = mode
    host = self._hosts.get((record.id, session_id))
    if host is not None:
        if host.busy:
            raise SessionBusy(session_id)
        host.harness.set_mode(mode, persist=True)
        return
    store = SessionManager(Path(record.path)).store(session_id)
    store.mode = mode.value
    store.save_meta()
```

`create_session`'s one existing call site (`http.py:268`) changes from
`_supervisor(request).set_mode(record.id, store.session_id, Mode(body.mode))`
to `_supervisor(request).set_mode(record, store.session_id, Mode(body.mode))`.
This makes creation take the "no host" branch (a session is never born with a
live host) and write `store.mode`/`save_meta()` — redundant with the
`store.mode = body.mode; store.save()` two lines above it in the same
function, but harmless: both write the same value moments apart, and
`save_meta()` is a metadata-only patch, not a rewrite.

The existing unit test `test_set_mode_reaches_factory`
(`tests/test_server_supervisor.py`) calls `sup.set_mode("ws", "s1", Mode.plan)`
with a bare string — it must be updated to build a `WorkspaceRecord` (the
file already has a `_record(tmp_path)` helper used by every other test) and
pass it in place of the bare id.

### 3. `Harness.set_mode` / `cycle_mode` — new `persist` parameter

Mirrors `set_thinking_level`'s existing shape:

```python
def set_mode(self, mode: Mode, *, persist: bool = False) -> None:
    """Set the approval mode. The single write point for ``deps.mode`` so the
    interface layer doesn't poke ``harness.deps`` field-by-field. persist
    defaults to False: the TUI's mode toggle/cycle is a live, per-launch
    setting (see session/store.py's SessionStore.mode docstring) and must
    keep behaving that way; only the server's live-switch path opts in."""
    self.deps.workspace.mode = mode
    if persist:
        self.session.set_mode(mode.value)

def cycle_mode(self, *, persist: bool = False) -> Mode:
    """Advance to the next approval mode and return it."""
    self.deps.workspace.mode = self.deps.workspace.mode.cycle()
    if persist:
        self.session.set_mode(self.deps.workspace.mode.value)
    return self.deps.workspace.mode
```

`cycle_mode` gains the parameter for symmetry even though no current caller
(TUI or server) needs `persist=True` on it — the server's live-switch path
always knows the target mode explicitly (`set_mode`, never a cycle), so this
is consistency with `set_mode`'s signature rather than new surface actually
exercised by a route in this doc.

No change needed to `wire_cli_model`: `ClaudeCliModel.mode_getter` is a lazy
closure (`lambda: self.mode.value`) already re-read on every tool-approval
check, so a live `deps.workspace.mode` flip is observed on the model's very
next check with no rewiring step, unlike a model switch.

### 4. `SessionCtrl.set_mode` — new persistence method

Mirrors `set_thinking`/`set_advisor`/`set_model` exactly (metadata-only patch
when a file exists, else a forced first persist):

```python
def set_mode(self, value: str) -> None:
    """Persist the session's approval mode (a Mode.value string). Same
    metadata-only patch rules as set_advisor/set_thinking: a switch can land
    mid-turn when in-memory history must never reach disk, so patch the
    header when a file exists, else force one clean persist."""
    if self.store is not None:
        self.store.mode = value
        if self.store.path.exists():
            self.store.save_meta()
        else:
            self.persist(force=True)
```

`SessionStore.mode` and `.save()`'s `"mode": self.mode` serialization already
exist (`session/store.py`) — no change needed there.

### Untouched by design

- The GET read path (`SessionInfo.mode` on `list_sessions`/`get_session`) —
  already complete; this doc's only job there is correcting the mobile spec's
  assumption.
- `Mode` enum, `.cycle()`, and all four existing TUI call sites of
  `set_mode`/`cycle_mode` — behavior unchanged (new `persist` param defaults
  to `False`, matching today's always-non-persistent TUI behavior exactly).
- `wire_cli_model` — no rewiring needed for a mode switch (see §3).
- `SessionStore`/`SessionManager` — `mode` field and serialization already
  support this; only a new `SessionCtrl` method is added.

## Testing

Unit (offline), extending existing suites:

- `tests/test_server_supervisor.py`: update `test_set_mode_reaches_factory` to
  pass a `WorkspaceRecord`; add a busy-host case (mirroring
  `test_set_model_persists_when_idle`'s idle case) that asserts
  `SessionBusy` is raised and the store is left untouched; add an idle-host
  case asserting `store.mode` persists via `save_meta` without rebuilding the
  harness.
- `tests/test_server_http.py`: add `test_set_mode_on_idle_session_persists`
  and `test_set_mode_rejected_while_running`, mirroring
  `test_set_model_on_idle_session_persists` /
  `test_set_model_rejected_while_running` exactly (same `_setup_workspace_and_session`
  / `_poll` helpers already in the file); add a 400 case for an unknown mode
  string.
- A `Harness.set_mode`/`cycle_mode` unit test (wherever `set_thinking_level`'s
  persist-flag test lives, e.g. `tests/test_agent.py` or a harness-focused
  test file) asserting `persist=False` (default) does not call
  `session.set_mode`, and `persist=True` does.

No live smoke needed — this is server-side plumbing with existing HTTP
integration-test coverage patterns to mirror.

## Docs

- `CHANGELOG.md` Unreleased entry: new `POST .../sessions/{sid}/mode` route
  letting a client switch a session's approval mode after creation (ask/auto/
  plan), 409 while a turn is running — same shape as the existing `/model`
  route.
