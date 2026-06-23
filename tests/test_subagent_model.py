"""Per-spawn model override: a spawn can run its sub-agent on a different model
than the orchestrator (e.g. a cheap model for read-only fan-out), falling back to
the current model when no override is given."""

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.deps import Deps
from marim_harness.permissions import Mode
from tests.conftest import _make_harness, _text_model


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
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(_text_model(), deps)
    h.subagents._build_model = lambda mid: pytest.fail("resolver must not run")

    sub, err = h.subagents.build("explore")
    assert err is None
    assert sub.model is h.current_model


def test_build_resolves_model_override(tmp_path: Path):
    """With an override, build() resolves it through the injected resolver and the
    sub-agent runs on the resolved model, not the current one."""
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
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
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(_text_model(), deps)
    assert h.subagents._build_model is None  # default: no model source in tests

    sub, err = h.subagents.build("explore", model="cheap")
    assert sub is None
    assert err is not None
    assert "cheap" in err


@pytest.mark.anyio
async def test_run_forwards_model_to_build(tmp_path: Path):
    """run() threads its model argument into build()."""
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(_text_model(), deps)
    seen: dict = {}

    def fake_build(type, max_output_chars=None, model=None, workspace_root=None):
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
                       max_output_chars=None, model=None, isolation=None):
        captured["model"] = model
        return "REPORT"

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(_spawn_with_model_model(), deps)
    h.deps.services.run_subagent = fake_run
    out = await h.run_turn("investigate")
    assert "REPORT" in out
    assert captured["model"] == "cheap"
