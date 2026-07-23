import asyncio

import pytest

from marim_harness.workflows.engine import WorkflowEngine, _script_title
from tests.conftest import _make_deps


def _engine(tmp_path, spawn, **kw):
    deps = _make_deps(tmp_path)
    return WorkflowEngine(deps, spawn, **kw), deps


async def _echo_spawn(type, task, stream_id, mcp_names, max_output_chars,
                      model, isolation, caller_depth, **kw):
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

    async def spawn(*a, **kw):
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

    async def spawn(type, task, *rest, **kw):
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
    async def spawn(type, task, *rest, output_schema=None):
        assert output_schema is not None
        assert "Output contract" not in task
        return '{"findings": ["bug in x"]}'

    eng, _ = _engine(tmp_path, spawn)
    out = await eng.run(SCHEMA_SCRIPT, None, "tc1")
    assert "bug in x" in out


@pytest.mark.anyio
async def test_schema_failure_respawns_once_with_the_validation_error(tmp_path):
    calls: list[str] = []

    async def spawn(type, task, *rest, **kw):
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
    async def spawn(type, task, *rest, **kw):
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

    async def spawn(type, task, *rest, **kw):
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
async def test_static_validation_rejects_undefined_name_before_spawning(tmp_path):
    """An unresolved name (the classic model-authored-script bug) is caught by
    static validation BEFORE anything runs: no sub-agent spawns, and — like the
    parse-failure path — no card is ever claimed for the run."""
    spawn_calls = 0

    async def spawn(*a, **kw):
        nonlocal spawn_calls
        spawn_calls += 1
        return "r"

    eng, deps = _engine(tmp_path, spawn)
    events = []
    deps.ui.on_workflow_start = lambda *a: events.append(("start", *a))
    deps.ui.on_workflow_done = lambda *a: events.append(("done", *a))
    out = await eng.run('x = await agent("t")\nresult', None, "tc1")
    assert "failed validation" in out
    assert "result" in out and "not defined" in out
    # Diagnostics must point at the USER script's line, unshifted by the
    # host-declaration prefix type_check needs.
    assert "workflow.py:2" in out
    assert spawn_calls == 0
    assert events == []


@pytest.mark.anyio
async def test_static_validation_accepts_host_functions_and_args(tmp_path):
    """agent()/log()/args are injected at run time, not defined in the script —
    validation must know the host surface or every real script would be
    rejected. This is the false-positive guard for the validation gate."""
    eng, _ = _engine(tmp_path, _echo_spawn)
    script = (
        "import asyncio\n"
        'results = await asyncio.gather(*[agent(f"sum {f}") for f in [str(args)]])\n'
        'log("ready")\n'
        "results[0]"
    )
    out = await eng.run(script, "a.py", "tc1")
    assert "failed validation" not in out
    assert "sum a.py" in out


@pytest.mark.anyio
async def test_static_validation_rejects_indexing_a_plain_report(tmp_path):
    """Without schema=, agent() returns a plain string report — indexing it
    with a string key is the second-most-common model-authored bug (after
    undefined names) and burned a real 27k-token spawn before dying. The
    overloaded host declaration types the no-schema return as str, so the
    type check rejects it BEFORE any sub-agent runs; schema'd indexing stays
    accepted (SCHEMA_SCRIPT above runs r["findings"] end-to-end)."""
    spawn_calls = 0

    async def spawn(*a, **kw):
        nonlocal spawn_calls
        spawn_calls += 1
        return "r"

    eng, deps = _engine(tmp_path, spawn)
    events = []
    deps.ui.on_workflow_start = lambda *a: events.append(("start", *a))
    script = 'r1 = await agent("summarize x")\nout = r1["result"]\nout'
    out = await eng.run(script, None, "tc1")
    assert "failed validation" in out
    assert "str" in out  # the diagnostic names the offending type
    assert "workflow.py:2" in out
    assert spawn_calls == 0
    assert events == []


@pytest.mark.anyio
async def test_uncaught_script_exception_names_the_line(tmp_path):
    eng, _ = _engine(tmp_path, _echo_spawn)
    out = await eng.run('x = 1\nraise ValueError("boom")\nx', None, "tc1")
    assert "Workflow script raised" in out and "boom" in out
    # Scripts are model-authored: the traceback (file/line frames) is what
    # lets the model fix the script on the next attempt instead of guessing.
    assert 'File "workflow.py", line 2' in out


@pytest.mark.anyio
async def test_script_exception_traceback_walks_helper_frames(tmp_path):
    eng, _ = _engine(tmp_path, _echo_spawn)
    # A runtime-only failure (KeyError on an empty dict): static validation
    # can't see it, so it exercises the traceback rendering, not the gate.
    script = 'def helper(d):\n    return d["missing"]\n\nhelper({})\n'
    out = await eng.run(script, None, "tc1")
    assert "Workflow script raised" in out
    assert "KeyError" in out
    assert 'File "workflow.py", line 2, in helper' in out


@pytest.mark.anyio
async def test_agent_failure_is_catchable_in_script(tmp_path):
    async def spawn(*a, **kw):
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
    # it fails at parse, validation, or run, it must surface as a tool-visible
    # error, never as file contents.
    assert "root:" not in out
    assert "raised" in out or "failed to parse" in out or "failed validation" in out
    out2 = await eng.run("import socket\nsocket", None, "tc1")
    assert "raised" in out2 or "failed to parse" in out2 or "failed validation" in out2


@pytest.mark.anyio
async def test_infinite_loop_is_killed_by_vm_limits(tmp_path):
    # A non-yielding spin loop is stopped by one of two co-timed guards, both
    # armed at `effective` (here 0.5s): the Monty VM's internal max_duration_secs
    # cap (surfaces as "Workflow script raised") or the outer asyncio.wait
    # wall-clock backstop (surfaces as "Workflow timed out"). Which one wins is a
    # genuine race under load — asserting only the VM-raise branch made this test
    # flaky (a loaded CI runner let the wall-clock backstop fire first). Both are
    # correct kills; the contract this pins is that the loop IS stopped, not that
    # it surfaces via one specific path. Mirrors the sibling assertion below.
    eng, _ = _engine(tmp_path, _echo_spawn)
    out = await eng.run("while True:\n    pass", None, "tc1", timeout_secs=0.5)
    assert "Workflow script raised" in out or "Workflow timed out" in out


@pytest.mark.anyio
async def test_cumulative_host_call_time_counts_toward_vm_duration_cap(tmp_path):
    """pydantic-monty's max_duration_secs is not compute-only: real
    wall-clock time spent awaiting host (agent()) calls counts too, once the
    interpreter regains control. This pins that behavior so a future
    pydantic-monty upgrade that silently changes it gets caught here rather
    than in a live workflow (see engine.py's module docstring)."""

    async def slow_spawn(*a, **kw):
        await asyncio.sleep(0.05)
        return "ok"

    eng, _ = _engine(tmp_path, slow_spawn, timeout_secs=5.0)
    script = "r = 0\nfor i in range(10):\n    r = await agent('x')\nr\n"
    out = await eng.run(script, None, "tc1", timeout_secs=0.2)
    assert "raised" in out or "timed out" in out


@pytest.mark.anyio
async def test_several_real_host_calls_under_budget_do_not_spuriously_time_out(
    tmp_path,
):
    """The regression this guards: a realistic multi-agent workflow (several
    real, non-instant host calls, fanned out with gather) must not be killed
    by the VM duration guard as long as their cumulative real time stays
    under the budget."""

    async def slow_spawn(*a, **kw):
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
    out = await eng.run(script, None, "tc1", timeout_secs=1.0)
    assert "raised" not in out and "timed out" not in out


def test_effective_timeout_clamps_to_ceiling():
    """Pure clamp rule: min(requested or 300, ceiling). Unit-tested directly
    because the 300s default is unobservable in a fast integration test."""
    from marim_harness.workflows.engine import _effective_timeout

    assert _effective_timeout(None, 1800.0) == 300.0      # omitted -> default
    assert _effective_timeout(60.0, 1800.0) == 60.0       # under ceiling -> honored
    assert _effective_timeout(9999.0, 1800.0) == 1800.0   # over ceiling -> clamped
    assert _effective_timeout(None, 10.0) == 10.0         # tiny ceiling clamps the default too


@pytest.mark.anyio
async def test_requested_timeout_over_ceiling_reports_the_clamped_value(tmp_path):
    """A clamped request must be visible: the timeout message reports the
    EFFECTIVE duration, not the requested one."""
    async def slow_spawn(*a, **kw):
        await asyncio.sleep(60)
        return "never"

    eng, _ = _engine(tmp_path, slow_spawn, timeout_secs=0.3)
    out = await eng.run('await agent("x")\n"done"', None, "tc1", timeout_secs=500.0)
    # 0.3 rendered through the message's {effective:.0f} formatting.
    assert "timed out after 0s" in out
    # The requested (unclamped) value must NOT appear.
    assert "500" not in out


@pytest.mark.anyio
async def test_requested_timeout_extends_past_the_default(tmp_path):
    """A run whose requested timeout exceeds the old 300s-style bound (scaled
    down here) survives, proving the per-call request really widens the VM
    duration limit and the outer wait together."""
    async def slow_spawn(*a, **kw):
        await asyncio.sleep(0.2)
        return "ok"

    # Ceiling 5.0; request 2.0. With the old fixed-cap behavior scaled to this
    # test, a 0.2s host call would have died under a smaller default.
    eng, _ = _engine(tmp_path, slow_spawn, timeout_secs=5.0)
    out = await eng.run('await agent("x")\n"done"', None, "tc1", timeout_secs=2.0)
    assert "done" in out and "timed out" not in out and "raised" not in out


@pytest.mark.anyio
async def test_wall_clock_timeout_cancels_children(tmp_path):
    cancelled = asyncio.Event()

    async def slow_spawn(*a, **kw):
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

    async def slow_spawn(*a, **kw):
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

    async def spawn(type, task, *rest, **kw):
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


def test_script_title_prefers_the_leading_comment():
    assert _script_title("# review sweep\nx = 1") == "review sweep"


def test_script_title_falls_back_to_a_line_count():
    assert _script_title("x = 1\n# late comment") == "workflow script (2 lines)"
    assert _script_title("\n\n#\nx = 1") == "workflow script (4 lines)"


@pytest.mark.anyio
async def test_workflow_start_and_done_hooks_fire_on_success(tmp_path):
    eng, deps = _engine(tmp_path, _echo_spawn)
    events = []
    deps.ui.on_workflow_start = lambda tcid, title: events.append(("start", tcid, title))
    deps.ui.on_workflow_done = lambda tcid, outcome, failed: events.append(("done", tcid, failed))
    out = await eng.run('# review sweep\n"ok"', None, "tcS")
    assert events == [("start", "tcS", "review sweep"), ("done", "tcS", False)]
    assert out == '"ok"'


@pytest.mark.anyio
async def test_raising_announce_callbacks_do_not_fail_or_lose_the_result(tmp_path):
    """A raising render callback (on_workflow_start/on_workflow_done) is a UI
    bug, not a script failure — same posture as log(). It must neither fail the
    workflow nor, on the success path, lose the already-computed result."""
    eng, deps = _engine(tmp_path, _echo_spawn)

    def boom(*a, **kw):
        raise RuntimeError("render bug")

    deps.ui.on_workflow_start = boom
    deps.ui.on_workflow_done = boom
    out = await eng.run('# sweep\n"the result"', None, "tcR")
    # The result survived both raising callbacks (not swallowed, not an error).
    assert out == '"the result"'


@pytest.mark.anyio
async def test_on_workflow_done_fires_failed_on_script_raise(tmp_path):
    eng, deps = _engine(tmp_path, _echo_spawn)
    events = []
    deps.ui.on_workflow_done = lambda tcid, outcome, failed: events.append((tcid, failed))
    # Undefined names are now caught by static validation before any run, so
    # a runtime raise is the fixture here.
    out = await eng.run('raise ValueError("boom")', None, "tcE")
    assert "raised" in out
    assert events == [("tcE", True)]


@pytest.mark.anyio
async def test_on_workflow_done_fires_failed_on_timeout(tmp_path):
    async def slow_spawn(*a, **kw):
        await asyncio.sleep(5)
        return "never"

    eng, deps = _engine(tmp_path, slow_spawn, timeout_secs=0.1)
    events = []
    deps.ui.on_workflow_done = lambda tcid, outcome, failed: events.append((tcid, failed, outcome))
    out = await eng.run('await agent("x")\n"done"', None, "tcT")
    assert "timed out" in out
    assert events == [("tcT", True, out)]


@pytest.mark.anyio
async def test_on_workflow_done_fires_on_cancellation(tmp_path):
    started = asyncio.Event()

    async def slow_spawn(*a, **kw):
        started.set()
        await asyncio.sleep(30)
        return "never"

    eng, deps = _engine(tmp_path, slow_spawn)
    events = []
    deps.ui.on_workflow_done = lambda tcid, outcome, failed: events.append((tcid, outcome, failed))
    run = asyncio.ensure_future(eng.run('await agent("x")\n"done"', None, "tcC"))
    await started.wait()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert events == [("tcC", "workflow aborted", True)]


@pytest.mark.anyio
async def test_parse_failure_fires_no_lifecycle_hooks(tmp_path):
    eng, deps = _engine(tmp_path, _echo_spawn)
    events = []
    deps.ui.on_workflow_start = lambda *a: events.append(a)
    deps.ui.on_workflow_done = lambda *a: events.append(a)
    out = await eng.run("def broken(:\n    pass", None, "tcP")
    assert "failed to parse" in out
    assert events == []


@pytest.mark.anyio
async def test_log_lines_carry_the_tool_call_id(tmp_path):
    eng, deps = _engine(tmp_path, _echo_spawn)
    lines = []
    deps.ui.on_workflow_log = lambda tcid, msg: lines.append((tcid, msg))
    await eng.run('log("step 1")\n"ok"', None, "tcL")
    assert lines == [("tcL", "step 1")]


@pytest.mark.anyio
async def test_log_callback_fires_on_the_event_loop(tmp_path):
    """Monty invokes sync host functions on its interpreter thread, where no
    event loop is running (async host functions like agent() are marshalled
    to the loop first). The engine must hand log() lines back to the loop
    before the UI callback runs: the TUI handler mounts widgets and app.py
    documents it as "fired on the app's event loop" -- calling it off-loop
    raised RuntimeError("no running event loop") INTO the script, failing a
    workflow whose agent() work had already completed."""
    eng, deps = _engine(tmp_path, _echo_spawn)
    loop = asyncio.get_running_loop()
    seen = []

    def needs_loop(tcid, msg):
        # Raises off-loop, exactly like the TUI's widget mount did.
        seen.append((tcid, msg, asyncio.get_running_loop() is loop))

    deps.ui.on_workflow_log = needs_loop
    out = await eng.run('log("step 1")\n"ok"', None, "tcL")
    assert "ok" in out and "raised" not in out
    assert seen == [("tcL", "step 1", True)]


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

    async def spawn(*a, **kw):
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


@pytest.mark.anyio
async def test_schema_rides_the_spawn_seam_not_the_prompt(tmp_path):
    seen = {}

    async def spawn(type, task, stream_id, *rest, output_schema=None):
        seen["task"], seen["output_schema"] = task, output_schema
        return '{"findings": []}'

    eng, _ = _engine(tmp_path, spawn)
    out = await eng.run(SCHEMA_SCRIPT, None, "tc1")
    assert out == "[]"
    assert seen["output_schema"] == FINDINGS
    assert "Output contract" not in seen["task"]


@pytest.mark.anyio
async def test_schema_retry_keeps_the_schema_on_the_seam(tmp_path):
    calls = []

    async def spawn(type, task, *rest, output_schema=None):
        calls.append((task, output_schema))
        if len(calls) == 1:
            return "not json at all"
        return '{"findings": []}'

    eng, _ = _engine(tmp_path, spawn)
    out = await eng.run(SCHEMA_SCRIPT, None, "tc1")
    assert out == "[]"
    assert len(calls) == 2
    retry_task, retry_schema = calls[1]
    assert "failed validation" in retry_task
    assert "Output contract" not in retry_task
    assert retry_schema == FINDINGS


@pytest.mark.anyio
async def test_unschemad_agent_calls_pass_no_schema(tmp_path):
    seen = {}

    async def spawn(type, task, *rest, output_schema=None):
        seen["output_schema"] = output_schema
        return "report"

    eng, _ = _engine(tmp_path, spawn)
    await eng.run('await agent("look around")', None, "tc1")
    assert seen["output_schema"] is None
