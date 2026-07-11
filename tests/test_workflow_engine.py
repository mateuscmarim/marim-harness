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


FINDINGS = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": {"type": "string"}}},
    "required": ["findings"],
}

SCHEMA_SCRIPT = (
    'r = await agent("review", type="explore", schema=' + repr(FINDINGS) + ")\n"
    'r["findings"]'
)


@pytest.mark.anyio
async def test_schema_valid_report_returns_a_dict_into_the_script(tmp_path):
    async def spawn(type, task, *rest):
        assert "Output contract" in task
        return '{"findings": ["bug in x"]}'

    eng, _ = _engine(tmp_path, spawn)
    out = await eng.run(SCHEMA_SCRIPT, None, "tc1")
    assert "bug in x" in out


@pytest.mark.anyio
async def test_schema_failure_respawns_once_with_the_validation_error(tmp_path):
    calls: list[str] = []

    async def spawn(type, task, *rest):
        calls.append(task)
        if len(calls) == 1:
            return "not json at all"
        return '{"findings": []}'

    eng, _ = _engine(tmp_path, spawn)
    out = await eng.run(SCHEMA_SCRIPT, None, "tc1")
    assert len(calls) == 2
    assert "failed validation" in calls[1]
    assert out == "[]"


@pytest.mark.anyio
async def test_schema_failure_after_retry_raises_into_the_script(tmp_path):
    async def spawn(type, task, *rest):
        return "still not json"

    eng, _ = _engine(tmp_path, spawn)
    script = (
        "try:\n"
        '    r = await agent("review", schema=' + repr(FINDINGS) + ")\n"
        "except Exception as e:\n"
        '    r = "caught: " + str(e)\n'
        "r"
    )
    out = await eng.run(script, None, "tc1")
    assert "caught:" in out and "schema validation" in out


@pytest.mark.anyio
async def test_syntax_error_returns_a_fixable_tool_message(tmp_path):
    eng, _ = _engine(tmp_path, _echo_spawn)
    out = await eng.run("def broken(:\n    pass", None, "tc1")
    assert "failed to parse" in out


@pytest.mark.anyio
async def test_uncaught_script_exception_names_the_line(tmp_path):
    eng, _ = _engine(tmp_path, _echo_spawn)
    out = await eng.run('x = 1\nraise ValueError("boom")\nx', None, "tc1")
    assert "Workflow script raised" in out and "boom" in out


@pytest.mark.anyio
async def test_agent_failure_is_catchable_in_script(tmp_path):
    async def spawn(*a):
        raise RuntimeError("spawn exploded")

    eng, _ = _engine(tmp_path, spawn)
    script = (
        "try:\n"
        '    r = await agent("x")\n'
        "except Exception as e:\n"
        '    r = "recovered: " + str(e)\n'
        "r"
    )
    out = await eng.run(script, None, "tc1")
    assert "recovered:" in out and "spawn exploded" in out


@pytest.mark.anyio
async def test_sandbox_denies_filesystem_and_imports(tmp_path):
    eng, _ = _engine(tmp_path, _echo_spawn)
    out = await eng.run('open("/etc/passwd").read()', None, "tc1")
    # Monty denies `open` (no OS access is configured on run_async); whether
    # it fails at parse or at run, it must surface as a tool-visible error,
    # never as file contents.
    assert "root:" not in out
    assert "raised" in out or "failed to parse" in out
    out2 = await eng.run("import socket\nsocket", None, "tc1")
    assert "raised" in out2 or "failed to parse" in out2


@pytest.mark.anyio
async def test_infinite_loop_is_killed_by_vm_limits(tmp_path, monkeypatch):
    import marim_harness.workflows.engine as engine_mod
    monkeypatch.setattr(engine_mod, "_VM_LIMITS", {"max_duration_secs": 0.5})
    eng, _ = _engine(tmp_path, _echo_spawn)
    out = await eng.run("while True:\n    pass", None, "tc1")
    assert "Workflow script raised" in out


@pytest.mark.anyio
async def test_wall_clock_timeout_cancels_children(tmp_path):
    cancelled = asyncio.Event()

    async def slow_spawn(*a):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "never"

    eng, _ = _engine(tmp_path, slow_spawn, timeout_secs=0.3)
    out = await eng.run('await agent("x")\n"done"', None, "tc1")
    assert "timed out" in out
    assert cancelled.is_set()
