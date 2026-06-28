"""Depth field on Deps — the foundation for nested sub-agent tracking."""

from pathlib import Path
from unittest.mock import MagicMock

from pydantic_ai.models.test import TestModel

from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.runtime.permissions import Mode
from marim_harness.subagents.runner import SubagentRunner
from marim_harness.tools.provider import BuiltinToolProvider


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
