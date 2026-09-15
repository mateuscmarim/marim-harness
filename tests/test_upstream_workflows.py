"""Workflow integration contracts: upstream execution, Marim authorization/state."""

import asyncio
import json
import time

import pytest
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from marim_harness.workflows.integration import WorkflowIntegration
from marim_harness.workflows.invocation import current_workflow
from marim_harness.workspace.agents import AgentDef
from tests.conftest import _make_deps


@pytest.mark.anyio
async def test_short_wall_deadline_also_bounds_compute_before_first_await(tmp_path, monkeypatch):
    import marim_harness.workflows.integration as integration_module

    monkeypatch.setattr(integration_module, "COMPUTE_TIMEOUT_SECS", 0.5)

    async def spawn(*args):
        pytest.fail("compute-only script dispatched")

    _, _, call = await _workflow(tmp_path, spawn, timeout_secs=0.01)
    started = time.monotonic()
    with pytest.raises(ModelRetry):
        await call("while True:\n    pass")
    assert time.monotonic() - started < 0.3


def _role(name="worker"):
    return AgentDef(name, "Reports on a task", "Do the task", frozenset(), "test")


async def _workflow(tmp_path, spawn, **kwargs):
    deps = _make_deps(tmp_path)
    integration = WorkflowIntegration(deps, spawn, lambda: [_role()], **kwargs)
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage(), tool_call_id="wf1")
    toolset = await integration.toolset().for_run(ctx)
    tool = (await toolset.get_tools(ctx))["run_workflow"]

    async def call(code):
        return await toolset.call_tool("run_workflow", {"code": code}, ctx, tool)

    return integration, deps, call


@pytest.mark.anyio
async def test_upstream_fanout_reaches_runner_with_child_ids(tmp_path):
    calls = []
    started = asyncio.Event()

    async def spawn(role, task, stream_id, *args, **kwargs):
        calls.append((role, task, stream_id, kwargs))
        if len(calls) == 2:
            started.set()
        await asyncio.wait_for(started.wait(), 2)
        return task.upper()

    _, _, call = await _workflow(tmp_path, spawn)
    result = await call('import asyncio\nawait asyncio.gather(worker(task="a"), worker(task="b"))')
    assert json.loads(result) == ["A", "B"]
    assert [c[2] for c in calls] == ["wf1::wf1", "wf1::wf2"]


@pytest.mark.anyio
async def test_workflow_deadline_cancels_and_drains_child(tmp_path):
    cleaned = asyncio.Event()

    async def spawn(*args, **kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    _, _, call = await _workflow(tmp_path, spawn, timeout_secs=0.05)
    result = await call('await worker(task="slow")')
    assert "timed out" in result
    assert cleaned.is_set()


@pytest.mark.anyio
async def test_disable_stale_call_does_not_dispatch(tmp_path):
    async def spawn(*args, **kwargs):
        pytest.fail("disabled workflow dispatched")

    integration, _, call = await _workflow(tmp_path, spawn)
    integration.enabled = False
    assert "unavailable" in await call('await worker(task="no")')


@pytest.mark.anyio
async def test_repeated_interrupt_does_not_interrupt_child_cleanup(tmp_path):
    started = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()

    async def spawn(*args, **kwargs):
        started.set()
        try:
            await asyncio.sleep(10)
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    _, _, call = await _workflow(tmp_path, spawn)
    running = asyncio.create_task(call('await worker(task="slow")'))
    await asyncio.wait_for(started.wait(), 2)
    running.cancel()
    await asyncio.wait_for(cleaning.wait(), 2)
    running.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert cleaned.is_set()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "code",
    [
        'await worker("positional")',
        'await missing(task="unknown")',
        'r = await worker(task="text")\nr["bad key"]',
        "def broken(: pass",
        "import socket\nsocket.socket()",
        'open("/etc/passwd").read()',
        'import os\nos.getenv("HOME")',
    ],
)
async def test_bad_scripts_and_host_access_do_not_dispatch(tmp_path, code):
    async def spawn(*args):
        pytest.fail("Bad script dispatched a worker")

    _, deps, call = await _workflow(tmp_path, spawn)
    done = []
    deps.ui.on_workflow_done = lambda *args: done.append(args)
    with pytest.raises(ModelRetry):
        await call(code)
    assert len(done) == 1 and done[0][2] is True
    assert current_workflow.get() is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ('{"answer": 1 + 1}', {"answer": 2}),
        ("x = 1", {}),
        ('print("debug")\n42', {"output": "debug\n", "result": 42}),
        ('print("debug")', {"output": "debug\n"}),
        ("False", False),
    ],
)
async def test_upstream_result_envelopes_finalize_cards(tmp_path, code, expected):
    async def spawn(*args):
        pytest.fail("Computation-only script dispatched a worker")

    _, deps, call = await _workflow(tmp_path, spawn)
    events = []
    deps.ui.on_workflow_start = lambda *args: events.append(("start", args))
    deps.ui.on_workflow_done = lambda *args: events.append(("done", args))
    assert json.loads(await call(code)) == expected
    assert [event[0] for event in events] == ["start", "done"]
    assert events[-1][1][2] is False


@pytest.mark.anyio
@pytest.mark.parametrize("scratchpad", [True, False])
async def test_large_results_preserve_full_output_and_absolute_pointer(tmp_path, scratchpad):
    from marim_harness.tools.impl.offload import find_offload_paths

    async def spawn(*args):
        return "unused"

    _, deps, call = await _workflow(tmp_path, spawn)
    scratch = tmp_path / "scratch"
    if scratchpad:
        scratch.mkdir()
        deps.services.get_scratchpad = lambda: scratch
    out = await call('["x" * 100] * 500')
    base = scratch / "workflow-output" if scratchpad else tmp_path / ".marim/workflow-output"
    path = base / "wf1.json"
    assert find_offload_paths(out) == [str(path)]
    assert len(out) <= 24_000
    assert json.loads(path.read_text()) == ["x" * 100] * 500


@pytest.mark.anyio
async def test_rendering_failures_do_not_discard_or_replay_work(tmp_path):
    calls = []

    async def spawn(*args):
        calls.append(args)
        return "paid-for result"

    def broken(*args):
        raise RuntimeError("rendering failed")

    _, deps, call = await _workflow(tmp_path, spawn)
    deps.ui.on_workflow_start = broken
    deps.ui.on_workflow_done = broken
    assert json.loads(await call('await worker(task="work")')) == "paid-for result"
    assert len(calls) == 1


@pytest.mark.anyio
async def test_failure_after_completed_work_retains_preview_without_replay(tmp_path):
    calls = []

    async def spawn(*args):
        calls.append(args)
        return "ALREADY_DONE"

    _, _, call = await _workflow(tmp_path, spawn)
    with pytest.raises(ModelRetry, match="ALREADY_DONE"):
        await call('await worker(task="mutate once")\nraise ValueError("late failure")')
    assert len(calls) == 1


@pytest.mark.anyio
async def test_upstream_call_budget_is_shared_until_a_new_run(tmp_path):
    calls = []

    async def spawn(*args):
        calls.append(args)
        return "ok"

    integration, deps, call = await _workflow(tmp_path, spawn)
    finished = []
    deps.ui.on_workflow_done = lambda *args: finished.append(args)
    await call('import asyncio\nawait asyncio.gather(*[worker(task="one") for _ in range(49)])')
    terminal = json.loads(await call('await worker(task="50")\nawait worker(task="51")'))
    assert "budget" in terminal["error"] and len(calls) == 50
    assert finished[-1][2] is True
    terminal = json.loads(await call('await worker(task="52")'))
    assert "budget" in terminal["error"] and len(calls) == 50
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage(), tool_call_id="new-run")
    toolset = await integration.toolset().for_run(ctx)
    tool = (await toolset.get_tools(ctx))["run_workflow"]
    await toolset.call_tool("run_workflow", {"code": 'await worker(task="new")'}, ctx, tool)
    assert len(calls) == 51


@pytest.mark.anyio
async def test_compute_cap_excludes_child_wait_and_stops_spin(tmp_path, monkeypatch):
    import marim_harness.workflows.integration as module

    monkeypatch.setattr(module, "COMPUTE_TIMEOUT_SECS", 0.02)

    async def spawn(*args):
        await asyncio.sleep(0.05)
        return "ok"

    _, _, call = await _workflow(tmp_path, spawn)
    assert json.loads(await call('await worker(task="wait")')) == "ok"
    with pytest.raises(ModelRetry):
        await asyncio.wait_for(call("while True:\n    pass"), 5)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_configured_deadline_fails_before_execution(tmp_path, timeout):
    with pytest.raises(ValueError, match="positive finite"):
        WorkflowIntegration(_make_deps(tmp_path), None, lambda: [], timeout_secs=timeout)


@pytest.mark.anyio
@pytest.mark.parametrize("error", [None, "a report field, not an execution error"])
async def test_result_error_field_does_not_mark_successful_workflow_failed(tmp_path, error):
    async def spawn(*args):
        return "ok"

    _, deps, call = await _workflow(tmp_path, spawn)
    finished = []
    deps.ui.on_workflow_done = lambda *args: finished.append(args)
    report = {"error": error, "result": "success"}
    assert json.loads(await call(repr(report))) == report
    assert finished[-1][2] is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ('b"abc"', "abc"),
        ("{1}", [1]),
        ("import datetime\ndatetime.date(2026, 9, 15)", "2026-09-15"),
        (
            'import datetime\nprint("date")\ndatetime.date(2026, 9, 15)',
            {"output": "date\n", "result": "2026-09-15"},
        ),
    ],
)
async def test_upstream_native_values_use_pydantic_tool_serialization(tmp_path, code, expected):
    async def spawn(*args):
        pytest.fail("data-only script dispatched")

    _, _, call = await _workflow(tmp_path, spawn)
    assert json.loads(await call(code)) == expected


@pytest.mark.anyio
async def test_large_converted_result_spills_full_json(tmp_path):
    async def spawn(*args):
        pytest.fail("data-only script dispatched")

    _, _, call = await _workflow(tmp_path, spawn)
    result = await call('b"x" * 30000')
    path = tmp_path / ".marim/workflow-output/wf1.json"
    assert str(path) in result and len(result) <= 24_000
    assert json.loads(path.read_text()) == "x" * 30000
