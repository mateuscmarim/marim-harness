# Session Ownership Claim (Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make it impossible for two processes to run a harness on the same session, so a concurrent TUI + daemon (or headless) can no longer silently overwrite each other's turns.

**Architecture:** A per-session claim file next to the session JSON, held for the owner's lifetime with a non-blocking `fcntl.flock`. Because flock is released by the kernel when the holding fd closes — including on process death — ownership is self-healing: there is no pid liveness check, no stale-claim sweeper, and no pid-reuse hazard. Every would-be owner (daemon, TUI, headless) acquires before running turns; failure to acquire is an explicit refusal naming the holder.

**Tech Stack:** Python 3.10+, stdlib `fcntl`/`os`/`json`, pytest, Starlette (server routes only).

**Spec:** `docs/superpowers/specs/2026-08-29-cross-process-session-events-design.md` (section 4, "Ownership and discovery")

## Global Constraints

- Ruff line length is 100. Lint set: `E,F,I,UP,B,SIM,C901`. Run `uv run ruff check src tests`.
- Cyclomatic complexity cap is 10 (`C901`). Extract named helpers rather than adding `# noqa: C901`.
- `requires-python = ">=3.10"`. No 3.11+ only syntax (no `except*`, no `Self`, no `typing.override`).
- Use `uv` for everything: `uv run pytest`, `uv run ruff`, `uv run pyright`. Never bare `python`/`pip`/`pytest`.
- Gates in order, all must pass before a task is done: `uv run ruff check src tests` → `uv run pyright` → `uv run pytest`.
- For a fast single-file run use `uv run pytest --no-cov tests/test_x.py`. Use `-n 0` for serial when debugging.
- `src/marim_harness/session/claim.py` must import **only stdlib**. The TUI reads claims without the `[serve]` extra installed, so it must never pull in starlette/uvicorn.

---

## File Structure

- **Create** `src/marim_harness/session/claim.py` — the claim primitive. One responsibility: acquire/release/inspect a single-owner claim on a session path. Stdlib only.
- **Create** `src/marim_harness/server/runtime.py` — write/read the daemon's `runtime.json`. Stdlib only (a future TUI reads it without the serve extra).
- **Modify** `src/marim_harness/server/host.py` — `SessionHost` holds a claim and releases it in `aclose`.
- **Modify** `src/marim_harness/server/supervisor.py` — `host_for` acquires before building; new `SessionClaimed` exception.
- **Modify** `src/marim_harness/server/http.py` — map `SessionClaimed` to `409 claimed`.
- **Modify** `src/marim_harness/interfaces/cli/default_cmd.py` — TUI and headless both claim, refuse cleanly, release on exit.
- **Modify** `src/marim_harness/interfaces/cli/serve.py` — write `runtime.json` on start, remove on exit.
- **Create** `tests/test_session_claim.py`, `tests/test_server_runtime.py`.
- **Modify** `tests/test_server_supervisor.py`, `tests/test_server_http.py`, `tests/test_default_cmd.py`.

---

### Task 1: The claim primitive

**Files:**
- Create: `src/marim_harness/session/claim.py`
- Test: `tests/test_session_claim.py`

**Interfaces:**
- Consumes: nothing (leaf module, stdlib only).
- Produces:
  - `claim_path(session_path) -> Path` — the sidecar path, `<session>.json.claim`.
  - `Holder` frozen dataclass with fields `pid: int`, `kind: str`, `endpoint: str | None` and method `describe() -> str`.
  - `SessionClaim` with `.path: Path`, `.release() -> None`, and context-manager support.
  - `try_acquire(session_path, *, kind: str, endpoint: str | None = None) -> SessionClaim | None` — `None` means someone else holds it.
  - `read_holder(session_path) -> Holder | None` — diagnostic read; only meaningful after a failed `try_acquire`.

**Background the implementer needs:**

`fcntl.flock` locks are attached to the *open file description*, not the process. Two separate `os.open()` calls conflict with each other even inside one process — which is why the tests below can exercise contention without spawning a subprocess.

The identity JSON is written **directly to the locked fd** (`ftruncate` + `lseek` + `write`), NOT via `atomic_write_text`. `atomic_write_text` does `os.replace`, which swaps in a *new inode*; the lock lives on the old one, so a later acquirer would open the new inode, lock it successfully, and you would have two owners. This is the same reason `atomic_io.file_lock` locks a sidecar rather than the target.

The existing `atomic_io.file_lock` uses `<path>.lock`. The claim MUST use a different suffix (`.claim`) — reusing `.lock` would mean holding a claim blocks every `SessionStore.save()` forever.

`SessionManager.list()` globs `*.json`, so a `<id>.json.claim` file is invisible to it. Do not name it `<id>.claim.json`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_session_claim.py`:

```python
"""The single-owner session claim: acquire, contend, release, inspect."""

import json
import os
from pathlib import Path

import pytest

from marim_harness.session.claim import (
    Holder,
    claim_path,
    read_holder,
    try_acquire,
)


@pytest.fixture
def session_file(tmp_path: Path) -> Path:
    path = tmp_path / "sessions" / "abc123.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    return path


def test_claim_path_is_a_sidecar_of_the_session_file(session_file: Path) -> None:
    assert claim_path(session_file) == session_file.with_name("abc123.json.claim")


def test_acquire_succeeds_on_an_unclaimed_session(session_file: Path) -> None:
    claim = try_acquire(session_file, kind="tui")
    assert claim is not None
    claim.release()


def test_second_acquire_is_refused_while_the_first_is_held(session_file: Path) -> None:
    first = try_acquire(session_file, kind="tui")
    assert first is not None
    try:
        assert try_acquire(session_file, kind="daemon") is None
    finally:
        first.release()


def test_release_makes_the_session_claimable_again(session_file: Path) -> None:
    first = try_acquire(session_file, kind="tui")
    assert first is not None
    first.release()
    second = try_acquire(session_file, kind="daemon")
    assert second is not None
    second.release()


def test_context_manager_releases_on_exit(session_file: Path) -> None:
    with try_acquire(session_file, kind="tui") as claim:
        assert claim is not None
    assert try_acquire(session_file, kind="daemon") is not None


def test_holder_identity_is_readable_by_the_refused_caller(session_file: Path) -> None:
    claim = try_acquire(session_file, kind="daemon", endpoint="http://127.0.0.1:8642")
    try:
        assert try_acquire(session_file, kind="tui") is None
        holder = read_holder(session_file)
        assert holder == Holder(
            pid=os.getpid(), kind="daemon", endpoint="http://127.0.0.1:8642"
        )
        assert "daemon" in holder.describe()
        assert "8642" in holder.describe()
    finally:
        claim.release()


def test_read_holder_is_none_when_no_claim_file_exists(session_file: Path) -> None:
    assert read_holder(session_file) is None


def test_read_holder_is_none_on_a_corrupt_claim_file(session_file: Path) -> None:
    claim_path(session_file).write_text("not json{{{")
    assert read_holder(session_file) is None


def test_read_holder_is_none_when_fields_are_missing(session_file: Path) -> None:
    claim_path(session_file).write_text(json.dumps({"kind": "tui"}))
    assert read_holder(session_file) is None


def test_claim_file_is_not_listed_as_a_session(session_file: Path) -> None:
    from marim_harness.session.store import SessionManager

    workspace = session_file.parent.parent / "ws"
    workspace.mkdir()
    manager = SessionManager(workspace, base_dir=session_file.parent.parent / "base")
    manager.dir.mkdir(parents=True, exist_ok=True)
    real = manager.session_path("real")
    real.write_text(json.dumps({"id": "real", "name": "real", "messages": []}))
    claim = try_acquire(real, kind="tui")
    try:
        assert [info.id for info in manager.list()] == ["real"]
    finally:
        claim.release()


def test_release_is_idempotent(session_file: Path) -> None:
    claim = try_acquire(session_file, kind="tui")
    assert claim is not None
    claim.release()
    claim.release()  # must not raise
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_session_claim.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.session.claim'`

- [ ] **Step 3: Write the implementation**

Create `src/marim_harness/session/claim.py`:

```python
"""Single-owner claims on a session, so two processes never run one harness.

A session file is shared state: the TUI, a headless run, and the serve daemon
all read it at start and write the whole history back at each persist. The
advisory lock in ``atomic_io.file_lock`` keeps those writes from tearing, but it
does not stop two *owners* from existing — both load the same baseline and the
second to finish silently overwrites the first's turn.

A claim fixes that by making ownership explicit for the owner's whole lifetime,
not just the width of one write. It is a non-blocking ``fcntl.flock`` held on a
``<session>.json.claim`` sidecar. Because flock is released by the kernel when
the holding fd closes — including when the process dies, is killed, or the
machine reboots — a claim is self-healing. There is deliberately no pid liveness
check, no stale sweeper, and no pid-reuse hazard to reason about: if you can
take the lock, nobody owns the session.

Two naming constraints, both load-bearing:

- The suffix is ``.claim``, never ``.lock``. ``atomic_io.file_lock`` locks
  ``<path>.lock`` around every ``SessionStore.save``; sharing that file would
  mean holding a claim blocks all persistence forever.
- It is ``<id>.json.claim``, never ``<id>.claim.json``. ``SessionManager.list``
  globs ``*.json``, and a claim that matched would render as a phantom session.
"""

import contextlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

# POSIX-only, exactly as in atomic_io. Guarded so this module still imports on a
# platform without it, where claiming degrades to always-succeed (see try_acquire).
try:  # pragma: no cover - exercised by import on the running platform
    import fcntl
except ImportError:  # pragma: no cover - Windows / no-fcntl platforms
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

CLAIM_SUFFIX = ".claim"


def claim_path(session_path) -> Path:
    """The claim sidecar for a session file: ``<id>.json`` -> ``<id>.json.claim``."""
    path = Path(session_path)
    return path.with_name(path.name + CLAIM_SUFFIX)


@dataclass(frozen=True)
class Holder:
    """Who owns a claimed session — read for the refusal message only."""

    pid: int
    kind: str  # "tui" | "headless" | "daemon"
    endpoint: str | None

    def describe(self) -> str:
        where = f" at {self.endpoint}" if self.endpoint else ""
        return f"{self.kind} (pid {self.pid}){where}"


class SessionClaim:
    """A held claim. Release it (or leave the ``with`` block) to give up ownership.

    Releasing is best-effort and idempotent: the kernel drops the lock when the
    fd closes regardless, so a failed unlock can never strand a session.
    """

    def __init__(self, path: Path, fd: int | None) -> None:
        self.path = path
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        if fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)

    def __enter__(self) -> "SessionClaim":
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


def try_acquire(
    session_path, *, kind: str, endpoint: str | None = None
) -> SessionClaim | None:
    """Take ownership of ``session_path``, or return None if someone else has it.

    Never blocks. On a platform without ``fcntl`` this always succeeds with an
    unlocked claim — matching ``atomic_io.file_lock``'s degrade-to-no-op stance,
    since locking is a safety net and refusing to run at all would be worse.
    """
    path = claim_path(session_path)
    if fcntl is None:
        return SessionClaim(path, None)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        logger.debug("claim file unavailable at %s, proceeding unclaimed: %s", path, exc)
        return SessionClaim(path, None)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Held by someone else. Close our fd (it holds no lock) and refuse.
        with contextlib.suppress(OSError):
            os.close(fd)
        return None
    _write_identity(fd, kind=kind, endpoint=endpoint)
    return SessionClaim(path, fd)


def _write_identity(fd: int, *, kind: str, endpoint: str | None) -> None:
    """Stamp who we are into the locked file, for a refused caller to report.

    Written straight to the locked fd — deliberately NOT via
    ``atomic_write_text``. That does ``os.replace``, which swaps in a new inode;
    our flock lives on the old one, so the next acquirer would open the new
    inode, lock it successfully, and both processes would believe they own the
    session. Best-effort: the identity is diagnostics, and failing to record it
    must not cost us the claim we already hold.
    """
    payload = json.dumps({"pid": os.getpid(), "kind": kind, "endpoint": endpoint})
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload.encode())
        os.fsync(fd)
    except OSError as exc:
        logger.debug("could not stamp claim identity: %s", exc)


def read_holder(session_path) -> Holder | None:
    """Who currently claims ``session_path``, or None if unreadable.

    Only meaningful right after a ``try_acquire`` returned None: the file
    outlives the lock, so its contents describe the *last* holder, who may be
    long gone. Never use this to decide ownership — that is what the lock is for.
    """
    try:
        data = json.loads(claim_path(session_path).read_text())
    except (OSError, ValueError):
        return None
    try:
        return Holder(
            pid=int(data["pid"]), kind=str(data["kind"]), endpoint=data.get("endpoint")
        )
    except (KeyError, TypeError, ValueError):
        return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_session_claim.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 5: Run the gates**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: all clean.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/session/claim.py tests/test_session_claim.py
git commit -m "feat(session): add a single-owner claim on a session file

A session is shared state that every owner rewrites wholesale, so two live
owners lose a turn to last-writer-wins. Claim it with a non-blocking flock
held for the owner's lifetime: the kernel releases it on process death, so
ownership is self-healing with no pid liveness check or stale sweeper."
```

---

### Task 2: The daemon claims each session it hosts

**Files:**
- Modify: `src/marim_harness/server/host.py` (constructor, `aclose`)
- Modify: `src/marim_harness/server/supervisor.py` (`host_for`, new exception, new `endpoint` field)
- Test: `tests/test_server_supervisor.py` (append)

**Interfaces:**
- Consumes: `try_acquire`, `read_holder`, `Holder`, `SessionClaim` from Task 1.
- Produces:
  - `SessionClaimed(Exception)` in `server/supervisor.py`, with attributes `.session_id: str` and `.holder: Holder | None`.
  - `SessionSupervisor.__init__` gains keyword `endpoint: str | None = None`.
  - `SessionHost.__init__` gains keyword `claim: SessionClaim | None = None`.

**Why the claim lives on the host:** every teardown path — `close_host`, `_evict_if_idle`, `close_workspace`, `aclose` — funnels through `SessionHost.aclose()`. Releasing there means idle eviction gives up ownership for free, which is correct: an evicted host is torn down and the session is resumable from disk, so nobody owns it any more.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_server_supervisor.py`:

```python
async def test_host_for_claims_the_session(tmp_path, monkeypatch):
    """A built host owns its session for as long as it lives."""
    from marim_harness.session.claim import try_acquire
    from marim_harness.session.store import SessionManager

    record, supervisor = _registered_workspace(tmp_path)
    host = await supervisor.host_for(record, "s1")
    session_path = SessionManager(Path(record.path)).session_path("s1")
    try:
        assert try_acquire(session_path, kind="tui") is None
    finally:
        await supervisor.close_host(record.id, "s1")
    assert host is not None


async def test_close_host_releases_the_claim(tmp_path):
    from marim_harness.session.claim import try_acquire
    from marim_harness.session.store import SessionManager

    record, supervisor = _registered_workspace(tmp_path)
    await supervisor.host_for(record, "s1")
    await supervisor.close_host(record.id, "s1")
    session_path = SessionManager(Path(record.path)).session_path("s1")
    claim = try_acquire(session_path, kind="tui")
    assert claim is not None
    claim.release()


async def test_idle_eviction_releases_the_claim(tmp_path):
    from marim_harness.session.claim import try_acquire
    from marim_harness.session.store import SessionManager

    record, supervisor = _registered_workspace(tmp_path)
    supervisor.idle_ttl = 0.0
    await supervisor.host_for(record, "s1")
    await supervisor.evict_idle()
    session_path = SessionManager(Path(record.path)).session_path("s1")
    claim = try_acquire(session_path, kind="tui")
    assert claim is not None
    claim.release()


async def test_host_for_refuses_a_session_claimed_by_another_process(tmp_path):
    from marim_harness.server.supervisor import SessionClaimed
    from marim_harness.session.claim import try_acquire
    from marim_harness.session.store import SessionManager

    record, supervisor = _registered_workspace(tmp_path)
    session_path = SessionManager(Path(record.path)).session_path("s1")
    session_path.parent.mkdir(parents=True, exist_ok=True)
    outsider = try_acquire(session_path, kind="tui")
    assert outsider is not None
    try:
        with pytest.raises(SessionClaimed) as excinfo:
            await supervisor.host_for(record, "s1")
        assert excinfo.value.session_id == "s1"
        assert excinfo.value.holder is not None
        assert excinfo.value.holder.kind == "tui"
    finally:
        outsider.release()


async def test_a_refused_claim_leaves_no_host_behind(tmp_path):
    """The refusal must not half-register a session: a later retry, once the
    outsider has gone, has to build cleanly rather than find a wedged entry."""
    from marim_harness.server.supervisor import SessionClaimed
    from marim_harness.session.claim import try_acquire
    from marim_harness.session.store import SessionManager

    record, supervisor = _registered_workspace(tmp_path)
    session_path = SessionManager(Path(record.path)).session_path("s1")
    session_path.parent.mkdir(parents=True, exist_ok=True)
    outsider = try_acquire(session_path, kind="tui")
    with pytest.raises(SessionClaimed):
        await supervisor.host_for(record, "s1")
    assert supervisor.peek(record.id, "s1") is None
    outsider.release()
    host = await supervisor.host_for(record, "s1")
    assert host is not None
    await supervisor.close_host(record.id, "s1")
```

If `_registered_workspace(tmp_path)` does not already exist in that file, add this helper near the top of it (after the imports), building on whatever fake-harness factory the file already uses for `SessionSupervisor`:

```python
def _registered_workspace(tmp_path):
    """A registered workspace plus a supervisor whose factory builds fakes."""
    from marim_harness.server.workspaces import WorkspaceRegistry

    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    registry = WorkspaceRegistry(tmp_path / "workspaces.json", tmp_path / "roots")
    record = registry.register("ws", workspace)
    supervisor = SessionSupervisor(factory=_fake_factory)
    return record, supervisor
```

Reuse the module's existing fake factory for `_fake_factory` — read the top of `tests/test_server_supervisor.py` and use whatever it already calls its stub harness builder. Do not introduce a second stub.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_server_supervisor.py -v`
Expected: FAIL — `ImportError: cannot import name 'SessionClaimed'` and claim assertions failing.

- [ ] **Step 3: Add the claim to SessionHost**

In `src/marim_harness/server/host.py`, the constructor is currently one line:

```python
    def __init__(self, harness: Harness, bus: EventBus, *, queue_limit: int = 8) -> None:
```

Widen it to add a keyword-only `claim` (keeping `queue_limit`, and leaving the
**entire existing body** — `self._queue`, `self._pending`, `self._turn_task`,
`self._closing`, `self._idle_since`, the `WakeDriver` wiring — untouched):

```python
    def __init__(
        self,
        harness: Harness,
        bus: EventBus,
        *,
        queue_limit: int = 8,
        claim: "SessionClaim | None" = None,
    ) -> None:
        self.harness = harness
        self.bus = bus
        # Ownership of the session file for this host's whole lifetime. Released
        # in aclose(), which every teardown path funnels through — including idle
        # eviction, where giving up ownership is correct: the harness is gone and
        # the session is resumable from disk, so nobody owns it.
        self._claim = claim
        # ... rest of the existing body unchanged, starting at self._queue = ...
```

Add the import at the top of `host.py`:

```python
from ..session.claim import SessionClaim
```

Then at the very end of `aclose()`, after the existing `session_end`/`aclose` teardown loop:

```python
        # Last, so ownership outlives every write above: the final persist must
        # complete while we still hold the session.
        if self._claim is not None:
            self._claim.release()
            self._claim = None
```

- [ ] **Step 4: Add the claim to the supervisor**

In `src/marim_harness/server/supervisor.py`, add the imports:

```python
from ..session.claim import Holder, SessionClaim, read_holder, try_acquire
```

Add the exception next to `SessionBusy`:

```python
class SessionClaimed(Exception):
    """Raised by host_for when another live process owns the session.

    Not a transient condition to retry: the holder keeps the session until it
    exits, so the caller's job is to report who has it, not to back off."""

    def __init__(self, session_id: str, holder: "Holder | None") -> None:
        super().__init__(session_id)
        self.session_id = session_id
        self.holder = holder
```

Add the `endpoint` field to `__init__` (so a refused TUI can be told where the daemon is):

```python
    def __init__(
        self,
        factory: HarnessFactory = default_harness_factory,
        *,
        idle_ttl: float = 900.0,
        ring_size: int = 1000,
        endpoint: str | None = None,
    ) -> None:
        self._factory = factory
        self.idle_ttl = idle_ttl
        self.endpoint = endpoint
```

(keep the rest of the existing body unchanged)

Then in `host_for`, replace the build block. The current tail of the `async with lock:` body is:

```python
            harness = await self._factory(Path(record.path), session_id, mode)
            host = SessionHost(harness, self.bus_for(*key))
            self._hosts[key] = host
            return host
```

Replace it with:

```python
            claim = self._claim_session(Path(record.path), session_id)
            try:
                harness = await self._factory(Path(record.path), session_id, mode)
            except BaseException:
                # Nothing was registered, so release rather than strand the
                # session behind a claim no host will ever come to own.
                claim.release()
                raise
            host = SessionHost(harness, self.bus_for(*key), claim=claim)
            self._hosts[key] = host
            return host
```

And add the helper (kept separate so `host_for` stays under the C901 ceiling):

```python
    def _claim_session(self, workspace: Path, session_id: str) -> SessionClaim:
        """Take ownership of the session, or raise SessionClaimed naming the holder."""
        session_path = SessionManager(workspace).session_path(session_id)
        claim = try_acquire(session_path, kind="daemon", endpoint=self.endpoint)
        if claim is None:
            raise SessionClaimed(session_id, read_holder(session_path))
        return claim
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_server_supervisor.py tests/test_server_host.py -v`
Expected: PASS. If pre-existing `test_server_host.py` tests construct `SessionHost(harness, bus)` positionally they keep working — `claim` is keyword-only with a `None` default.

- [ ] **Step 6: Run the gates**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: all clean.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/server/host.py src/marim_harness/server/supervisor.py tests/test_server_supervisor.py
git commit -m "feat(serve): claim a session before hosting it

host_for now takes the session's claim before building the harness and hands
it to the SessionHost, which releases it in aclose(). Every teardown path —
close_host, idle eviction, workspace delete — funnels through aclose, so an
evicted host correctly gives ownership back."
```

---

### Task 3: Refuse a claimed session over HTTP with 409

**Files:**
- Modify: `src/marim_harness/server/http.py` (`post_message`, around line 460)
- Test: `tests/test_server_http.py` (append)

**Interfaces:**
- Consumes: `SessionClaimed` from Task 2.
- Produces: `409` with body `{"error": {"code": "claimed", "message": ...}}`.

`post_message` is the only caller of `host_for` in the whole HTTP layer, so this is the single site that needs the handler.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_server_http.py`:

```python
def test_post_message_409s_when_another_process_claims_the_session(client, tmp_path):
    """A session open in the TUI must be refused, not silently co-hosted."""
    from marim_harness.session.claim import try_acquire
    from marim_harness.session.store import SessionManager

    ws = _register_workspace(client, tmp_path)
    created = client.post(
        f"/v1/workspaces/{ws}/sessions", json={"name": "s"}, headers=AUTH
    )
    session_id = created.json()["id"]
    session_path = SessionManager(_workspace_path(client, ws)).session_path(session_id)
    outsider = try_acquire(session_path, kind="tui")
    assert outsider is not None
    try:
        response = client.post(
            f"/v1/workspaces/{ws}/sessions/{session_id}/messages",
            json={"prompt": "hi"},
            headers=AUTH,
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "claimed"
        assert "tui" in response.json()["error"]["message"]
    finally:
        outsider.release()
```

Use whatever fixtures `tests/test_server_http.py` already defines for `client`, `AUTH`, and workspace registration — read the top of the file and match them exactly. If there is no `_workspace_path` helper, read the registered path out of the `GET /v1/workspaces` response instead.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest --no-cov tests/test_server_http.py -k claims -v`
Expected: FAIL — the request raises `SessionClaimed` out of the route (500 or a propagated exception) instead of returning 409.

- [ ] **Step 3: Handle the exception in the route**

In `src/marim_harness/server/http.py`, add to the imports from `.supervisor`:

```python
from .supervisor import SessionBusy, SessionClaimed, SessionSupervisor
```

(merge into the existing `.supervisor` import line rather than adding a second one)

Then replace line 460's bare call:

```python
    host = await _supervisor(request).host_for(record, session_id)
```

with:

```python
    try:
        host = await _supervisor(request).host_for(record, session_id)
    except SessionClaimed as exc:
        who = exc.holder.describe() if exc.holder is not None else "another process"
        return _error(
            409, "claimed",
            f"session is owned by {who}; close it there before driving it here",
        )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest --no-cov tests/test_server_http.py -k claims -v`
Expected: PASS.

- [ ] **Step 5: Document the new status**

Two edits in `docs/reference/serve-api.md`.

First, the shared error-code list (in `## Conventions`) — replace:

```markdown
Codes used: `unauthorized` (401), `bad_request` (400), `not_found` (404),
`busy` (409), `not_running` (409), `queue_full` (429), `host_closed` (404),
`unreadable` (500), `trust_store_error` (500).
```

with:

```markdown
Codes used: `unauthorized` (401), `bad_request` (400), `not_found` (404),
`busy` (409), `claimed` (409), `not_running` (409), `queue_full` (429),
`host_closed` (404), `unreadable` (500), `trust_store_error` (500).
```

Second, under `### POST /v1/workspaces/{ws}/sessions/{sid}/messages`, extend the
error bullets — replace:

```markdown
- `429 queue_full` — the per-session turn queue is at capacity.
- `404 host_closed` — the host was torn down mid-submit; retry.
```

with:

```markdown
- `429 queue_full` — the per-session turn queue is at capacity.
- `404 host_closed` — the host was torn down mid-submit; retry.
- `409 claimed` — another live process (a local TUI or headless run) owns this
  session. A claim is held for its holder's lifetime, so unlike `busy` this is
  not transient and retrying will not clear it; the message names the holder.
  Close the session there first.
```

- [ ] **Step 6: Run the gates**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: all clean.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/server/http.py tests/test_server_http.py docs/reference/serve-api.md
git commit -m "feat(serve): 409 on a session owned by another process

post_message is the only host_for caller, so one handler covers the surface.
The message names the holder, since a claim is held for the holder's lifetime
and retrying will not help."
```

---

### Task 4: The TUI and headless claim their session

**Files:**
- Modify: `src/marim_harness/interfaces/cli/default_cmd.py` (headless path ~line 145, TUI path ~line 173)
- Test: `tests/test_default_cmd.py` (append)

**Interfaces:**
- Consumes: `try_acquire`, `read_holder` from Task 1.
- Produces: module-level `_acquire_session(harness, *, kind, err) -> tuple[SessionClaim | None, bool]` in `default_cmd.py` — the bool is "may we proceed".

Both CLI paths build a harness and then run turns against it, so **both** must claim. Headless is as capable of clobbering a TUI session as the daemon is.

The claim is taken *after* `build_harness` because that is what resolves which session we got (`--resume` vs a fresh one). Building first is safe: `build_harness` only loads, and no turn — hence no write — has happened yet.

`harness.session` is a `SessionController` whose `.store` is `SessionStore | None`. When it is `None` the session is anonymous and unpersisted, so there is nothing to claim and nothing to clobber: proceed unclaimed.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_default_cmd.py`:

```python
def test_acquire_session_claims_and_permits(tmp_path):
    from marim_harness.interfaces.cli.default_cmd import _acquire_session

    harness = _harness_with_session(tmp_path / "s.json")
    err = io.StringIO()
    claim, ok = _acquire_session(harness, kind="tui", err=err)
    try:
        assert ok is True
        assert claim is not None
        assert err.getvalue() == ""
    finally:
        claim.release()


def test_acquire_session_refuses_when_already_owned(tmp_path):
    from marim_harness.interfaces.cli.default_cmd import _acquire_session
    from marim_harness.session.claim import try_acquire

    session_path = tmp_path / "s.json"
    harness = _harness_with_session(session_path)
    outsider = try_acquire(session_path, kind="daemon", endpoint="http://127.0.0.1:8642")
    err = io.StringIO()
    try:
        claim, ok = _acquire_session(harness, kind="tui", err=err)
        assert ok is False
        assert claim is None
        assert "already open" in err.getvalue()
        assert "daemon" in err.getvalue()
        assert "8642" in err.getvalue()
    finally:
        outsider.release()


def test_acquire_session_permits_an_anonymous_session(tmp_path):
    """No store means nothing persisted, so there is nothing to clobber."""
    from marim_harness.interfaces.cli.default_cmd import _acquire_session

    harness = SimpleNamespace(session=SimpleNamespace(store=None))
    claim, ok = _acquire_session(harness, kind="tui", err=io.StringIO())
    assert ok is True
    assert claim is None
```

Add these helpers near the top of `tests/test_default_cmd.py` if the module does not already have equivalents (it already imports `io` in some tests — check before adding a duplicate import):

```python
import io
from types import SimpleNamespace


def _harness_with_session(session_path):
    """A stand-in exposing only what _acquire_session reads."""
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text("{}")
    store = SimpleNamespace(path=session_path, session_id=session_path.stem)
    return SimpleNamespace(session=SimpleNamespace(store=store))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_default_cmd.py -k acquire -v`
Expected: FAIL — `ImportError: cannot import name '_acquire_session'`.

- [ ] **Step 3: Add the helper**

In `src/marim_harness/interfaces/cli/default_cmd.py`, add at module level (near the other module-level helpers, above `main`):

```python
def _acquire_session(harness, *, kind: str, err) -> "tuple[SessionClaim | None, bool]":
    """Take ownership of this run's session, or explain who already has it.

    Returns ``(claim, may_proceed)``. A ``None`` claim with ``may_proceed`` True
    means there was nothing to claim: an anonymous session has no file on disk,
    so no other process can be overwriting it.
    """
    from ...session.claim import read_holder, try_acquire

    store = getattr(harness.session, "store", None)
    if store is None:
        return None, True
    claim = try_acquire(store.path, kind=kind)
    if claim is not None:
        return claim, True
    holder = read_holder(store.path)
    who = holder.describe() if holder is not None else "another process"
    print(
        f"session {store.session_id} is already open in {who}.\n"
        "Close it there first, or start a new session (drop --resume).",
        file=err,
    )
    return None, False
```

Add the type-only import at the top of the file, inside the existing `TYPE_CHECKING` block if there is one, otherwise create it:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...session.claim import SessionClaim
```

(The runtime import stays inside the function, matching this module's deferred-import discipline — `default_cmd` is on the cold-start path.)

- [ ] **Step 4: Wire the headless path**

Replace lines 145-148 (the headless build-and-run block):

```python
        harness = build_harness(workspace, mode=mode, resume=args.resume)
        return asyncio.run(
            run_headless(harness, prompt, args.output_format, out=out, err=err)
        )
```

with:

```python
        harness = build_harness(workspace, mode=mode, resume=args.resume)
        claim, may_proceed = _acquire_session(harness, kind="headless", err=err)
        if not may_proceed:
            return 2
        try:
            return asyncio.run(
                run_headless(harness, prompt, args.output_format, out=out, err=err)
            )
        finally:
            if claim is not None:
                claim.release()
```

- [ ] **Step 5: Wire the TUI path**

Replace lines 173-175 (the TUI build-and-run block):

```python
    harness = build_harness(workspace, mode=mode, resume=args.resume)
    HarnessApp(harness, history=PromptHistory(default_history_path())).run()
    return 0
```

with:

```python
    harness = build_harness(workspace, mode=mode, resume=args.resume)
    claim, may_proceed = _acquire_session(harness, kind="tui", err=err)
    if not may_proceed:
        return 2
    try:
        HarnessApp(harness, history=PromptHistory(default_history_path())).run()
    finally:
        if claim is not None:
            claim.release()
    return 0
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_default_cmd.py -v`
Expected: PASS, including the pre-existing tests in that file.

- [ ] **Step 7: Run the gates**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: all clean.

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/interfaces/cli/default_cmd.py tests/test_default_cmd.py
git commit -m "feat(cli): claim the session in the TUI and headless paths

Both build a harness and run turns against it, so both can clobber a session
the daemon (or each other) already owns. Claim after build_harness, which is
what resolves --resume into an actual session id, and refuse with the holder
named rather than racing."
```

---

### Task 5: The daemon publishes where it is listening

**Files:**
- Create: `src/marim_harness/server/runtime.py`
- Modify: `src/marim_harness/interfaces/cli/serve.py` (`main`, around lines 346-371)
- Test: `tests/test_server_runtime.py`

**Interfaces:**
- Consumes: `atomic_io.atomic_write_text`.
- Produces:
  - `runtime_path(state_dir) -> Path`
  - `write_runtime(state_dir, *, host: str, port: int) -> Path`
  - `read_runtime(state_dir) -> DaemonRuntime | None`
  - `clear_runtime(state_dir) -> None`
  - `DaemonRuntime` frozen dataclass: `host: str`, `port: int`, `pid: int`, `started: str`, plus `url` property.

**Scope note for the reviewer:** nothing in phase 2 reads `runtime.json`. It exists so phase 4's discovery — the TUI deciding whether a daemon is reachable — has something to read, and it is testable on its own. If you would rather not carry unused code, this task can be dropped without affecting Tasks 1-4.

This module is **stdlib only** and lives outside the starlette-importing modules on purpose: the TUI will read it on machines that never installed the `[serve]` extra.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_server_runtime.py`:

```python
"""The daemon's runtime.json: where it is listening, for a client to find."""

import json
from pathlib import Path

from marim_harness.server.runtime import (
    DaemonRuntime,
    clear_runtime,
    read_runtime,
    runtime_path,
    write_runtime,
)


def test_write_then_read_roundtrips(tmp_path: Path) -> None:
    write_runtime(tmp_path, host="127.0.0.1", port=8642)
    runtime = read_runtime(tmp_path)
    assert isinstance(runtime, DaemonRuntime)
    assert runtime.host == "127.0.0.1"
    assert runtime.port == 8642
    assert runtime.pid > 0
    assert runtime.started


def test_url_is_reconstructable(tmp_path: Path) -> None:
    write_runtime(tmp_path, host="127.0.0.1", port=8642)
    assert read_runtime(tmp_path).url == "http://127.0.0.1:8642"


def test_write_creates_the_state_dir(tmp_path: Path) -> None:
    target = tmp_path / "does" / "not" / "exist"
    write_runtime(target, host="127.0.0.1", port=1)
    assert runtime_path(target).exists()


def test_read_is_none_when_absent(tmp_path: Path) -> None:
    assert read_runtime(tmp_path) is None


def test_read_is_none_on_corrupt_json(tmp_path: Path) -> None:
    runtime_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    runtime_path(tmp_path).write_text("{{{not json")
    assert read_runtime(tmp_path) is None


def test_read_is_none_when_fields_are_missing(tmp_path: Path) -> None:
    runtime_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    runtime_path(tmp_path).write_text(json.dumps({"host": "127.0.0.1"}))
    assert read_runtime(tmp_path) is None


def test_clear_removes_the_file(tmp_path: Path) -> None:
    write_runtime(tmp_path, host="127.0.0.1", port=8642)
    clear_runtime(tmp_path)
    assert read_runtime(tmp_path) is None


def test_clear_is_a_noop_when_absent(tmp_path: Path) -> None:
    clear_runtime(tmp_path)  # must not raise
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_server_runtime.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.server.runtime'`

- [ ] **Step 3: Write the implementation**

Create `src/marim_harness/server/runtime.py`:

```python
"""Where the running daemon is listening, published for local clients.

The token file and the workspace registry both persist under the server state
dir, but nothing recorded the bind address, so a client on the same machine had
no way to find the daemon without being told. ``runtime.json`` closes that: it
is written at startup and removed on clean exit.

Stdlib only, and deliberately outside the starlette-importing modules — a local
TUI reads this on machines that never installed the ``[serve]`` extra.

Presence of the file is a hint, not proof of life: a ``SIGKILL``ed daemon leaves
it behind. Treat a connection failure as the authoritative answer.
"""

import contextlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..atomic_io import atomic_write_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DaemonRuntime:
    host: str
    port: int
    pid: int
    started: str

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


def runtime_path(state_dir) -> Path:
    return Path(state_dir) / "runtime.json"


def write_runtime(state_dir, *, host: str, port: int) -> Path:
    """Record this process as the live daemon. Overwrites any stale file."""
    path = runtime_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        json.dumps(
            {
                "host": host,
                "port": port,
                "pid": os.getpid(),
                "started": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        ),
    )
    return path


def read_runtime(state_dir) -> DaemonRuntime | None:
    """The recorded daemon, or None if absent or unreadable."""
    try:
        data = json.loads(runtime_path(state_dir).read_text())
    except (OSError, ValueError):
        return None
    try:
        return DaemonRuntime(
            host=str(data["host"]),
            port=int(data["port"]),
            pid=int(data["pid"]),
            started=str(data["started"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def clear_runtime(state_dir) -> None:
    """Remove the record on clean shutdown. Never raises."""
    with contextlib.suppress(OSError):
        runtime_path(state_dir).unlink(missing_ok=True)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_server_runtime.py -v`
Expected: PASS, 8 tests.

- [ ] **Step 5: Wire it into serve**

In `src/marim_harness/interfaces/cli/serve.py`, add to the guarded import block (the one that already imports `create_app`, `SessionSupervisor`, `WorkspaceRegistry`):

```python
        from ...server.runtime import clear_runtime, write_runtime
```

Pass the endpoint to the supervisor so a refused client can be told where the daemon is — replace:

```python
    supervisor = SessionSupervisor(idle_ttl=args.idle_ttl)
```

with:

```python
    supervisor = SessionSupervisor(
        idle_ttl=args.idle_ttl, endpoint=f"http://{args.host}:{args.port}"
    )
```

Then wrap the serve loop — replace:

```python
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0
```

with:

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

- [ ] **Step 6: Run the serve tests**

Run: `uv run pytest --no-cov tests/test_cli_serve.py tests/test_server_runtime.py -v`
Expected: PASS.

- [ ] **Step 7: Document it**

In `docs/reference/serve-api.md`, in `## Installation and startup`, replace the
state-dir paragraph:

```markdown
The state dir is `$XDG_DATA_HOME/marim-harness/server` (default
`~/.local/share/marim-harness/server`). It holds the bearer token file, the
workspace registry (`workspaces.json`), and by default the managed-workspaces
root.
```

with:

```markdown
The state dir is `$XDG_DATA_HOME/marim-harness/server` (default
`~/.local/share/marim-harness/server`). It holds the bearer token file, the
workspace registry (`workspaces.json`), and by default the managed-workspaces
root.

While the daemon is running it also holds `runtime.json` —
`{"host": ..., "port": ..., "pid": ..., "started": ...}` — written at startup
and removed on clean exit, so a client on the same machine can find the daemon
without being told the port. A killed daemon leaves the file behind, so treat
it as a hint and let the connection attempt be the authoritative answer.
```

- [ ] **Step 8: Run the gates**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: all clean.

- [ ] **Step 9: Commit**

```bash
git add src/marim_harness/server/runtime.py tests/test_server_runtime.py src/marim_harness/interfaces/cli/serve.py docs/reference/serve-api.md
git commit -m "feat(serve): publish the daemon's bind address in runtime.json

The state dir recorded the token and the workspace registry but never where
the daemon was listening, so a local client had no way to find it. Stdlib-only
module, kept out of the starlette imports so a TUI without the serve extra can
still read it."
```

---

## Deferred to phase 4 (not in this plan)

**Workspace auto-register.** The spec lists it under phase 2, but it requires the
TUI to make an HTTP call to the daemon, which means pulling phase 4's
`RemoteSessionHost` client forward. It belongs with the attach work, where it
has an actual consumer. Nothing in Tasks 1-5 depends on it.

## Verification of the whole phase

After Task 5, confirm the data-loss bug is actually closed, by hand:

1. `uv run marim serve --port 8642` in one terminal.
2. In a second terminal, `cd` to a scratch project and run `uv run marim` (TUI); send one prompt so a session exists and is claimed.
3. Register the workspace and find the session id:
   `curl -H "Authorization: Bearer $(cat ~/.local/share/marim-harness/server/token)" -X POST localhost:8642/v1/workspaces -d '{"name":"scratch","path":"/abs/path/to/scratch"}' -H 'Content-Type: application/json'`
   then `GET /v1/workspaces/scratch/sessions`.
4. `POST .../messages` against the session the TUI has open.
5. Expect `409` with `"code": "claimed"` and a message naming the TUI and its pid — **not** a second turn running.
6. Quit the TUI, repeat step 4, expect `202` and a normally running turn.

Use a free model for any live turn (`MARIM_PROVIDER=zen`, `MARIM_MODEL=mimo-v2.5-free`) — no paid model without explicit approval.
