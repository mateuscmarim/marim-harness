import asyncio

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from marim_harness.runtime.deps import Deps, WorkspaceConfig, HarnessServices
from marim_harness.runtime.permissions import Mode
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import _make_deps

# Management tools and the background flag are main-agent only.
_MANAGEMENT_TOOLS = {"jobs", "job_output", "wait_for_job", "cancel_job"}


def _tool_names(agent: Agent, deps: Deps) -> set[str]:
    m = TestModel(call_tools=[])
    with agent.override(model=m):
        agent.run_sync("go", deps=deps)
    return {t.name for t in m.last_model_request_parameters.function_tools}


def _call_once(tool_name: str, args: dict):
    """A FunctionModel that calls a tool once, then echoes its return."""
    state: dict = {}
    captured: dict = {}

    def model(messages, info):
        if not state:
            state["called"] = True
            return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args)])
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "ToolReturnPart":
                    captured["ret"] = str(p.content)
        return ModelResponse(parts=[TextPart(content=captured.get("ret", ""))])

    return FunctionModel(model), captured


def _main_agent() -> Agent:
    agent = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider().register(agent)
    return agent


def test_management_tools_on_main_agent(tmp_path):
    names = _tool_names(_main_agent(), _make_deps(tmp_path, mode=Mode.ask))
    assert names >= _MANAGEMENT_TOOLS


def test_management_tools_absent_on_subagent(tmp_path):
    agent = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider().register_subagent(
        agent, _MANAGEMENT_TOOLS | {"read_file", "bash"}
    )
    names = _tool_names(agent, _make_deps(tmp_path, mode=Mode.ask))
    assert names == {"read_file", "bash"}


def _bash_agent() -> Agent:
    """The gated ``bash`` requires approval on the main agent; register it plain
    (the same function sub-agents get) to exercise its background branch directly."""
    agent = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider().register_subagent(agent, {"bash"})
    return agent


@pytest.mark.anyio
async def test_bash_background_registers_job(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.ask)
    agent = _bash_agent()
    model, captured = _call_once("bash", {"command": "echo hi", "background": True})
    with agent.override(model=model):
        await agent.run("go", deps=deps)
    assert captured["ret"].startswith("Started job-")
    jobs = deps.jobs.list()
    assert len(jobs) == 1
    assert jobs[0].kind == "bash"
    assert jobs[0].label == "echo hi"
    await deps.jobs.wait(jobs[0].id)


@pytest.mark.anyio
async def test_bash_foreground_does_not_register_job(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.ask)
    agent = _bash_agent()
    model, captured = _call_once("bash", {"command": "echo hi", "background": False})
    with agent.override(model=model):
        await agent.run("go", deps=deps)
    assert "exit 0" in captured["ret"]
    assert deps.jobs.list() == []


@pytest.mark.anyio
async def test_spawn_agent_background_registers_job(tmp_path):
    async def fake_bg(type: str, task: str, mcp_names=None, max_output_chars=None,
                      model=None, isolation=None, stream_id: str = "") -> str:
        return f"report for {type}"

    deps = Deps(workspace=WorkspaceConfig(root=tmp_path),
                services=HarnessServices(run_background_agent=fake_bg))
    agent = _main_agent()
    model, captured = _call_once(
        "spawn_agent", {"type": "explore", "task": "look around", "background": True}
    )
    with agent.override(model=model):
        await agent.run("go", deps=deps)
    assert captured["ret"].startswith("Started job-")
    jobs = deps.jobs.list()
    assert len(jobs) == 1
    assert jobs[0].kind == "agent"
    result = await deps.jobs.wait(jobs[0].id)
    assert result == "report for explore"


@pytest.mark.anyio
async def test_spawn_agent_background_unavailable(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.ask)  # no run_background_agent
    agent = _main_agent()
    model, captured = _call_once(
        "spawn_agent", {"type": "explore", "task": "x", "background": True}
    )
    with agent.override(model=model):
        await agent.run("go", deps=deps)
    assert "not available" in captured["ret"]
    assert deps.jobs.list() == []


@pytest.mark.anyio
async def test_jobs_tool_lists_jobs(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.ask)

    async def slow() -> str:
        await asyncio.sleep(5)
        return "done"

    deps.jobs.register("agent", "a slow one", slow())
    agent = _main_agent()
    model, captured = _call_once("jobs", {})
    with agent.override(model=model):
        await agent.run("go", deps=deps)
    assert "a slow one" in captured["ret"]
    await deps.jobs.cancel_all()


@pytest.mark.anyio
async def test_jobs_tool_empty(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.ask)
    agent = _main_agent()
    model, captured = _call_once("jobs", {})
    with agent.override(model=model):
        await agent.run("go", deps=deps)
    assert "No background jobs" in captured["ret"]


@pytest.mark.anyio
async def test_job_output_tool(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.ask)

    async def quick() -> str:
        return "the result"

    job_id = deps.jobs.register("agent", "quick", quick())
    await deps.jobs.wait(job_id)
    agent = _main_agent()
    model, captured = _call_once("job_output", {"id": job_id})
    with agent.override(model=model):
        await agent.run("go", deps=deps)
    assert captured["ret"] == "the result"


@pytest.mark.anyio
async def test_wait_for_job_tool(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.ask)

    async def quick() -> str:
        return "waited result"

    job_id = deps.jobs.register("agent", "quick", quick())
    agent = _main_agent()
    model, captured = _call_once("wait_for_job", {"id": job_id, "timeout": 5})
    with agent.override(model=model):
        await agent.run("go", deps=deps)
    assert captured["ret"] == "waited result"


@pytest.mark.anyio
async def test_cancel_job_tool(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.ask)

    async def slow() -> str:
        await asyncio.sleep(5)
        return "done"

    job_id = deps.jobs.register("agent", "slow", slow())
    agent = _main_agent()
    model, captured = _call_once("cancel_job", {"id": job_id})
    with agent.override(model=model):
        await agent.run("go", deps=deps)
    assert captured["ret"] == f"cancelled {job_id}"
    assert deps.jobs.get(job_id).status == "cancelled"
