from types import SimpleNamespace

import pytest

from marim_harness.tools.workflow_tools import run_workflow
from tests.conftest import _make_deps


def _ctx(deps, tool_call_id="tc1"):
    return SimpleNamespace(deps=deps, tool_call_id=tool_call_id)


@pytest.mark.anyio
async def test_unavailable_seam_returns_install_hint(tmp_path):
    deps = _make_deps(tmp_path)
    deps.services.run_workflow = None
    out = await run_workflow(_ctx(deps), "1 + 1")
    assert "workflows" in out.lower() and "install" in out.lower()


@pytest.mark.anyio
async def test_delegates_script_args_and_tool_call_id(tmp_path):
    deps = _make_deps(tmp_path)
    seen = {}

    async def fake_runner(script, args, tool_call_id):
        seen.update(script=script, args=args, tool_call_id=tool_call_id)
        return "result"

    deps.services.run_workflow = fake_runner
    out = await run_workflow(_ctx(deps, "abc"), "1 + 1", args={"k": 1})
    assert out == "result"
    assert seen == {"script": "1 + 1", "args": {"k": 1}, "tool_call_id": "abc"}
