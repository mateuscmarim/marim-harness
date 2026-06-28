"""Depth field on Deps — the foundation for nested sub-agent tracking."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.runtime.permissions import Mode
from marim_harness.subagents.runner import SubagentRunner
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import _make_harness


def _make_deps(tmp_path: Path, **kw) -> Deps:
    return Deps(
        workspace=WorkspaceConfig(root=tmp_path, mode=Mode.auto),
        **kw,
    )


def test_subagent_depth_defaults_to_zero():
    deps = _make_deps(Path("/tmp"))
    assert deps.subagent_depth == 0


def test_subagent_depth_can_be_set():
    deps = _make_deps(Path("/tmp"), subagent_depth=1)
    assert deps.subagent_depth == 1


def test_subagent_depth_increments_via_replace():
    deps = _make_deps(Path("/tmp"))
    child = deps.replace(subagent_depth=deps.subagent_depth + 1)
    assert child.subagent_depth == 1
    assert deps.subagent_depth == 0  # original unchanged


def _make_runner(tmp_path: Path, max_depth: int = 3) -> SubagentRunner:
    from marim_harness.mcp.manager import McpManager

    deps = _make_deps(tmp_path)
    provider = BuiltinToolProvider()
    mcp = MagicMock(spec=McpManager)
    mcp.granted_servers.return_value = ([], [])
    hooks = MagicMock()
    session = MagicMock()
    return SubagentRunner(
        provider=provider, mcp=mcp, deps=deps, hooks=hooks,
        session=session, get_model=lambda: TestModel(),
        max_depth=max_depth,
    )


def test_runner_default_max_depth_is_3(tmp_path: Path):
    runner = _make_runner(tmp_path)
    assert runner._max_depth == 3


def test_build_at_depth_1_registers_spawn_agent(tmp_path: Path):
    """Depth-1 sub-agent can still spawn (1+1=2 < 3), so spawn_agent is registered."""
    runner = _make_runner(tmp_path, max_depth=3)
    sub, err = runner.build("explore", depth=1)
    assert sub is not None
    assert err is None
    # spawn_agent should be among the registered tools
    tool_names = set(sub._function_toolset.tools.keys())
    assert "spawn_agent" in tool_names


def test_build_at_depth_2_skips_spawn_agent(tmp_path: Path):
    """Depth-2 grandchild cannot spawn (2+1=3 >= 3), so spawn_agent is NOT registered."""
    runner = _make_runner(tmp_path, max_depth=3)
    sub, err = runner.build("explore", depth=2)
    assert sub is not None
    assert err is None
    tool_names = set(sub._function_toolset.tools.keys())
    assert "spawn_agent" not in tool_names


def test_build_at_depth_0_registers_spawn_agent(tmp_path: Path):
    """Depth-0 (the default) gets spawn_agent — backward compatible."""
    runner = _make_runner(tmp_path, max_depth=3)
    sub, err = runner.build("explore", depth=0)
    assert sub is not None
    tool_names = set(sub._function_toolset.tools.keys())
    assert "spawn_agent" in tool_names


def test_spawn_agent_refuses_at_depth_limit():
    """At depth 2 with max_depth=3, spawning would produce depth 3 → refused."""
    import asyncio
    from types import SimpleNamespace

    from marim_harness.tools.provider import spawn_agent

    deps = _make_deps(Path("/tmp"), subagent_depth=2)
    ctx = SimpleNamespace(deps=deps, tool_call_id="tc1")

    result = asyncio.run(spawn_agent(
        ctx, type="explore", task="do thing", max_depth=3
    ))
    assert "Cannot spawn" in result
    assert "depth 2" in result


def test_spawn_agent_allows_below_depth_limit():
    """At depth 1 with max_depth=3, spawning produces depth 2 → allowed."""
    # This test verifies the depth check passes; the actual spawn
    # will fail because services.run_subagent is None in the test context,
    # but the depth check should not be the thing that blocks it.
    import asyncio
    from types import SimpleNamespace

    from marim_harness.tools.provider import spawn_agent

    deps = _make_deps(Path("/tmp"), subagent_depth=1)
    ctx = SimpleNamespace(deps=deps, tool_call_id="tc1")

    result = asyncio.run(spawn_agent(
        ctx, type="explore", task="do thing", max_depth=3
    ))
    # Should NOT contain "Cannot spawn" — it should fail for another reason
    # (no subagent runner wired in test context)
    assert "Cannot spawn" not in result


@pytest.mark.anyio
async def test_nested_spawn_integration(tmp_path: Path):
    """End-to-end: main → sub → grandchild chain works.

    The main agent spawns a sub-agent (depth 1). The sub-agent spawns
    a grandchild (depth 2). The grandchild cannot spawn further.
    """
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])

    deps = _make_deps(tmp_path)
    h = _make_harness(FunctionModel(fn), deps)

    # Verify runner has max_depth
    assert h.subagents._max_depth == 3

    # Build depth-0 sub-agent → should have spawn_agent
    sub, _ = h.subagents.build("explore", depth=0)
    assert sub is not None
    tool_names = set(sub._function_toolset.tools.keys())
    assert "spawn_agent" in tool_names

    # Build depth-1 sub-agent → should have spawn_agent (can spawn depth-2)
    sub2, _ = h.subagents.build("explore", depth=1)
    assert sub2 is not None
    tool_names_2 = set(sub2._function_toolset.tools.keys())
    assert "spawn_agent" in tool_names_2

    # Build depth-2 sub-agent → should NOT have spawn_agent (leaf)
    sub3, _ = h.subagents.build("explore", depth=2)
    assert sub3 is not None
    tool_names_3 = set(sub3._function_toolset.tools.keys())
    assert "spawn_agent" not in tool_names_3


def _depth_spy(runner):
    """Replace ``runner._run_to_completion`` with a spy that records the
    ``subagent_depth`` of every sub-agent actually run, then delegates to the
    real implementation. Returns the list the spy appends to. Drives the *real*
    run() → _execute_spawn → _prepare_spawn → _execute_*_spawn path (not just
    build()), so it catches a depth mis-wire that only shows up at runtime."""
    depths: list[int] = []
    orig = runner._run_to_completion

    async def spy(sub, task, run_deps, granted, handler, stream_id=None):
        depths.append(run_deps.subagent_depth)
        return await orig(sub, task, run_deps, granted, handler, stream_id)

    runner._run_to_completion = spy
    return depths


@pytest.mark.anyio
async def test_run_uses_caller_depth_not_runner_deps(tmp_path: Path):
    """C1 regression: run() must size the child off the *caller's* depth, not the
    runner's own deps (which are pinned at the main agent's depth 0).

    A depth-1 sub-agent spawning means caller_depth=1, so the child must run at
    subagent_depth == 2. Before the fix run() read self.deps.subagent_depth (0)
    and produced depth 1 — silently collapsing the chain by a level."""
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="done")])

    deps = _make_deps(tmp_path)
    h = _make_harness(FunctionModel(fn), deps)
    depths = _depth_spy(h.subagents)

    await h.subagents.run("explore", "do it", "sid", caller_depth=1)

    assert depths == [2]


@pytest.mark.anyio
async def test_nested_spawn_runtime_chain_propagates_depth(tmp_path: Path):
    """C1/C2 regression, true runtime chain: main → sub → grandchild.

    The model emits a spawn_agent call whenever it has that tool (a depth-1
    sub-agent does; the depth-2 grandchild does not, so it just replies). Driving
    run() for real, the sub must land at depth 1 and the grandchild it spawns at
    depth 2 — proving both that the caller's depth threads through (C1) and that
    the child's bound spawn_agent ceiling is the absolute max, so the nested
    spawn isn't wrongly refused (C2)."""
    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    def fn(messages, info):
        tool_names = {t.name for t in info.function_tools}
        already_spawned = any(
            isinstance(part, ToolReturnPart) and part.tool_name == "spawn_agent"
            for m in messages
            for part in getattr(m, "parts", [])
        )
        if "spawn_agent" in tool_names and not already_spawned:
            return ModelResponse(
                parts=[ToolCallPart(
                    tool_name="spawn_agent",
                    args={"type": "explore", "task": "nested work"},
                )]
            )
        return ModelResponse(parts=[TextPart(content="done")])

    deps = _make_deps(tmp_path)
    h = _make_harness(FunctionModel(fn), deps)
    depths = _depth_spy(h.subagents)

    # Main agent (depth 0) spawns: caller_depth=0 → sub at depth 1, which spawns
    # a grandchild at depth 2. The grandchild has no spawn_agent, so it stops.
    await h.subagents.run("explore", "top task", "sid", caller_depth=0)

    assert depths == [1, 2]
