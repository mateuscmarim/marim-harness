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

from marim_harness.subagents import SubagentRunner
from tests.conftest import _make_deps, _make_harness


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
    base = _make_harness(model, _make_deps(tmp_path)).subagents
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
async def test_unbounded_when_uncapped(tmp_path: Path):
    active = {"now": 0, "max": 0}
    runner = _runner_with_concurrency(tmp_path, _tracking_model(active), concurrency=None)
    await asyncio.gather(*[
        runner.run("explore", f"t{i}", stream_id=f"s{i}") for i in range(5)
    ])
    assert active["max"] == 5


def test_harness_config_threads_concurrency_to_the_runner(tmp_path: Path):
    """The HarnessConfig knob reaches the runner that enforces it."""
    deps = _make_deps(tmp_path)
    harness = _make_harness(_tracking_model({"now": 0, "max": 0}), deps,
                            subagent_concurrency=4)
    assert harness.subagents._concurrency == 4


def test_default_harness_config_is_capped(tmp_path: Path):
    """An out-of-the-box HarnessConfig bounds fan-out at the shared default —
    a runaway workflow (one spawn per character of a mis-typed args string, in
    one live run) must queue, not fire everything at once. Explicit None stays
    the unbounded escape hatch, shared with the MARIM_SUBAGENT_CONCURRENCY=0
    env sentinel."""
    from marim_harness.config.model import DEFAULT_SUBAGENT_CONCURRENCY

    deps = _make_deps(tmp_path)
    harness = _make_harness(_tracking_model({"now": 0, "max": 0}), deps)
    assert harness.subagents._concurrency == DEFAULT_SUBAGENT_CONCURRENCY

    deps2 = _make_deps(tmp_path / "w2")
    unbounded = _make_harness(_tracking_model({"now": 0, "max": 0}), deps2,
                              subagent_concurrency=None)
    assert unbounded.subagents._concurrency is None
