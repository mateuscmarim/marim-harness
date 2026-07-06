# `marim serve` Server Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `marim serve` HTTP daemon that manages workspaces and agent sessions remotely — REST for control, SSE for streaming, parked approvals/ask_user answered over the wire.

**Architecture:** A transport-neutral core in a new `src/marim_harness/server/` package (schema, event bus, session host, supervisor, workspace registry, auth) with a single transport-aware module (`http.py`, Starlette). One `SessionHost` per active session wraps a `Harness` built via the existing `build_harness` seam, enforces one-turn-at-a-time with a bounded queue, and implements the `bind_ui` callback contract by publishing to a per-session `EventBus` and parking approvals/`ask_user` as answerable futures.

**Tech Stack:** Python ≥3.10, Starlette + uvicorn (new optional extra `[serve]`), Pydantic (already core), pytest + Starlette `TestClient` + pydantic-ai `FunctionModel` for tests.

**Spec:** `docs/superpowers/specs/2026-07-06-marim-serve-design.md`

## Global Constraints

- `requires-python = ">=3.10"` — no 3.11+-only syntax (`asyncio.timeout`, `datetime.UTC`, `tomllib`, `except*`). Use `asyncio.wait_for` and `timezone.utc`.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM` (imports sorted).
- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright`. Never bare `python`/`pip`.
- Verify order before claiming done (matches CI): ruff → pyright → pytest.
- No live/paid models in any test — `FunctionModel`/`TestModel` only.
- Preserve existing long "why" comments when editing near them (resumability, deps/services cycle).
- Tool/HTTP-visible docstrings and long invariant comments follow existing house style.
- All new modules under `src/marim_harness/server/` must import cleanly WITHOUT starlette installed **except** `http.py` (starlette) — the CLI guards that import. Everything else uses only core deps.

## File Structure

Create:
- `src/marim_harness/stream_events.py` — shared pydantic-ai → dict event mapping (extracted from headless)
- `src/marim_harness/server/__init__.py` — empty (deliberate: no re-exports, mirrors `runtime/`)
- `src/marim_harness/server/schema.py` — `Event` envelope, `sse_format`, `STREAM_EVENT_TYPES`, request-body models
- `src/marim_harness/server/bus.py` — `EventBus` + `Subscription`
- `src/marim_harness/server/workspaces.py` — `WorkspaceRecord`, `WorkspaceRegistry`
- `src/marim_harness/server/auth.py` — token create/load/compare
- `src/marim_harness/server/host.py` — `SessionHost`, `PendingAsk`, `TurnQueueFull`
- `src/marim_harness/server/supervisor.py` — `SessionSupervisor`, `default_harness_factory`
- `src/marim_harness/server/http.py` — Starlette app factory (only transport-aware module)
- `src/marim_harness/interfaces/cli/serve.py` — CLI entry (`main(argv, *, out, err) -> int`)
- Tests: `tests/test_stream_events.py`, `tests/test_server_bus.py`, `tests/test_server_workspaces.py`, `tests/test_server_auth.py`, `tests/test_server_host.py`, `tests/test_server_http.py`

Modify:
- `src/marim_harness/interfaces/cli/headless.py` — delete local `_event_obj`, import shared mapping
- `src/marim_harness/runtime/bootstrap.py` — add `session_id` parameter
- `src/marim_harness/session/store.py` — add public `SessionManager.session_path`
- `src/marim_harness/interfaces/cli/router.py` — add `"serve"` to `_MANAGEMENT`
- `pyproject.toml` — `[serve]` extra + dev-group starlette/uvicorn
- `CLAUDE.md` — one line in Commands for `marim serve`

---

### Task 1: Extract the stream-event mapping into `stream_events.py`

**Files:**
- Create: `src/marim_harness/stream_events.py`
- Modify: `src/marim_harness/interfaces/cli/headless.py` (delete `_event_obj` at lines 85–109, import the shared function)
- Test: `tests/test_stream_events.py`

**Interfaces:**
- Consumes: pydantic-ai event classes from `pydantic_ai.messages`.
- Produces: `event_to_dict(event: object) -> dict | None` in `marim_harness.stream_events`. Dict shapes (unchanged from headless): `{"type": "text"|"thinking", "text": str}`, `{"type": "tool_call", "name": str, "args": dict, "id": str}`, `{"type": "tool_result", "id": str, "content": str}`. Returns `None` for events not surfaced. Tasks 3 and 7 depend on these exact `"type"` strings.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stream_events.py
"""The shared pydantic-ai stream-event -> dict mapping (used by headless
stream-json and the server's event bus)."""

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
)

from marim_harness.stream_events import event_to_dict


def test_text_part_start_and_delta():
    start = PartStartEvent(index=0, part=TextPart(content="hi"))
    delta = PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=" there"))
    assert event_to_dict(start) == {"type": "text", "text": "hi"}
    assert event_to_dict(delta) == {"type": "text", "text": " there"}


def test_thinking_part_start_and_delta():
    start = PartStartEvent(index=0, part=ThinkingPart(content="hmm"))
    delta = PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta="..."))
    assert event_to_dict(start) == {"type": "thinking", "text": "hmm"}
    assert event_to_dict(delta) == {"type": "thinking", "text": "..."}


def test_tool_call_event():
    event = FunctionToolCallEvent(
        part=ToolCallPart(tool_name="read_file", args={"path": "a.txt"}, tool_call_id="tc-1")
    )
    assert event_to_dict(event) == {
        "type": "tool_call",
        "name": "read_file",
        "args": {"path": "a.txt"},
        "id": "tc-1",
    }


def test_tool_result_event():
    event = FunctionToolResultEvent(
        part=ToolReturnPart(tool_name="read_file", content="foo", tool_call_id="tc-1")
    )
    obj = event_to_dict(event)
    assert obj is not None
    assert obj["type"] == "tool_result"
    assert obj["id"] == "tc-1"
    assert obj["content"] == "foo"


def test_unmapped_event_returns_none():
    class Unknown:
        pass

    assert event_to_dict(Unknown()) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_stream_events.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.stream_events'`

- [ ] **Step 3: Create the module (move the code verbatim from headless)**

```python
# src/marim_harness/stream_events.py
"""Map Pydantic AI streaming events to plain JSON-serializable dicts.

One mapping, two consumers: the headless CLI's ``stream-json`` output and the
server's per-session event bus. Keeping it shared means an app consuming
``marim -p --output-format stream-json`` and one consuming ``marim serve``'s
SSE stream see the same event vocabulary."""

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)


def event_to_dict(event) -> dict | None:
    """Map a Pydantic AI streaming event to a JSON-serializable dict, or None to
    skip events we don't surface."""
    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
        return {"type": "text", "text": event.part.content or ""}
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        return {"type": "text", "text": event.delta.content_delta or ""}
    if isinstance(event, PartStartEvent) and isinstance(event.part, ThinkingPart):
        return {"type": "thinking", "text": event.part.content or ""}
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, ThinkingPartDelta):
        return {"type": "thinking", "text": event.delta.content_delta or ""}
    if isinstance(event, FunctionToolCallEvent):
        return {
            "type": "tool_call",
            "name": event.part.tool_name,
            "args": event.part.args_as_dict(),
            "id": event.part.tool_call_id,
        }
    if isinstance(event, FunctionToolResultEvent):
        return {
            "type": "tool_result",
            "id": event.tool_call_id,
            "content": str(getattr(event.part, "content", "")),
        }
    return None
```

- [ ] **Step 4: Rewire headless to the shared function**

In `src/marim_harness/interfaces/cli/headless.py`:
1. Delete the entire `_event_obj` function (lines 85–109) and its now-unused imports from `pydantic_ai.messages` (the whole `from pydantic_ai.messages import (...)` block at lines 11–20).
2. Add `from ...stream_events import event_to_dict` alongside the other `...` imports.
3. In `run_headless`'s `handler`, replace `obj = _event_obj(event)` with `obj = event_to_dict(event)`.

- [ ] **Step 5: Run the new test plus the headless suite**

Run: `uv run pytest --no-cov tests/test_stream_events.py tests/test_headless.py -v`
(If `tests/test_headless.py` doesn't exist, run `uv run pytest --no-cov -k headless` instead.)
Expected: PASS

- [ ] **Step 6: Lint, typecheck, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/stream_events.py src/marim_harness/interfaces/cli/headless.py tests/test_stream_events.py
git commit -m "refactor: extract shared stream-event mapping from headless"
```

---

### Task 2: `build_harness(session_id=...)` and `SessionManager.session_path`

**Files:**
- Modify: `src/marim_harness/runtime/bootstrap.py:31-45` (signature + docstring), `:76-84` (store selection), `:154-155` (resume call)
- Modify: `src/marim_harness/session/store.py` (add `session_path` right after `_path`, line ~253)
- Test: `tests/test_bootstrap.py` (append), `tests/test_session.py` (append)

**Interfaces:**
- Produces: `build_harness(workspace: Path, *, mode: Mode | None = None, resume: bool = False, session_id: str | None = None) -> Harness`. Passing `session_id` opens exactly that session (existing or new-with-that-id) and replays its history if any; passing both `resume=True` and `session_id` raises `ValueError`. Also `SessionManager.session_path(session_id: str) -> Path` (public wrapper over `_path`; the file may not exist). Task 8's `default_harness_factory` and Task 9's history endpoint depend on these.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bootstrap.py` (it already has `_stub_model_plumbing` and `_isolate_sessions` helpers at the top — reuse them):

```python
def test_build_harness_opens_specific_session(tmp_path: Path, monkeypatch):
    _stub_model_plumbing(monkeypatch)
    sessions_base = _isolate_sessions(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()

    # Seed a named session with one prior exchange, then a newer decoy session
    # (so "latest" would pick the wrong one if session_id were ignored).
    seed_manager = SessionManager(ws, base_dir=sessions_base)
    target = seed_manager.create("target")
    target.save(_history(), RunUsage())
    decoy = seed_manager.create("decoy")
    decoy.save(_history(), RunUsage())

    harness = bootstrap.build_harness(ws, mode=Mode.auto, session_id=target.session_id)
    assert harness.session.store is not None
    assert harness.session.store.session_id == target.session_id
    assert len(harness.session.history) > 0  # history replayed


def test_build_harness_rejects_resume_plus_session_id(tmp_path: Path, monkeypatch):
    _stub_model_plumbing(monkeypatch)
    _isolate_sessions(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    import pytest

    with pytest.raises(ValueError):
        bootstrap.build_harness(ws, resume=True, session_id="whatever")
```

Note: `harness.session.store` / `harness.session.history` are the `SessionController` fields the existing suite reads; if `test_bootstrap.py` accesses them differently (check its existing resume test), match that access pattern instead.

Append to `tests/test_session.py`:

```python
def test_session_path_is_public_and_matches_store(tmp_path):
    from marim_harness.session import SessionManager

    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "base")
    store = manager.create("named")
    assert manager.session_path(store.session_id) == store.path
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_bootstrap.py -k "specific_session or resume_plus" tests/test_session.py::test_session_path_is_public_and_matches_store -v`
Expected: FAIL — `TypeError: build_harness() got an unexpected keyword argument 'session_id'` and `AttributeError: ... no attribute 'session_path'`

- [ ] **Step 3: Implement**

In `src/marim_harness/session/store.py`, directly below `_path` (line ~253):

```python
    def session_path(self, session_id: str) -> Path:
        """Public path lookup for a session's JSON file (which may not exist
        yet). Read-only callers — the server's history endpoint — use this to
        read persisted messages without opening a SessionStore (whose
        construction reserves the id)."""
        return self._path(session_id)
```

In `src/marim_harness/runtime/bootstrap.py`, change the signature and store selection:

```python
def build_harness(
    workspace: Path,
    *,
    mode: Mode | None = None,
    resume: bool = False,
    session_id: str | None = None,
) -> Harness:
```

Extend the docstring's last paragraph with:

```
    ``session_id`` opens exactly that session (used by the server, which picks
    sessions explicitly rather than "latest"); it replays any saved history,
    and is mutually exclusive with ``resume``.
```

Replace the store-selection block (currently lines 76–84) with:

```python
    if resume and session_id is not None:
        raise ValueError("pass resume or session_id, not both")

    manager = SessionManager(workspace)
    if session_id is not None:
        store = manager.store(session_id)
    else:
        latest = manager.latest() if resume else None
        store = manager.store(latest.id) if latest is not None else manager.create()

    # When starting fresh (not resuming, no explicit session), pick up the model
    # from the most recent session so the user doesn't have to re-select it after
    # every restart. Resumed/explicit sessions get theirs via harness.resume().
    if not resume and session_id is None and store.model and store.model != model_id:
        model_id = store.model
        model = model_source.build(model_id)
```

And change the resume call at the bottom (currently `if resume: harness.resume()`):

```python
    if resume or session_id is not None:
        harness.resume()
```

(`harness.resume()` on a session whose file doesn't exist yet loads the empty default — `SessionStore.load` returns `([], RunUsage(), [], None, [])` — so an explicit new id is safe.)

- [ ] **Step 4: Run the tests**

Run: `uv run pytest --no-cov tests/test_bootstrap.py tests/test_session.py -v`
Expected: PASS (all, including pre-existing)

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/runtime/bootstrap.py src/marim_harness/session/store.py tests/test_bootstrap.py tests/test_session.py
git commit -m "feat(bootstrap): open a specific session via session_id; public session_path"
```

---

### Task 3: `server/schema.py` — envelope, SSE formatting, request models

**Files:**
- Create: `src/marim_harness/server/__init__.py` (empty), `src/marim_harness/server/schema.py`
- Test: `tests/test_server_schema.py`

**Interfaces:**
- Produces: `Event` (frozen dataclass: `seq: int, ts: str, type: str, data: dict`; method `as_dict() -> dict`), `sse_format(event: Event) -> str`, `STREAM_EVENT_TYPES: dict[str, str]` (`{"text": "text.delta", "thinking": "thinking.delta", "tool_call": "tool.call", "tool_result": "tool.result"}`), and pydantic request models `WorkspaceIn(name, path=None, git_url=None)`, `SessionIn(name=None, mode=None)`, `Attachment(data_b64, media_type)`, `MessageIn(prompt, attachments=None)`, `SteerIn(text)`, `AskAnswerIn(approve=None, reason=None, answers=None, cancel=False)` with `as_answer() -> dict`. Tasks 4, 7, 9 consume these.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_schema.py
from marim_harness.server.schema import STREAM_EVENT_TYPES, AskAnswerIn, Event, sse_format


def test_event_as_dict_and_sse_format():
    event = Event(seq=7, ts="2026-07-06T00:00:00+00:00", type="turn.started", data={"a": 1})
    assert event.as_dict() == {
        "seq": 7,
        "ts": "2026-07-06T00:00:00+00:00",
        "type": "turn.started",
        "data": {"a": 1},
    }
    assert sse_format(event) == 'id: 7\nevent: turn.started\ndata: {"a": 1}\n\n'


def test_stream_event_types_cover_shared_mapping():
    assert STREAM_EVENT_TYPES == {
        "text": "text.delta",
        "thinking": "thinking.delta",
        "tool_call": "tool.call",
        "tool_result": "tool.result",
    }


def test_ask_answer_shapes():
    assert AskAnswerIn(approve=True).as_answer() == {"approve": True, "reason": None}
    assert AskAnswerIn(approve=False, reason="nope").as_answer() == {
        "approve": False,
        "reason": "nope",
    }
    assert AskAnswerIn(answers={"Color": "red"}).as_answer() == {"answers": {"Color": "red"}}
    assert AskAnswerIn(cancel=True).as_answer() == {"cancel": True}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_server_schema.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.server'`

- [ ] **Step 3: Implement**

Create empty `src/marim_harness/server/__init__.py` containing only:

```python
"""marim serve: the HTTP server daemon's transport-neutral core.

Like ``runtime/``, the package root deliberately re-exports nothing — import
submodules directly. Only ``http.py`` may import starlette (an optional
extra); everything else stays importable on a bare install."""
```

```python
# src/marim_harness/server/schema.py
"""Transport-neutral wire schema: the event envelope every transport carries
and the request-body models the HTTP layer validates with.

The envelope is the contract — SSE + POST is merely the first transport. A
future WebSocket endpoint pipes the same ``Event`` dicts both ways with no
changes here (which is why nothing in this module knows about HTTP)."""

import json
from dataclasses import dataclass

from pydantic import BaseModel


@dataclass(frozen=True)
class Event:
    """One bus message. ``seq`` is monotonic per session and doubles as the
    SSE event id for Last-Event-ID resume."""

    seq: int
    ts: str
    type: str
    data: dict

    def as_dict(self) -> dict:
        return {"seq": self.seq, "ts": self.ts, "type": self.type, "data": self.data}


def sse_format(event: Event) -> str:
    """Render an Event as a Server-Sent Events frame."""
    return f"id: {event.seq}\nevent: {event.type}\ndata: {json.dumps(event.data)}\n\n"


# Maps stream_events.event_to_dict()'s "type" field to the wire event type
# published on the bus. Events whose type isn't listed here are not surfaced.
STREAM_EVENT_TYPES = {
    "text": "text.delta",
    "thinking": "thinking.delta",
    "tool_call": "tool.call",
    "tool_result": "tool.result",
}


class WorkspaceIn(BaseModel):
    """POST /v1/workspaces. ``path`` registers an existing directory;
    otherwise a managed workspace is created (cloned when ``git_url`` set)."""

    name: str
    path: str | None = None
    git_url: str | None = None


class SessionIn(BaseModel):
    name: str | None = None
    mode: str | None = None  # "auto" | "ask" | "plan"; None -> configured default


class Attachment(BaseModel):
    data_b64: str
    media_type: str


class MessageIn(BaseModel):
    prompt: str
    attachments: list[Attachment] | None = None


class SteerIn(BaseModel):
    text: str


class AskAnswerIn(BaseModel):
    """POST answer for a parked ask. Approvals use approve/reason; ask_user
    questions use answers (or cancel)."""

    approve: bool | None = None
    reason: str | None = None
    answers: dict | None = None
    cancel: bool = False

    def as_answer(self) -> dict:
        if self.answers is not None:
            return {"answers": self.answers}
        if self.cancel:
            return {"cancel": True}
        return {"approve": bool(self.approve), "reason": self.reason}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_server_schema.py -v`
Expected: PASS

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/server tests/test_server_schema.py
git commit -m "feat(server): transport-neutral wire schema"
```

---

### Task 4: `server/bus.py` — per-session event bus

**Files:**
- Create: `src/marim_harness/server/bus.py`
- Test: `tests/test_server_bus.py`

**Interfaces:**
- Consumes: `Event` from Task 3.
- Produces: `EventBus(ring_size: int = 1000)` with `publish(type: str, data: dict) -> Event` (stamps seq/ts, appends to ring, fans out), `attach(after_seq: int | None = None) -> Subscription`, `last_seq: int`, `subscriber_count: int`. `Subscription` with `async next_event(timeout: float | None = None) -> Event | None` (backlog first, then live queue; `None` on timeout — the heartbeat tick) and `close()`. When `after_seq` predates the ring, the backlog is prefixed with a synthetic `stream.gap` event. Tasks 7–9 consume this. Deliberately NOT an async generator: the HTTP layer's heartbeat needs `asyncio.wait_for` around each read, and cancelling a suspended async-generator `__anext__` kills the generator — a plain queue read survives it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_bus.py
import anyio
import pytest

from marim_harness.server.bus import EventBus

pytestmark = pytest.mark.anyio


async def test_publish_stamps_monotonic_seq_and_ts():
    bus = EventBus()
    a = bus.publish("turn.started", {"turn_id": "t1"})
    b = bus.publish("text.delta", {"text": "hi"})
    assert (a.seq, b.seq) == (1, 2)
    assert a.ts  # ISO timestamp present
    assert bus.last_seq == 2


async def test_subscriber_receives_backlog_then_live():
    bus = EventBus()
    bus.publish("a", {})
    bus.publish("b", {})
    sub = bus.attach(after_seq=0)
    assert (await sub.next_event()).type == "a"
    assert (await sub.next_event()).type == "b"
    bus.publish("c", {})
    assert (await sub.next_event()).type == "c"
    sub.close()
    assert bus.subscriber_count == 0


async def test_attach_after_seq_skips_already_seen():
    bus = EventBus()
    bus.publish("a", {})
    bus.publish("b", {})
    sub = bus.attach(after_seq=1)
    assert (await sub.next_event()).type == "b"
    sub.close()


async def test_gap_event_when_resume_point_fell_off_ring():
    bus = EventBus(ring_size=2)
    for name in ("a", "b", "c", "d"):  # ring now holds only c(3), d(4)
        bus.publish(name, {})
    sub = bus.attach(after_seq=1)  # asks for 2..: 2 is gone
    first = await sub.next_event()
    assert first.type == "stream.gap"
    assert first.data == {"resync": "history"}
    assert (await sub.next_event()).type == "c"
    assert (await sub.next_event()).type == "d"
    sub.close()


async def test_next_event_timeout_returns_none():
    bus = EventBus()
    sub = bus.attach()
    with anyio.fail_after(2):
        assert await sub.next_event(timeout=0.01) is None
    sub.close()
```

(The repo's `conftest.py` already provides the `anyio_backend` fixture pinned to asyncio.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_server_bus.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.server.bus'`

- [ ] **Step 3: Implement**

```python
# src/marim_harness/server/bus.py
"""Per-session event bus: monotonic sequence numbers, a bounded replay ring,
and queue-based subscriptions.

The bus outlives any single SessionHost (the supervisor keys buses separately
from hosts) so an SSE client can attach, disconnect, and resume with
Last-Event-ID across host evictions within the daemon's lifetime.

Subscription is a queue handle, not an async generator: the SSE writer wraps
each read in ``asyncio.wait_for`` to emit keepalive comments, and cancelling a
suspended async-generator ``__anext__`` would kill the generator — a plain
``Queue.get`` just retries."""

import asyncio
from collections import deque
from datetime import datetime, timezone

from .schema import Event


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Subscription:
    """One attached consumer: a snapshot backlog (replayed events, possibly
    prefixed by a synthetic ``stream.gap``) then a live queue."""

    def __init__(self, bus: "EventBus", queue: "asyncio.Queue[Event]",
                 backlog: list[Event]) -> None:
        self._bus = bus
        self._queue = queue
        self._backlog = backlog

    async def next_event(self, timeout: float | None = None) -> Event | None:
        """The next event, or None when ``timeout`` elapses (heartbeat tick)."""
        if self._backlog:
            return self._backlog.pop(0)
        if timeout is None:
            return await self._queue.get()
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        self._bus._detach(self._queue)


class EventBus:
    def __init__(self, ring_size: int = 1000) -> None:
        self._ring: deque[Event] = deque(maxlen=ring_size)
        self._seq = 0
        self._queues: set[asyncio.Queue[Event]] = set()

    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)

    def publish(self, type: str, data: dict) -> Event:
        self._seq += 1
        event = Event(seq=self._seq, ts=_now(), type=type, data=data)
        self._ring.append(event)
        for queue in self._queues:
            queue.put_nowait(event)
        return event

    def attach(self, after_seq: int | None = None) -> Subscription:
        """Attach a consumer. With ``after_seq``, replay ring events newer than
        it; when the resume point has fallen off the ring, prefix a synthetic
        ``stream.gap`` telling the client to re-sync via the history endpoint.

        No await between registering the queue and snapshotting the backlog, so
        an event is never both replayed and queued (single event loop)."""
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._queues.add(queue)
        backlog: list[Event] = []
        if after_seq is not None:
            replayed = [e for e in self._ring if e.seq > after_seq]
            oldest_held = self._ring[0].seq if self._ring else self._seq + 1
            if after_seq + 1 < oldest_held:
                gap_seq = replayed[0].seq - 1 if replayed else self._seq
                backlog.append(
                    Event(seq=gap_seq, ts=_now(), type="stream.gap", data={"resync": "history"})
                )
            backlog.extend(replayed)
        return Subscription(self, queue, backlog)

    def _detach(self, queue: "asyncio.Queue[Event]") -> None:
        self._queues.discard(queue)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_server_bus.py -v`
Expected: PASS

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/server/bus.py tests/test_server_bus.py
git commit -m "feat(server): per-session event bus with ring-buffer resume"
```

---

### Task 5: `server/workspaces.py` — workspace registry

**Files:**
- Create: `src/marim_harness/server/workspaces.py`
- Test: `tests/test_server_workspaces.py`

**Interfaces:**
- Consumes: `atomic_write_text` from `marim_harness.atomic_io`.
- Produces: `WorkspaceRecord` (frozen dataclass: `id, name, path, kind, created`, all `str`; `kind` is `"registered" | "managed"`; `as_dict() -> dict`) and `WorkspaceRegistry(state_file: Path, workspaces_root: Path)` with `list() -> list[WorkspaceRecord]`, `get(ws_id) -> WorkspaceRecord | None`, `register(name: str, path: Path) -> WorkspaceRecord` (raises `ValueError` if not a directory), `create_managed(name: str, git_url: str | None = None) -> WorkspaceRecord` (raises `ValueError` on clone failure), `delete(ws_id: str, *, purge: bool = False) -> None` (raises `KeyError` if unknown, `ValueError` if `purge` on a registered workspace). Tasks 8–9 consume this.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_workspaces.py
import json
import subprocess

import pytest

from marim_harness.server.workspaces import WorkspaceRegistry


def _registry(tmp_path):
    return WorkspaceRegistry(tmp_path / "state" / "workspaces.json", tmp_path / "managed")


def test_register_existing_directory(tmp_path):
    reg = _registry(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    record = reg.register("My Proj", project)
    assert record.kind == "registered"
    assert record.path == str(project.resolve())
    assert record.id == "my-proj"
    assert reg.get("my-proj") == record
    assert reg.list() == [record]


def test_register_missing_directory_raises(tmp_path):
    with pytest.raises(ValueError):
        _registry(tmp_path).register("nope", tmp_path / "does-not-exist")


def test_registry_persists_across_instances(tmp_path):
    reg = _registry(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    reg.register("proj", project)
    again = _registry(tmp_path)
    assert [r.id for r in again.list()] == ["proj"]


def test_create_managed_empty(tmp_path):
    reg = _registry(tmp_path)
    record = reg.create_managed("fresh")
    assert record.kind == "managed"
    assert (tmp_path / "managed" / "fresh").is_dir()


def test_create_managed_git_clone(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=origin, check=True)
    (origin / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=origin, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=origin, check=True,
    )
    reg = _registry(tmp_path)
    record = reg.create_managed("cloned", git_url=str(origin))
    assert (tmp_path / "managed" / "cloned" / "README.md").read_text() == "hello"
    assert record.kind == "managed"


def test_create_managed_bad_clone_cleans_up(tmp_path):
    reg = _registry(tmp_path)
    with pytest.raises(ValueError):
        reg.create_managed("bad", git_url=str(tmp_path / "no-such-repo"))
    assert not (tmp_path / "managed" / "bad").exists()
    assert reg.get("bad") is None


def test_delete_and_purge(tmp_path):
    reg = _registry(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    reg.register("proj", project)
    managed = reg.create_managed("m")

    with pytest.raises(ValueError):
        reg.delete("proj", purge=True)  # purge is managed-only
    reg.delete("proj")
    assert project.exists()  # registered dirs are never removed

    reg.delete(managed.id, purge=True)
    assert not (tmp_path / "managed" / "m").exists()
    with pytest.raises(KeyError):
        reg.delete("m")


def test_slug_collision_gets_suffix(tmp_path):
    reg = _registry(tmp_path)
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    first = reg.register("Same Name", a)
    second = reg.register("Same Name", b)
    assert first.id == "same-name"
    assert second.id == "same-name-2"


def test_state_file_is_json(tmp_path):
    reg = _registry(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    reg.register("proj", project)
    data = json.loads((tmp_path / "state" / "workspaces.json").read_text())
    assert data["workspaces"][0]["id"] == "proj"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_server_workspaces.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# src/marim_harness/server/workspaces.py
"""Server-side workspace registry: named directories sessions run in.

Two flavors. *Registered* workspaces point at existing directories on the host
(like opening a project) and are never deleted from disk. *Managed* workspaces
are created by the server under a workspaces root — empty or git-cloned — and
may be purged on delete. Persisted as one JSON file under the server state
dir."""

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..atomic_io import atomic_write_text

_CLONE_TIMEOUT_SECONDS = 600


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "workspace"


@dataclass(frozen=True)
class WorkspaceRecord:
    id: str
    name: str
    path: str
    kind: str  # "registered" | "managed"
    created: str

    def as_dict(self) -> dict:
        return asdict(self)


class WorkspaceRegistry:
    def __init__(self, state_file: Path, workspaces_root: Path) -> None:
        self._file = state_file
        self.workspaces_root = workspaces_root
        self._records: dict[str, WorkspaceRecord] = self._load()

    def _load(self) -> dict[str, WorkspaceRecord]:
        if not self._file.exists():
            return {}
        try:
            data = json.loads(self._file.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        records = {}
        for raw in data.get("workspaces", []):
            record = WorkspaceRecord(**raw)
            records[record.id] = record
        return records

    def _save(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"workspaces": [r.as_dict() for r in self._records.values()]}
        atomic_write_text(self._file, json.dumps(payload, indent=2))

    def list(self) -> list[WorkspaceRecord]:
        return list(self._records.values())

    def get(self, ws_id: str) -> WorkspaceRecord | None:
        return self._records.get(ws_id)

    def _unique_id(self, base: str) -> str:
        candidate = base
        suffix = 2
        while candidate in self._records or (self.workspaces_root / candidate).exists():
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def register(self, name: str, path: Path) -> WorkspaceRecord:
        resolved = path.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"not a directory: {resolved}")
        record = WorkspaceRecord(
            id=self._unique_id(_slugify(name)), name=name, path=str(resolved),
            kind="registered", created=_now(),
        )
        self._records[record.id] = record
        self._save()
        return record

    def create_managed(self, name: str, git_url: str | None = None) -> WorkspaceRecord:
        ws_id = self._unique_id(_slugify(name))
        target = self.workspaces_root / ws_id
        target.mkdir(parents=True, exist_ok=False)
        if git_url is not None:
            try:
                subprocess.run(
                    ["git", "clone", git_url, str(target)],
                    check=True, capture_output=True, text=True,
                    timeout=_CLONE_TIMEOUT_SECONDS,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                shutil.rmtree(target, ignore_errors=True)
                detail = getattr(exc, "stderr", "") or str(exc)
                raise ValueError(f"git clone failed: {detail.strip()}") from exc
        record = WorkspaceRecord(
            id=ws_id, name=name, path=str(target.resolve()), kind="managed", created=_now(),
        )
        self._records[record.id] = record
        self._save()
        return record

    def delete(self, ws_id: str, *, purge: bool = False) -> None:
        record = self._records.get(ws_id)
        if record is None:
            raise KeyError(ws_id)
        if purge and record.kind != "managed":
            raise ValueError("purge applies only to managed workspaces")
        del self._records[ws_id]
        self._save()
        if purge:
            shutil.rmtree(record.path, ignore_errors=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_server_workspaces.py -v`
Expected: PASS

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/server/workspaces.py tests/test_server_workspaces.py
git commit -m "feat(server): workspace registry (registered + managed dirs)"
```

---

### Task 6: `server/auth.py` — bearer token

**Files:**
- Create: `src/marim_harness/server/auth.py`
- Test: `tests/test_server_auth.py`

**Interfaces:**
- Produces: `load_or_create_token(state_dir: Path) -> str` (creates `state_dir/token` mode 0600 on first call, returns existing thereafter) and `token_matches(expected: str, presented: str | None) -> bool` (constant-time; `None`/empty → False). Task 9 consumes both; Task 10 calls `load_or_create_token`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_auth.py
import stat

from marim_harness.server.auth import load_or_create_token, token_matches


def test_creates_token_with_0600_and_persists(tmp_path):
    state = tmp_path / "server"
    token = load_or_create_token(state)
    assert len(token) >= 32
    path = state / "token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_or_create_token(state) == token  # stable across calls


def test_token_matches():
    assert token_matches("secret", "secret")
    assert not token_matches("secret", "wrong")
    assert not token_matches("secret", None)
    assert not token_matches("secret", "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_server_auth.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# src/marim_harness/server/auth.py
"""Single-owner bearer-token auth for the server daemon.

The token is generated once, stored 0600 under the server state dir, and
printed by ``marim serve`` at startup. Every request except /health must carry
it (Authorization: Bearer, or ?access_token= on the SSE endpoint, where
browser EventSource cannot set headers)."""

import secrets
from hmac import compare_digest
from pathlib import Path


def load_or_create_token(state_dir: Path) -> str:
    path = state_dir / "token"
    if path.exists():
        existing = path.read_text().strip()
        if existing:
            return existing
    state_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)  # touch honors umask; force the mode we promised
    path.write_text(token + "\n")
    return token


def token_matches(expected: str, presented: str | None) -> bool:
    if not presented:
        return False
    return compare_digest(expected.encode(), presented.encode())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_server_auth.py -v`
Expected: PASS

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/server/auth.py tests/test_server_auth.py
git commit -m "feat(server): bearer-token auth helpers"
```

---

### Task 7: `server/host.py` — SessionHost (turn queue, parked asks, interrupt/steer)

**Files:**
- Create: `src/marim_harness/server/host.py`
- Test: `tests/test_server_host.py`

**Interfaces:**
- Consumes: `Harness` (its `bind_ui`, `run_turn`, `steer`, `session`, `model_id`, `aclose`), `EventBus.publish`, `STREAM_EVENT_TYPES`, `event_to_dict`, `usage_summary(usage, model_id)` from `marim_harness.usage`, `format_provider_error` from `runtime/errors`, `ToolDenied` from pydantic_ai.
- Produces: `TurnQueueFull(Exception)`; `PendingAsk` dataclass (`id, kind, payload, created, future`; `as_dict()` excludes the future); `SessionHost(harness, bus, *, queue_limit: int = 8)` — must be constructed inside a running event loop — with `submit(prompt: str, attachments: list | None = None) -> str` (turn id; raises `TurnQueueFull`), `interrupt() -> bool`, `steer(text: str) -> None`, `pending_asks() -> list[dict]`, `answer_ask(ask_id: str, answer: dict) -> bool`, `status: str` (`"idle" | "running" | "waiting_ask"`), `busy: bool`, `queued: int`, `idle_seconds: float`, `async aclose() -> None`. Answer dicts (from `AskAnswerIn.as_answer()`): approval → `{"approve": bool, "reason": str | None}`; question → `{"answers": dict}` or `{"cancel": True}`. Tasks 8–9 consume this.

Bus event vocabulary published by the host (Task 9's SSE carries these verbatim): `turn.started {turn_id, prompt}`, `text.delta/thinking.delta {text}`, `tool.call {name, args, id}`, `tool.result {id, content}`, `turn.finished {turn_id, output, usage}` (or `{turn_id, interrupted: true}`), `turn.error {turn_id, error}`, `ask.pending {id, kind, payload, created}`, `ask.resolved {id, ...}`, `session.status {status}`, `steer.accepted {text}`, `subagent.event {stream_id, event}`, `tasks.changed {}`, `jobs.changed {}`, `session.renamed {from, to}`, `compaction.started {}`, `compaction.finished {before, after}` (token counts).

- [ ] **Step 1: Write the failing tests**

The tests build a real `Harness` over `FunctionModel`. `tests/conftest.py` has a `_make_deps`/`_make_harness` pair (lines 40 and 185) that do exactly this, but **`from conftest import ...` does NOT work in this repo** — `tests/__init__.py` exists, so pytest's default import mode puts the project root (not `tests/`) on `sys.path`, and `conftest` is never importable as a bare top-level module (verified: raises `ModuleNotFoundError: No module named 'conftest'`). So this test file defines its own copies of the two helpers (byte-identical logic to conftest's) rather than importing them.

```python
# tests/test_server_host.py
"""SessionHost: the server-side implementation of the bind_ui contract —
turn queue, parked asks, interrupt, steer — observed through the event bus."""

import asyncio
import json as _json
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from marim_harness.runtime.deps import Deps, UIHooks, WorkspaceConfig
from marim_harness.runtime.harness import Harness
from marim_harness.runtime.permissions import Mode
from marim_harness.server.bus import EventBus
from marim_harness.server.host import SessionHost, TurnQueueFull
from marim_harness.tools.provider import BuiltinToolProvider

pytestmark = pytest.mark.anyio

_UI_HOOK_FIELDS = {
    "request_approval", "ask_user", "on_subagent_event", "on_subagent_notice",
    "on_subagent_model", "on_subagent_usage", "detach_fanout", "interactive",
    "notifier",
}


def _make_deps(root: Path, mode: Mode = Mode.auto, **kw) -> Deps:
    """Local copy of tests/conftest.py's helper (bare `conftest` import doesn't
    work here — see the note above)."""
    ui_kw = {k: kw.pop(k) for k in list(kw) if k in _UI_HOOK_FIELDS}
    return Deps(
        workspace=WorkspaceConfig(root=root, mode=mode),
        ui=UIHooks(**ui_kw) if ui_kw else UIHooks(),
        **kw,
    )


def _make_harness(model, deps, **config_kwargs) -> Harness:
    """Local copy of tests/conftest.py's helper."""
    return Harness(model=model, provider=BuiltinToolProvider(), deps=deps,
                   instructions="You are a coding agent.", **config_kwargs)


def _text_only_model() -> FunctionModel:
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="done")])

    async def stream_fn(messages, info):
        yield "done"

    return FunctionModel(fn, stream_function=stream_fn)


def _edit_model() -> FunctionModel:
    """read a.txt then edit it then say done — the edit defers for approval."""
    state = {"n": 0}

    def fn(messages, info):
        state["n"] += 1
        if state["n"] == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="read_file", args={"path": "a.txt"})]
            )
        if state["n"] == 2:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="edit_file",
                args={"path": "a.txt",
                      "edits": [{"old_string": "foo", "new_string": "bar"}]},
            )])
        return ModelResponse(parts=[TextPart(content="done")])

    stream_state = {"n": 0}

    async def stream_fn(messages, info):
        stream_state["n"] += 1
        if stream_state["n"] == 1:
            yield {0: DeltaToolCall(name="read_file",
                                    json_args=_json.dumps({"path": "a.txt"}),
                                    tool_call_id="tc-read-1")}
        elif stream_state["n"] == 2:
            yield {0: DeltaToolCall(
                name="edit_file",
                json_args=_json.dumps({"path": "a.txt",
                                       "edits": [{"old_string": "foo",
                                                  "new_string": "bar"}]}),
                tool_call_id="tc-edit-1")}
        else:
            yield "done"

    return FunctionModel(fn, stream_function=stream_fn)


async def _wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


async def _drain_until(bus_events: list, type_: str, timeout=5.0):
    await _wait_for(lambda: any(e.type == type_ for e in bus_events), timeout)
    return next(e for e in bus_events if e.type == type_)


def _spy(bus: EventBus) -> list:
    events: list = []
    original = bus.publish

    def publish(type, data):
        event = original(type, data)
        events.append(event)
        return event

    bus.publish = publish  # type: ignore[method-assign]
    return events


async def test_simple_turn_publishes_lifecycle_events(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.auto)
    host = SessionHost(_make_harness(_text_only_model(), deps), EventBus())
    events = _spy(host.bus)
    turn_id = host.submit("hi")
    finished = await _drain_until(events, "turn.finished")
    assert finished.data["turn_id"] == turn_id
    assert finished.data["output"] == "done"
    assert "usage" in finished.data
    assert any(e.type == "turn.started" for e in events)
    assert any(e.type == "text.delta" for e in events)
    await _wait_for(lambda: host.status == "idle")
    await host.aclose()


async def test_approval_parks_then_answer_approves(tmp_path):
    (tmp_path / "a.txt").write_text("foo\n")
    deps = _make_deps(tmp_path, mode=Mode.ask)
    host = SessionHost(_make_harness(_edit_model(), deps), EventBus())
    events = _spy(host.bus)
    host.submit("edit it")
    await _wait_for(lambda: host.status == "waiting_ask")
    [ask] = host.pending_asks()
    assert ask["kind"] == "approval"
    assert ask["payload"]["tool_name"] == "edit_file"
    assert host.answer_ask(ask["id"], {"approve": True, "reason": None})
    finished = await _drain_until(events, "turn.finished")
    assert finished.data["output"] == "done"
    assert (tmp_path / "a.txt").read_text() == "bar\n"
    assert any(e.type == "ask.pending" for e in events)
    assert any(e.type == "ask.resolved" for e in events)
    await host.aclose()


async def test_approval_denied(tmp_path):
    (tmp_path / "a.txt").write_text("foo\n")
    deps = _make_deps(tmp_path, mode=Mode.ask)
    host = SessionHost(_make_harness(_edit_model(), deps), EventBus())
    events = _spy(host.bus)
    host.submit("edit it")
    await _wait_for(lambda: host.status == "waiting_ask")
    [ask] = host.pending_asks()
    assert host.answer_ask(ask["id"], {"approve": False, "reason": "not today"})
    await _drain_until(events, "turn.finished")
    assert (tmp_path / "a.txt").read_text() == "foo\n"  # edit refused
    await host.aclose()


async def test_answer_unknown_ask_returns_false(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.auto)
    host = SessionHost(_make_harness(_text_only_model(), deps), EventBus())
    assert not host.answer_ask("nope", {"approve": True})
    await host.aclose()


async def test_interrupt_cancels_parked_turn(tmp_path):
    (tmp_path / "a.txt").write_text("foo\n")
    deps = _make_deps(tmp_path, mode=Mode.ask)
    host = SessionHost(_make_harness(_edit_model(), deps), EventBus())
    events = _spy(host.bus)
    host.submit("edit it")
    await _wait_for(lambda: host.status == "waiting_ask")
    assert host.interrupt()
    finished = await _drain_until(events, "turn.finished")
    assert finished.data.get("interrupted") is True
    await _wait_for(lambda: host.status == "idle")
    assert host.pending_asks() == []
    assert not host.interrupt()  # nothing running now
    await host.aclose()


async def test_queue_limit(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.ask)
    (tmp_path / "a.txt").write_text("foo\n")
    host = SessionHost(_make_harness(_edit_model(), deps), EventBus(), queue_limit=1)
    host.submit("first")  # will park on approval, occupying the worker
    await _wait_for(lambda: host.status == "waiting_ask")
    host.submit("second")  # sits in the queue
    with pytest.raises(TurnQueueFull):
        host.submit("third")
    assert host.queued == 1
    await host.aclose()


async def test_steer_buffers_and_publishes(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.auto)
    harness = _make_harness(_text_only_model(), deps)
    host = SessionHost(harness, EventBus())
    events = _spy(host.bus)
    host.steer("also check b.txt")
    assert harness.take_buffered_steers() == [("also check b.txt", None)]
    assert any(e.type == "steer.accepted" for e in events)
    await host.aclose()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_server_host.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.server.host'`

- [ ] **Step 3: Implement**

```python
# src/marim_harness/server/host.py
"""One live session: a Harness, its turn queue, and its parked asks.

SessionHost is the server-side implementation of the ``bind_ui`` contract the
TUI fills interactively. Stream events, sub-agent events, and lifecycle
notices publish onto the session's EventBus; ``request_approval`` and
``ask_user`` park as PendingAsk futures any authenticated client can answer
(no timeout — spec: park and wait).

One turn at a time: submissions enter a bounded queue drained by a single
worker task, mirroring the TUI's exclusive-worker discipline (a Harness is not
safe under concurrent run_turn calls). Interrupt cancels the running turn's
task; the TurnController's existing resumable-flush machinery handles rollback,
and the dirty mid-approval history is never persisted — so a daemon crash with
a parked ask simply rolls the session back to its last clean baseline."""

import asyncio
import contextlib
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic_ai import ToolDenied

from ..ask_user import Question
from ..runtime.errors import format_provider_error
from ..runtime.harness import Harness
from ..stream_events import event_to_dict
from ..usage import usage_summary
from .bus import EventBus
from .schema import STREAM_EVENT_TYPES

logger = logging.getLogger(__name__)


class TurnQueueFull(Exception):
    """submit() refused: the per-session turn queue is at capacity."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PendingAsk:
    id: str
    kind: str  # "approval" | "question"
    payload: dict
    created: str
    future: "asyncio.Future[dict]" = field(repr=False)

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "payload": self.payload,
                "created": self.created}


class SessionHost:
    """Must be constructed inside a running event loop (it starts its worker
    task immediately)."""

    def __init__(self, harness: Harness, bus: EventBus, *, queue_limit: int = 8) -> None:
        self.harness = harness
        self.bus = bus
        self._queue: asyncio.Queue[tuple[str, str, list | None]] = asyncio.Queue(
            maxsize=queue_limit
        )
        self._pending: dict[str, PendingAsk] = {}
        self._turn_task: asyncio.Task | None = None
        self._closing = False
        loop = asyncio.get_running_loop()
        self._idle_since = loop.time()
        harness.bind_ui(
            request_approval=self._request_approval,
            ask_user=self._ask_user,
            on_subagent_event=self._on_subagent_event,
            on_tasks_changed=lambda: self.bus.publish("tasks.changed", {}),
            on_jobs_changed=lambda: self.bus.publish("jobs.changed", {}),
            on_rename=lambda old, new: self.bus.publish(
                "session.renamed", {"from": old, "to": new}
            ),
            on_compact_start=lambda: self.bus.publish("compaction.started", {}),
            on_compact=lambda before, after: self.bus.publish(
                "compaction.finished", {"before": before, "after": after}
            ),
        )
        self._worker = loop.create_task(self._worker_loop())

    # ------------------------------------------------------------- state --
    @property
    def status(self) -> str:
        if self._pending:
            return "waiting_ask"
        if self._turn_task is not None or not self._queue.empty():
            return "running"
        return "idle"

    @property
    def busy(self) -> bool:
        return self.status != "idle"

    @property
    def queued(self) -> int:
        return self._queue.qsize()

    @property
    def idle_seconds(self) -> float:
        if self.busy:
            return 0.0
        return asyncio.get_running_loop().time() - self._idle_since

    # ----------------------------------------------------------- control --
    def submit(self, prompt: str, attachments: list | None = None) -> str:
        turn_id = secrets.token_hex(8)
        try:
            self._queue.put_nowait((turn_id, prompt, attachments))
        except asyncio.QueueFull:
            raise TurnQueueFull() from None
        return turn_id

    def interrupt(self) -> bool:
        """Cancel the running turn. Returns False when nothing is running."""
        if self._turn_task is None:
            return False
        self._turn_task.cancel()
        return True

    def steer(self, text: str) -> None:
        self.harness.steer(text)
        self.bus.publish("steer.accepted", {"text": text})

    def pending_asks(self) -> list[dict]:
        return [ask.as_dict() for ask in self._pending.values()]

    def answer_ask(self, ask_id: str, answer: dict) -> bool:
        ask = self._pending.pop(ask_id, None)
        if ask is None or ask.future.done():
            return False
        ask.future.set_result(answer)
        self.bus.publish("ask.resolved", {"id": ask_id, "answer": answer})
        return True

    # ---------------------------------------------------- bind_ui bridge --
    def _park(self, kind: str, payload: dict) -> PendingAsk:
        ask = PendingAsk(
            id=secrets.token_hex(8), kind=kind, payload=payload, created=_now(),
            future=asyncio.get_running_loop().create_future(),
        )
        self._pending[ask.id] = ask
        self.bus.publish("ask.pending", ask.as_dict())
        self._publish_status()
        return ask

    async def _request_approval(self, call: object):
        payload = {
            "tool_name": getattr(call, "tool_name", None),
            "args": getattr(call, "args", None),
            "tool_call_id": getattr(call, "tool_call_id", None),
        }
        ask = self._park("approval", payload)
        try:
            answer = await ask.future
        finally:
            self._pending.pop(ask.id, None)
            self._publish_status()
        if answer.get("approve"):
            return True
        return ToolDenied(str(answer.get("reason") or "denied by client"))

    async def _ask_user(self, questions: list[Question]) -> dict | None:
        payload = {
            "questions": [
                {
                    "question": q.question,
                    "header": q.header,
                    "multi": q.multi,
                    "options": [
                        {"label": c.label, "description": c.description} for c in q.options
                    ],
                }
                for q in questions
            ]
        }
        ask = self._park("question", payload)
        try:
            answer = await ask.future
        finally:
            self._pending.pop(ask.id, None)
            self._publish_status()
        if answer.get("cancel"):
            return None
        answers = answer.get("answers")
        return answers if isinstance(answers, dict) else None

    async def _on_subagent_event(self, stream_id: str, event: object, usage: object) -> None:
        obj = event_to_dict(event)
        if obj is not None:
            self.bus.publish("subagent.event", {"stream_id": stream_id, "event": obj})

    def _publish_status(self) -> None:
        self.bus.publish("session.status", {"status": self.status})

    # ------------------------------------------------------------- turns --
    async def _worker_loop(self) -> None:
        while True:
            turn_id, prompt, attachments = await self._queue.get()
            self._turn_task = asyncio.get_running_loop().create_task(
                self._run_one_turn(turn_id, prompt, attachments)
            )
            try:
                await self._turn_task
            except asyncio.CancelledError:
                if self._closing:
                    raise
                self.bus.publish("turn.finished", {"turn_id": turn_id, "interrupted": True})
            finally:
                self._turn_task = None
                self._cancel_pending("interrupted")
                self._idle_since = asyncio.get_running_loop().time()
                self._publish_status()

    def _cancel_pending(self, reason: str) -> None:
        """Clear asks left behind by an interrupted turn (a clean turn leaves
        none — each ask is popped where it is awaited)."""
        for ask in list(self._pending.values()):
            if not ask.future.done():
                ask.future.cancel()
            self.bus.publish("ask.resolved", {"id": ask.id, "cancelled": True, "reason": reason})
        self._pending.clear()

    async def _run_one_turn(self, turn_id: str, prompt: str, attachments) -> None:
        self.bus.publish("turn.started", {"turn_id": turn_id, "prompt": prompt})
        self._publish_status()

        async def handler(ctx, events):
            async for event in events:
                obj = event_to_dict(event)
                if obj is None:
                    continue
                wire_type = STREAM_EVENT_TYPES.get(obj.pop("type"))
                if wire_type is not None:
                    self.bus.publish(wire_type, obj)

        try:
            output = await self.harness.run_turn(
                prompt, event_stream_handler=handler, attachments=attachments
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # surface, don't crash the worker
            detail = format_provider_error(exc) or f"{type(exc).__name__}: {exc}"
            logger.warning("turn %s failed: %s", turn_id, detail)
            self.bus.publish("turn.error", {"turn_id": turn_id, "error": detail})
            return
        self.bus.publish(
            "turn.finished",
            {
                "turn_id": turn_id,
                "output": output,
                "usage": usage_summary(self.harness.session.usage, self.harness.model_id),
            },
        )

    # ---------------------------------------------------------- teardown --
    async def aclose(self) -> None:
        """Interrupt anything running, then run the same guarded teardown the
        headless CLI does (autoname, final persist, session_end, aclose)."""
        self._closing = True
        if self._turn_task is not None:
            self._turn_task.cancel()
        self._worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._worker
        for label, step in (
            ("wait_autoname", self.harness.session.wait_autoname),
            ("finalize_active_time", self.harness.session.finalize_active_time),
            ("persist", lambda: self.harness.session.persist(force=True)),
        ):
            try:
                result = step()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001 - teardown is best-effort
                logger.warning("host teardown step %s failed", label, exc_info=True)
        for label, coro_fn in (
            ("session_end", lambda: self.harness.session_end("exit")),
            ("aclose", self.harness.aclose),
        ):
            try:
                await coro_fn()
            except Exception:  # noqa: BLE001 - teardown is best-effort
                logger.warning("host teardown step %s failed", label, exc_info=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_server_host.py -v`
Expected: PASS

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/server/host.py tests/test_server_host.py
git commit -m "feat(server): SessionHost — turn queue, parked asks, interrupt/steer"
```

---

### Task 8: `server/supervisor.py` — host registry + idle eviction

**Files:**
- Create: `src/marim_harness/server/supervisor.py`
- Test: `tests/test_server_supervisor.py`

**Interfaces:**
- Consumes: `SessionHost`, `EventBus`, `WorkspaceRecord`, `build_harness(workspace, mode=..., session_id=...)` from Task 2, `Mode`.
- Produces: `HarnessFactory = Callable[[Path, str, Mode | None], Awaitable[Harness]]`; `default_harness_factory` (build_harness + connect + session_start); `SessionSupervisor(factory=default_harness_factory, *, idle_ttl: float = 900.0, ring_size: int = 1000)` with `bus_for(ws_id: str, session_id: str) -> EventBus` (buses outlive hosts), `set_mode(ws_id, session_id, mode: Mode) -> None` (in-memory; a daemon restart falls back to the configured default — documented v1 limitation), `async host_for(record: WorkspaceRecord, session_id: str) -> SessionHost` (get-or-create under a per-key lock), `peek(ws_id, session_id) -> SessionHost | None`, `async close_host(ws_id, session_id) -> bool`, `start_evictor() -> None`, `async evict_idle() -> None`, `async aclose() -> None`. Task 9 consumes all of these.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_supervisor.py
"""Note: this file defines its own `_make_deps`/`_make_harness` copies rather
than importing them from tests/conftest.py — bare `from conftest import ...`
does not resolve in this repo (tests/__init__.py makes the project root, not
tests/, the sys.path entry; verified with ModuleNotFoundError)."""

import asyncio
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.runtime.deps import Deps, UIHooks, WorkspaceConfig
from marim_harness.runtime.harness import Harness
from marim_harness.runtime.permissions import Mode
from marim_harness.server.supervisor import SessionSupervisor
from marim_harness.server.workspaces import WorkspaceRecord
from marim_harness.tools.provider import BuiltinToolProvider

pytestmark = pytest.mark.anyio

_UI_HOOK_FIELDS = {
    "request_approval", "ask_user", "on_subagent_event", "on_subagent_notice",
    "on_subagent_model", "on_subagent_usage", "detach_fanout", "interactive",
    "notifier",
}


def _make_deps(root: Path, mode: Mode = Mode.auto, **kw) -> Deps:
    ui_kw = {k: kw.pop(k) for k in list(kw) if k in _UI_HOOK_FIELDS}
    return Deps(
        workspace=WorkspaceConfig(root=root, mode=mode),
        ui=UIHooks(**ui_kw) if ui_kw else UIHooks(),
        **kw,
    )


def _make_harness(model, deps, **config_kwargs) -> Harness:
    return Harness(model=model, provider=BuiltinToolProvider(), deps=deps,
                   instructions="You are a coding agent.", **config_kwargs)


def _model() -> FunctionModel:
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])

    async def stream_fn(messages, info):
        yield "ok"

    return FunctionModel(fn, stream_function=stream_fn)


def _factory(created: list):
    async def factory(workspace: Path, session_id: str, mode: Mode | None):
        created.append((workspace, session_id, mode))
        return _make_harness(_model(), _make_deps(workspace, mode=mode or Mode.auto))

    return factory


def _record(tmp_path) -> WorkspaceRecord:
    (tmp_path / "ws").mkdir(exist_ok=True)
    return WorkspaceRecord(id="ws", name="ws", path=str(tmp_path / "ws"),
                           kind="registered", created="2026-07-06")


async def test_host_for_creates_once_and_reuses(tmp_path):
    created: list = []
    sup = SessionSupervisor(_factory(created))
    record = _record(tmp_path)
    a, b = await asyncio.gather(sup.host_for(record, "s1"), sup.host_for(record, "s1"))
    assert a is b
    assert len(created) == 1  # per-key lock: no double build under concurrency
    assert created[0][1] == "s1"
    assert sup.peek("ws", "s1") is a
    assert sup.peek("ws", "other") is None
    await sup.aclose()


async def test_set_mode_reaches_factory(tmp_path):
    created: list = []
    sup = SessionSupervisor(_factory(created))
    record = _record(tmp_path)
    sup.set_mode("ws", "s1", Mode.plan)
    await sup.host_for(record, "s1")
    assert created[0][2] is Mode.plan
    await sup.aclose()


async def test_bus_survives_eviction(tmp_path):
    sup = SessionSupervisor(_factory([]), idle_ttl=0.0)
    record = _record(tmp_path)
    host = await sup.host_for(record, "s1")
    bus = sup.bus_for("ws", "s1")
    bus.publish("marker", {})
    await asyncio.sleep(0.05)  # host goes idle
    await sup.evict_idle()
    assert sup.peek("ws", "s1") is None
    assert sup.bus_for("ws", "s1") is bus  # same bus, ring intact
    assert bus.last_seq >= 1
    assert host.harness is not None  # closed, not corrupted


async def test_busy_or_subscribed_hosts_survive_eviction(tmp_path):
    sup = SessionSupervisor(_factory([]), idle_ttl=0.0)
    record = _record(tmp_path)
    await sup.host_for(record, "s1")
    sub = sup.bus_for("ws", "s1").attach()  # live subscriber blocks eviction
    await asyncio.sleep(0.05)
    await sup.evict_idle()
    assert sup.peek("ws", "s1") is not None
    sub.close()
    await sup.aclose()


async def test_close_host(tmp_path):
    sup = SessionSupervisor(_factory([]))
    record = _record(tmp_path)
    await sup.host_for(record, "s1")
    assert await sup.close_host("ws", "s1")
    assert sup.peek("ws", "s1") is None
    assert not await sup.close_host("ws", "s1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_server_supervisor.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# src/marim_harness/server/supervisor.py
"""Registry of live SessionHosts, one per (workspace, session).

Hosts are created lazily on the first prompt and evicted after sitting idle
(no running turn, nothing queued, no SSE subscriber) — the harness is torn
down cleanly and the session stays resumable from disk. Buses are keyed
separately and OUTLIVE hosts: an SSE client can stay attached (or resume with
Last-Event-ID) across an eviction, and a live subscriber blocks eviction so a
watching client never sees its stream silently reset.

``set_mode`` is in-memory only: a mode chosen at session creation survives
until the daemon restarts, after which the configured default applies
(documented v1 limitation — the session file doesn't persist a mode)."""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..runtime.harness import Harness
from ..runtime.permissions import Mode
from .bus import EventBus
from .host import SessionHost
from .workspaces import WorkspaceRecord

logger = logging.getLogger(__name__)

HarnessFactory = Callable[[Path, str, "Mode | None"], Awaitable[Harness]]

_EVICT_POLL_CEILING_SECONDS = 60.0


async def default_harness_factory(
    workspace: Path, session_id: str, mode: Mode | None
) -> Harness:
    """Build a full production harness for one session: the same wiring as the
    TUI/headless (models, MCP, LSP, hooks) via build_harness, plus the connect
    + session_start lifecycle headless performs around a run."""
    from ..runtime.bootstrap import build_harness

    harness = build_harness(workspace, mode=mode, session_id=session_id)
    await harness.connect()
    await harness.session_start("resume" if harness.session.history else "startup")
    return harness


class SessionSupervisor:
    def __init__(
        self,
        factory: HarnessFactory = default_harness_factory,
        *,
        idle_ttl: float = 900.0,
        ring_size: int = 1000,
    ) -> None:
        self._factory = factory
        self.idle_ttl = idle_ttl
        self._ring_size = ring_size
        self._buses: dict[tuple[str, str], EventBus] = {}
        self._hosts: dict[tuple[str, str], SessionHost] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._modes: dict[tuple[str, str], Mode] = {}
        self._evictor: asyncio.Task | None = None

    def bus_for(self, ws_id: str, session_id: str) -> EventBus:
        key = (ws_id, session_id)
        if key not in self._buses:
            self._buses[key] = EventBus(ring_size=self._ring_size)
        return self._buses[key]

    def set_mode(self, ws_id: str, session_id: str, mode: Mode) -> None:
        self._modes[(ws_id, session_id)] = mode

    def peek(self, ws_id: str, session_id: str) -> SessionHost | None:
        return self._hosts.get((ws_id, session_id))

    async def host_for(self, record: WorkspaceRecord, session_id: str) -> SessionHost:
        key = (record.id, session_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            host = self._hosts.get(key)
            if host is not None:
                return host
            harness = await self._factory(Path(record.path), session_id, self._modes.get(key))
            host = SessionHost(harness, self.bus_for(*key))
            self._hosts[key] = host
            return host

    async def close_host(self, ws_id: str, session_id: str) -> bool:
        key = (ws_id, session_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            host = self._hosts.pop(key, None)
        if host is None:
            return False
        await host.aclose()
        return True

    def start_evictor(self) -> None:
        if self._evictor is None:
            self._evictor = asyncio.get_running_loop().create_task(self._evict_loop())

    async def _evict_loop(self) -> None:
        interval = min(self.idle_ttl, _EVICT_POLL_CEILING_SECONDS) or 1.0
        while True:
            await asyncio.sleep(interval)
            try:
                await self.evict_idle()
            except Exception:  # noqa: BLE001 - the sweep must never die
                logger.warning("idle-eviction sweep failed", exc_info=True)

    async def evict_idle(self) -> None:
        for key, host in list(self._hosts.items()):
            bus = self._buses.get(key)
            subscribers = bus.subscriber_count if bus is not None else 0
            if (
                not host.busy
                and host.queued == 0
                and subscribers == 0
                and host.idle_seconds >= self.idle_ttl
            ):
                await self.close_host(*key)

    async def aclose(self) -> None:
        if self._evictor is not None:
            self._evictor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._evictor
            self._evictor = None
        for key in list(self._hosts):
            await self.close_host(*key)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_server_supervisor.py -v`
Expected: PASS

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/server/supervisor.py tests/test_server_supervisor.py
git commit -m "feat(server): session supervisor with idle eviction"
```

---

### Task 9: `server/http.py` — Starlette app + `[serve]` extra

**Files:**
- Modify: `pyproject.toml` (add extra + dev-group deps) — FIRST, so tests can import starlette
- Create: `src/marim_harness/server/http.py`
- Test: `tests/test_server_http.py`

**Interfaces:**
- Consumes: everything from Tasks 3–8 plus `SessionManager` (`list`, `create`, `delete`, `session_path`), `RunUsage` from `pydantic_ai.usage`, `Mode`.
- Produces: `create_app(*, registry: WorkspaceRegistry, supervisor: SessionSupervisor, token: str) -> Starlette`. Endpoint table exactly as in the spec (all under `/v1`; errors `{"error": {"code", "message"}}`). Task 10 consumes `create_app`.

- [ ] **Step 1: Add dependencies**

In `pyproject.toml`, extend `[project.optional-dependencies]`:

```toml
[project.optional-dependencies]
# The interactive TUI. A bare install is headless-only (`marim -p`); the `marim`
# interactive launch prints an install hint when textual is absent.
tui = ["textual>=0.80"]
# The `marim serve` HTTP daemon (REST + SSE). Bare installs print an install
# hint when starlette/uvicorn are absent.
serve = ["starlette>=0.40", "uvicorn>=0.30"]
```

And add `"starlette>=0.40"` and `"uvicorn>=0.30"` to the `[dependency-groups]` dev list (so the test suite and CI have them). Then run:

```bash
uv sync
uv run python -c "import starlette, uvicorn; print('ok')"
```

Expected: `ok`

- [ ] **Step 2: Write the failing integration test**

```python
# tests/test_server_http.py
"""End-to-end HTTP surface: workspaces, sessions, turns, parked asks, SSE.

Uses Starlette's TestClient (runs the ASGI app in a worker thread with a real
event loop, so SessionHost worker tasks run) and a FunctionModel harness
factory — no network, no real providers.

Note: this file defines its own `_make_deps`/`_make_harness` copies rather
than importing them from tests/conftest.py — bare `from conftest import ...`
does not resolve in this repo (tests/__init__.py makes the project root, not
tests/, the sys.path entry; verified with ModuleNotFoundError)."""

import json as _json
import time
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from starlette.testclient import TestClient

from marim_harness.runtime.deps import Deps, UIHooks, WorkspaceConfig
from marim_harness.runtime.harness import Harness
from marim_harness.runtime.permissions import Mode
from marim_harness.server.http import create_app
from marim_harness.server.supervisor import SessionSupervisor
from marim_harness.server.workspaces import WorkspaceRegistry
from marim_harness.tools.provider import BuiltinToolProvider

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

_UI_HOOK_FIELDS = {
    "request_approval", "ask_user", "on_subagent_event", "on_subagent_notice",
    "on_subagent_model", "on_subagent_usage", "detach_fanout", "interactive",
    "notifier",
}


def _make_deps(root: Path, mode: Mode = Mode.auto, **kw) -> Deps:
    ui_kw = {k: kw.pop(k) for k in list(kw) if k in _UI_HOOK_FIELDS}
    return Deps(
        workspace=WorkspaceConfig(root=root, mode=mode),
        ui=UIHooks(**ui_kw) if ui_kw else UIHooks(),
        **kw,
    )


def _make_harness(model, deps, **config_kwargs) -> Harness:
    return Harness(model=model, provider=BuiltinToolProvider(), deps=deps,
                   instructions="You are a coding agent.", **config_kwargs)


def _edit_model() -> FunctionModel:
    state = {"n": 0}

    def fn(messages, info):
        state["n"] += 1
        if state["n"] == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="read_file", args={"path": "a.txt"})]
            )
        if state["n"] == 2:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="edit_file",
                args={"path": "a.txt",
                      "edits": [{"old_string": "foo", "new_string": "bar"}]},
            )])
        return ModelResponse(parts=[TextPart(content="done")])

    stream_state = {"n": 0}

    async def stream_fn(messages, info):
        stream_state["n"] += 1
        if stream_state["n"] == 1:
            yield {0: DeltaToolCall(name="read_file",
                                    json_args=_json.dumps({"path": "a.txt"}),
                                    tool_call_id="tc-read-1")}
        elif stream_state["n"] == 2:
            yield {0: DeltaToolCall(
                name="edit_file",
                json_args=_json.dumps({"path": "a.txt",
                                       "edits": [{"old_string": "foo",
                                                  "new_string": "bar"}]}),
                tool_call_id="tc-edit-1")}
        else:
            yield "done"

    return FunctionModel(fn, stream_function=stream_fn)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))

    async def factory(workspace: Path, session_id: str, mode):
        from marim_harness.session import SessionManager

        manager = SessionManager(workspace)
        store = manager.store(session_id)
        deps = _make_deps(workspace, mode=mode or Mode.ask)
        return _make_harness(_edit_model(), deps, store=store, manager=manager)

    registry = WorkspaceRegistry(tmp_path / "state" / "workspaces.json", tmp_path / "managed")
    supervisor = SessionSupervisor(factory, idle_ttl=3600.0)
    app = create_app(registry=registry, supervisor=supervisor, token=TOKEN)
    with TestClient(app) as test_client:
        yield test_client, tmp_path


def _setup_workspace_and_session(client, tmp_path, mode="ask"):
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    (project / "a.txt").write_text("foo\n")
    ws = client.post("/v1/workspaces", headers=AUTH,
                     json={"name": "proj", "path": str(project)}).json()
    sid = client.post(f"/v1/workspaces/{ws['id']}/sessions", headers=AUTH,
                      json={"name": "run1", "mode": mode}).json()["id"]
    return ws["id"], sid, project


def _poll(client, url, predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(url, headers=AUTH)
        if response.status_code == 200 and predicate(response.json()):
            return response.json()
        time.sleep(0.05)
    raise AssertionError(f"timed out polling {url}")


def test_health_needs_no_auth(client):
    test_client, _ = client
    assert test_client.get("/v1/health").status_code == 200


def test_everything_else_requires_token(client):
    test_client, _ = client
    assert test_client.get("/v1/workspaces").status_code == 401
    assert test_client.get("/v1/workspaces", headers={
        "Authorization": "Bearer wrong"}).status_code == 401


def test_workspace_crud(client):
    test_client, tmp_path = client
    project = tmp_path / "proj"
    project.mkdir()
    created = test_client.post("/v1/workspaces", headers=AUTH,
                               json={"name": "proj", "path": str(project)})
    assert created.status_code == 201
    ws_id = created.json()["id"]
    listed = test_client.get("/v1/workspaces", headers=AUTH).json()
    assert [w["id"] for w in listed["workspaces"]] == [ws_id]
    managed = test_client.post("/v1/workspaces", headers=AUTH, json={"name": "scratch"})
    assert managed.json()["kind"] == "managed"
    assert test_client.delete(f"/v1/workspaces/{ws_id}", headers=AUTH).status_code == 200
    assert test_client.delete("/v1/workspaces/nope", headers=AUTH).status_code == 404


def test_session_create_and_list(client):
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    sessions = test_client.get(f"/v1/workspaces/{ws_id}/sessions", headers=AUTH).json()
    assert sid in [s["id"] for s in sessions["sessions"]]
    detail = test_client.get(f"/v1/workspaces/{ws_id}/sessions/{sid}", headers=AUTH).json()
    assert detail["status"] == "idle"
    assert detail["pending_asks"] == []
    missing = test_client.get(f"/v1/workspaces/{ws_id}/sessions/nope", headers=AUTH)
    assert missing.status_code == 404


def test_full_turn_with_parked_approval(client):
    test_client, tmp_path = client
    ws_id, sid, project = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"

    accepted = test_client.post(f"{base}/messages", headers=AUTH, json={"prompt": "edit it"})
    assert accepted.status_code == 202
    turn_id = accepted.json()["turn_id"]

    # The edit parks for approval; answer it over the API.
    state = _poll(test_client, base, lambda s: s["status"] == "waiting_ask")
    [ask] = state["pending_asks"]
    assert ask["payload"]["tool_name"] == "edit_file"
    answered = test_client.post(f"{base}/asks/{ask['id']}", headers=AUTH,
                                json={"approve": True})
    assert answered.status_code == 200

    _poll(test_client, base, lambda s: s["status"] == "idle")
    assert (project / "a.txt").read_text() == "bar\n"

    # Replay the whole stream via SSE and find the lifecycle events.
    events = []
    with test_client.stream("GET", f"{base}/events?access_token={TOKEN}",
                            headers={"Last-Event-ID": "0"}) as stream:
        for line in stream.iter_lines():
            if line.startswith("event: "):
                events.append(line.removeprefix("event: "))
            if line.startswith("data: ") and '"turn_id"' in line and "output" in line:
                payload = _json.loads(line.removeprefix("data: "))
                if payload.get("turn_id") == turn_id and "output" in payload:
                    break
    assert "turn.started" in events
    assert "ask.pending" in events
    assert "ask.resolved" in events
    assert "turn.finished" in events

    # History endpoint serves the persisted messages.
    history = test_client.get(f"{base}/history", headers=AUTH).json()
    assert history["message_count"] > 0
    assert len(history["messages"]) > 0


def test_interrupt_parked_turn(client):
    test_client, tmp_path = client
    ws_id, sid, project = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
    test_client.post(f"{base}/messages", headers=AUTH, json={"prompt": "edit it"})
    _poll(test_client, base, lambda s: s["status"] == "waiting_ask")
    assert test_client.post(f"{base}/interrupt", headers=AUTH).json()["interrupted"] is True
    _poll(test_client, base, lambda s: s["status"] == "idle")
    assert (project / "a.txt").read_text() == "foo\n"
    # Nothing running now.
    assert test_client.post(f"{base}/interrupt", headers=AUTH).json()["interrupted"] is False


def test_steer_requires_running_turn(client):
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
    refused = test_client.post(f"{base}/steer", headers=AUTH, json={"text": "hey"})
    assert refused.status_code == 409
    test_client.post(f"{base}/messages", headers=AUTH, json={"prompt": "edit it"})
    _poll(test_client, base, lambda s: s["status"] == "waiting_ask")
    assert test_client.post(f"{base}/steer", headers=AUTH,
                            json={"text": "hey"}).status_code == 200
    test_client.post(f"{base}/interrupt", headers=AUTH)


def test_unknown_workspace_and_session_404(client):
    test_client, _ = client
    assert test_client.get("/v1/workspaces/nope/sessions", headers=AUTH).status_code == 404
    assert test_client.post("/v1/workspaces/nope/sessions/x/messages", headers=AUTH,
                            json={"prompt": "hi"}).status_code == 404
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_server_http.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.server.http'`

- [ ] **Step 4: Implement**

```python
# src/marim_harness/server/http.py
"""The HTTP transport: Starlette routes + SSE over the transport-neutral core.

This is the ONLY server module allowed to import starlette (an optional
extra). Handlers validate with the schema models, delegate to the supervisor /
registry, and translate outcomes to status codes. Auth is an explicit check at
the top of every handler (not middleware): BaseHTTPMiddleware buffers
streaming bodies, and an explicit call is easier to follow and test."""

import asyncio
import base64
import contextlib
import json
from dataclasses import asdict
from pathlib import Path

from pydantic import ValidationError
from pydantic_ai.usage import RunUsage
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ..runtime.permissions import Mode
from ..session import SessionManager
from .auth import token_matches
from .host import TurnQueueFull
from .schema import AskAnswerIn, MessageIn, SessionIn, SteerIn, WorkspaceIn, sse_format
from .supervisor import SessionSupervisor
from .workspaces import WorkspaceRegistry

_HEARTBEAT_SECONDS = 15.0


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _unauthorized(request: Request) -> JSONResponse | None:
    """None when the request carries a valid token; the 401 response otherwise.
    The SSE endpoint additionally accepts ?access_token= because a browser
    EventSource cannot set headers."""
    token = request.app.state.token
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer ") and token_matches(token, header[len("Bearer "):]):
        return None
    if request.url.path.endswith("/events"):
        presented = request.query_params.get("access_token")
        if presented and token_matches(token, presented):
            return None
    return _error(401, "unauthorized", "missing or invalid bearer token")


def _registry(request: Request) -> WorkspaceRegistry:
    return request.app.state.registry


def _supervisor(request: Request) -> SessionSupervisor:
    return request.app.state.supervisor


def _workspace(request: Request):
    return _registry(request).get(request.path_params["ws"])


def _session_exists(record, session_id: str) -> bool:
    manager = SessionManager(Path(record.path))
    return manager.session_path(session_id).exists()


async def _json_body(request: Request, model):
    try:
        return model(**await request.json())
    except (ValidationError, ValueError, TypeError) as exc:
        raise _BadBody(str(exc)) from exc


class _BadBody(Exception):
    pass


async def health(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def list_workspaces(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    records = [r.as_dict() for r in _registry(request).list()]
    return JSONResponse({"workspaces": records})


async def create_workspace(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    try:
        body = await _json_body(request, WorkspaceIn)
    except _BadBody as exc:
        return _error(400, "bad_request", str(exc))
    try:
        if body.path is not None:
            record = _registry(request).register(body.name, Path(body.path))
        else:
            record = _registry(request).create_managed(body.name, git_url=body.git_url)
    except ValueError as exc:
        return _error(400, "bad_request", str(exc))
    return JSONResponse(record.as_dict(), status_code=201)


async def delete_workspace(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    purge = request.query_params.get("purge") == "true"
    try:
        _registry(request).delete(request.path_params["ws"], purge=purge)
    except KeyError:
        return _error(404, "not_found", "unknown workspace")
    except ValueError as exc:
        return _error(400, "bad_request", str(exc))
    return JSONResponse({"deleted": True})


async def list_sessions(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    infos = SessionManager(Path(record.path)).list()
    return JSONResponse({"sessions": [asdict(i) for i in infos]})


async def create_session(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    try:
        body = await _json_body(request, SessionIn)
    except _BadBody as exc:
        return _error(400, "bad_request", str(exc))
    if body.mode is not None and body.mode not in (m.value for m in Mode):
        return _error(400, "bad_request", f"unknown mode: {body.mode}")
    store = SessionManager(Path(record.path)).create(body.name)
    # An immediate empty save makes the session file exist, so list/history/
    # message endpoints see it before its first turn.
    store.save([], RunUsage())
    if body.mode is not None:
        _supervisor(request).set_mode(record.id, store.session_id, Mode(body.mode))
    return JSONResponse({"id": store.session_id, "name": store.name}, status_code=201)


async def get_session(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    session_id = request.path_params["sid"]
    infos = {i.id: i for i in SessionManager(Path(record.path)).list()}
    info = infos.get(session_id)
    if info is None:
        return _error(404, "not_found", "unknown session")
    host = _supervisor(request).peek(record.id, session_id)
    return JSONResponse({
        "session": asdict(info),
        "status": host.status if host else "idle",
        "queued": host.queued if host else 0,
        "pending_asks": host.pending_asks() if host else [],
    })


async def delete_session(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    session_id = request.path_params["sid"]
    if not _session_exists(record, session_id):
        return _error(404, "not_found", "unknown session")
    host = _supervisor(request).peek(record.id, session_id)
    if host is not None and host.busy:
        return _error(409, "busy", "session has a running turn; interrupt it first")
    await _supervisor(request).close_host(record.id, session_id)
    SessionManager(Path(record.path)).delete(session_id)
    return JSONResponse({"deleted": True})


async def post_message(request: Request) -> Response:
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
        body = await _json_body(request, MessageIn)
    except _BadBody as exc:
        return _error(400, "bad_request", str(exc))
    attachments = None
    if body.attachments:
        attachments = [
            (base64.b64decode(a.data_b64), a.media_type) for a in body.attachments
        ]
    host = await _supervisor(request).host_for(record, session_id)
    try:
        turn_id = host.submit(body.prompt, attachments)
    except TurnQueueFull:
        return _error(429, "queue_full", "turn queue is full; wait for the running turn")
    return JSONResponse({"turn_id": turn_id}, status_code=202)


async def post_interrupt(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    host = _supervisor(request).peek(record.id, request.path_params["sid"])
    interrupted = host.interrupt() if host is not None else False
    return JSONResponse({"interrupted": interrupted})


async def post_steer(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    try:
        body = await _json_body(request, SteerIn)
    except _BadBody as exc:
        return _error(400, "bad_request", str(exc))
    host = _supervisor(request).peek(record.id, request.path_params["sid"])
    if host is None or not host.busy:
        return _error(409, "not_running", "no running turn to steer")
    host.steer(body.text)
    return JSONResponse({"ok": True})


async def list_asks(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    host = _supervisor(request).peek(record.id, request.path_params["sid"])
    return JSONResponse({"asks": host.pending_asks() if host else []})


async def answer_ask(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    try:
        body = await _json_body(request, AskAnswerIn)
    except _BadBody as exc:
        return _error(400, "bad_request", str(exc))
    host = _supervisor(request).peek(record.id, request.path_params["sid"])
    if host is None:
        return _error(404, "not_found", "no live session host")
    if not host.answer_ask(request.path_params["aid"], body.as_answer()):
        return _error(404, "not_found", "unknown or already-answered ask")
    return JSONResponse({"ok": True})


async def get_events(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    session_id = request.path_params["sid"]
    bus = _supervisor(request).bus_for(record.id, session_id)
    last = request.headers.get("last-event-id", "")
    after_seq = int(last) if last.isdigit() else None

    async def stream():
        subscription = bus.attach(after_seq=after_seq)
        try:
            while True:
                event = await subscription.next_event(timeout=_HEARTBEAT_SECONDS)
                if event is None:
                    yield ": keepalive\n\n"  # comment frame keeps proxies open
                    continue
                yield sse_format(event)
        finally:
            subscription.close()

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


async def get_history(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    session_id = request.path_params["sid"]
    path = SessionManager(Path(record.path)).session_path(session_id)
    if not path.exists():
        return _error(404, "not_found", "unknown session")
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return _error(500, "unreadable", "session file is unreadable")
    messages = data.get("messages", [])
    try:
        offset = max(0, int(request.query_params.get("offset", "0")))
        limit = max(1, int(request.query_params.get("limit", "100")))
    except ValueError:
        return _error(400, "bad_request", "offset/limit must be integers")
    return JSONResponse({
        "id": data.get("id"),
        "name": data.get("name"),
        "model": data.get("model"),
        "message_count": len(messages),
        "offset": offset,
        "messages": messages[offset:offset + limit],
    })


def create_app(
    *, registry: WorkspaceRegistry, supervisor: SessionSupervisor, token: str
) -> Starlette:
    base = "/v1/workspaces/{ws}/sessions/{sid}"
    routes = [
        Route("/v1/health", health, methods=["GET"]),
        Route("/v1/workspaces", list_workspaces, methods=["GET"]),
        Route("/v1/workspaces", create_workspace, methods=["POST"]),
        Route("/v1/workspaces/{ws}", delete_workspace, methods=["DELETE"]),
        Route("/v1/workspaces/{ws}/sessions", list_sessions, methods=["GET"]),
        Route("/v1/workspaces/{ws}/sessions", create_session, methods=["POST"]),
        Route(base, get_session, methods=["GET"]),
        Route(base, delete_session, methods=["DELETE"]),
        Route(f"{base}/messages", post_message, methods=["POST"]),
        Route(f"{base}/interrupt", post_interrupt, methods=["POST"]),
        Route(f"{base}/steer", post_steer, methods=["POST"]),
        Route(f"{base}/asks", list_asks, methods=["GET"]),
        Route(f"{base}/asks/{{aid}}", answer_ask, methods=["POST"]),
        Route(f"{base}/events", get_events, methods=["GET"]),
        Route(f"{base}/history", get_history, methods=["GET"]),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        supervisor.start_evictor()
        try:
            yield
        finally:
            # Graceful shutdown: interrupt running turns (resumable flush),
            # cancel parked asks, persist every host.
            await supervisor.aclose()

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.registry = registry
    app.state.supervisor = supervisor
    app.state.token = token
    return app
```

Unused-import check: `asyncio` is unused in the final file — remove it (the heartbeat lives in `Subscription.next_event`). Run ruff to confirm.

- [ ] **Step 5: Run the test suite**

Run: `uv run pytest --no-cov tests/test_server_http.py -v`
Expected: PASS

- [ ] **Step 6: Lint, typecheck, commit**

```bash
uv run ruff check src tests && uv run pyright
git add pyproject.toml uv.lock src/marim_harness/server/http.py tests/test_server_http.py
git commit -m "feat(server): HTTP transport — REST + SSE over the server core"
```

---

### Task 10: `marim serve` CLI entry + router + docs

**Files:**
- Create: `src/marim_harness/interfaces/cli/serve.py`
- Modify: `src/marim_harness/interfaces/cli/router.py:13` (`_MANAGEMENT`)
- Modify: `CLAUDE.md` (Commands section, one line)
- Test: `tests/test_cli_serve.py`

**Interfaces:**
- Consumes: `create_app`, `load_or_create_token`, `SessionSupervisor`, `WorkspaceRegistry`, uvicorn.
- Produces: `main(argv: list[str], *, out=sys.stdout, err=sys.stderr) -> int` matching the management-command contract the router dispatches to.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_serve.py
"""The serve CLI entry: routing, arg parsing, and startup wiring (uvicorn is
stubbed — we never bind a real port in tests)."""

import io


def test_router_reserves_serve_keyword():
    from marim_harness.interfaces.cli.router import _MANAGEMENT

    assert "serve" in _MANAGEMENT


def test_serve_main_builds_app_and_runs_uvicorn(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    import uvicorn

    calls = {}

    def fake_run(app, **kwargs):
        calls["app"] = app
        calls["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)
    from marim_harness.interfaces.cli import serve

    out, err = io.StringIO(), io.StringIO()
    code = serve.main(["--port", "9999"], out=out, err=err)
    assert code == 0
    assert calls["kwargs"]["host"] == "127.0.0.1"
    assert calls["kwargs"]["port"] == 9999
    assert calls["app"].state.token  # token generated and wired
    token_file = tmp_path / "xdg-data" / "marim-harness" / "server" / "token"
    assert token_file.exists()
    assert "9999" in out.getvalue()
    assert str(token_file) in out.getvalue()


def test_serve_main_rejects_unknown_args(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    import pytest

    from marim_harness.interfaces.cli import serve

    with pytest.raises(SystemExit):
        serve.main(["--bogus"], out=io.StringIO(), err=io.StringIO())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_cli_serve.py -v`
Expected: FAIL — `"serve" in _MANAGEMENT` is False; `No module named 'marim_harness.interfaces.cli.serve'`

- [ ] **Step 3: Implement**

In `src/marim_harness/interfaces/cli/router.py` line 13:

```python
_MANAGEMENT = {"sessions", "config", "models", "plugin", "mcp", "serve"}
```

```python
# src/marim_harness/interfaces/cli/serve.py
"""``marim serve`` — run marim as a long-lived HTTP daemon (REST + SSE).

Binds 127.0.0.1 by default; expose it via a reverse proxy or tailscale and
authenticate with the bearer token printed at startup (persisted 0600 under
the server state dir). Requires the ``serve`` extra (starlette + uvicorn)."""

import argparse
import os
import sys
from pathlib import Path


def _default_state_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "marim-harness" / "server"


def main(argv: list[str], *, out=sys.stdout, err=sys.stderr) -> int:
    parser = argparse.ArgumentParser(
        prog="marim serve",
        description="Run the marim HTTP server daemon (sessions over REST + SSE).",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8642,
                        help="bind port (default: 8642)")
    parser.add_argument("--workspaces-root", type=Path, default=None,
                        help="directory for managed workspaces "
                             "(default: <state-dir>/workspaces)")
    parser.add_argument("--idle-ttl", type=float, default=900.0,
                        help="seconds before an idle session's harness is evicted "
                             "(default: 900)")
    args = parser.parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        print(
            "marim serve requires the server extra. Install with:\n"
            "  uv add 'marim-harness[serve]'  (or: pip install 'marim-harness[serve]')",
            file=err,
        )
        return 1
    from ...server.auth import load_or_create_token
    from ...server.http import create_app
    from ...server.supervisor import SessionSupervisor
    from ...server.workspaces import WorkspaceRegistry

    state_dir = _default_state_dir()
    token = load_or_create_token(state_dir)
    registry = WorkspaceRegistry(
        state_dir / "workspaces.json",
        args.workspaces_root or state_dir / "workspaces",
    )
    supervisor = SessionSupervisor(idle_ttl=args.idle_ttl)
    app = create_app(registry=registry, supervisor=supervisor, token=token)

    print(f"marim serve listening on http://{args.host}:{args.port}", file=out)
    print(f"bearer token: {state_dir / 'token'}", file=out)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0
```

In `CLAUDE.md`, under the Commands code block, add after the pytest lines' block (in the prose or the fenced block, matching surrounding style):

```
uv run marim serve --port 8642   # HTTP daemon (REST + SSE); needs the [serve] extra
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_cli_serve.py -v`
Expected: PASS

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/interfaces/cli/serve.py src/marim_harness/interfaces/cli/router.py tests/test_cli_serve.py CLAUDE.md
git commit -m "feat(cli): marim serve command"
```

---

### Task 11: Full-suite verification + SSE resume edge test

**Files:**
- Test: `tests/test_server_http.py` (append one test)

**Interfaces:** none new — this task closes the loop on spec requirements not yet covered by a dedicated test (SSE `Last-Event-ID` resume across a reconnect) and runs the CI gauntlet.

- [ ] **Step 1: Append the SSE-resume test**

```python
def test_sse_resume_with_last_event_id(client):
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path, mode="auto")
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
    test_client.post(f"{base}/messages", headers=AUTH, json={"prompt": "go"})
    _poll(test_client, base, lambda s: s["status"] == "idle")

    # First read: take only the first event and note its id.
    first_id = None
    with test_client.stream("GET", f"{base}/events?access_token={TOKEN}",
                            headers={"Last-Event-ID": "0"}) as stream:
        for line in stream.iter_lines():
            if line.startswith("id: "):
                first_id = int(line.removeprefix("id: "))
                break
    assert first_id is not None

    # Reconnect after that id: replay resumes strictly after it.
    with test_client.stream("GET", f"{base}/events?access_token={TOKEN}",
                            headers={"Last-Event-ID": str(first_id)}) as stream:
        for line in stream.iter_lines():
            if line.startswith("id: "):
                assert int(line.removeprefix("id: ")) == first_id + 1
                break
```

- [ ] **Step 2: Run it**

Run: `uv run pytest --no-cov tests/test_server_http.py::test_sse_resume_with_last_event_id -v`
Expected: PASS

- [ ] **Step 3: Run the CI gauntlet in CI order**

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```

Expected: all green, coverage threshold intact. Fix anything that surfaces before proceeding.

- [ ] **Step 4: Commit**

```bash
git add tests/test_server_http.py
git commit -m "test(server): SSE Last-Event-ID resume across reconnects"
```

---

## Deferred (explicitly NOT in this plan, per spec)

WebSocket transport, mode-switch endpoint, checkpoints/rewind over the API, multi-user auth, per-session resource quotas, process-per-session isolation, push notifications.
