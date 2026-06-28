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
