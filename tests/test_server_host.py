"""SessionHost: the server-side implementation of the bind_ui contract —
turn queue, parked asks, interrupt, steer — observed through the event bus."""

import asyncio
import inspect
import json as _json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, PartStartEvent, TextPart, ToolCallPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.usage import RunUsage

from marim_harness.ask_user import Choice
from marim_harness.runtime.deps import Deps, PlanDecision, UIHooks, WorkspaceConfig
from marim_harness.runtime.harness import Harness
from marim_harness.runtime.permissions import Mode
from marim_harness.server.bus import EventBus
from marim_harness.server.host import SessionHost, TurnQueueFull
from marim_harness.tools.provider import BuiltinToolProvider

pytestmark = pytest.mark.anyio

_UI_HOOK_FIELDS = {
    "request_approval",
    "ask_user",
    "on_subagent_event",
    "on_subagent_notice",
    "on_subagent_model",
    "on_subagent_usage",
    "detach_fanout",
    "interactive",
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
    return Harness(
        model=model,
        provider=BuiltinToolProvider(),
        deps=deps,
        instructions="You are a coding agent.",
        **config_kwargs,
    )


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
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="edit_file",
                        args={
                            "path": "a.txt",
                            "edits": [{"old_string": "foo", "new_string": "bar"}],
                        },
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="done")])

    stream_state = {"n": 0}

    async def stream_fn(messages, info):
        stream_state["n"] += 1
        if stream_state["n"] == 1:
            yield {
                0: DeltaToolCall(
                    name="read_file",
                    json_args=_json.dumps({"path": "a.txt"}),
                    tool_call_id="tc-read-1",
                )
            }
        elif stream_state["n"] == 2:
            yield {
                0: DeltaToolCall(
                    name="edit_file",
                    json_args=_json.dumps(
                        {"path": "a.txt", "edits": [{"old_string": "foo", "new_string": "bar"}]}
                    ),
                    tool_call_id="tc-edit-1",
                )
            }
        else:
            yield "done"

    return FunctionModel(fn, stream_function=stream_fn)


# Wall-clock budget for the polling helpers below. Deliberately generous, because
# it only bounds the *failing* path: a wait that is going to succeed returns as
# soon as its predicate flips, so raising this costs a passing run nothing. The
# old 5s budget was tight enough that runner load alone could break it — this
# suite finishes in ~50s on a developer machine but has taken ~295s on the CI
# 3.12 leg, and at that ~6x dilation a genuinely-correct wake arrived after the
# deadline. What actually catches a real hang is pytest-timeout's 120s per-test
# ceiling (see [tool.pytest.ini_options] in pyproject.toml), which kills the test
# with a stack dump instead of silently reporting "not reached in time".
_WAIT_TIMEOUT = float(os.environ.get("MARIM_TEST_WAIT_TIMEOUT", "30"))


async def _wait_for(
    predicate, timeout: float | None = None, what: str | Callable[[], str] = "condition"
):
    """Poll ``predicate`` until it is true or the budget expires.

    ``what`` describes what is being awaited and may be a callable, resolved only
    on failure — that lets a caller quote live state (the events seen so far) in
    the message without paying to build it on every successful wait.
    """
    # `is None`, not `or`: an explicit timeout=0 is a legitimate "fail on the first
    # unmet check" and must not be swallowed as unset.
    deadline = asyncio.get_running_loop().time() + (_WAIT_TIMEOUT if timeout is None else timeout)
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"{what() if callable(what) else what} not reached in time")
        await asyncio.sleep(0.01)


async def _drain_until(bus_events: list, type_: str, timeout: float | None = None):
    await _wait_for(
        lambda: any(e.type == type_ for e in bus_events),
        timeout,
        # Built lazily: the list is still filling while we wait, so a snapshot
        # taken at call time would always read as empty.
        what=lambda: f"event {type_!r} (saw {[e.type for e in bus_events]})",
    )
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


async def test_wait_for_timeout_reports_observed_state():
    """A timed-out wait must say what it saw, not just that it gave up.

    The bare "condition not reached in time" message cost real diagnosis time on a
    CI failure: it named neither the awaited condition nor the events that *did*
    arrive. Both forms of ``what`` are covered here, including that the callable is
    resolved at failure time rather than at call time.
    """
    seen = ["turn.started", "turn.finished"]
    with pytest.raises(AssertionError, match=r"event 'wake' \(saw \['turn.started'"):
        await _wait_for(lambda: False, timeout=0.0, what=lambda: f"event 'wake' (saw {seen})")
    with pytest.raises(AssertionError, match="host idle not reached in time"):
        await _wait_for(lambda: False, timeout=0.0, what="host idle")


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
    # The live running total rides the wire as turn.usage — published on change
    # only, so a one-response turn yields a single one, ahead of its stream.
    usages = [e for e in events if e.type == "turn.usage"]
    assert len(usages) == 1
    assert usages[0].data == {"turn_id": turn_id, "total_tokens": usages[0].data["total_tokens"]}
    assert events.index(usages[0]) < events.index(next(e for e in events if e.type == "text.delta"))
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
    parked = next(e for e in events if e.type == "ask.pending").data["id"]
    assert host.interrupt()
    finished = await _drain_until(events, "turn.finished")
    assert finished.data.get("interrupted") is True
    await _wait_for(lambda: host.status == "idle")
    assert host.pending_asks() == []
    # The parked ask must be announced as cancelled — exactly once — or a
    # client's approval panel outlives the turn it belonged to.
    resolved = [e.data for e in events if e.type == "ask.resolved"]
    assert resolved == [{"id": parked, "cancelled": True, "reason": "interrupted"}]
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


async def _settling_job(host, *, label="explore: probe", result="job result"):
    """Register a background job on the host that finishes immediately, so its
    settle drives the on_jobs_changed -> maybe_wake path."""

    async def work():
        return result

    return host.harness.deps.jobs.register("agent", label, work())


async def test_settled_job_wakes_idle_session_with_autonomous_trigger(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.auto)
    host = SessionHost(_make_harness(_text_only_model(), deps), EventBus())
    events = _spy(host.bus)
    await _settling_job(host)
    started = await _drain_until(events, "turn.started")
    assert started.data["trigger"] == "autonomous"
    assert started.data["prompt"] == ""
    await _drain_until(events, "turn.finished")
    await _wait_for(lambda: host.status == "idle")
    autonomous_starts = [
        e for e in events if e.type == "turn.started" and e.data.get("trigger") == "autonomous"
    ]
    assert len(autonomous_starts) == 1  # turn-end trigger must not double-fire
    await host.aclose()


async def test_wake_disabled_in_serve_does_not_fire(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.auto)
    host = SessionHost(_make_harness(_text_only_model(), deps), EventBus())
    host.harness.autonomous_wake = False  # runtime toggle; is_enabled reads it live
    events = _spy(host.bus)
    await _settling_job(host)
    await _wait_for(lambda: not host.harness.deps.jobs.any_running())
    await asyncio.sleep(0.05)  # give any erroneous wake a chance to enqueue
    assert [
        e for e in events if e.type == "turn.started" and e.data.get("trigger") == "autonomous"
    ] == []
    await _wait_for(lambda: host.status == "idle")
    await host.aclose()


async def test_user_turn_carries_user_trigger(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.auto)
    host = SessionHost(_make_harness(_text_only_model(), deps), EventBus())
    events = _spy(host.bus)
    host.submit("hi")
    started = await _drain_until(events, "turn.started")
    assert started.data["trigger"] == "user"
    await _wait_for(lambda: host.status == "idle")
    await host.aclose()


async def test_job_settled_mid_turn_wakes_after_turn_ends(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.auto)
    release = asyncio.Event()

    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="done")])

    async def stream_fn(messages, info):
        await release.wait()
        yield "done"

    host = SessionHost(
        _make_harness(FunctionModel(fn, stream_function=stream_fn), deps), EventBus()
    )
    events = _spy(host.bus)
    host.submit("do work")  # user turn starts, blocks
    await _drain_until(events, "turn.started")  # (the user turn)
    await _settling_job(host)  # settles WHILE the turn is busy
    await asyncio.sleep(0.05)
    assert [
        e for e in events if e.type == "turn.started" and e.data.get("trigger") == "autonomous"
    ] == []
    release.set()  # let the user turn finish
    await _wait_for(
        lambda: any(
            e.type == "turn.started" and e.data.get("trigger") == "autonomous" for e in events
        ),
        what=lambda: (
            f"autonomous turn.started after the user turn (saw "
            f"{[(e.type, e.data.get('trigger')) for e in events]})"
        ),
    )
    await _wait_for(lambda: host.status == "idle", what="host idle")
    await host.aclose()


async def test_aclose_releases_the_claim_even_when_teardown_raises(tmp_path):
    """The release must be unconditional, not merely last. `await self._worker`
    only suppresses CancelledError, and the worker's own finally block can
    raise — if that escape skipped the release, the claim would be held by an fd
    nothing references (the host is already popped from the supervisor) and the
    session would 409 for the daemon's whole life."""
    from marim_harness.session.claim import try_acquire

    session_path = tmp_path / "sessions" / "s1.json"
    session_path.parent.mkdir(parents=True)
    claim = try_acquire(session_path, kind="daemon")
    assert claim is not None

    deps = _make_deps(tmp_path, mode=Mode.auto)
    host = SessionHost(_make_harness(_text_only_model(), deps), EventBus(), claim=claim)

    # Swap the real worker for one that explodes out of its own finally.
    host._worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await host._worker

    async def exploding_worker():
        try:
            await asyncio.sleep(3600)
        finally:
            raise RuntimeError("worker finally exploded")

    host._worker = asyncio.create_task(exploding_worker())
    await asyncio.sleep(0)  # let it reach the sleep

    with pytest.raises(RuntimeError, match="worker finally exploded"):
        await host.aclose()

    # Propagated, but only after the session was freed.
    freed = try_acquire(session_path, kind="tui")
    assert freed is not None
    freed.release()


# --------------------------------------------------------------------------
# The full bind_ui vocabulary: every named param the harness exposes must be
# wired by the host, or a hook silently goes dead (only the daemon exercises
# these, so nothing else would notice a missed one).
# --------------------------------------------------------------------------


def _bound_ui_hook(harness: Harness, name: str):
    """Where each bind_ui param actually lands: most go straight onto
    deps.ui, but on_tasks_changed/on_jobs_changed and the session-lifecycle
    hooks (on_compact*, on_notice, on_rename) are stored on deps.tasks/
    deps.jobs/session instead (see Harness.bind_ui)."""
    if name in ("on_tasks_changed",):
        return harness.deps.tasks.on_change
    if name in ("on_jobs_changed",):
        return harness.deps.jobs.on_change
    if name in ("on_compact", "on_compact_start", "on_notice", "on_rename"):
        return getattr(harness.session, name)
    return getattr(harness.deps.ui, name)


async def test_every_bind_ui_param_is_wired_by_the_host(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.auto)
    harness = _make_harness(_text_only_model(), deps)
    host = SessionHost(harness, EventBus())

    params = [p for p in inspect.signature(Harness.bind_ui).parameters if p != "self"]
    assert params  # sanity: bind_ui still takes named params
    for name in params:
        assert _bound_ui_hook(harness, name) is not None, name
    await host.aclose()


async def test_workflow_events_published(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.auto)
    harness = _make_harness(_text_only_model(), deps)
    host = SessionHost(harness, EventBus())
    events = _spy(host.bus)
    ui = harness.deps.ui
    assert ui.on_workflow_spawn is not None
    assert ui.on_workflow_start is not None
    assert ui.on_workflow_log is not None
    assert ui.on_workflow_spawn_done is not None
    assert ui.on_workflow_done is not None

    await ui.on_workflow_spawn("s1", "agent", "do the thing", "tc-parent")
    ui.on_workflow_start("tc-1", "My Workflow")
    ui.on_workflow_log("tc-1", "step one")
    ui.on_workflow_spawn_done("s1", "report text")
    ui.on_workflow_done("tc-1", "success", False)

    spawned = next(e for e in events if e.type == "workflow.spawned")
    assert spawned.data == {
        "stream_id": "s1",
        "spawn_type": "agent",
        "task": "do the thing",
        "parent_tool_call_id": "tc-parent",
    }
    started = next(e for e in events if e.type == "workflow.started")
    assert started.data == {"tool_call_id": "tc-1", "title": "My Workflow"}
    logged = next(e for e in events if e.type == "workflow.logged")
    assert logged.data == {"tool_call_id": "tc-1", "message": "step one"}
    spawn_done = next(e for e in events if e.type == "workflow.spawn_finished")
    assert spawn_done.data == {"stream_id": "s1", "report": "report text"}
    finished = next(e for e in events if e.type == "workflow.finished")
    assert finished.data == {"tool_call_id": "tc-1", "outcome": "success", "failed": False}
    await host.aclose()


async def test_subagent_side_channels_published(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.auto)
    harness = _make_harness(_text_only_model(), deps)
    host = SessionHost(harness, EventBus())
    events = _spy(host.bus)
    ui = harness.deps.ui
    assert ui.on_subagent_notice is not None
    assert ui.on_subagent_model is not None
    assert ui.on_subagent_thinking is not None
    assert ui.on_subagent_usage is not None

    await ui.on_subagent_notice("s1", "retrying")
    await ui.on_subagent_model("s1", "opus")
    await ui.on_subagent_thinking("s1", "high")
    await ui.on_subagent_usage("s1", RunUsage(input_tokens=10, output_tokens=5))

    notice = next(e for e in events if e.type == "subagent.notice")
    assert notice.data == {"stream_id": "s1", "message": "retrying"}
    model = next(e for e in events if e.type == "subagent.model")
    assert model.data == {"stream_id": "s1", "model": "opus"}
    thinking = next(e for e in events if e.type == "subagent.thinking")
    assert thinking.data == {"stream_id": "s1", "level": "high"}
    usage_evt = next(e for e in events if e.type == "subagent.usage")
    assert usage_evt.data["stream_id"] == "s1"
    assert usage_evt.data["usage"]["input_tokens"] == 10
    assert usage_evt.data["usage"]["output_tokens"] == 5
    await host.aclose()


async def test_cli_activity_converts_events_to_wire(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.auto)
    harness = _make_harness(_text_only_model(), deps)
    host = SessionHost(harness, EventBus())
    events = _spy(host.bus)

    assert harness.deps.ui.on_cli_activity is not None
    stream_event = PartStartEvent(index=0, part=TextPart(content="hi"))
    await harness.deps.ui.on_cli_activity([stream_event])

    activity = next(e for e in events if e.type == "subagent.cli_activity")
    assert activity.data["events"], "expected at least one wire event"
    assert activity.data["events"][0]["type"] == "text.delta"
    await host.aclose()


async def test_cli_activity_drops_unconvertible_events_without_publishing_empty(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.auto)
    harness = _make_harness(_text_only_model(), deps)
    host = SessionHost(harness, EventBus())
    events = _spy(host.bus)

    assert harness.deps.ui.on_cli_activity is not None
    await harness.deps.ui.on_cli_activity([])  # no events converted -> nothing published
    assert not any(e.type == "subagent.cli_activity" for e in events)
    await host.aclose()


async def test_ttft_mode_and_notice_published(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.auto)
    harness = _make_harness(_text_only_model(), deps)
    host = SessionHost(harness, EventBus())
    events = _spy(host.bus)

    assert harness.deps.ui.on_ttft is not None
    assert harness.deps.ui.on_mode_change is not None
    assert harness.session.on_notice is not None
    harness.deps.ui.on_ttft(0.5)
    harness.deps.ui.on_mode_change()
    harness.session.on_notice("heads up")

    ttft = next(e for e in events if e.type == "session.ttft")
    assert ttft.data == {"seconds": 0.5}
    mode_evt = next(e for e in events if e.type == "session.mode_changed")
    assert mode_evt.data == {"mode": harness.deps.workspace.mode.value}
    notice = next(e for e in events if e.type == "session.notice")
    assert notice.data == {"message": "heads up"}
    await host.aclose()


async def test_present_plan_parks_and_resolves(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.auto)
    harness = _make_harness(_text_only_model(), deps)
    host = SessionHost(harness, EventBus())
    events = _spy(host.bus)

    assert harness.deps.ui.on_present_plan is not None
    task = asyncio.ensure_future(
        harness.deps.ui.on_present_plan("Sum", ["a"], [Choice(label="Now"), Choice(label="Later")])
    )
    pending = await _drain_until(events, "ask.pending")
    assert pending.data["kind"] == "plan"
    assert pending.data["payload"]["summary"] == "Sum"
    assert pending.data["payload"]["steps"] == ["a"]
    assert pending.data["payload"]["choices"] == [
        {"label": "Now", "description": None},
        {"label": "Later", "description": None},
    ]
    [ask] = host.pending_asks()
    assert host.answer_ask(ask["id"], {"choice": "Now", "feedback": None})
    decision = await task
    assert decision == PlanDecision(choice="Now", feedback=None)
    await host.aclose()


async def test_present_plan_cancel_maps_to_keep_planning(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.auto)
    harness = _make_harness(_text_only_model(), deps)
    host = SessionHost(harness, EventBus())

    assert harness.deps.ui.on_present_plan is not None
    task = asyncio.ensure_future(
        harness.deps.ui.on_present_plan("Sum", ["a"], [Choice(label="Now")])
    )
    await _wait_for(lambda: host.pending_asks() != [])
    [ask] = host.pending_asks()
    assert host.answer_ask(ask["id"], {"cancel": True})
    decision = await task
    assert decision == PlanDecision(choice="Keep planning")
    await host.aclose()


async def test_run_turn_publishes_lifecycle_and_returns_outcome(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.auto)
    harness = _make_harness(_text_only_model(), deps)
    host = SessionHost(harness, EventBus())
    events = _spy(host.bus)

    outcome = await host.run_turn("hi")

    assert outcome.result == "done"
    started = next(e for e in events if e.type == "turn.started")
    assert started.data["prompt"] == "hi"
    finished = next(e for e in events if e.type == "turn.finished")
    assert finished.data["output"] == "done"
    assert "usage" in finished.data
    assert harness.session.history  # persisted
    await host.aclose()


async def test_run_turn_error_publishes_and_raises(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.auto)

    def fn(messages, info):
        raise RuntimeError("boom")

    async def stream_fn(messages, info):
        yield "partial "
        raise RuntimeError("boom")

    harness = _make_harness(FunctionModel(fn, stream_function=stream_fn), deps)
    host = SessionHost(harness, EventBus())
    events = _spy(host.bus)

    with pytest.raises(RuntimeError, match="boom"):
        await host.run_turn("hi")

    error = next(e for e in events if e.type == "turn.error")
    assert "boom" in error.data["error"]
    assert not any(e.type == "turn.finished" for e in events)
    await host.aclose()


async def test_run_turn_rejects_concurrent_second_call(tmp_path):
    (tmp_path / "a.txt").write_text("foo\n")
    deps = _make_deps(tmp_path, mode=Mode.ask)
    harness = _make_harness(_edit_model(), deps)
    host = SessionHost(harness, EventBus())

    task = asyncio.create_task(host.run_turn("edit it"))
    await _wait_for(lambda: host.pending_asks() != [])
    with pytest.raises(RuntimeError, match="already in flight"):
        await host.run_turn("second")

    [ask] = host.pending_asks()
    assert host.answer_ask(ask["id"], {"approve": True, "reason": None})
    outcome = await task
    assert outcome.result == "done"
    await host.aclose()


async def test_run_turn_interrupt_publishes_finished_interrupted(tmp_path):
    (tmp_path / "a.txt").write_text("foo\n")
    deps = _make_deps(tmp_path, mode=Mode.ask)
    harness = _make_harness(_edit_model(), deps)
    host = SessionHost(harness, EventBus())
    events = _spy(host.bus)

    task = asyncio.create_task(host.run_turn("edit it"))
    await _wait_for(lambda: host.pending_asks() != [])
    parked = host.pending_asks()[0]["id"]
    assert host.interrupt()
    with pytest.raises(asyncio.CancelledError):
        await task

    finished = next(e for e in events if e.type == "turn.finished")
    assert finished.data.get("interrupted") is True
    resolved = [e.data for e in events if e.type == "ask.resolved"]
    assert resolved == [{"id": parked, "cancelled": True, "reason": "interrupted"}]
    await _wait_for(lambda: host.status == "idle")
    await host.aclose()
