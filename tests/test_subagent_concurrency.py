"""Sub-agent fan-out concurrency cap.

When the orchestrator fans out many spawns at once, every spawn fires a model
request concurrently — which is exactly what trips an upstream provider's
rate limit on a shared route. ``SubagentRunner(concurrency=N)`` bounds how many
native model requests and external CLI runs execute at once; the rest queue.
Native tools do not hold capacity while waiting for children. ``concurrency=None``
(the default) keeps the old unbounded behaviour.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.usage import RunUsage

from marim_harness.subagents import SubagentRunner
from marim_harness.workspace.agents import AgentDef
from tests.conftest import _make_deps, _make_harness


def _tracking_model(
    active: dict, started: asyncio.Queue | None = None, release: asyncio.Event | None = None
) -> FunctionModel:
    """A model that records how many runs are inside it at once."""

    async def fn(messages, info):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        if started is not None:
            started.put_nowait(None)
        try:
            if release is not None:
                await release.wait()
            return ModelResponse(parts=[TextPart(content="ok")])
        finally:
            active["now"] -= 1

    return FunctionModel(fn)


def _runner_with_concurrency(tmp_path: Path, model, concurrency: int | None):
    base = _make_harness(model, _make_deps(tmp_path)).subagents
    return SubagentRunner(
        base.provider,
        base.mcp,
        base.deps,
        base.hooks,
        base.session,
        get_model=base._get_model,
        concurrency=concurrency,
    )


async def _assert_concurrency(
    tmp_path: Path, concurrency: int | None, expected: int, models: list[str | None] | None = None
):
    active = {"now": 0, "max": 0}
    started = asyncio.Queue()
    release = asyncio.Event()
    runner = _runner_with_concurrency(
        tmp_path, _tracking_model(active, started, release), concurrency=concurrency
    )
    runner._build_model = lambda _: _tracking_model(active, started, release)
    tasks = [
        asyncio.create_task(runner.run("explore", f"t{i}", stream_id=f"s{i}", model=model))
        for i, model in enumerate(models or [None] * 5)
    ]

    async def ready():
        for _ in range(expected):
            await started.get()
        # Every excess request must be queued before any in-flight request can
        # finish, so slow startup cannot hide a missing cap or fake a low peak.
        if concurrency is not None:
            await _wait_for_queue(runner, len(tasks) - expected)

    try:
        # The timeout detects deadlock (e.g. an erroneously lower cap); elapsed
        # time never determines whether requests overlap.
        await asyncio.wait_for(ready(), timeout=10)
        assert active["now"] == expected
        release.set()
        await asyncio.gather(*tasks)
        assert active["max"] == expected
        assert active["now"] == 0
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.anyio
async def test_concurrency_cap_bounds_simultaneous_spawns(tmp_path: Path):
    await _assert_concurrency(tmp_path, concurrency=2, expected=2)


@pytest.mark.anyio
async def test_unbounded_when_uncapped(tmp_path: Path):
    await _assert_concurrency(tmp_path, concurrency=None, expected=5)


@pytest.mark.anyio
@pytest.mark.parametrize("concurrency", [1, 2])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("background", [False, True])
async def test_nested_spawns_complete_when_all_parent_slots_are_occupied(
    tmp_path, concurrency, streaming, background
):
    """Every parent requests children only after all parent requests are in flight."""
    parents_ready = asyncio.Event()
    parents = 0
    children = 0
    active = 0
    peak = 0

    async def fn(messages, info):
        nonlocal parents, children, active, peak
        active += 1
        peak = max(peak, active)
        try:
            returns = [
                part.content
                for message in messages
                for part in message.parts
                if isinstance(part, ToolReturnPart) and part.tool_name == "spawn_agent"
            ]
            if returns:
                return ModelResponse(parts=[TextPart(content=";".join(returns))])
            if "spawn_agent" not in {tool.name for tool in info.function_tools}:
                children += 1
                await asyncio.sleep(0)
                return ModelResponse(parts=[TextPart(content="child done")])
            parents += 1
            if parents == concurrency:
                parents_ready.set()
            await parents_ready.wait()
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "spawn_agent",
                        {"type": "explore", "task": f"child {i}"},
                    )
                    for i in range(2)
                ]
            )
        finally:
            active -= 1

    async def stream(messages, info):
        response = await fn(messages, info)
        for i, part in enumerate(response.parts):
            if isinstance(part, TextPart):
                yield part.content
            else:
                yield {i: DeltaToolCall(name=part.tool_name, json_args=json.dumps(part.args))}

    deps = _make_deps(tmp_path)
    if streaming:

        async def on_event(*args):
            pass

        deps.ui.on_subagent_event = on_event
    harness = _make_harness(
        FunctionModel(fn, stream_function=stream), deps, subagent_concurrency=concurrency
    )
    run = harness.subagents.run_background if background else harness.subagents.run
    results = await asyncio.wait_for(
        asyncio.gather(
            *[run("explore", "parent", stream_id=f"parent-{i}") for i in range(concurrency)]
        ),
        timeout=3,
    )
    assert results == ["child done;child done"] * concurrency
    assert children == 2 * concurrency
    assert peak == concurrency
    assert active == 0


async def _wait_for_queue(runner, count):
    """Observe the public upstream queue counter; never use a delay to infer queueing."""

    async def wait():
        while runner._limiter.waiting_count != count:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=3)


@pytest.mark.anyio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("cancel_running", [False, True])
async def test_cancel_queued_spawn_preserves_running_request(tmp_path, streaming, cancel_running):
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    peak = 0

    async def stream(messages, info):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            yield "start "
            entered.set()
            await release.wait()
            yield "done"
        finally:
            active -= 1

    async def fn(messages, info):
        text = "".join([part async for part in stream(messages, info)])
        return ModelResponse(parts=[TextPart(content=text)])

    deps = _make_deps(tmp_path)
    if streaming:

        async def on_event(*args):
            pass

        deps.ui.on_subagent_event = on_event
    harness = _make_harness(FunctionModel(fn, stream_function=stream), deps, subagent_concurrency=1)
    runner = harness.subagents
    tasks = [asyncio.create_task(runner.run("explore", "first", stream_id="first"))]
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        tasks.append(asyncio.create_task(runner.run("explore", "queued", stream_id="queued")))
        await _wait_for_queue(runner, 1)
        tasks[1].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[1]
        assert active == 1
        assert runner._limiter.running_count == 1
        assert runner._limiter.waiting_count == 0
        tasks.append(asyncio.create_task(runner.run("explore", "later", stream_id="later")))
        await _wait_for_queue(runner, 1)
        if cancel_running:
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(tasks[0], timeout=3)
        release.set()
        if not cancel_running:
            assert await asyncio.wait_for(tasks[0], timeout=3) == "start done"
        assert await asyncio.wait_for(tasks[2], timeout=3) == "start done"
        assert peak == 1
        assert active == 0
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("background", [False, True])
async def test_cancel_parent_drains_running_and_queued_children(tmp_path, background):
    child_entered = asyncio.Event()
    child_stopped = asyncio.Event()
    release = asyncio.Event()

    async def fn(messages, info):
        returns = [
            part.content
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == "spawn_agent"
        ]
        if returns:
            return ModelResponse(parts=[TextPart(content=";".join(returns))])
        if "spawn_agent" in {tool.name for tool in info.function_tools}:
            return ModelResponse(
                parts=[
                    ToolCallPart("spawn_agent", {"type": "explore", "task": f"child {i}"})
                    for i in range(2)
                ]
            )
        child_entered.set()
        try:
            await release.wait()
            return ModelResponse(parts=[TextPart(content="child done")])
        finally:
            child_stopped.set()

    harness = _make_harness(FunctionModel(fn), _make_deps(tmp_path), subagent_concurrency=1)
    runner = harness.subagents
    run = runner.run_background if background else runner.run
    parent = asyncio.create_task(run("explore", "parent", stream_id="parent"))
    try:
        await asyncio.wait_for(child_entered.wait(), timeout=3)
        await _wait_for_queue(runner, 1)
        parent.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(parent, timeout=3)
        assert child_stopped.is_set()
        assert runner._limiter.running_count == 0
        assert runner._limiter.waiting_count == 0
        release.set()
        assert (
            await asyncio.wait_for(run("explore", "next parent", stream_id="next"), timeout=3)
            == "child done;child done"
        )
    finally:
        parent.cancel()
        await asyncio.gather(parent, return_exceptions=True)


@pytest.mark.anyio
async def test_model_overrides_share_request_pool(tmp_path):
    await _assert_concurrency(
        tmp_path, concurrency=2, expected=2, models=[None, "cheap", "other", None, "cheap"]
    )


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["claude-cli", "codex-cli"])
@pytest.mark.parametrize("cancel_cli", [False, True])
async def test_native_and_cli_spawns_share_pool(tmp_path, monkeypatch, backend, cancel_cli):
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    peak = 0

    async def operation():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        entered.set()
        try:
            await release.wait()
        finally:
            active -= 1

    async def fn(messages, info):
        await operation()
        return ModelResponse(parts=[TextPart(content="ok")])

    async def cli(*args, **kwargs):
        await operation()
        return SimpleNamespace(
            output="ok",
            transcript=[],
            usage=RunUsage(),
            child_transcripts={},
            session_id="cli-session",
            thread_id="cli-thread",
        )

    role = AgentDef("cli", "CLI worker", "Work", frozenset(), "test", backend=backend)
    harness = _make_harness(
        FunctionModel(fn), _make_deps(tmp_path), subagent_concurrency=1, extra_agents=(role,)
    )
    runner = harness.subagents
    monkeypatch.setattr(runner._cli, "run_cli", cli)
    monkeypatch.setattr(runner._codex, "run_codex", cli)
    tasks = [asyncio.create_task(runner.run("cli", "first", stream_id="first"))]
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        tasks.extend(
            asyncio.create_task(runner.run(role, "queued", stream_id=role))
            for role in ["explore", "cli"]
        )
        await _wait_for_queue(runner, 2)
        assert active == 1
        if cancel_cli:
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(tasks[0], timeout=3)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3)
        assert results[1:] == ["ok", "ok"]
        if not cancel_cli:
            assert results[0] == "ok"
        assert peak == 1
        assert active == 0
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_harness_config_threads_concurrency_to_the_runner(tmp_path: Path):
    """The HarnessConfig knob reaches the runner that enforces it."""
    deps = _make_deps(tmp_path)
    harness = _make_harness(_tracking_model({"now": 0, "max": 0}), deps, subagent_concurrency=4)
    assert harness.subagents._concurrency == 4


def test_default_harness_config_is_capped(tmp_path: Path):
    """An out-of-the-box HarnessConfig bounds fan-out at the shared default —
    a runaway workflow (one spawn per character of a mis-typed args string, in
    one live run) must queue, not fire everything at once. Explicit None stays
    the unbounded escape hatch, shared with the MARIM_SUBAGENT_CONCURRENCY=0
    env sentinel."""
    from marim_harness.config.model import DEFAULT_SUBAGENT_CONCURRENCY

    deps = _make_deps(tmp_path)
    harness = _make_harness(_tracking_model({"now": 0, "max": 0}), deps)
    assert harness.subagents._concurrency == DEFAULT_SUBAGENT_CONCURRENCY

    deps2 = _make_deps(tmp_path / "w2")
    unbounded = _make_harness(
        _tracking_model({"now": 0, "max": 0}), deps2, subagent_concurrency=None
    )
    assert unbounded.subagents._concurrency is None
