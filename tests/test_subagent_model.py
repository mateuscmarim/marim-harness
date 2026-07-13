"""Per-spawn model override: a spawn can run its sub-agent on a different model
than the orchestrator (e.g. a cheap model for read-only fan-out), falling back to
the current model when no override is given."""

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.config.model import SubagentTiers
from marim_harness.tools.names import GATED_TOOLS
from marim_harness.workspace.agents import _builtins
from tests.conftest import _make_deps, _make_harness, _text_model


def _spawn_with_model_model() -> FunctionModel:
    """Main agent: spawn explore with an explicit model override, then echo."""
    def fn(messages, info):
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "ToolReturnPart" and \
                        getattr(p, "tool_name", "") == "spawn_agent":
                    return ModelResponse(parts=[TextPart(content=f"done: {p.content}")])
        return ModelResponse(parts=[ToolCallPart(
            tool_name="spawn_agent",
            args={"type": "explore", "task": "find X", "model": "cheap"},
        )])

    return FunctionModel(fn)


def test_build_uses_current_model_by_default(tmp_path: Path):
    """With no override, build() uses the harness's current model and never calls
    the per-spawn resolver."""
    deps = _make_deps(tmp_path)
    h = _make_harness(_text_model(), deps)
    h.subagents._build_model = lambda mid: pytest.fail("resolver must not run")

    sub, err = h.subagents.build("explore")
    assert err is None
    assert sub.model is h.current_model


def test_build_resolves_model_override(tmp_path: Path):
    """With an override, build() resolves it through the injected resolver and the
    sub-agent runs on the resolved model, not the current one."""
    deps = _make_deps(tmp_path)
    h = _make_harness(_text_model(), deps)
    other = _text_model()
    seen: dict = {}

    def resolve(mid):
        seen["id"] = mid
        return other

    h.subagents._build_model = resolve
    sub, err = h.subagents.build("explore", model="cheap")
    assert err is None
    assert seen["id"] == "cheap"
    assert sub.model is other
    assert sub.model is not h.current_model


def test_build_model_override_unavailable_without_resolver(tmp_path: Path):
    """When no resolver is wired (no model source), an override can't be honored —
    build() returns a clear error rather than silently ignoring the request."""
    deps = _make_deps(tmp_path)
    h = _make_harness(_text_model(), deps)
    assert h.subagents._build_model is None  # default: no model source in tests

    sub, err = h.subagents.build("explore", model="cheap")
    assert sub is None
    assert err is not None
    assert "cheap" in err


@pytest.mark.anyio
async def test_run_forwards_model_to_build(tmp_path: Path):
    """run() threads its model argument into build()."""
    deps = _make_deps(tmp_path)
    h = _make_harness(_text_model(), deps)
    seen: dict = {}

    def fake_build(
        type, max_output_chars=None, model=None, workspace_root=None, *,
        defn=None, depth=0, mask_trigger=None, checkpoint=None, output_schema=None,
        tier=None,
    ):
        seen["model"] = model
        return None, "stop here"

    h.subagents.build = fake_build
    await h.subagents.run("explore", "go", "sid", model="cheap")
    assert seen["model"] == "cheap"


@pytest.mark.anyio
async def test_spawn_agent_tool_forwards_model(tmp_path: Path):
    """The spawn_agent tool passes its model argument down to the foreground
    runner, so the model's choice of model reaches the sub-agent build."""
    captured: dict = {}

    async def fake_run(type, task, stream_id, mcp_names=None,
                       max_output_chars=None, model=None, isolation=None,
                       caller_depth: int = 0, tier=None):
        captured["model"] = model
        return "REPORT"

    deps = _make_deps(tmp_path)
    h = _make_harness(_spawn_with_model_model(), deps)
    h.deps.services.run_subagent = fake_run
    out = await h.run_turn("investigate")
    assert "REPORT" in out
    assert captured["model"] == "cheap"


def test_build_wires_configured_tiers_through_read_only_and_tool_reach(tmp_path: Path):
    """Integration coverage for build() with a CONFIGURED SubagentTiers: the
    real ``read_only = not (defn.tools & GATED_TOOLS)`` computation and the
    ``spec_tier=defn.tier`` wiring must route a read-only built-in ("explore")
    to the cheap tier and a mutating one ("general") to the high tier. Would
    fail if the `not` were dropped/inverted or if build() read `defn.model`
    (a different field) instead of `defn.tier`."""
    explore = _builtins()["explore"]
    general = _builtins()["general"]
    # Sanity-check the fixtures actually exercise the read_only line as intended:
    # explore has no gated-tool overlap, general does.
    assert not (explore.tools & GATED_TOOLS)
    assert general.tools & GATED_TOOLS

    deps = _make_deps(tmp_path)
    tiers = SubagentTiers(cheap="tier-cheap-model", high="tier-high-model")
    h = _make_harness(_text_model(), deps, subagent_tiers=tiers)
    seen: list[str] = []

    def resolve(mid):
        seen.append(mid)
        return _text_model()

    h.subagents._build_model = resolve

    sub, err = h.subagents.build("explore")
    assert err is None
    assert seen == ["tier-cheap-model"]

    sub, err = h.subagents.build("general")
    assert err is None
    assert seen == ["tier-cheap-model", "tier-high-model"]
