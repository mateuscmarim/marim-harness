import asyncio
import json
from types import SimpleNamespace

import pytest

from marim_harness.tools.workflow_tools import run_workflow
from marim_harness.workflows.engine import WorkflowEngine
from tests.conftest import _make_deps

SWEEP = """
import asyncio

SCHEMA = {"type": "object",
          "properties": {"findings": {"type": "array", "items": {"type": "string"}}},
          "required": ["findings"]}

async def review(dim):
    r = await agent("Review the diff for " + dim + " issues",
                    type="explore", schema=SCHEMA)
    return r["findings"]

per_dim = await asyncio.gather(*[review(d) for d in ["bugs", "performance", "style"]])
log("reviewed " + str(len(per_dim)) + " dimensions")
{"findings": [f for fs in per_dim for f in fs]}
"""

CANNED = {
    "bugs": ["off-by-one in pager"],
    "performance": ["N+1 query in listing"],
    "style": [],
}


@pytest.mark.anyio
async def test_parallel_review_sweep_end_to_end(tmp_path):
    announced = []

    async def spawn(type, task, stream_id, mcp, cap, model, iso, depth):
        assert type == "explore" and "Output contract" in task
        dim = next(d for d in CANNED if d in task)
        await asyncio.sleep(0.01)
        return json.dumps({"findings": CANNED[dim]})

    async def on_spawn(stream_id, type_, task, parent):
        announced.append(stream_id)

    deps = _make_deps(tmp_path)
    deps.ui.on_workflow_spawn = on_spawn
    engine = WorkflowEngine(deps, spawn)
    deps.services.run_workflow = engine.run

    ctx = SimpleNamespace(deps=deps, tool_call_id="sweep1")
    out = await run_workflow(ctx, SWEEP)

    data = json.loads(out)
    assert data == {"findings": ["off-by-one in pager", "N+1 query in listing"]}
    assert sorted(announced) == ["sweep1::wf1", "sweep1::wf2", "sweep1::wf3"]
