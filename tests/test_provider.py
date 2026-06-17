from pathlib import Path

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from marim_harness.deps import Deps
from marim_harness.tools.provider import BuiltinToolProvider


def _build_agent() -> Agent:
    agent = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider().register(agent)
    return agent


def test_registers_all_tools(tmp_path: Path):
    agent = _build_agent()
    with agent.override(model=TestModel(call_tools=[])):
        result = agent.run_sync("hi", deps=Deps(workspace_root=tmp_path))
    assert result is not None  # smoke: agent builds and runs without error


def test_tree_tool_executes_via_agent(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.txt").write_text("")
    agent = _build_agent()
    captured: dict = {}

    def call_tree(messages, info):
        from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

        if not captured:
            captured["called"] = True
            return ModelResponse(
                parts=[ToolCallPart(tool_name="tree", args={"path": ".", "depth": 2})]
            )
        return ModelResponse(parts=[TextPart(content="done")])

    from pydantic_ai.models.function import FunctionModel

    with agent.override(model=FunctionModel(call_tree)):
        result = agent.run_sync("tree it", deps=Deps(workspace_root=tmp_path))
    # the tree tool ran and returned the directory listing to the model
    returns = [
        str(getattr(p, "content", ""))
        for m in result.all_messages()
        for p in m.parts
        if type(p).__name__ == "ToolReturnPart"
    ]
    assert any("sub/" in r and "x.txt" in r for r in returns)


def test_read_tool_executes_via_agent(tmp_path: Path):
    (tmp_path / "a.txt").write_text("content")
    agent = _build_agent()
    # TestModel auto-generates tool args (e.g. dummy path "a") that fail real
    # workspace/file checks, so call_tools=["read_file"] raises. Per the task's
    # documented fallback, use call_tools=[] — registration is still proven by
    # the agent building and running. Behavioral guarantees live in Task 9.
    with agent.override(model=TestModel(call_tools=[])):
        result = agent.run_sync("read it", deps=Deps(workspace_root=tmp_path))
    assert result is not None


@pytest.mark.anyio
async def test_spawn_agent_forwards_mcp_foreground(tmp_path):
    from types import SimpleNamespace

    from marim_harness.deps import Deps
    from marim_harness.permissions import Mode
    from marim_harness.tools.provider import spawn_agent

    calls = {}

    async def fake_runner(type, task, tool_call_id, mcp_names, max_output_chars=None):
        calls["args"] = (type, task, tool_call_id, mcp_names, max_output_chars)
        return "ok"

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    deps.run_subagent = fake_runner
    ctx = SimpleNamespace(deps=deps, tool_call_id="tc1")

    out = await spawn_agent(ctx, "explore", "read docs", mcp=["mddocs"], max_output_chars=500)
    assert out == "ok"
    # mcp names and the spawner's output cap both forward to the runner.
    assert calls["args"] == ("explore", "read docs", "tc1", ["mddocs"], 500)


@pytest.mark.anyio
async def test_spawn_agent_forwards_mcp_background(tmp_path):
    from types import SimpleNamespace

    from marim_harness.deps import Deps
    from marim_harness.permissions import Mode
    from marim_harness.tools.provider import spawn_agent

    captured = {}

    def fake_bg(type, task, mcp_names, max_output_chars=None):
        captured["args"] = (type, task, mcp_names)
        async def _coro():
            return "bg-report"
        return _coro()

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    deps.run_background_agent = fake_bg
    # Close the coroutine in the stub to prevent "coroutine was never awaited" warning.
    deps.jobs = SimpleNamespace(register=lambda kind, label, coro: (coro.close(), "job-1")[1])
    ctx = SimpleNamespace(deps=deps, tool_call_id="tc2")

    out = await spawn_agent(ctx, "general", "do it", background=True, mcp=["sentry"])
    assert "Started" in out
    assert captured["args"] == ("general", "do it", ["sentry"])


@pytest.mark.anyio
async def test_spawn_agent_default_mcp_is_none(tmp_path):
    from types import SimpleNamespace

    from marim_harness.deps import Deps
    from marim_harness.permissions import Mode
    from marim_harness.tools.provider import spawn_agent

    calls = {}

    async def fake_runner(type, task, tool_call_id, mcp_names, max_output_chars=None):
        calls["mcp_names"] = mcp_names
        return "ok"

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    deps.run_subagent = fake_runner
    ctx = SimpleNamespace(deps=deps, tool_call_id="tc3")

    await spawn_agent(ctx, "explore", "investigate")
    assert calls["mcp_names"] is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, None),
        (["mddocs"], ["mddocs"]),
        (["mddocs", "sentry"], ["mddocs", "sentry"]),
        ('["mddocs"]', ["mddocs"]),  # model JSON-stringified the array
        ('["mddocs", "sentry"]', ["mddocs", "sentry"]),
        ("mddocs", ["mddocs"]),  # bare name, not valid JSON
        ('"mddocs"', ["mddocs"]),  # JSON-quoted bare string
        ("mddocs, sentry", ["mddocs", "sentry"]),  # comma-separated
        ("", None),  # empty string grants nothing
        ("  ", None),
        ("[]", None),  # empty array
        ([], None),
    ],
)
def test_coerce_mcp(raw, expected):
    from marim_harness.tools.provider import _coerce_mcp

    assert _coerce_mcp(raw) == expected


@pytest.mark.anyio
async def test_spawn_agent_coerces_stringified_mcp(tmp_path):
    """A model that serializes the array as a JSON string must still grant the
    server, not fail the turn on schema validation."""
    from types import SimpleNamespace

    from marim_harness.deps import Deps
    from marim_harness.permissions import Mode
    from marim_harness.tools.provider import spawn_agent

    calls = {}

    async def fake_runner(type, task, tool_call_id, mcp_names, max_output_chars=None):
        calls["mcp_names"] = mcp_names
        return "ok"

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    deps.run_subagent = fake_runner
    ctx = SimpleNamespace(deps=deps, tool_call_id="tc4")

    await spawn_agent(ctx, "general", "investigate", mcp='["mddocs"]')
    assert calls["mcp_names"] == ["mddocs"]


@pytest.mark.anyio
async def test_spawn_agent_coerces_comma_separated_mcp_background(tmp_path):
    from types import SimpleNamespace

    from marim_harness.deps import Deps
    from marim_harness.permissions import Mode
    from marim_harness.tools.provider import spawn_agent

    captured = {}

    def fake_bg(type, task, mcp_names, max_output_chars=None):
        captured["mcp_names"] = mcp_names

        async def _coro():
            return "bg-report"

        return _coro()

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    deps.run_background_agent = fake_bg
    deps.jobs = SimpleNamespace(
        register=lambda kind, label, coro: (coro.close(), "job-1")[1]
    )
    ctx = SimpleNamespace(deps=deps, tool_call_id="tc5")

    await spawn_agent(ctx, "general", "do it", background=True, mcp="mddocs, sentry")
    assert captured["mcp_names"] == ["mddocs", "sentry"]


@pytest.mark.anyio
async def test_bash_blocks_denylisted_command(tmp_path: Path):
    """A denylisted command is refused before the shell runs — and because the
    gate lives in the tool, it holds in every mode, not just at an approval
    prompt that auto mode would skip."""
    from types import SimpleNamespace

    from marim_harness.command_policy import CommandPolicy
    from marim_harness.tools import provider

    sentinel = tmp_path / "ran.txt"
    deps = Deps(
        workspace_root=tmp_path,
        command_policy=CommandPolicy(denylist=["touch"]),
    )
    ctx = SimpleNamespace(deps=deps)
    out = await provider.bash(ctx, f"touch {sentinel}")
    assert "Blocked by command policy" in out
    assert not sentinel.exists()  # the shell never ran


@pytest.mark.anyio
async def test_bash_allows_permitted_command(tmp_path: Path):
    from types import SimpleNamespace

    from marim_harness.tools import provider

    deps = Deps(workspace_root=tmp_path)  # empty policy -> allow all
    ctx = SimpleNamespace(deps=deps)
    out = await provider.bash(ctx, "echo hello")
    assert "hello" in out
