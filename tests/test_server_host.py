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
