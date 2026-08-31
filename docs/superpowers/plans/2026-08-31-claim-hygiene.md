# Claim Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the five phase-4 carry-overs from session-ownership claims: the claim follows the TUI's active view, a doomed daemon can't disturb `runtime.json`, CLI ownership is taken before any session read, delete refuses claimed sessions, and `claim.py` documents what it actually guarantees.

**Architecture:** Ownership moves into the Harness (`adopt_claim`/`release_claim`); `switch_session` refuses claimed targets before touching the outgoing session and swaps claims on success; serve pre-binds its socket before publishing `runtime.json`; `SessionManager.delete` becomes the shared refuse-while-claimed seam for CLI and HTTP. `SessionClaimed` relocates from `server/supervisor.py` to `session/claim.py` (re-exported from supervisor so existing imports keep working).

**Tech Stack:** Python ≥3.10, stdlib `fcntl`/`socket`, pytest, uv. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-31-claim-hygiene-design.md` (commit `c3c5e115` + correction).

## Global Constraints

- `requires-python` is `>=3.10`: no 3.11+-only syntax.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM,C901`; complexity cap 10 — no `# noqa: C901`. `ruff format` is CI-enforced: run `uv run ruff format src tests` before every commit.
- Gates, in this order, all green before a task is marked done:
  `uv run ruff check src tests` · `uv run ruff format --check src tests` · `uv run pyright` · `uv run pytest` (final task also runs `uv run --python 3.10 pytest`).
- Use `uv` for everything — never bare `python`/`pip`/`pytest`.
- Phase-2 invariants stay intact: (1) the sidecar is `<id>.json.claim` — never `.lock`, never `<id>.claim.json`; (2) claim identity JSON is written to the locked fd only — never via `atomic_write_text`; (3) daemon-side release stays in `SessionHost.aclose()`. This plan ADDS release points (view swap, Harness teardown) — it never moves the daemon's.
- The degrade-to-unclaimed stance on claim-file open failure (`claim.py` OSError path) is ACCEPTED by user ruling — do not change it, only document it (Task 1).
- Every phase-2 claim test must keep passing unchanged.

---

### Task 1: Relocate `SessionClaimed` + reword the claim docstring

Claims reach beyond the server (Harness, SessionManager, TUI), so the exception describing a held claim belongs in `session/claim.py`. Item 5 (docstring honesty) rides along because it touches the same module header.

**Files:**
- Modify: `src/marim_harness/session/claim.py` (module docstring lines 1-10; add exception class)
- Modify: `src/marim_harness/server/supervisor.py:41-50` (delete class, import it)
- Test: `tests/test_session_claim.py` (append)

**Interfaces:**
- Produces: `marim_harness.session.claim.SessionClaimed` — `SessionClaimed(session_id: str, holder: Holder | None)`, fields `.session_id`, `.holder`, `str(exc)` is the session id. `marim_harness.server.supervisor.SessionClaimed` remains importable (re-export) so `server/http.py:47` and existing tests keep working unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_session_claim.py`:

```python
def test_session_claimed_lives_in_claim_module_and_supervisor_reexports():
    from marim_harness.server.supervisor import SessionClaimed as ViaSupervisor
    from marim_harness.session.claim import Holder, SessionClaimed

    assert SessionClaimed is ViaSupervisor
    exc = SessionClaimed("20260831-1", Holder(pid=123, kind="tui", endpoint=None))
    assert exc.session_id == "20260831-1"
    assert exc.holder is not None and exc.holder.kind == "tui"
    assert str(exc) == "20260831-1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_session_claim.py -k session_claimed_lives --no-cov -q`
Expected: FAIL with `ImportError` (no `SessionClaimed` in `session/claim.py`).

- [ ] **Step 3: Implement**

In `src/marim_harness/session/claim.py`: replace the module docstring (lines 1-10) with:

```python
"""Non-blocking flock-based session-ownership claims.

A claim marks the session a process is ACTIVELY DRIVING: ``try_acquire`` opens
(or creates) the ``<id>.json.claim`` sidecar, ``flock(LOCK_EX | LOCK_NB)`` it,
and writes the holder's identity into the LOCKED fd. The kernel releases the
lock on process death — no pid liveness checks, no stale-lock cleanup.

Ownership follows the active view: a process swaps claims when it switches
sessions (Harness.switch_session), so a claim is held for as long as its
holder drives that session — not necessarily the process's whole lifetime.

Degrade stance (accepted, mirrors atomic_io.file_lock): if the claim file
cannot be opened, the caller proceeds UNCLAIMED rather than refusing to run.
Locking is a safety net against two simultaneous owners under normal
operation, not an absolute guarantee under resource failure.

Naming invariant: the sidecar is ``<id>.json.claim`` — not ``.lock``
(atomic_io.file_lock's name; sharing it would block every SessionStore.save)
and not ``<id>.claim.json`` (SessionManager.list's *.json glob would render
it as a phantom session).
"""
```

Add the exception after the `Holder` class (before `SessionClaim`):

```python
class SessionClaimed(Exception):
    """Raised when a session is owned by another live process.

    Not a transient condition to retry: the holder keeps the session until it
    exits (or switches away from it), so the caller's job is to report who has
    it, not to back off."""

    def __init__(self, session_id: str, holder: "Holder | None") -> None:
        super().__init__(session_id)
        self.session_id = session_id
        self.holder = holder
```

In `src/marim_harness/server/supervisor.py`: delete the class definition at lines 41-50 (`class SessionClaimed(Exception):` through its `self.holder = holder`) and replace it with an import. Find supervisor's existing `from ..session.claim import ...` line (it already imports `Holder`, `read_holder`, `try_acquire` — verify with `grep -n 'session.claim' src/marim_harness/server/supervisor.py`) and add `SessionClaimed` to it. If the imports come from separate lines, add `SessionClaimed` to whichever imports from `..session.claim`. The name must remain importable as `marim_harness.server.supervisor.SessionClaimed` (re-export by import is enough).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_session_claim.py tests/test_server_supervisor.py tests/test_server_http.py --no-cov -q`
Expected: PASS (existing supervisor/http claim tests exercise the re-export).

- [ ] **Step 5: Commit**

```bash
uv run ruff format src tests && uv run ruff check src tests
git add src/marim_harness/session/claim.py src/marim_harness/server/supervisor.py tests/test_session_claim.py
git commit -m "refactor(claim): move SessionClaimed to session.claim; reword claim docstring"
```

---

### Task 2: Delete refuses claimed sessions

`SessionManager.delete` (`session/store.py:533`) is the shared seam: the CLI (`interfaces/cli/sessions.py:94`) and the HTTP DELETE route (`server/http.py:452`) both call it. Note the deletion ORDER is already session-file-then-sidecar — the fix is the refuse-while-claimed guard plus a comment.

**Files:**
- Modify: `src/marim_harness/session/store.py` (`SessionManager.delete`, lines 533-545)
- Modify: `src/marim_harness/server/http.py` (`delete_session` route, ~line 438-455)
- Modify: `src/marim_harness/interfaces/cli/sessions.py` (`_cmd_delete`, ~line 88-96)
- Test: `tests/test_session.py` (append), `tests/test_server_http.py` (append), `tests/test_cli_sessions.py` (create)

**Interfaces:**
- Consumes: `SessionClaimed` and `read_holder` from `marim_harness.session.claim` (Task 1); `SessionManager.session_path(session_id) -> Path` (already public, store.py:377).
- Produces: `SessionManager.delete(session_id)` raises `SessionClaimed(session_id, holder)` when a claim is held. HTTP DELETE returns 409 `{"code": "claimed", ...}`; `marim sessions delete` exits 2 with the holder named on stderr.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session.py` (module helpers: `_manager(tmp_path)`,
`_history()`, `RunUsage` import — see `test_delete_removes_session` at line 416
for the exact pattern; note `create()` does NOT persist — `store.save(...)` is
what puts the file on disk):

```python
def test_delete_refuses_a_claimed_session(tmp_path: Path):
    from marim_harness.session.claim import SessionClaimed, try_acquire

    mgr = _manager(tmp_path)
    store = mgr.create("doomed")
    store.save(_history(), RunUsage())
    outsider = try_acquire(store.path, kind="daemon", endpoint="http://127.0.0.1:8642")
    assert outsider is not None
    try:
        with pytest.raises(SessionClaimed) as excinfo:
            mgr.delete(store.session_id)
        assert excinfo.value.session_id == store.session_id
        assert store.path.exists()  # nothing removed while claimed
    finally:
        outsider.release()
    mgr.delete(store.session_id)  # released → deletes fine
    assert not store.path.exists()
```

If `pytest` isn't already imported at the top of `tests/test_session.py`, add it to the imports.

Append to `tests/test_server_http.py` — copied line-for-line from the sibling
`test_post_message_409s_when_another_process_claims_the_session` (~line 925)
with the route swapped to DELETE:

```python
def test_delete_session_409s_when_another_process_claims_it(client):
    """A claimed session is being driven elsewhere; deletion must be refused."""
    from marim_harness.session.claim import try_acquire
    from marim_harness.session.store import SessionManager

    test_client, tmp_path = client
    ws_id, session_id, project = _setup_workspace_and_session(test_client, tmp_path)
    session_path = SessionManager(project).session_path(session_id)
    outsider = try_acquire(session_path, kind="tui")
    assert outsider is not None
    try:
        response = test_client.delete(
            f"/v1/workspaces/{ws_id}/sessions/{session_id}", headers=AUTH
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "claimed"
        assert "tui" in response.json()["error"]["message"]
        assert session_path.exists()  # nothing deleted while claimed
    finally:
        outsider.release()
```

(If the DELETE route's URL differs from `/v1/workspaces/{ws_id}/sessions/{session_id}`,
`grep -n 'delete_session' src/marim_harness/server/http.py` and read the route
table at the bottom of that file — use the route's real path.)

Create `tests/test_cli_sessions.py`:

```python
import io
from pathlib import Path

from marim_harness.interfaces.cli.sessions import main
from marim_harness.session import SessionManager
from marim_harness.session.claim import try_acquire


def test_delete_refuses_claimed_session_and_names_holder(tmp_path: Path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{}")  # create() does not persist; list() needs the file
    outsider = try_acquire(store.path, kind="daemon", endpoint="http://127.0.0.1:8643")
    assert outsider is not None
    out, err = io.StringIO(), io.StringIO()
    try:
        rc = main(["delete", store.session_id, "--workspace", str(tmp_path)], out=out, err=err)
    finally:
        outsider.release()
    assert rc == 2
    assert store.path.exists()
    assert "owned by daemon" in err.getvalue()
    assert "http://127.0.0.1:8643" in err.getvalue()
```

Verify `main`'s argv shape first: `grep -n '_build_parser\|add_subparsers\|"delete"' src/marim_harness/interfaces/cli/sessions.py` — if the delete subcommand spells its arguments differently (e.g. positional id, `--workspace` defaulting to cwd), match the parser exactly. If `main` has no `workspace` argument and uses cwd, `monkeypatch.chdir(tmp_path)` instead and drop `--workspace`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_session.py::test_delete_refuses_a_claimed_session tests/test_cli_sessions.py --no-cov -q`
Expected: FAIL (delete currently removes claimed sessions without complaint).

- [ ] **Step 3: Implement**

In `session/store.py` `SessionManager.delete`, insert at the TOP of the method body (before `import shutil`), and update the claim-unlink comment:

```python
        from .claim import SessionClaimed, read_holder, try_acquire

        # A claimed session is being actively driven elsewhere; deleting it
        # would erase live history out from under its holder. try_acquire is
        # the authoritative check (a live flock): release() leaves stale
        # identity content in the sidecar, so read_holder alone proves
        # nothing — it only supplies the holder identity for the message.
        # A held-but-unreadable sidecar degrades to "another process".
        # The probe's own brief identity write is harmless: this method
        # unlinks the sidecar below. The resurrection window between
        # probe.release() and the unlink is the same accepted micro-race
        # documented in the spec (a claim appearing after the check).
        session_path = self._path(session_id)
        probe = try_acquire(session_path, kind="delete")
        if probe is None:
            holder = read_holder(session_path)
            raise SessionClaimed(session_id, holder)
        probe.release()
```

And reword the existing claim-sidecar comment (the "Unlinking it cannot break a live holder…" block) to end with: "The file-then-sidecar order matters: the session file goes first so a racing reader finds the session gone before the claim does."

In `server/http.py` `delete_session` (~line 452), wrap the delete call:

```python
    try:
        SessionManager(Path(record.path)).delete(session_id)
    except SessionClaimed as exc:
        who = exc.holder.describe() if exc.holder is not None else "another process"
        return _error(409, "claimed", f"session is owned by {who}; close it there before deleting it")
```

(`SessionClaimed` is already imported at http.py:47 via the supervisor re-export.)

In `interfaces/cli/sessions.py` `_cmd_delete` (~line 88), wrap the delete:

```python
    from marim_harness.session.claim import SessionClaimed

    try:
        manager.delete(args.id)
    except SessionClaimed as exc:
        who = exc.holder.describe() if exc.holder is not None else "another process"
        print(f"session {args.id} is owned by {who}; close it there first.", file=err)
        return 2
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_session.py tests/test_cli_sessions.py tests/test_server_http.py --no-cov -q`
Expected: PASS (including the pre-existing `test_delete_removes_session`).

- [ ] **Step 5: Commit**

```bash
uv run ruff format src tests && uv run ruff check src tests
git add src/marim_harness/session/store.py src/marim_harness/server/http.py src/marim_harness/interfaces/cli/sessions.py tests/test_session.py tests/test_server_http.py tests/test_cli_sessions.py
git commit -m "feat(session): refuse deleting a claimed session (CLI + HTTP)"
```

---

### Task 3: Harness owns the claim; switch and new-session swap it

The core of item 1. The Harness adopts the launch claim from the CLI, refuses switching onto a claimed target BEFORE touching the outgoing session, and swaps claims on success. `/clear` (reset) stays on the same session and touches nothing.

**Files:**
- Modify: `src/marim_harness/runtime/harness.py` (`__init__` field area; `new_session` at ~line 732; `switch_session` at ~line 744)
- Test: `tests/test_agent.py` (append; module already has `_switch_harness` at line 282)

**Interfaces:**
- Consumes: `try_acquire`, `read_holder`, `SessionClaimed` from `marim_harness.session.claim`; `SessionManager.session_path(session_id) -> Path` (store.py:377).
- Produces: `Harness.adopt_claim(claim: SessionClaim | None, *, kind: str) -> None`; `Harness.release_claim() -> None` (idempotent); `Harness.switch_session` raises `SessionClaimed` for a claimed target; `Harness.new_session` swaps claims. `harness._claim` / `harness._claim_kind` are the internal state tests may read.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agent.py` (after `test_switch_session_restores_its_model`, ~line 396). `_switch_harness(tmp_path)` builds a Harness on a fresh session via `SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")` — reuse it verbatim:

```python
def test_switch_session_swaps_claims(tmp_path: Path):
    from marim_harness.session.claim import try_acquire

    h = _switch_harness(tmp_path)
    outgoing_id = h.session.store.session_id
    outgoing_path = h.session.store.path
    h.adopt_claim(try_acquire(outgoing_path, kind="tui"), kind="tui")

    beta = h.session.create()  # a second session to switch to
    beta.path.parent.mkdir(parents=True, exist_ok=True)
    beta.path.write_text("{}")  # create() does not persist; the switch loads
    assert h.switch_session(beta.session_id) >= 0

    # The outgoing claim is released, the incoming one held.
    assert try_acquire(outgoing_path, kind="probe") is not None
    assert try_acquire(h.session.store.path, kind="probe") is None


def test_switch_session_refuses_claimed_target_without_touching_outgoing(tmp_path: Path):
    from marim_harness.session.claim import SessionClaimed, try_acquire

    h = _switch_harness(tmp_path)
    outgoing_id = h.session.store.session_id
    h.adopt_claim(try_acquire(h.session.store.path, kind="tui"), kind="tui")
    beta = h.session.create()
    outsider = try_acquire(beta.path, kind="daemon", endpoint="http://127.0.0.1:8643")
    assert outsider is not None
    try:
        with pytest.raises(SessionClaimed) as excinfo:
            h.switch_session(beta.session_id)
        assert excinfo.value.holder is not None
        assert excinfo.value.holder.kind == "daemon"
        # Still on the outgoing session, its claim intact.
        assert h.session.store.session_id == outgoing_id
        assert try_acquire(h.session.store.path, kind="probe") is None
    finally:
        outsider.release()


def test_switch_session_failed_load_releases_the_tentative_claim(tmp_path: Path):
    from marim_harness.session.claim import try_acquire

    h = _switch_harness(tmp_path)
    h.adopt_claim(try_acquire(h.session.store.path, kind="tui"), kind="tui")
    beta = h.session.create()
    beta.path.write_text("{corrupt")  # forces SessionLoadError on switch
    with pytest.raises(Exception):
        h.switch_session(beta.session_id)
    # The tentative claim on beta was released; outgoing still held.
    assert try_acquire(beta.path, kind="probe") is not None
    assert try_acquire(h.session.store.path, kind="probe") is None


def test_new_session_swaps_claims(tmp_path: Path):
    from marim_harness.session.claim import try_acquire

    h = _switch_harness(tmp_path)
    old_path = h.session.store.path
    h.adopt_claim(try_acquire(old_path, kind="tui"), kind="tui")
    h.new_session("fresh")
    assert try_acquire(old_path, kind="probe") is not None
    assert try_acquire(h.session.store.path, kind="probe") is None


def test_release_claim_is_idempotent(tmp_path: Path):
    from marim_harness.session.claim import try_acquire

    h = _switch_harness(tmp_path)
    h.adopt_claim(try_acquire(h.session.store.path, kind="tui"), kind="tui")
    h.release_claim()
    h.release_claim()  # second call is a no-op
    assert try_acquire(h.session.store.path, kind="probe") is not None
```

Notes for the implementer: `_switch_harness` returns a harness whose `h.session` is the `SessionManager`; `h.session.store` is the active `SessionStore` (has `.path`, `.session_id`); `h.session.create()` returns a fresh store (see bootstrap.py:134 for the same call). If `beta.path` is not the attribute name, use `h.session.session_path(beta.session_id)` instead (store.py:377). Same probe pattern: a successful `try_acquire(..., kind="probe")` means the slot is FREE — release those probe claims where they matter or let the test end release them (they die with the process anyway; flock is per-open-fd).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_agent.py -k "swaps_claims or refuses_claimed_target or failed_load_releases or release_claim_is_idempotent" --no-cov -q`
Expected: FAIL (`Harness` has no `adopt_claim`).

- [ ] **Step 3: Implement**

In `runtime/harness.py`:

(a) Add imports-safe state in `Harness.__init__` (find the field-initialization region; keep these with the other `self.` assignments):

```python
        # Ownership of the session this harness actively drives (see
        # session/claim.py). Adopted from the CLI launch path; swapped by
        # switch_session/new_session; released through release_claim(), which
        # every teardown path calls. None means unclaimed (fresh launch before
        # adoption, or the accepted degrade stance).
        self._claim = None
        self._claim_kind = "tui"
```

(b) Add the two ownership methods (place them near `new_session`):

```python
    def adopt_claim(self, claim, *, kind: str) -> None:
        """Take ownership of an externally acquired claim (the CLI launch path
        claims before the Harness exists). Adopting a second claim releases the
        first — ownership is one-session-at-a-time."""
        if self._claim is not None and self._claim is not claim:
            self._claim.release()
        self._claim = claim
        self._claim_kind = kind

    def release_claim(self) -> None:
        """Give up the current claim, if any. Idempotent: teardown paths call
        this unconditionally, and a session switch may have already swapped the
        claim this object was created for."""
        claim, self._claim = self._claim, None
        if claim is not None:
            claim.release()
```

(Type annotations: if the module annotates freely, annotate `claim: SessionClaim | None` with a local import or a string annotation — match the file's existing style. `SessionClaim` must NOT be imported at module top if that would add weight; a local import inside the methods or a `TYPE_CHECKING` guard both fit — follow the file's existing pattern for optional imports.)

(c) `switch_session` — the existing method body (harness.py:744 down to its `return count`) becomes `_switch_session_body`, unchanged including its long job-history comment. The new `switch_session`:

```python
    def switch_session(self, session_id: str) -> int:
        """Load another session, moving the ownership claim with the view.

        Ownership is claimed BEFORE any outgoing-state mutation: a refused
        switch is a total no-op, and a failed load releases the tentative
        claim so we never hold a session we never reached. The outgoing claim
        is released only after the incoming session loaded and was claimed.
        """
        from ..session.claim import SessionClaimed, read_holder, try_acquire

        target = self.session.session_path(session_id)
        tentative = try_acquire(target, kind=self._claim_kind)
        if tentative is None:
            raise SessionClaimed(session_id, read_holder(target))
        try:
            count = self._switch_session_body(session_id)
        except Exception:
            tentative.release()
            raise
        old, self._claim = self._claim, tentative
        if old is not None:
            old.release()
        return count
```

Rename the current method to `_switch_session_body(self, session_id: str) -> int` keeping its body and comments exactly, EXCEPT its leading docstring/comment block stays (it explains the job-history ordering — load-bearing, do not trim).

(d) `new_session` (harness.py:732) — swap claims right after the manager creates the fresh session:

```python
    def new_session(self, name: str | None = None) -> None:
        from ..session.claim import try_acquire

        self.session.new_session(name)
        # Ownership follows the view: claim the fresh session, release the one
        # we're leaving. A brand-new unique id cannot realistically be held; on
        # the impossible case fresh is None and we proceed unclaimed (accepted
        # degrade stance, same as a failed claim-file open).
        fresh = try_acquire(
            self.session.session_path(self.session.session_id), kind=self._claim_kind
        )
        old, self._claim = self._claim, fresh
        if old is not None:
            old.release()
        self.checkpoints.reload()
        ...  # rest of the method unchanged (model/advisor/thinking lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent.py tests/test_agent_sessions.py tests/test_switch_jobs_history.py --no-cov -q`
Expected: PASS — the pre-existing switch tests exercise `_switch_session_body` through the new wrapper with no claim adopted (`self._claim` None → no refuse, no swap), which must remain a no-op path.

- [ ] **Step 5: Full gates, then commit**

Run all four gates (Global Constraints). Then:

```bash
git add src/marim_harness/runtime/harness.py tests/test_agent.py
git commit -m "feat(harness): claims follow the active view (adopt/swap on switch and new)"
```

---

### Task 4: TUI refusal notice for a claimed switch target

`app.switch_to_session_id` (`interfaces/tui/app.py:655-661`) is the single funnel for `/sessions`, the session picker (`_on_session_chosen`, app.py:747-750), and `commands.py:156`. The refusal raised by Task 3 surfaces here.

**Files:**
- Modify: `src/marim_harness/interfaces/tui/app.py` (`switch_to_session_id`, ~line 655)
- Test: `tests/test_app.py` (append near `test_switch_session_refused_while_busy`, line 4109)

**Interfaces:**
- Consumes: `SessionClaimed` from `marim_harness.session.claim`; `self._refuse_if_session_busy`, `self.post_system` (both already used in app.py:655-671).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_app.py` next to `test_switch_session_refused_while_busy` (match its decorator — `@pytest.mark.anyio` — and the `_app(tmp_path)` helper):

```python
@pytest.mark.anyio
async def test_switch_session_refused_when_claimed_elsewhere(tmp_path: Path, monkeypatch):
    from marim_harness.session.claim import Holder, SessionClaimed

    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        current = app.harness.session.store.session_id

        async def refusing(session_id) -> None:
            raise SessionClaimed(
                session_id, Holder(pid=999, kind="daemon", endpoint="http://127.0.0.1:8643")
            )

        monkeypatch.setattr(app.session, "switch_to_session_id", refusing)
        await app.switch_to_session_id("20260101-000000-abc123")
        await pilot.pause()
        # No crash, no switch — the TUI stays on its session.
        assert app.harness.session.store.session_id == current
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_app.py -k claimed_elsewhere --no-cov -q`
Expected: FAIL (`SessionClaimed` propagates uncaught — `run_test` surfaces it).

- [ ] **Step 3: Implement**

In `interfaces/tui/app.py` replace `switch_to_session_id` (~line 655):

```python
    async def switch_to_session_id(self, session_id: str) -> None:
        """Load an existing session and show where it left off. Refused mid-turn
        for the same reason as /new — the running turn writes to the session it
        would be switched away from — and refused when another process owns the
        target: claims follow the active view, so driving a session means
        claiming it, and a claimed session is off-limits until its holder
        releases it."""
        if await self._refuse_if_session_busy("switch sessions"):
            return
        from ..session.claim import SessionClaimed

        try:
            await self.session.switch_to_session_id(session_id)
        except SessionClaimed as exc:
            who = exc.holder.describe() if exc.holder is not None else "another process"
            await self.post_system(f"Can't switch sessions: {exc.session_id} is owned by {who}.")
```

(`session_view.switch_to_session_id` at session_view.py:687 needs no change — it calls `harness.switch_session`, which now raises, and the exception propagates to this catch.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_app.py -k "switch_session" --no-cov -q`
Expected: PASS (busy-refusal test unaffected — the busy guard still returns before the claim path).

- [ ] **Step 5: Commit**

```bash
uv run ruff format src tests && uv run ruff check src tests
git add src/marim_harness/interfaces/tui/app.py tests/test_app.py
git commit -m "feat(tui): refuse switching onto a claimed session with a holder notice"
```

---

### Task 5: CLI claims before `build_harness`

Ownership is taken BEFORE `build_harness` reads the store for a resumed session; a fresh session (no `--resume`) is still claimed post-build by the existing `_acquire_session` (its id only exists after the build). The claim is adopted by the Harness (Task 3) so a switch can move it, and released through the Harness on exit.

**Files:**
- Modify: `src/marim_harness/interfaces/cli/default_cmd.py` (`_run_claimed` at ~line 137; headless path ~line 206-225; TUI path ~line 246-251)
- Test: `tests/test_default_cmd.py` (append; module pattern: `SimpleNamespace` stubs + `io.StringIO` err)

**Interfaces:**
- Consumes: `Harness.adopt_claim`/`release_claim` (Task 3); `build_harness(workspace, mode=..., resume=...)` and `build_harness(workspace, mode=..., session_id=...)` (bootstrap.py:65+, mutually exclusive per bootstrap.py:126); `SessionManager(workspace).latest()` (store.py:514) and `.session_path(id)` (store.py:377).
- Produces: `_resolve_target_session(workspace: Path, resume: bool) -> str | None` and `_claim_target(workspace: Path, target: str | None, *, kind: str, err) -> tuple[SessionClaim | None, bool]` — both importable for tests.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_default_cmd.py`:

```python
def test_resolve_target_session_none_without_resume(tmp_path):
    from marim_harness.interfaces.cli.default_cmd import _resolve_target_session

    assert _resolve_target_session(tmp_path, resume=False) is None


def test_resolve_target_session_picks_latest(tmp_path):
    from marim_harness.interfaces.cli.default_cmd import _resolve_target_session

    (tmp_path / "20260101-000000-aaaaaa.json").write_text("{}")
    (tmp_path / "20260202-000000-bbbbbb.json").write_text("{}")
    assert _resolve_target_session(tmp_path, resume=True) == "20260202-000000-bbbbbb"


def test_resolve_target_session_none_when_no_sessions(tmp_path):
    from marim_harness.interfaces.cli.default_cmd import _resolve_target_session

    assert _resolve_target_session(tmp_path, resume=True) is None


def test_claim_target_claims_resolved_session(tmp_path):
    import io

    from marim_harness.interfaces.cli.default_cmd import _claim_target
    from marim_harness.session import SessionManager

    session_path = tmp_path / "20260101-000000-aaaaaa.json"
    session_path.write_text("{}")
    claim, ok = _claim_target(
        tmp_path, "20260101-000000-aaaaaa", kind="headless", err=io.StringIO()
    )
    try:
        assert ok is True and claim is not None
    finally:
        if claim is not None:
            claim.release()


def test_claim_target_refuses_owned_session(tmp_path):
    import io

    from marim_harness.interfaces.cli.default_cmd import _claim_target
    from marim_harness.session.claim import try_acquire

    session_path = tmp_path / "20260101-000000-aaaaaa.json"
    session_path.write_text("{}")
    outsider = try_acquire(session_path, kind="daemon", endpoint="http://127.0.0.1:8643")
    err = io.StringIO()
    try:
        claim, ok = _claim_target(
            tmp_path, "20260101-000000-aaaaaa", kind="headless", err=err
        )
    finally:
        outsider.release()
    assert ok is False and claim is None
    assert "owned by daemon" in err.getvalue()
    assert "http://127.0.0.1:8643" in err.getvalue()


def test_run_default_refuses_before_building_a_claimed_session(tmp_path, monkeypatch):
    """The ordering proof: with --resume, refusal happens before build_harness
    is even called. Mirrors _stub_launch_paths (tests/test_default_cmd.py:69)
    but records the build call instead of stubbing it away."""
    from marim_harness.interfaces.cli import default_cmd
    from marim_harness.interfaces.cli import headless as headless_mod
    from marim_harness.interfaces.cli.default_cmd import run_default
    from marim_harness.runtime import bootstrap
    from marim_harness.session.claim import try_acquire

    session_path = tmp_path / "20260101-000000-aaaaaa.json"
    session_path.write_text("{}")
    outsider = try_acquire(session_path, kind="daemon", endpoint="http://127.0.0.1:8642")
    assert outsider is not None
    built = []
    monkeypatch.setattr(bootstrap, "build_harness", lambda *a, **kw: built.append("build"))
    monkeypatch.setattr(default_cmd, "_tui_available", lambda: True)
    monkeypatch.setattr(default_cmd, "_launch_tui", lambda h: built.append("tui"))
    monkeypatch.setattr(headless_mod, "run_headless", lambda *a, **kw: built.append("headless"))
    err = io.StringIO()
    try:
        code = run_default(
            ["--workspace", str(tmp_path), "--resume", "-p", "x"],
            stdin=_TtyStdin(), out=io.StringIO(), err=err,
        )
    finally:
        outsider.release()
    assert code == 2
    assert built == []
    assert "already open" in err.getvalue()
```

IMPORTANT: `run_default(argv, *, stdin=None, out=None, err=None)` takes an ARGV
LIST (default_cmd.py:181), and `_TtyStdin` (tests/test_default_cmd.py:62) is the
module's tty-stdin stand-in — both are already in this test module. The
`--workspace` flag exists (default_cmd.py:39-41). The workspace passed to
`_resolve_target_session`/`_claim_target` is `Path(args.workspace).resolve()` —
so files created directly in `tmp_path` line up. The pre-existing phase-2 tests
(`test_run_default_refuses_a_claimed_session_without_launching` and
`test_run_default_releases_the_claim_after_a_normal_run`) must keep passing —
they exercise the post-build path (no `--resume`), which this task preserves.
If any quoted line number is off, `grep -n` for the name; do not improvise the
semantics.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_default_cmd.py -k "resolve_target_session or claim_target or refuses_before_building" --no-cov -q`
Expected: FAIL (helpers don't exist).

- [ ] **Step 3: Implement**

In `interfaces/cli/default_cmd.py`, add after `_acquire_session` (~line 135):

```python
def _resolve_target_session(workspace: Path, resume: bool) -> str | None:
    """The session id a launch will reattach to, resolved BEFORE build_harness
    so ownership can be taken before any store read. --resume is a flag: it
    means the workspace's most recent session (bootstrap.py's manager.latest()
    rule). No resume (or no sessions yet) -> None: a fresh session's id only
    exists after the build, so it is claimed post-build by _acquire_session."""
    if not resume:
        return None
    from ...session.store import SessionManager

    latest = SessionManager(workspace).latest()
    return latest.id if latest is not None else None


def _claim_target(workspace: Path, target: str | None, *, kind: str, err):
    """Claim a pre-resolved target session before build_harness reads it.

    Returns ``(claim, may_proceed)``; ``may_proceed`` False means the refusal
    message was printed. ``target`` None -> ``(None, True)``: nothing to claim
    up front."""
    if target is None:
        return None, True
    from ...session.claim import read_holder, try_acquire
    from ...session.store import SessionManager

    session_path = SessionManager(workspace).session_path(target)
    claim = try_acquire(session_path, kind=kind)
    if claim is not None:
        return claim, True
    holder = read_holder(session_path)
    who = holder.describe() if holder is not None else "another process"
    print(
        f"session {target} is already open in {who}.\n"
        "Close it there first, or start a new session (drop --resume).",
        file=err,
    )
    return None, False
```

(`Path` must be importable in annotations — check the module's existing imports; add `from pathlib import Path` if absent.)

Replace `_run_claimed` (line 137-151):

```python
def _run_claimed(harness, *, kind: str, err, run, claim=None) -> int:
    """Run `run()` under a session claim, adopted by the Harness, and release
    it on the way out.

    `claim` is a PRE-BUILD claim for a resumed session (ownership taken before
    build_harness read the store — see _claim_target). When None, the session
    is claimed post-build (_acquire_session: fresh sessions have their id only
    after the build). Either way the claim is adopted by the Harness so an
    in-run session switch can move it, and released through the Harness on
    exit — idempotent, since a switch may have already swapped it.

    Returns 2 without calling `run` when a post-build claim finds the session
    owned elsewhere.
    """
    if claim is None:
        claim, may_proceed = _acquire_session(harness, kind=kind, err=err)
        if not may_proceed:
            return 2
    harness.adopt_claim(claim, kind=kind)
    try:
        return run()
    finally:
        harness.release_claim()
```

Headless path (~line 206-225) — replace the `harness = build_harness(...)` + `return _run_claimed(...)` block:

```python
        mode = Mode(args.mode) if args.mode else Mode.auto
        target = _resolve_target_session(workspace, args.resume)
        claim, may_proceed = _claim_target(workspace, target, kind="headless", err=err)
        if not may_proceed:
            return 2
        try:
            harness = build_harness(
                workspace,
                mode=mode,
                session_id=target if target is not None else None,
                resume=args.resume and target is None,
            )
        except BaseException:
            if claim is not None:
                claim.release()
            raise
        return _run_claimed(
            harness,
            kind="headless",
            err=err,
            claim=claim,
            run=lambda: asyncio.run(
                run_headless(harness, prompt, args.output_format, out=out, err=err)
            ),
        )
```

TUI path (~line 246-251) — same shape with `kind="tui"` and `run=lambda: _launch_tui(harness)`:

```python
    mode = Mode(args.mode) if args.mode else None
    target = _resolve_target_session(workspace, args.resume)
    claim, may_proceed = _claim_target(workspace, target, kind="tui", err=err)
    if not may_proceed:
        return 2
    try:
        harness = build_harness(
            workspace,
            mode=mode,
            session_id=target if target is not None else None,
            resume=args.resume and target is None,
        )
    except BaseException:
        if claim is not None:
            claim.release()
        raise
    return _run_claimed(harness, kind="tui", err=err, claim=claim, run=lambda: _launch_tui(harness))
```

Semantics check: `resume=args.resume and target is None` — when a target resolved, bootstrap gets `session_id=target` (exact reattach, same store `manager.latest()` picked); when no target (no flag, or flag with zero sessions), bootstrap keeps its original `resume` behavior (latest-or-create). Equivalent to before, minus the read-before-claim window.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_default_cmd.py --no-cov -q`
Expected: PASS — including the phase-2 pinned refusal tests (message text unchanged).

- [ ] **Step 5: Full gates, then commit**

Run all four gates. Then:

```bash
git add src/marim_harness/interfaces/cli/default_cmd.py tests/test_default_cmd.py
git commit -m "feat(cli): claim the resumed session before build_harness reads it"
```

---

### Task 6: serve binds before publishing `runtime.json`

A doomed second daemon must die at the bind, BEFORE it can overwrite (and later pid-guard-clear) the live daemon's `runtime.json`. Pre-bind the listen socket, publish only after success, hand the fd to uvicorn.

**Files:**
- Modify: `src/marim_harness/interfaces/cli/serve.py` (add `bind_listener`; rework the run block at ~line 431-440)
- Test: `tests/test_cli_serve.py` (append; module monkeypatches uvicorn + state dir — see `test_serve_main_builds_app_and_runs_uvicorn` at line 13 and `test_serve_publishes_runtime_json_for_the_life_of_the_run` at line 38)

**Interfaces:**
- Consumes: `write_runtime`/`clear_runtime`/`format_base_url` (server/runtime.py), unchanged.
- Produces: `bind_listener(host: str, port: int) -> socket.socket` — raises `OSError` when the port is taken; strips surrounding `[...]` from an IPv6 literal and picks AF_INET6 vs AF_INET. `uvicorn.run(app, fd=<listener fd>, log_level="warning")` replaces `host=`/`port=` kwargs.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_serve.py`:

```python
def test_bind_listener_second_bind_fails():
    import socket

    from marim_harness.interfaces.cli.serve import bind_listener

    first = bind_listener("127.0.0.1", 0)
    try:
        port = first.getsockname()[1]
        import pytest

        with pytest.raises(OSError):
            bind_listener("127.0.0.1", port)
    finally:
        first.close()


def test_bind_listener_strips_ipv6_brackets():
    from marim_harness.interfaces.cli.serve import bind_listener

    sock = bind_listener("[::1]", 0)
    try:
        assert sock.family.name == "AF_INET6"
    finally:
        sock.close()


def test_serve_bind_failure_publishes_no_runtime_json(tmp_path, monkeypatch):
    import io

    import pytest

    from marim_harness.interfaces.cli import serve

    occupier = serve.bind_listener("127.0.0.1", 0)
    port = occupier.getsockname()[1]
    monkeypatch.setattr(serve, "_default_state_dir", lambda: tmp_path)
    ran = []
    monkeypatch.setattr(
        "uvicorn.run", lambda *a, **k: ran.append(True)
    )
    err = io.StringIO()
    try:
        rc = serve.main(["--port", str(port)], out=io.StringIO(), err=err)
    finally:
        occupier.close()
    assert rc == 1
    assert ran == []
    assert not (tmp_path / "runtime.json").exists()
    assert "cannot bind" in err.getvalue()
```

Match `serve.main`'s real signature/parameter names first (`grep -n 'def main' src/marim_harness/interfaces/cli/serve.py`) — the existing tests in this file show the exact calling convention, including how the state dir is redirected and how uvicorn is patched; copy that plumbing. The uvicorn patch target must match where serve imports it (serve imports uvicorn inside `main`'s try-block — patch `"uvicorn.run"` at source-module level as the existing test does).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_serve.py -k "bind_listener or bind_failure" --no-cov -q`
Expected: FAIL (`bind_listener` doesn't exist).

- [ ] **Step 3: Implement**

Add to `interfaces/cli/serve.py` (module level, near the other helpers):

```python
def bind_listener(host: str, port: int):
    """Bind the listen socket BEFORE publishing runtime.json.

    A daemon that cannot take the port (a squatter while another daemon is
    live) dies here — before writing runtime.json — so it can neither
    overwrite the live daemon's discovery record nor clear it on the way out.
    Accepts bare ("::1") or bracketed ("[::1]") IPv6 literals."""
    import socket

    addr = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    family = socket.AF_INET6 if ":" in addr else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((addr, port))
    except OSError:
        sock.close()
        raise
    return sock
```

In `run_serve` (the `main` body), replace the block:

```python
    write_runtime(state_dir, host=args.host, port=args.port)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        # Best-effort: a SIGKILL leaves the file behind, which is why readers
        # treat it as a hint and let the connection attempt be authoritative.
        clear_runtime(state_dir)
    return 0
```

with (keep everything ABOVE it — supervisor/app/startup/banner/qr — unchanged):

```python
    try:
        listener = bind_listener(args.host, args.port)
    except OSError as exc:
        print(
            f"marim serve: cannot bind {args.host}:{args.port}: {exc}", file=err
        )
        return 1
    write_runtime(state_dir, host=args.host, port=args.port)
    try:
        # fd handoff: uvicorn serves on the socket we already bound, so the
        # bind failure (a live daemon on this port) was decided BEFORE
        # runtime.json was published. `listener` stays referenced for the
        # whole run — closing it early would drop the listening socket.
        uvicorn.run(app, fd=listener.fileno(), log_level="warning")
    finally:
        # Best-effort: a SIGKILL leaves the file behind, which is why readers
        # treat it as a hint and let the connection attempt be authoritative.
        clear_runtime(state_dir)
        listener.close()
    return 0
```

Update the existing `test_serve_main_builds_app_and_runs_uvicorn` and any sibling test asserting `uvicorn.run` kwargs: they must now expect `fd=<int>` instead of `host=`/`port=` (assert the fd is an int, or capture and check `socket.fromfd` — simplest: assert `"fd" in kwargs and isinstance(kwargs["fd"], int)` and that `host`/`port` are absent). Keep their other assertions intact.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_serve.py tests/test_server_runtime.py --no-cov -q`
Expected: PASS.

- [ ] **Step 5: Full gates, then commit**

Run all four gates. Then:

```bash
git add src/marim_harness/interfaces/cli/serve.py tests/test_cli_serve.py
git commit -m "fix(serve): bind before publishing runtime.json (fd handoff to uvicorn)"
```

---

### Task 7: Docs + final verification sweep

**Files:**
- Modify: `docs/reference/serve-api.md` (~line 516 claim paragraph; the 409 code bullet list)

- [ ] **Step 1: Update serve-api.md**

Find the claim paragraph (~line 516; `grep -n "phase 4" docs/reference/serve-api.md`):

```
…neither claim is released or transferred on an in-TUI switch (/sessions or
/new) — the launched-against session stays claimed until that process exits
and the switched-into session runs unclaimed. Claim handling on session
switches will be done in phase 4…
```

Replace that claim-switch statement with:

```
Claims follow the active view: an in-TUI switch (/sessions or the picker)
claims the target session and releases the one being left; switching onto a
session owned by another process is refused with a notice naming the holder
(kind, pid, endpoint). /new releases the outgoing claim and claims the fresh
session. The claim lifecycle for headless and daemon paths is unchanged
(held until exit; the daemon releases through SessionHost teardown).
```

In the same file's 409 `claimed` documentation (added by phase 2), extend the route list: the code is returned by `POST /messages` AND `DELETE /workspaces/{wid}/sessions/{sid}` when the session is owned elsewhere. Match the file's existing bullet style — read the surrounding lines before editing.

- [ ] **Step 2: Full final gates**

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run pyright
uv run pytest
uv run --python 3.10 pytest -q
```

All five must exit 0. If `ruff format --check` flags anything, run `uv run ruff format src tests` and re-commit the formatting as part of this task's commit.

- [ ] **Step 3: Commit**

```bash
git add docs/reference/serve-api.md
git commit -m "docs(serve-api): claims follow the active view; delete refuses claimed sessions"
```

- [ ] **Step 4: Phase-2 invariant smoke-check**

```bash
grep -rn 'claim.json\|\.lock' src/marim_harness/session/claim.py | head
uv run pytest tests/test_session_claim.py tests/test_server_supervisor.py tests/test_server_host.py tests/test_default_cmd.py -q --no-cov
```

Expected: no sidecar-name drift (`<id>.json.claim` only), every phase-2 claim test still green. Report the branch SHA and gate results back.

---

## Self-review notes (plan author)

- Spec coverage: item 1 → Tasks 3+4 (+Task 1 foundation); item 2 → Task 6; item 3 → Task 5; item 4 → Task 2; item 5 → Task 1; serve-api doc update → Task 7. Non-goals respected (degrade stance untouched; no phase-3/4 machinery).
- Spec correction applied alongside this plan: `--resume` is a `store_true` flag (default_cmd.py:42-46), so the resolver is latest()-based, not id-based.
- Ordering: Task 1 (exception home) → Task 2 (delete, uses it) → Task 3 (Harness) → Task 4 (TUI, uses 3) → Task 5 (CLI, uses 3) → Task 6 (independent) → Task 7 (docs/gates).
- Risk noted for implementers: `_switch_harness`/`_app` test helpers and `serve.main` signatures are quoted from the tree at `02ebcf4b` (master); if a quoted block doesn't match, stop and report rather than improvise.
