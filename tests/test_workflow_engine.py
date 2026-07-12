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


@pytest.mark.anyio
async def test_printed_output_recovers_a_none_final_expression(tmp_path):
    """A script that ends on print(result) instead of a bare `result`
    evaluates to None, but its payload went through print -- possibly after
    several expensive sub-agent runs. The engine returns the printed output
    with a corrective note instead of an error, so the model doesn't have to
    re-run the whole workflow just to fix its last line."""
    eng, _ = _engine(tmp_path, _echo_spawn)
    script = 'r = await agent("t1")\nprint("PAYLOAD: " + r)'
    out = await eng.run(script, None, "tcP")
    assert "LAST EXPRESSION" in out
    assert "PAYLOAD: [general@0] t1" in out


@pytest.mark.anyio
async def test_none_final_expression_without_prints_is_an_error(tmp_path):
    eng, _ = _engine(tmp_path, _echo_spawn)
    out = await eng.run("x = 1", None, "tcQ")
    assert "unusable" in out
    assert "None" in out


@pytest.mark.anyio
async def test_on_workflow_spawn_done_fires_after_each_child_resolves(tmp_path):
    """A workflow child has no literal tool-call/tool-return pair for the
    TUI's on_tool_result to intercept (claim_workflow_spawn mounts its card
    standalone), so the engine must call the completion hook itself once the
    child's agent() call resolves -- otherwise the card is stuck "pending"
    forever, even after the workflow completes successfully."""
    finished: list[tuple] = []

    def on_done(stream_id, report):
        finished.append((stream_id, report))

    eng, deps = _engine(tmp_path, _echo_spawn)
    deps.ui.on_workflow_spawn_done = on_done
    script = 'r = await agent("t1")\nr'
    await eng.run(script, None, "tcY")
    assert finished == [("tcY::wf1", "[general@0] t1")]


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
async def test_bad_schema_fails_fast_without_spawning(tmp_path):
    spawn_calls = 0

    async def spawn(type, task, *rest):
        nonlocal spawn_calls
        spawn_calls += 1
        return '{"findings": []}'

    eng, _ = _engine(tmp_path, spawn)
    bad_schema = {"type": "not-a-real-type"}
    script = (
        "try:\n"
        '    r = await agent("review", schema=' + repr(bad_schema) + ")\n"
        "except Exception as e:\n"
        '    r = "caught: " + str(e)\n'
        "r"
    )
    out = await eng.run(script, None, "tc1")
    assert "caught:" in out and "not a valid JSON Schema" in out
    assert spawn_calls == 0


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
    monkeypatch.setattr(engine_mod, "_MAX_VM_DURATION_SECS", 0.5)
    eng, _ = _engine(tmp_path, _echo_spawn)
    out = await eng.run("while True:\n    pass", None, "tc1")
    assert "Workflow script raised" in out


@pytest.mark.anyio
async def test_cumulative_host_call_time_counts_toward_vm_duration_cap(tmp_path, monkeypatch):
    """pydantic-monty's max_duration_secs is not compute-only: real
    wall-clock time spent awaiting host (agent()) calls counts too, once the
    interpreter regains control. This pins that behavior so a future
    pydantic-monty upgrade that silently changes it gets caught here rather
    than in a live workflow (see engine.py's _MAX_VM_DURATION_SECS)."""
    import marim_harness.workflows.engine as engine_mod
    monkeypatch.setattr(engine_mod, "_MAX_VM_DURATION_SECS", 0.2)

    async def slow_spawn(*a):
        await asyncio.sleep(0.05)
        return "ok"

    eng, _ = _engine(tmp_path, slow_spawn, timeout_secs=5.0)
    script = "r = 0\nfor i in range(10):\n    r = await agent('x')\nr\n"
    out = await eng.run(script, None, "tc1")
    assert "Workflow script raised" in out and "time limit exceeded" in out


@pytest.mark.anyio
async def test_several_real_host_calls_under_budget_do_not_spuriously_time_out(
    tmp_path, monkeypatch
):
    """The regression this guards: a realistic multi-agent workflow (several
    real, non-instant host calls, fanned out with gather) must not be killed
    by the VM duration guard as long as their cumulative real time stays
    under the budget."""
    import marim_harness.workflows.engine as engine_mod
    monkeypatch.setattr(engine_mod, "_MAX_VM_DURATION_SECS", 1.0)

    async def slow_spawn(*a):
        await asyncio.sleep(0.1)
        return "ok"

    eng, _ = _engine(tmp_path, slow_spawn, timeout_secs=5.0)
    script = (
        "import asyncio\n"
        "async def one():\n"
        "    return await agent('x')\n"
        "r = await asyncio.gather(*[one() for _ in range(3)])\n"
        "r\n"
    )
    out = await eng.run(script, None, "tc1")
    assert "raised" not in out and "timed out" not in out


def test_vm_duration_cap_is_generous_enough_for_real_workflows():
    from marim_harness.workflows.engine import _MAX_VM_DURATION_SECS

    assert _MAX_VM_DURATION_SECS >= 120.0


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


@pytest.mark.anyio
async def test_cancelling_the_run_aborts_children_and_reraises(tmp_path):
    started = asyncio.Event()
    child_cancelled = asyncio.Event()

    async def slow_spawn(*a):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            child_cancelled.set()
            raise
        return "never"

    eng, _ = _engine(tmp_path, slow_spawn)
    script = (
        "import asyncio\n"
        'await asyncio.gather(agent("a"), agent("b"))\n'
        '"done"'
    )
    run = asyncio.ensure_future(eng.run(script, None, "tc1"))
    await started.wait()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    # The drain must have completed: children saw the cancel, and the whole
    # thing neither hung nor crashed the interpreter (the Monty GIL bug).
    assert child_cancelled.is_set()


@pytest.mark.anyio
async def test_post_abort_agent_calls_refuse_immediately(tmp_path):
    # A script that catches WorkflowCancelled and tries to keep spawning must
    # be refused by every subsequent agent() call.
    calls = 0
    release = asyncio.Event()

    async def spawn(type, task, *rest):
        nonlocal calls
        calls += 1
        if calls == 1:
            await release.wait()  # parked until cancelled
        return "r"

    eng, _ = _engine(tmp_path, spawn)
    script = (
        "out = []\n"
        "try:\n"
        '    out.append(await agent("first"))\n'
        "except Exception:\n"
        "    try:\n"
        '        out.append(await agent("second"))\n'
        "    except Exception as e:\n"
        '        out.append("refused: " + str(e))\n'
        "out"
    )
    run = asyncio.ensure_future(eng.run(script, None, "tc1"))
    await asyncio.sleep(0.1)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    # Only the first spawn ever launched; the second was refused pre-spawn.
    assert calls == 1


@pytest.mark.anyio
async def test_abort_during_spawn_announce_refuses_the_child(tmp_path):
    # Regression for the _spawn_child race: the abort pre-check happens
    # before `await announce(...)`, and the child task is only created and
    # registered in state.children after that await returns. If
    # _abort_and_drain runs while announce is in flight, it cancels only
    # already-registered children -- a child spawned right after slips
    # through uncancelled and unmonitored. state.abort must be re-checked
    # after announce returns, before the child task is ever created.
    spawn_calls = 0
    announce_started = asyncio.Event()
    release_announce = asyncio.Event()

    async def spawn(*a):
        nonlocal spawn_calls
        spawn_calls += 1
        return "r"

    async def on_spawn(stream_id, type_, task, parent):
        announce_started.set()
        await release_announce.wait()

    eng, deps = _engine(tmp_path, spawn)
    deps.ui.on_workflow_spawn = on_spawn
    script = 'await agent("x")\n"done"'
    run = asyncio.ensure_future(eng.run(script, None, "tc1"))
    await announce_started.wait()
    run.cancel()
    await asyncio.sleep(0.05)  # let the cancellation reach _abort_and_drain
    release_announce.set()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert spawn_calls == 0
