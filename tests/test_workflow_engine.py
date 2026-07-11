import asyncio

import pytest

from marim_harness.workflows.engine import WorkflowEngine
from tests.conftest import _make_deps


def _engine(tmp_path, spawn, **kw):
    deps = _make_deps(tmp_path)
    return WorkflowEngine(deps, spawn, **kw), deps


async def _echo_spawn(type, task, stream_id, mcp_names, max_output_chars,
                      model, isolation, caller_depth):
    await asyncio.sleep(0)
    return f"[{type}@{caller_depth}] {task}"


@pytest.mark.anyio
async def test_last_expression_is_the_tool_result(tmp_path):
    eng, _ = _engine(tmp_path, _echo_spawn)
    out = await eng.run('{"answer": 1 + 1}', None, "tc1")
    assert '"answer": 2' in out


@pytest.mark.anyio
async def test_agent_calls_reach_the_spawner_with_synth_stream_ids(tmp_path):
    seen: list[tuple] = []

    async def spawn(*a):
        seen.append(a)
        return "report"

    eng, _ = _engine(tmp_path, spawn)
    script = 'r = await agent("do x", type="explore")\nr'
    out = await eng.run(script, None, "tc1")
    assert '"report"' in out or "report" in out
    (type_, task, stream_id, mcp, cap, model, iso, depth) = seen[0]
    assert type_ == "explore" and task == "do x"
    assert stream_id == "tc1::wf1"
    assert mcp is None and depth == 0


@pytest.mark.anyio
async def test_gather_fans_out_concurrently(tmp_path):
    running = 0
    peak = 0

    async def spawn(type, task, *rest):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return task

    eng, _ = _engine(tmp_path, spawn)
    script = (
        "import asyncio\n"
        'results = await asyncio.gather(*[agent(d) for d in ["a", "b", "c"]])\n'
        "results"
    )
    out = await eng.run(script, None, "tc1")
    assert peak == 3
    assert '"a"' in out and '"c"' in out


@pytest.mark.anyio
async def test_args_are_injected(tmp_path):
    eng, _ = _engine(tmp_path, _echo_spawn)
    out = await eng.run("args['target']", {"target": "src/"}, "tc1")
    assert "src/" in out


@pytest.mark.anyio
async def test_oversized_result_spills_to_workspace_file(tmp_path):
    eng, deps = _engine(tmp_path, _echo_spawn)
    out = await eng.run('["x" * 100] * 500', None, "tc9")
    assert ".marim/workflow-output/tc9.json" in out
    assert (deps.workspace.root / ".marim/workflow-output/tc9.json").exists()


@pytest.mark.anyio
async def test_on_workflow_spawn_fires_before_each_child(tmp_path):
    announced: list[tuple] = []

    async def on_spawn(stream_id, type_, task, parent):
        announced.append((stream_id, type_, task, parent))

    eng, deps = _engine(tmp_path, _echo_spawn)
    deps.ui.on_workflow_spawn = on_spawn
    script = (
        "import asyncio\n"
        'await asyncio.gather(agent("t1"), agent("t2"))\n'
        '"done"'
    )
    await eng.run(script, None, "tcX")
    ids = sorted(a[0] for a in announced)
    assert ids == ["tcX::wf1", "tcX::wf2"]
    assert all(a[3] == "tcX" for a in announced)
