"""spawn_agent(after=...): harness-enforced ordering between detached spawns.

Tool-level tests drive the module function directly with a stub RunContext —
spawn_agent only reads ``ctx.deps`` and ``ctx.tool_call_id`` — and a fake
``run_background_agent`` service that records the tasks it was started with."""

import asyncio

import pytest

from marim_harness.runtime.deps import HarnessServices
from marim_harness.runtime.permissions import Mode
from marim_harness.tools.provider import spawn_agent
from tests.conftest import _make_deps

pytestmark = pytest.mark.anyio


class _Ctx:
    def __init__(self, deps):
        self.deps = deps
        self.tool_call_id = "tc-1"


def _fake_runner(calls: list, gate: asyncio.Event | None = None):
    """A run_background_agent stub. Records each started task; result echoes the
    task's first line so tests can tell whose report got injected where."""

    async def run(type, task, mcp_names, budget, model, isolation, stream_id, depth):
        if gate is not None:
            await gate.wait()
        calls.append(task)
        return f"report[{task.splitlines()[0]}]"

    return run


def _ctx(tmp_path, calls, gate=None, **deps_kw):
    deps = _make_deps(
        tmp_path, mode=Mode.auto,
        services=HarnessServices(run_background_agent=_fake_runner(calls, gate)),
        **deps_kw,
    )
    return _Ctx(deps)


def _job_id(spawn_result: str) -> str:
    # "Started job-N (agent) — <label>"
    assert spawn_result.startswith("Started job-"), spawn_result
    return spawn_result.split()[1]


async def test_dependent_waits_then_receives_injected_report(tmp_path):
    calls: list = []
    # Two independent gates (rather than the shared-gate helper) so the test
    # can observe the dependent's output_fn contract in full: "(waiting on
    # job-N)" while blocked on the prerequisite, then "(still running)" once
    # it has left the waiting phase and started its own inner run but hasn't
    # finished yet (F5) — a single shared gate can't distinguish those states.
    gate_a, gate_b = asyncio.Event(), asyncio.Event()

    async def run(type, task, mcp_names, budget, model, isolation, stream_id, depth):
        await (gate_a if task.startswith("task A") else gate_b).wait()
        calls.append(task)
        return f"report[{task.splitlines()[0]}]"

    deps = _make_deps(tmp_path, mode=Mode.auto,
                      services=HarnessServices(run_background_agent=run))
    ctx = _Ctx(deps)
    a = _job_id(await spawn_agent(ctx, type="general", task="task A", background=True))
    b = _job_id(await spawn_agent(
        ctx, type="general", task="task B", background=True, after=a))
    await asyncio.sleep(0)
    assert calls == []  # A gated, B waiting on A — neither inner run started
    assert f"(waiting on {a})" in ctx.deps.jobs.output(b)

    gate_a.set()
    await ctx.deps.jobs.wait(a, 5)
    # B's wrapper has left the waiting phase and is now inside its own inner
    # run, blocked on gate_b — output_fn must report the generic "still
    # running" marker, not the stale "(waiting on ...)" note.
    for _ in range(400):
        if ctx.deps.jobs.output(b) != f"(waiting on {a})":
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("dependent job never left its waiting phase")
    assert ctx.deps.jobs.output(b) == "(still running)"

    gate_b.set()
    await ctx.deps.jobs.wait(b, 5)
    assert len(calls) == 2
    assert calls[0].startswith("task A")
    assert calls[1].startswith("task B")
    assert "## Results of prerequisite jobs" in calls[1]
    assert "report[task A]" in calls[1]
    assert ctx.deps.jobs.get(b).status == "done"


async def test_multiple_prerequisites_injected_in_order(tmp_path):
    calls: list = []
    ctx = _ctx(tmp_path, calls)
    a = _job_id(await spawn_agent(ctx, type="general", task="task A", background=True))
    b = _job_id(await spawn_agent(ctx, type="general", task="task B", background=True))
    c = _job_id(await spawn_agent(
        ctx, type="general", task="task C", background=True, after=[b, a]))
    await ctx.deps.jobs.wait(c, 5)
    task_c = calls[-1]
    assert task_c.startswith("task C")
    # Injection follows the order given in after=[b, a].
    assert task_c.index("report[task B]") < task_c.index("report[task A]")


async def test_failed_prerequisite_skips_dependent(tmp_path):
    calls: list = []

    async def run(type, task, mcp_names, budget, model, isolation, stream_id, depth):
        if task.startswith("task A"):
            raise RuntimeError("boom")
        calls.append(task)
        return "ok"

    deps = _make_deps(tmp_path, mode=Mode.auto,
                      services=HarnessServices(run_background_agent=run))
    ctx = _Ctx(deps)
    a = _job_id(await spawn_agent(ctx, type="general", task="task A", background=True))
    b = _job_id(await spawn_agent(
        ctx, type="general", task="task B", background=True, after=a))
    result = await ctx.deps.jobs.wait(b, 5)
    assert ctx.deps.jobs.get(b).status == "failed"
    assert "PrerequisiteFailed" in result and a in result
    assert calls == []  # the dependent's sub-agent was never started


async def test_cancelled_prerequisite_fails_dependent(tmp_path):
    calls: list = []
    gate = asyncio.Event()
    ctx = _ctx(tmp_path, calls, gate)
    a = _job_id(await spawn_agent(ctx, type="general", task="task A", background=True))
    b = _job_id(await spawn_agent(
        ctx, type="general", task="task B", background=True, after=a))
    await ctx.deps.jobs.cancel(a)
    await ctx.deps.jobs.wait(b, 5)
    assert ctx.deps.jobs.get(b).status == "failed"
    assert calls == []


async def test_cancelling_waiting_dependent_leaves_prerequisite_running(tmp_path):
    calls: list = []
    gate = asyncio.Event()
    ctx = _ctx(tmp_path, calls, gate)
    a = _job_id(await spawn_agent(ctx, type="general", task="task A", background=True))
    b = _job_id(await spawn_agent(
        ctx, type="general", task="task B", background=True, after=a))
    await ctx.deps.jobs.cancel(b)
    assert ctx.deps.jobs.get(b).status == "cancelled"
    assert ctx.deps.jobs.get(a).status == "running"
    gate.set()
    await ctx.deps.jobs.wait(a, 5)
    assert calls and calls[0].startswith("task A")


async def test_unknown_after_id_registers_nothing(tmp_path):
    calls: list = []
    ctx = _ctx(tmp_path, calls)
    out = await spawn_agent(
        ctx, type="general", task="task B", background=True, after="job-77")
    assert "job-77" in out and "no such job" in out
    assert ctx.deps.jobs.list() == []


async def test_after_refused_on_foreground_spawn(tmp_path):
    calls: list = []
    ctx = _ctx(tmp_path, calls)
    a = _job_id(await spawn_agent(ctx, type="general", task="task A", background=True))
    out = await spawn_agent(
        ctx, type="general", task="task B", background=False, after=a)
    assert "detached" in out and "wait_for_job" in out
    assert len(ctx.deps.jobs.list()) == 1  # only A


async def test_after_refused_at_depth(tmp_path):
    calls: list = []
    ctx = _ctx(tmp_path, calls)
    a = _job_id(await spawn_agent(ctx, type="general", task="task A", background=True))
    # Same registry as seen from inside a depth-1 sub-agent: `after` must be
    # refused there (background spawning is main-agent-only, so a depth>0 spawn
    # can never end up detached). The dep must exist so the unknown-id check
    # doesn't fire first and mask the refusal under test.
    ctx.deps.subagent_depth = 1
    out = await spawn_agent(ctx, type="general", task="task B", after=a)
    assert "detached" in out
    assert len(ctx.deps.jobs.list()) == 1  # only A; nothing new registered


async def test_chain_runs_strictly_in_order(tmp_path):
    calls: list = []
    ctx = _ctx(tmp_path, calls)
    a = _job_id(await spawn_agent(ctx, type="general", task="task A", background=True))
    b = _job_id(await spawn_agent(
        ctx, type="general", task="task B", background=True, after=a))
    c = _job_id(await spawn_agent(
        ctx, type="general", task="task C", background=True, after=b))
    await ctx.deps.jobs.wait(c, 5)
    starts = [t.splitlines()[0] for t in calls]
    assert starts == ["task A", "task B", "task C"]
    # C sees B's report; A's report reaches C only inside B's injected text,
    # so C's own prerequisite section must reference job B, not job A.
    assert f"### {b} " in calls[2] and f"### {a} " not in calls[2]


async def test_injected_heading_clips_multiline_label(tmp_path):
    """A background spawn's label falls back to the full composed task when
    `description` is omitted. The injected '### job-N — ...' heading must stay
    one line (via jobs._one_line) even when the prerequisite's task/label
    spans many lines — otherwise a dependent's prompt embeds its
    prerequisite's entire multi-section prompt inside a heading."""
    calls: list = []
    ctx = _ctx(tmp_path, calls)
    multiline_task = "task A summary line\n\n## Scope\nlots of extra detail\nmore detail"
    a = _job_id(await spawn_agent(
        ctx, type="general", task=multiline_task, background=True))
    b = _job_id(await spawn_agent(
        ctx, type="general", task="task B", background=True, after=a))
    await ctx.deps.jobs.wait(b, 5)
    task_b = calls[-1]

    heading_start = task_b.index(f"### {a}")
    heading_line = task_b[heading_start:task_b.index("\n", heading_start)]
    assert heading_line == f"### {a} — general: task A summary line"
    assert "## Scope" not in heading_line
    assert "## Scope" not in task_b  # dropped entirely, not just off the heading line


async def test_bash_job_as_prerequisite(tmp_path):
    calls: list = []
    ctx = _ctx(tmp_path, calls)

    async def _bash() -> str:
        return "bash-output"

    bash_id = ctx.deps.jobs.register("bash", "ls -la", _bash())
    b = _job_id(await spawn_agent(
        ctx, type="general", task="task B", background=True, after=bash_id))
    await ctx.deps.jobs.wait(b, 5)
    assert "bash-output" in calls[0]
