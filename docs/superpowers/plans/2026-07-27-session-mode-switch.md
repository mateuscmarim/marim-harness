# Live Session Mode Switch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a client change an existing session's approval mode (`ask`/`auto`/`plan`) after creation via a new `POST .../sessions/{sid}/mode` route, satisfying marim-mobile's already-shipped mode-chip UI (`marim-mobile/docs/superpowers/specs/2026-07-26-session-mode-switch-design.md`).

**Architecture:** Per the approved spec (`docs/superpowers/specs/2026-07-27-session-mode-switch-design.md`), mirror the existing `/model` route's supervisor-mediated live-vs-persist pattern end to end: a new `SetModeIn` DTO + `set_session_mode` handler in `server/http.py`, `SessionSupervisor.set_mode` changed to take a full `WorkspaceRecord` (mirroring `set_model`'s three-way branch: busy → `SessionBusy`/409, live idle host → switch on the harness, no host → persist straight to the store), a new `Harness.set_mode`/`cycle_mode` `persist` keyword defaulting to `False` (so the TUI's existing non-persistent mode toggle is unaffected — only the new route path passes `persist=True`), and a new `SessionCtrl.set_mode` metadata-only persist method mirroring `set_thinking`/`set_advisor`. The GET read path (`SessionInfo.mode`) already works and needs no change.

**Tech Stack:** Python 3.10+, pytest (`pytest.mark.anyio` for async supervisor/http tests), ruff (line 100), pyright. Use `uv run …` for everything.

## Global Constraints

- New route: `POST /v1/workspaces/{ws}/sessions/{sid}/mode` with body `{"mode": "auto"|"ask"|"plan"}` → `200 {"ok": true, "mode": "<mode>"}`; `409 {"error": {"code": "busy", ...}}` while a turn is running; `400 {"error": {"code": "bad_request", ...}}` for an unrecognized mode string (spec §1).
- `SessionSupervisor.set_mode` signature becomes `(self, record: WorkspaceRecord, session_id: str, mode: Mode) -> None`, mirroring `set_model` exactly: busy live host raises `SessionBusy`; idle live host switches live; no host persists via `SessionManager(...).store(session_id)` + `save_meta()` (spec §2).
- `create_session`'s existing call site changes from `set_mode(record.id, ...)` to `set_mode(record, ...)` (spec §2).
- `Harness.set_mode`/`cycle_mode` gain `persist: bool = False` (default **False**, opposite of `set_model`'s `True` default) — only `self.session.set_mode(mode.value)` when `persist` is true (spec §3). Do NOT change the default to `True` — it would silently make the TUI's mode toggle durable.
- `SessionCtrl.set_mode(self, value: str) -> None` mirrors `set_thinking`/`set_advisor`'s metadata-only-patch-else-force-persist pattern exactly (spec §4).
- No changes to `wire_cli_model`, the `Mode` enum, `SessionInfo`, or any of the four existing TUI call sites of `set_mode`/`cycle_mode` beyond the new optional keyword (spec, "Untouched by design").
- Python ≥3.10 syntax only. Run `uv run ruff check src tests` → `uv run pyright` → tests before each commit.

---

### Task 1: `SessionCtrl.set_mode` persistence method

**Files:**
- Modify: `src/marim_harness/session/ctrl.py` (add near `set_thinking`, current lines ~376-385)

**Interfaces:**
- Consumes: `self.store.mode: str | None` (already exists on `SessionStore`), `self.store.save_meta()`, `self.persist(force=True)` (both already exist and are used by `set_model`/`set_advisor`/`set_thinking`).
- Produces: `SessionCtrl.set_mode(self, value: str) -> None` — Task 2's harness-level tests call through to this method via `Harness.set_mode(..., persist=True)`.

This method has no dedicated unit test of its own, matching `set_thinking` (its exact structural twin), which is likewise only exercised indirectly through the harness-level test in `test_thinking_controller.py`. Task 2 covers this method's behavior the same way.

- [ ] **Step 1: Implement `SessionCtrl.set_mode`**

In `src/marim_harness/session/ctrl.py`, add directly after `set_thinking` (after the method ending around line 385):

```python

    def set_mode(self, value: str) -> None:
        """Persist the session's approval mode (a Mode.value string). Same
        metadata-only patch rules as set_advisor/set_thinking: a switch can
        land mid-turn when in-memory history must never reach disk, so patch
        the header when a file exists, else force one clean persist."""
        if self.store is not None:
            self.store.mode = value
            if self.store.path.exists():
                self.store.save_meta()
            else:
                self.persist(force=True)
```

- [ ] **Step 2: Type-check in isolation**

Run: `uv run pyright src/marim_harness/session/ctrl.py`
Expected: no new errors. (Behavioral verification happens in Task 2, which is the first task able to exercise this method end to end via `Harness`.)

- [ ] **Step 3: Commit**

```bash
git add src/marim_harness/session/ctrl.py
git commit -m "feat: add SessionCtrl.set_mode persistence method"
```

---

### Task 2: `Harness.set_mode`/`cycle_mode` gain a `persist` keyword

**Files:**
- Modify: `src/marim_harness/runtime/harness.py` (the `set_mode`/`cycle_mode` methods, current lines ~867-875)
- Test: `tests/test_mode_switch.py` (new file)

**Interfaces:**
- Consumes: `SessionCtrl.set_mode(self, value: str) -> None` (Task 1).
- Produces: `Harness.set_mode(self, mode: Mode, *, persist: bool = False) -> None`, `Harness.cycle_mode(self, *, persist: bool = False) -> Mode` — Task 3's `SessionSupervisor.set_mode` calls `harness.set_mode(mode, persist=True)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mode_switch.py`:

```python
"""Harness mode wiring: live switch is always applied, persistence is opt-in
(default False, so the TUI's cycle/toggle stays a live, per-launch setting)."""

from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.runtime.harness import Harness
from marim_harness.runtime.permissions import Mode
from marim_harness.session import SessionManager
from marim_harness.tools.provider import BuiltinToolProvider
from pydantic_ai.models.test import TestModel


def _harness(tmp_path, **kwargs) -> Harness:
    deps = Deps(workspace=WorkspaceConfig(root=tmp_path, mode=Mode.auto))
    return Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, "Be helpful.", **kwargs
    )


def test_set_mode_default_does_not_persist(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    h = _harness(tmp_path, store=store, manager=manager)
    h.set_mode(Mode.plan)
    assert h.mode is Mode.plan
    assert store.mode is None


def test_set_mode_persist_true_writes_store(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    h = _harness(tmp_path, store=store, manager=manager)
    h.set_mode(Mode.plan, persist=True)
    assert h.mode is Mode.plan
    assert store.mode == "plan"


def test_cycle_mode_default_does_not_persist(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    h = _harness(tmp_path, store=store, manager=manager)
    result = h.cycle_mode()
    assert result is h.mode
    assert store.mode is None


def test_cycle_mode_persist_true_writes_store(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    h = _harness(tmp_path, store=store, manager=manager)
    result = h.cycle_mode(persist=True)
    assert store.mode == result.value
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mode_switch.py -v`
Expected: FAIL — `TypeError: set_mode() got an unexpected keyword argument 'persist'` (and same for `cycle_mode`).

- [ ] **Step 3: Implement the `persist` keyword**

In `src/marim_harness/runtime/harness.py`, replace the current `set_mode`/`cycle_mode` (around line 867):

```python
    def set_mode(self, mode: Mode, *, persist: bool = False) -> None:
        """Set the approval mode. The single write point for ``deps.mode`` so
        the interface layer doesn't poke ``harness.deps`` field-by-field.
        persist defaults to False: the TUI's mode toggle/cycle is a live,
        per-launch setting (see session/store.py's SessionStore.mode
        docstring) and must keep behaving that way; only the server's
        live-switch path opts in."""
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

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mode_switch.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/runtime/harness.py tests/test_mode_switch.py
git commit -m "feat: add persist keyword to Harness.set_mode/cycle_mode"
```

---

### Task 3: `SessionSupervisor.set_mode` — record-based signature + live/idle/busy branch

**Files:**
- Modify: `src/marim_harness/server/supervisor.py` (the `set_mode` method, current lines ~95-100)
- Modify: `src/marim_harness/server/http.py` (`create_session`'s one call site, current line ~268)
- Test: `tests/test_server_supervisor.py`

**Interfaces:**
- Consumes: `Harness.set_mode(mode, *, persist=True)` (Task 2), `SessionManager(Path).store(session_id)` (existing), `WorkspaceRecord` (existing, `server/workspaces.py`).
- Produces: `SessionSupervisor.set_mode(self, record: WorkspaceRecord, session_id: str, mode: Mode) -> None` — later tasks (route handler) call this exact signature.

- [ ] **Step 1: Update the existing test for the new signature, add busy/idle cases**

In `tests/test_server_supervisor.py`, replace `test_set_mode_reaches_factory`:

```python
async def test_set_mode_reaches_factory(tmp_path):
    created: list = []
    sup = SessionSupervisor(_factory(created))
    record = _record(tmp_path)
    sup.set_mode(record, "s1", Mode.plan)
    await sup.host_for(record, "s1")
    assert created[0][2] is Mode.plan
    await sup.aclose()
```

Then append two new tests below `test_set_model_persists_when_idle`:

```python
async def test_set_mode_persists_when_idle(tmp_path):
    """No live host for the session (idle) -> set_mode persists straight to
    the on-disk session store instead of touching a harness."""
    record = _record(tmp_path)
    manager = SessionManager(Path(record.path))
    store = manager.create("s1")
    store.save([], RunUsage())
    sid = store.session_id

    sup = SessionSupervisor(_factory([]))
    sup.set_mode(record, sid, Mode.plan)

    assert manager.store(sid).mode == "plan"


async def test_set_mode_raises_when_host_busy(tmp_path):
    record = _record(tmp_path)
    sup = SessionSupervisor(_factory([]))
    host = await sup.host_for(record, "s1")
    host.harness._busy = True  # simulate a running turn; matches host.busy's check

    with pytest.raises(SessionBusy):
        sup.set_mode(record, "s1", Mode.plan)

    await sup.aclose()
```

Before writing `test_set_mode_raises_when_host_busy`, check how `host.busy` is actually driven in this file's existing busy tests (`test_set_model_rejected_while_running` is an HTTP-level test, not a supervisor-level one — there is no existing supervisor-level "busy" test to copy verbatim). Read `src/marim_harness/server/host.py`'s `busy` property to find the real field to flip (do not guess `_busy` blindly — inspect the property first and adjust the test to set whatever it actually reads, e.g. an active-turn task/flag).

Add the import this test file needs if not already present: `from marim_harness.server.supervisor import SessionBusy` (check the existing top-of-file imports first — `SessionSupervisor` is already imported from the same module; add `SessionBusy` to that same import line rather than a new one).

- [ ] **Step 2: Run tests to verify the new/changed ones fail**

Run: `uv run pytest tests/test_server_supervisor.py -v`
Expected: `test_set_mode_reaches_factory` fails (old signature still bare `ws_id`), the two new tests fail (`set_mode` doesn't persist or raise yet).

- [ ] **Step 3: Implement the new `set_mode`**

In `src/marim_harness/server/supervisor.py`, replace the current `set_mode` (lines ~95-100):

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

- [ ] **Step 4: Update `create_session`'s call site**

In `src/marim_harness/server/http.py`, in `create_session` (current line ~268), change:

```python
    if body.mode is not None:
        _supervisor(request).set_mode(record.id, store.session_id, Mode(body.mode))
```

to:

```python
    if body.mode is not None:
        _supervisor(request).set_mode(record, store.session_id, Mode(body.mode))
```

- [ ] **Step 5: Run all supervisor and http tests to verify they pass**

Run: `uv run pytest tests/test_server_supervisor.py tests/test_server_http.py -v`
Expected: PASS. (`test_server_http.py` tests unaffected by this task should stay green — this confirms `create_session`'s call-site change didn't break session creation with a mode.)

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/server/supervisor.py src/marim_harness/server/http.py tests/test_server_supervisor.py
git commit -m "feat: SessionSupervisor.set_mode takes a WorkspaceRecord and live-switches"
```

---

### Task 4: `POST .../sessions/{sid}/mode` route

**Files:**
- Modify: `src/marim_harness/server/schema.py` (add `SetModeIn` next to `SetModelIn`)
- Modify: `src/marim_harness/server/http.py` (new handler + route registration)
- Test: `tests/test_server_http.py`

**Interfaces:**
- Consumes: `SessionSupervisor.set_mode(record, session_id, mode)` (Task 3), `Mode` enum (existing), `_supervisor`/`_workspace`/`_unauthorized`/`_session_exists`/`_json_body`/`_BadBody`/`_error` (all existing helpers already used by `set_session_model`).
- Produces: `set_session_mode(request) -> Response` handler, registered at `POST {base}/mode`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_server_http.py`, add directly after `test_set_model_rejected_while_running`:

```python
def test_set_mode_on_idle_session_persists(client):
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
    resp = test_client.post(f"{base}/mode", headers=AUTH, json={"mode": "plan"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "mode": "plan"}
    detail = test_client.get(base, headers=AUTH).json()
    assert detail["session"]["mode"] == "plan"


def test_set_mode_rejected_while_running(client):
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
    test_client.post(f"{base}/messages", headers=AUTH, json={"prompt": "edit it"})
    _poll(test_client, base, lambda s: s["status"] == "waiting_ask")
    refused = test_client.post(f"{base}/mode", headers=AUTH, json={"mode": "plan"})
    assert refused.status_code == 409
    test_client.post(f"{base}/interrupt", headers=AUTH)


def test_set_mode_rejects_unknown_value(client):
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
    resp = test_client.post(f"{base}/mode", headers=AUTH, json={"mode": "yolo"})
    assert resp.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server_http.py -v -k mode`
Expected: FAIL — 404 (no `/mode` route registered yet).

- [ ] **Step 3: Add the `SetModeIn` DTO**

In `src/marim_harness/server/schema.py`, directly after `SetModelIn`:

```python


class SetModeIn(BaseModel):
    mode: str
```

- [ ] **Step 4: Add the handler and route**

In `src/marim_harness/server/http.py`:

Add `SetModeIn` to the existing `from .schema import (...)` block (alongside `SetModelIn`).

Add the handler directly after `set_session_model` (current lines ~502-520):

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

In `create_app()`'s routes list (current line ~761), add directly after the `/model` route:

```python
        Route(f"{base}/model", set_session_model, methods=["POST"]),
        Route(f"{base}/mode", set_session_mode, methods=["POST"]),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_server_http.py -v -k mode`
Expected: PASS (all mode-related tests, including the two supervisor-driven ones added in Task 3's file if run together).

- [ ] **Step 6: Run the full test suite**

Run: `uv run pytest tests/`
Expected: all pass, no regressions in `create_session`'s mode-at-creation behavior or any other route.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/server/schema.py src/marim_harness/server/http.py tests/test_server_http.py
git commit -m "feat: add POST .../sessions/{sid}/mode route"
```

---

### Task 5: Lint, type-check, and changelog

**Files:**
- Modify: `CHANGELOG.md`

**Steps:**

- [ ] **Step 1: Run ruff and pyright**

Run: `uv run ruff check src tests`
Expected: no errors (fix any line-length/import-order issues if raised).

Run: `uv run pyright`
Expected: no new errors introduced by this plan's changes.

- [ ] **Step 2: Run the full test suite one more time**

Run: `uv run pytest tests/`
Expected: all green.

- [ ] **Step 3: Add the CHANGELOG entry**

In `CHANGELOG.md`, under `## [Unreleased]`, add a new bullet (following the existing entries' style):

```markdown
- Live session mode switch: new `POST /v1/workspaces/{ws}/sessions/{sid}/mode`
  route lets a client change an existing session's approval mode
  (ask/auto/plan) after creation — same live-vs-persist shape as the existing
  `/model` route, 409 while a turn is running. The TUI's own mode
  toggle/cycle is unaffected (still a live, per-launch setting, not
  persisted).
```

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog entry for the session mode-switch route"
```
