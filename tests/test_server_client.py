"""The remote side of the TUI's session seam (phase 4a): ``RemoteSessionHost``
over the daemon's REST routes and ``RemoteSubscription`` over its WebSocket.

Three layers, each tested at its own seam:

- one end-to-end pass against a REAL uvicorn server (REST + WebSocket): the
  contract that matters is that the client speaks the daemon's actual wire;
- the REST error mapping through ``httpx.MockTransport`` (no server), where
  the interesting cases are the refusals a live turn is awkward to stage;
- ``RemoteSubscription``'s reconnect/gap/lost machinery with a scripted
  ``open_socket`` and an injected clock + sleep, so no test waits on a
  backoff.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel

from marim_harness.runtime.deps import Deps, UIHooks, WorkspaceConfig
from marim_harness.runtime.harness import Harness
from marim_harness.runtime.permissions import Mode
from marim_harness.server import client as client_mod
from marim_harness.server.attach import RemoteTarget
from marim_harness.server.client import (
    ReconnectPolicy,
    RemoteInfo,
    RemoteSessionHost,
    RemoteSubscription,
    RemoteUnavailable,
    usage_from_summary,
)
from marim_harness.server.host import HostClosed, TurnQueueFull
from marim_harness.server.http import create_app
from marim_harness.server.schema import Event
from marim_harness.server.supervisor import SessionSupervisor
from marim_harness.server.workspaces import WorkspaceRegistry
from marim_harness.session.claim import SessionClaimed
from marim_harness.tools.provider import BuiltinToolProvider

pytestmark = pytest.mark.anyio

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


# ------------------------------------------------------------ real server --


def _reply_model() -> FunctionModel:
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="done")])

    async def stream_fn(messages, info):
        yield "done"

    return FunctionModel(fn, stream_function=stream_fn)


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """A real daemon on an ephemeral loopback port, in a background thread —
    the WebSocket half needs a real socket (starlette's TestClient cannot
    serve the client library's ``websockets`` connection)."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))

    async def factory(workspace: Path, session_id: str, mode):
        from marim_harness.session import SessionManager

        manager = SessionManager(workspace)
        store = manager.store(session_id)
        deps = Deps(workspace=WorkspaceConfig(root=workspace, mode=mode or Mode.auto), ui=UIHooks())
        return Harness(
            model=_reply_model(),
            provider=BuiltinToolProvider(),
            deps=deps,
            instructions="You are a coding agent.",
            store=store,
            manager=manager,
        )

    registry = WorkspaceRegistry(tmp_path / "state" / "workspaces.json", tmp_path / "managed")
    supervisor = SessionSupervisor(factory, idle_ttl=3600.0)
    app = create_app(registry=registry, supervisor=supervisor, token=TOKEN)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    uv_server = uvicorn.Server(config)
    thread = threading.Thread(target=uv_server.run, daemon=True)
    thread.start()
    while not uv_server.started:
        time.sleep(0.01)
    port = uv_server.servers[0].sockets[0].getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    project = tmp_path / "proj"
    project.mkdir()
    with httpx.Client(base_url=endpoint, timeout=30.0) as tc:
        ws = tc.post("/v1/workspaces", headers=AUTH, json={"name": "proj", "path": str(project)})
        ws_id = ws.json()["id"]
        sid = tc.post(
            f"/v1/workspaces/{ws_id}/sessions", headers=AUTH, json={"name": "run1", "mode": "auto"}
        ).json()["id"]
    target = RemoteTarget(
        endpoint=endpoint, token=TOKEN, workspace_id=ws_id, session_id=sid, workspace_root=project
    )
    yield target
    uv_server.should_exit = True
    thread.join(timeout=10.0)


async def _drain_until(feed, wanted: str, *, limit: float = 15.0) -> list[Event]:
    seen: list[Event] = []
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        event = await feed.next_event(timeout=1.0)
        if event is None:
            continue
        seen.append(event)
        if event.type == wanted:
            return seen
    raise AssertionError(f"never saw {wanted}; got {[e.type for e in seen]}")


async def test_remote_host_end_to_end_against_a_real_daemon(server):
    target = server
    states: list[str] = []
    host = RemoteSessionHost(target, target.workspace_root, on_state=states.append)
    try:
        assert await host.load_session() == "idle"
        assert host.info.session_id == target.session_id
        assert host.info.session_name == "run1"
        assert host.info.mode == "auto"
        assert host.info.workspace_root == target.workspace_root
        # Cold session (no host loaded yet): the live-only fields are absent.
        assert host.info.compact_threshold == 0

        feed = host.attach(after_seq=0)
        turn_id = await host.submit("hi")
        events = await _drain_until(feed, "session.status")
        while events[-1].data.get("status") != "idle":
            events += await _drain_until(feed, "session.status")
        types = [e.type for e in events]
        assert "turn.started" in types and "turn.finished" in types
        started = next(e for e in events if e.type == "turn.started")
        assert started.data["turn_id"] == turn_id
        assert started.data["trigger"] == "user"
        assert states[0] == "connected"
        # The read model followed the finish (usage is cumulative on the wire).
        assert host.info.usage.total_tokens >= 0
        await host.load_session()
        assert host.info.compact_threshold > 0  # a loaded host answers the live fields

        snapshot = await host.history()
        assert [type(m).__name__ for m in snapshot.messages] == ["ModelRequest", "ModelResponse"]
        assert snapshot.history_seq is not None and snapshot.history_seq > 0
        assert host.info.message_count == 2
        assert host.info.history_tokens > 0

        await host.refresh()  # nothing new: the count is stable
        assert host.info.message_count == 2

        assert await host.interrupt() is False  # idle: nothing to interrupt
        assert await host.pending_asks() == []
        assert await host.answer_ask("nope", {"approve": True}) is False
        await host.set_mode("ask")
        assert host.info.mode == "ask"
        await host.load_session()
        assert host.info.mode == "ask"  # the daemon agrees
        # Idle → the daemon refuses (409 not_running) and the refusal is
        # raised so the app can say the text did not go.
        with pytest.raises(RemoteUnavailable, match="steer not delivered"):
            await host.steer("later")
    finally:
        await host.close()


async def test_feed_reports_lost_on_a_bad_token(server):
    """A bad token cannot be fixed by retrying: lost at once, no backoff
    loop. On the real wire the server's ``close(4401)`` before ``accept``
    reaches the client as a rejected handshake (HTTP 403), which is why the
    fatal set names statuses as well as close codes."""
    target = server
    bad = RemoteTarget(
        endpoint=target.endpoint,
        token="wrong",
        workspace_id=target.workspace_id,
        session_id=target.session_id,
        workspace_root=target.workspace_root,
    )
    states: list[str] = []
    host = RemoteSessionHost(bad, bad.workspace_root, on_state=states.append)
    try:
        feed = host.attach()
        for _ in range(200):
            if feed.state == "lost":
                break
            await asyncio.sleep(0.05)
        assert feed.state == "lost"
        assert states == ["lost"]
    finally:
        await host.close()


# ------------------------------------------------------- mock transport --


def _target() -> RemoteTarget:
    return RemoteTarget(
        endpoint="http://daemon",
        token="tok",
        workspace_id="ws1",
        session_id="s1",
        workspace_root=Path("/ws"),
    )


def _mock_host(handler) -> RemoteSessionHost:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://daemon",
        headers={"Authorization": "Bearer tok"},
    )
    return RemoteSessionHost(_target(), Path("/ws"), client=client)


def _error(status: int, code: str, message: str = "nope") -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


async def test_submit_maps_daemon_refusals_to_host_errors():
    seen: list[dict] = []
    answers = iter(
        [
            _error(429, "queue_full"),
            _error(404, "host_closed"),
            _error(409, "claimed"),
            _error(500, "boom", "it broke"),
            httpx.Response(202, json={"turn_id": "t9"}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer tok"
        assert request.url.path == "/v1/workspaces/ws1/sessions/s1/messages"
        seen.append(json.loads(request.content))
        return next(answers)

    host = _mock_host(handler)
    with pytest.raises(TurnQueueFull):
        await host.submit("a")
    with pytest.raises(HostClosed):
        await host.submit("b")
    with pytest.raises(SessionClaimed):
        await host.submit("c")
    with pytest.raises(RemoteUnavailable, match="it broke"):
        await host.submit("d")
    turn = await host.submit("e", [(b"\x89PNG", "image/png")], trigger="system")
    assert turn == "t9"
    assert seen[-1]["trigger"] == "system"
    assert seen[-1]["attachments"] == [{"data_b64": "iVBORw==", "media_type": "image/png"}]
    assert seen[0] == {"prompt": "a", "trigger": "user"}  # no attachments key when none


async def test_transport_failure_is_remote_unavailable_naming_the_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    host = _mock_host(handler)
    with pytest.raises(RemoteUnavailable, match="http://daemon"):
        await host.interrupt()
    with pytest.raises(HostClosed):  # the app's existing handler catches it as such
        await host.load_session()


async def test_mode_and_model_switch_update_the_read_model_only_on_success():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/mode"):
            body = json.loads(request.content)
            return httpx.Response(200 if body["mode"] != "plan" else 400, json={"mode": "x"})
        if request.url.path.endswith("/model"):
            return _error(409, "busy", "a turn is running")
        raise AssertionError(request.url.path)

    host = _mock_host(handler)
    await host.set_mode("auto")
    assert host.info.mode == "auto"
    with pytest.raises(RemoteUnavailable, match="mode not switched"):
        await host.set_mode("plan")
    assert host.info.mode == "auto"
    with pytest.raises(RemoteUnavailable, match="a turn is running"):
        await host.set_model("m2")
    assert host.info.model_id is None


async def test_model_switch_success_relabels():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "m2"})

    host = _mock_host(handler)
    await host.set_model("m2")
    assert (host.info.model_id, host.info.model_label) == ("m2", "m2")


def _raw_history() -> list[dict]:
    messages = [
        ModelRequest(parts=[UserPromptPart(content="hello there, count these words please")]),
        ModelResponse(parts=[TextPart(content="a reply long enough to be worth some tokens")]),
        ModelRequest(parts=[UserPromptPart(content="and one more request for the tail")]),
    ]
    return ModelMessagesTypeAdapter.dump_python(messages, mode="json")


async def test_history_pages_through_the_route_and_refresh_counts_only_the_tail(monkeypatch):
    monkeypatch.setattr(client_mod, "HISTORY_PAGE", 2)
    raw = _raw_history()
    state = {"messages": raw[:2], "seq": 5}
    calls: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/history"):
            offset = int(request.url.params["offset"])
            limit = int(request.url.params["limit"])
            calls.append((offset, limit))
            page = state["messages"][offset : offset + limit]
            return httpx.Response(
                200,
                json={
                    "message_count": len(state["messages"]),
                    "history_seq": state["seq"],
                    "messages": page,
                },
            )
        if path == "/v1/workspaces/ws1/sessions/s1":
            return httpx.Response(
                200,
                json={
                    "session": {"name": "run", "message_count": len(state["messages"])},
                    "status": "idle",
                    "usage": {"input_tokens": 3, "output_tokens": 4},
                    "compact_threshold": 1000,
                },
            )
        raise AssertionError(path)

    host = _mock_host(handler)
    snapshot = await host.history()
    assert len(snapshot.messages) == 2 and snapshot.history_seq == 5
    assert calls == [(0, 2)]  # one page, exactly message_count wide: no second fetch
    counted = host.info.history_tokens
    assert counted > 0

    # A turn landed on the daemon: refresh counts only the new tail.
    state["messages"] = raw
    calls.clear()
    await host.refresh()
    assert calls == [(2, 2)]
    assert host.info.history_tokens > counted
    assert host.info.message_count == 3
    assert host.info.usage.input_tokens == 3 and host.info.usage.output_tokens == 4
    assert host.info.compact_threshold == 1000

    # The transcript shrank (compaction / rewind): recount from the start.
    state["messages"] = raw[:1]
    calls.clear()
    await host.refresh()
    assert calls == [(0, 2)]
    assert host.info.message_count == 1
    assert 0 < host.info.history_tokens < counted


async def test_history_paginates_a_long_transcript(monkeypatch):
    monkeypatch.setattr(client_mod, "HISTORY_PAGE", 1)
    raw = _raw_history()

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        return httpx.Response(
            200,
            json={"message_count": 3, "history_seq": 1, "messages": raw[offset : offset + 1]},
        )

    host = _mock_host(handler)
    snapshot = await host.history()
    assert len(snapshot.messages) == 3


async def test_unreadable_history_and_asks_degrade_predictably():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/asks"):
            return _error(500, "boom")
        return _error(404, "not_found", "unknown session")

    host = _mock_host(handler)
    # A failed read is raised, never reported as "no asks" (which would have
    # the app dismiss every mounted panel).
    with pytest.raises(RemoteUnavailable, match="asks not readable"):
        await host.pending_asks()
    with pytest.raises(RemoteUnavailable, match="unknown session"):
        await host.history()
    with pytest.raises(RemoteUnavailable, match="session not readable"):
        await host.load_session()


async def test_answer_ask_and_steer_status_mapping():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/asks/broken"):
            return _error(500, "boom", "it broke")
        if "/asks/" in request.url.path:
            return httpx.Response(200 if request.url.path.endswith("/live") else 404, json={})
        return _error(409, "idle", "no turn to steer")

    host = _mock_host(handler)
    assert await host.answer_ask("live", {"approve": True}) is True
    assert await host.answer_ask("gone", {"approve": True}) is False
    # Only "unknown/already answered" is a False; a failure the daemon did
    # not take the verdict through is raised, not swallowed.
    with pytest.raises(RemoteUnavailable, match="answer not delivered: it broke"):
        await host.answer_ask("broken", {"approve": True})
    with pytest.raises(RemoteUnavailable, match="steer not delivered: no turn to steer"):
        await host.steer("x")


# --------------------------------------------------------- read model --


def test_remote_info_apply_session_tolerates_an_older_daemon():
    info = RemoteInfo(workspace_root=Path("/ws"), session_id="s1", mode="ask")
    info.apply_session({"session": {"name": "n", "model": "m"}, "status": "idle"})
    assert (info.session_name, info.mode, info.model_id, info.model_label) == ("n", "ask", "m", "m")
    assert info.usage.total_tokens == 0 and info.compact_threshold == 0
    info.apply_session(
        {
            "session": {
                "mode": "auto",
                "model_label": "Model M",
                "advisor_model": "adv",
                "thinking": "high",
                "message_count": "7",
                "duration_seconds": 12.5,
            },
            "usage": {"input_tokens": "2", "output_tokens": None, "cache_read_tokens": "x"},
            "compact_threshold": 99,
        }
    )
    assert info.mode == "auto" and info.model_label == "Model M"
    assert (info.advisor_model_id, info.thinking_level_id) == ("adv", "high")
    assert info.message_count == 7 and info.duration_seconds == 12.5
    assert (info.usage.input_tokens, info.usage.output_tokens, info.usage.cache_read_tokens) == (
        2,
        0,
        0,
    )
    assert info.compact_threshold == 99


def test_remote_info_observe_follows_events():
    info = RemoteInfo(workspace_root=Path("/ws"), session_id="s1")

    def ev(type_: str, data: dict) -> Event:
        return Event(seq=1, ts="", type=type_, data=data)

    info.observe(ev("session.renamed", {"to": "new"}))
    info.observe(ev("session.mode_changed", {"mode": "plan"}))
    info.observe(ev("turn.finished", {"usage": {"input_tokens": 10, "output_tokens": 1}}))
    info.observe(ev("turn.finished", {"usage": {"input_tokens": 12, "output_tokens": 2}}))
    info.observe(ev("compaction.finished", {"after": 42}))
    info.observe(ev("text.delta", {"text": "ignored"}))
    assert info.session_name == "new" and info.mode == "plan"
    assert info.usage.input_tokens == 12  # replaced, not summed
    assert info.history_tokens == 42
    assert usage_from_summary({}).total_tokens == 0


# ------------------------------------------------------ subscription --


class _Socket:
    """One scripted connection: yields ``frames`` then ends cleanly, raises
    ``exc`` after them, or (``hang``) stays open for good."""

    def __init__(self, frames, exc: BaseException | None = None, *, hang: bool = False) -> None:
        self.frames = frames
        self.exc = exc
        self.hang = hang

    async def __aenter__(self):
        return self._iter()

    async def __aexit__(self, *exc_info):
        return False

    async def _iter(self):
        for frame in self.frames:
            yield frame
        if self.exc is not None:
            raise self.exc
        if self.hang:
            await asyncio.Event().wait()


def _frame(seq: int, type_: str = "text.delta", **data) -> str:
    return json.dumps({"seq": seq, "ts": "t", "type": type_, "data": data})


class _Script:
    """``open_socket`` over a list of connections; exhausted → refused."""

    def __init__(self, sockets) -> None:
        self.sockets = list(sockets)
        self.opened: list[int | None] = []

    def __call__(self, after_seq):
        self.opened.append(after_seq)
        if not self.sockets:
            raise ConnectionRefusedError("down")
        return self.sockets.pop(0)


class _Clock:
    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _subscription(script, **kw) -> tuple[RemoteSubscription, list[str], list[float]]:
    states: list[str] = []
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        await asyncio.sleep(0)

    kw.setdefault("clock", _Clock(1.0))
    policy_fields = {k: kw.pop(k) for k in ("lost_after", "backoff", "clock") if k in kw}
    policy = ReconnectPolicy(sleep=sleep, **policy_fields)
    sub = RemoteSubscription(script, on_state=states.append, policy=policy, **kw)
    return sub, states, sleeps


async def _next(sub: RemoteSubscription) -> Event:
    event = await sub.next_event(timeout=2.0)
    assert event is not None, "feed delivered nothing"
    return event


async def _wait_state(sub: RemoteSubscription, state: str) -> None:
    for _ in range(500):
        if sub.state == state:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"state stayed {sub.state}, wanted {state}")


async def test_subscription_delivers_in_order_and_reconnects_after_the_last_seq():
    script = _Script(
        [
            _Socket([_frame(1, text="a"), _frame(2, text="b")]),  # clean close
            _Socket([_frame(3, text="c")], exc=OSError("reset")),
            _Socket([_frame(4, type="session.status", status="idle")], hang=True),
        ]
    )
    observed: list[int] = []
    sub, states, sleeps = _subscription(
        script, after_seq=0, on_event=lambda e: observed.append(e.seq)
    )
    try:
        seqs = [(await _next(sub)).seq for _ in range(4)]
        assert seqs == [1, 2, 3, 4] and observed == seqs
        assert sub.last_seq == 4
        # Every reconnect resumes at the last delivered seq, not the original.
        assert script.opened == [0, 2, 3]
        assert states == ["connected", "reconnecting", "connected", "reconnecting", "connected"]
        assert sleeps == [0.5, 0.5]  # a fresh outage restarts the backoff
        assert await sub.next_event(timeout=0.01) is None
    finally:
        sub.close()


async def test_subscription_turns_a_seq_regression_into_a_stream_gap():
    script = _Script([_Socket([_frame(7, text="a"), _frame(2, text="restarted")], hang=True)])
    sub, _states, _sleeps = _subscription(script)
    try:
        first = await _next(sub)
        gap = await _next(sub)
        replay = await _next(sub)
        assert first.seq == 7
        assert gap.type == "stream.gap" and gap.data == {"resync": "history"}
        assert replay.seq == 2 and sub.last_seq == 2
    finally:
        sub.close()


async def test_subscription_drops_unreadable_frames():
    script = _Script([_Socket(["not json", json.dumps({"type": "x"}), _frame(1)], hang=True)])
    sub, _s, _z = _subscription(script)
    try:
        assert (await _next(sub)).seq == 1
    finally:
        sub.close()


async def test_subscription_gives_up_after_lost_after_with_capped_backoff():
    script = _Script([])  # never connects
    sub, states, sleeps = _subscription(script, lost_after=60.0, clock=_Clock(10.0))
    await _wait_state(sub, "lost")
    assert states == ["reconnecting", "lost"]
    # 0.5 → 1 → 2 → 4 → 8 → 8 (capped); the clock steps 10s per check, so the
    # 60s budget allows six attempts before the seventh check trips it.
    assert sleeps == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]
    assert sub._task.done()


async def test_subscription_lost_at_once_on_a_fatal_close_code():
    exc = RuntimeError("closed")
    exc.rcvd = SimpleNamespace(code=4404)  # type: ignore[attr-defined]
    script = _Script([_Socket([], exc=exc), _Socket([_frame(1)])])
    sub, states, sleeps = _subscription(script)
    await _wait_state(sub, "lost")
    assert states == ["connected", "lost"]
    assert sleeps == [] and len(script.opened) == 1


async def test_subscription_lost_at_once_on_a_rejected_handshake():
    """uvicorn turns a close-before-accept into an HTTP status on the
    upgrade; websockets raises with ``.response.status_code``."""
    exc = RuntimeError("rejected")
    exc.response = SimpleNamespace(status_code=403)  # type: ignore[attr-defined]

    def open_socket(after_seq):
        raise exc

    sub, states, sleeps = _subscription(open_socket)
    await _wait_state(sub, "lost")
    assert states == ["lost"] and sleeps == []


async def test_subscription_close_cancels_the_driver():
    script = _Script([_Socket([_frame(1)], hang=True)])
    sub, _s, _z = _subscription(script)
    await sub.next_event()
    sub.close()
    with pytest.raises(asyncio.CancelledError):
        await sub._task
