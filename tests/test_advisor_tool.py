"""The advisor tool: presence gated by the services.advise seam (prepare
hook), advice pass-through, and the per-turn call cap."""

from types import SimpleNamespace

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests

from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.runtime.permissions import Mode
from marim_harness.tools import advisor_tools
from marim_harness.tools.provider import BuiltinToolProvider


def _deps(tmp_path) -> Deps:
    return Deps(workspace=WorkspaceConfig(root=tmp_path, mode=Mode.auto))


def _tool_capture_agent(seen: list):
    async def fn(messages, info: AgentInfo) -> ModelResponse:
        seen.extend(t.name for t in info.function_tools)
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(
        FunctionModel(fn), deps_type=Deps,
        output_type=[str, DeferredToolRequests],
    )
    BuiltinToolProvider().register(agent)
    return agent


async def _noop_advise(messages: list) -> str:
    return "stub advice"


@pytest.mark.anyio
async def test_tool_absent_when_no_advisor_configured(tmp_path):
    seen: list[str] = []
    deps = _deps(tmp_path)
    assert deps.services.advise is None
    await _tool_capture_agent(seen).run("hi", deps=deps)
    assert "advisor" not in seen
    assert "read_file" in seen  # sanity: registration itself worked


@pytest.mark.anyio
async def test_tool_present_when_advisor_configured(tmp_path):
    seen: list[str] = []
    deps = _deps(tmp_path)
    deps.services.advise = _noop_advise
    await _tool_capture_agent(seen).run("hi", deps=deps)
    assert "advisor" in seen


@pytest.mark.anyio
async def test_advisor_forwards_messages_and_returns_advice(tmp_path):
    got: list = []

    async def advise(messages: list) -> str:
        got.append(messages)
        return "Use TDD."

    deps = _deps(tmp_path)
    deps.services.advise = advise
    ctx = SimpleNamespace(deps=deps, messages=["m1", "m2"])
    out = await advisor_tools.advisor(ctx)
    assert out == "Use TDD."
    assert got == [["m1", "m2"]]
    assert deps.advisor_uses == 1


@pytest.mark.anyio
async def test_advisor_cap_exhaustion_returns_error_string(tmp_path):
    deps = _deps(tmp_path)
    deps.services.advise = _noop_advise
    deps.advisor_max_uses = 1
    ctx = SimpleNamespace(deps=deps, messages=[])
    assert await advisor_tools.advisor(ctx) == "stub advice"
    second = await advisor_tools.advisor(ctx)
    assert "limit" in second
    assert "Continue without" in second
    assert deps.advisor_uses == 1  # a refused call doesn't consume budget


@pytest.mark.anyio
async def test_advisor_without_seam_degrades(tmp_path):
    # Defensive: the prepare hook normally hides the tool, but a race (advisor
    # turned off mid-turn) can still land a call on a None seam.
    ctx = SimpleNamespace(deps=_deps(tmp_path), messages=[])
    out = await advisor_tools.advisor(ctx)
    assert "No advisor is configured" in out


@pytest.mark.anyio
async def test_run_turn_resets_advisor_uses(tmp_path):
    from marim_harness.runtime.harness import Harness

    deps = _deps(tmp_path)
    harness = Harness(TestModel(call_tools=[]), BuiltinToolProvider(), deps, "Be helpful.")
    deps.advisor_uses = 3
    await harness.run_turn("hi")
    assert deps.advisor_uses == 0
