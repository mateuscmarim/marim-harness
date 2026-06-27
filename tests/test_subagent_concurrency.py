"""Sub-agent fan-out concurrency cap.

When the orchestrator fans out many spawns at once, every spawn fires a model
request concurrently — which is exactly what trips an upstream provider's
rate limit on a shared route. ``SubagentRunner(concurrency=N)`` bounds how many
spawns run their model loop at the same time; the rest queue. ``concurrency=None``
(the default) keeps the old unbounded behaviour.
"""

import asyncio
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.runtime.deps import Deps
from marim_harness.runtime.permissions import Mode
from marim_harness.subagents import SubagentRunner
from tests.conftest import _make_harness


def _tracking_model(active: dict) -> FunctionModel:
    """A model that records how many runs are inside it at once."""
    async def fn(messages, info):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.05)
        active["now"] -= 1
        return ModelResponse(parts=[TextPart(content="ok")])
    return FunctionModel(fn)


def _runner_with_concurrency(tmp_path: Path, model, concurrency: int | None):
    base = _make_harness(model, Deps(workspace_root=tmp_path, mode=Mode.auto)).subagents
    return SubagentRunner(
        base.provider, base.mcp, base.deps, base.hooks, base.session,
        get_model=base._get_model, concurrency=concurrency,
    )


@pytest.mark.anyio
async def test_concurrency_cap_bounds_simultaneous_spawns(tmp_path: Path):
    active = {"now": 0, "max": 0}
    runner = _runner_with_concurrency(tmp_path, _tracking_model(active), concurrency=2)
    await asyncio.gather(*[
        runner.run("explore", f"t{i}", stream_id=f"s{i}") for i in range(5)
    ])
    assert active["max"] == 2


@pytest.mark.anyio
async def test_unbounded_by_default(tmp_path: Path):
    active = {"now": 0, "max": 0}
    runner = _runner_with_concurrency(tmp_path, _tracking_model(active), concurrency=None)
    await asyncio.gather(*[
        runner.run("explore", f"t{i}", stream_id=f"s{i}") for i in range(5)
    ])
    assert active["max"] == 5


def test_harness_config_threads_concurrency_to_the_runner(tmp_path: Path):
    """The HarnessConfig knob reaches the runner that enforces it."""
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_tracking_model({"now": 0, "max": 0}), deps,
                            subagent_concurrency=4)
    assert harness.subagents._concurrency == 4
