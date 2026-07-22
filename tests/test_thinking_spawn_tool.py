"""spawn_agent's thinking override reaches the run_subagent service seam."""

from types import SimpleNamespace

import pytest

from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.runtime.permissions import Mode
from marim_harness.tools import spawn_tools


def _ctx(tmp_path, run_subagent):
    deps = Deps(workspace=WorkspaceConfig(root=tmp_path, mode=Mode.auto))
    deps.services.run_subagent = run_subagent
    return SimpleNamespace(deps=deps, tool_call_id="tc1")


@pytest.mark.anyio
async def test_spawn_forwards_thinking_to_service(tmp_path):
    seen = {}

    async def run_subagent(*args):
        seen["args"] = args
        return "done"

    ctx = _ctx(tmp_path, run_subagent)
    out = await spawn_tools.spawn_agent(
        ctx, "coder", "do it", thinking="high"
    )
    assert out == "done"
    # thinking rides at the tail of the positional dispatch (mirrors tier).
    assert "high" in seen["args"]


@pytest.mark.anyio
async def test_spawn_defaults_thinking_none(tmp_path):
    seen = {}

    async def run_subagent(*args):
        seen["args"] = args
        return "done"

    ctx = _ctx(tmp_path, run_subagent)
    await spawn_tools.spawn_agent(ctx, "coder", "do it")
    assert None in seen["args"]
