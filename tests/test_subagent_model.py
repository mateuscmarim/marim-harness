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


@pytest.mark.anyio
async def test_native_tier_spawn_reports_resolved_model_to_ui(tmp_path: Path):
    """A tier-routed NATIVE spawn reports its resolved model to the UI relabel
    callback (the same seam claude-cli uses), so the sub-agents card shows the
    tier model instead of the main-model fallback that the spawn_agent args imply.
    Regression: native tier spawns silently displayed the main model, because the
    card is built from the tool args (which carry no tier-resolved model) and only
    claude-cli spawns ever fired on_subagent_model to correct it."""
    deps = _make_deps(tmp_path)
    tiers = SubagentTiers(high="tier-high-model")
    h = _make_harness(_text_model(), deps, subagent_tiers=tiers)
    h.subagents._build_model = lambda mid: _text_model()
    seen: list[tuple[str, str]] = []

    async def on_model(stream_id: str, model: str) -> None:
        seen.append((stream_id, model))

    h.deps.ui.on_subagent_model = on_model
    # "general" is mutating (touches gated tools) → high tier → tier-high-model.
    await h.subagents.run("general", "do it", "sid-hi")
    assert ("sid-hi", "tier-high-model") in seen


@pytest.mark.anyio
async def test_native_spawn_reports_final_usage_to_ui(tmp_path: Path):
    """A native spawn pushes its FINAL run usage to the UI (the same seam the CLI
    backend uses), so a spawn that makes a single model request (no tool calls) still
    shows tokens/cost. Regression: the card accrued usage only from mid-stream events,
    which carry the run's *running* total — 0 throughout a one-request spawn, since the
    only response's usage isn't tallied until the run ends — so 0-tool spawns rendered
    blank tokens/cost while multi-request (tool-using) spawns did not."""
    deps = _make_deps(tmp_path)
    h = _make_harness(_text_model(), deps)  # _text_model: one text response, no tools
    seen: list[tuple[str, object]] = []

    async def on_usage(stream_id: str, usage: object) -> None:
        seen.append((stream_id, usage))

    h.deps.ui.on_subagent_usage = on_usage
    await h.subagents.run("explore", "reply ALPHA", "sid-u")
    assert seen and seen[0][0] == "sid-u"
    assert seen[0][1] is not None  # the run's final RunUsage, not None


@pytest.mark.anyio
async def test_native_inherit_spawn_does_not_relabel(tmp_path: Path):
    """A spawn that inherits the main model (no configured tier) does NOT fire the
    relabel callback — the card's main-model fallback is already correct, so there
    is nothing to override."""
    deps = _make_deps(tmp_path)
    h = _make_harness(_text_model(), deps)  # no tiers → every spawn inherits main
    seen: list[tuple[str, str]] = []

    async def on_model(stream_id: str, model: str) -> None:
        seen.append((stream_id, model))

    h.deps.ui.on_subagent_model = on_model
    await h.subagents.run("explore", "look", "sid-x")
    assert seen == []


@pytest.mark.anyio
async def test_set_subagent_tiering_enabled_flips_live_routing(tmp_path: Path):
    """Disabling tiering live makes new spawns inherit the main model instead of
    routing to their tier — without discarding the configured tier slugs. A
    mutating spawn that routed to tier-high-model no longer reports a resolved
    model (nothing to relabel), and re-enabling restores routing."""
    deps = _make_deps(tmp_path)
    tiers = SubagentTiers(high="tier-high-model")
    h = _make_harness(_text_model(), deps, subagent_tiers=tiers)
    h.subagents._build_model = lambda mid: _text_model()
    seen: list[tuple[str, str]] = []

    async def on_model(stream_id: str, model: str) -> None:
        seen.append((stream_id, model))

    h.deps.ui.on_subagent_model = on_model

    # Disable → the high-tier mutating spawn inherits main (no relabel fired).
    h.set_subagent_tiering_enabled(False)
    assert h.subagents._tiers.enabled is False
    assert h.subagents._tiers.high == "tier-high-model"  # slug preserved
    await h.subagents.run("general", "do it", "sid-off")
    assert seen == []

    # Re-enable → routing is restored and the tier model is reported again.
    h.set_subagent_tiering_enabled(True)
    await h.subagents.run("general", "do it", "sid-on")
    assert ("sid-on", "tier-high-model") in seen


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
