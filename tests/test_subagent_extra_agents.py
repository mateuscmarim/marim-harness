"""Programmatic sub-agent defs (HarnessConfig.extra_agents) take precedence
over workspace/built-in discovery in SubagentRunner._resolve_agent."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai.models.test import TestModel

from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.runtime.permissions import Mode
from marim_harness.subagents.runner import SubagentRunner
from marim_harness.tools.provider import BuiltinToolProvider
from marim_harness.workspace.agents import AgentDef

REVIEWER = AgentDef(
    name="reviewer", description="reviews diffs", prompt="You review diffs.",
    tools=frozenset({"read_file", "grep"}), source="programmatic",
)


@pytest.fixture
def subagent_runner_factory(tmp_path: Path):
    """Builds a SubagentRunner with cheap test doubles, mirroring the minimal
    constructor call in test_subagent_depth.py, plus whatever kwargs the
    caller wants layered on top (e.g. extra_agents)."""
    from marim_harness.mcp.manager import McpManager

    def _make(**kw) -> SubagentRunner:
        deps = Deps(workspace=WorkspaceConfig(root=tmp_path, mode=Mode.auto))
        provider = BuiltinToolProvider()
        mcp = MagicMock(spec=McpManager)
        mcp.granted_servers.return_value = ([], [])
        mcp.granted_toolsets = AsyncMock(return_value=([], []))
        hooks = MagicMock()
        session = MagicMock()
        return SubagentRunner(
            provider=provider, mcp=mcp, deps=deps, hooks=hooks,
            session=session, get_model=lambda: TestModel(),
            **kw,
        )

    return _make


def test_resolve_agent_prefers_extra_defs(subagent_runner_factory):
    runner = subagent_runner_factory(extra_agents=(REVIEWER,))
    assert runner._resolve_agent("reviewer") is REVIEWER
    # Built-ins still resolve through discovery:
    assert runner._resolve_agent("explore") is not None
    assert runner._resolve_agent("no-such-agent") is None
