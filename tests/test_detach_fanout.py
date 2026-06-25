"""Detached fan-out: spawn_agent auto-routes to a background job when the
detach-fanout mode is on and the session is interactive."""

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.deps import Deps
from marim_harness.permissions import Mode
from marim_harness.tools.provider import _DETACH_OUTPUT_BUDGET
from tests.conftest import _last_instructions, _make_harness


def _spawn_once_model() -> FunctionModel:
    """Main agent: emit one spawn_agent (background omitted), then finish."""
    def fn(messages, info):
        if "sub-agent" in _last_instructions(messages):
            return ModelResponse(parts=[TextPart(content="SUB")])
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "ToolReturnPart" and \
                        getattr(p, "tool_name", "") == "spawn_agent":
                    return ModelResponse(parts=[TextPart(content="done")])
        return ModelResponse(parts=[ToolCallPart(
            tool_name="spawn_agent", args={"type": "explore", "task": "look"})])
    return FunctionModel(fn)


@pytest.mark.anyio
async def test_detach_mode_routes_spawn_to_background(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_spawn_once_model(), deps)
    harness.deps.detach_fanout = True
    harness.deps.interactive = True
    await harness.run_turn("go")
    # A background job was registered (not run inline) ...
    assert len(harness.deps.jobs.list()) == 1
    # ... and the tool returned the detached handoff, visible in history.
    blob = "".join(
        str(p.content)
        for m in harness.session.history
        for p in getattr(m, "parts", [])
        if type(p).__name__ == "ToolReturnPart"
    )
    assert "Started detached sub-agent" in blob


@pytest.mark.anyio
async def test_inline_when_not_interactive(tmp_path: Path):
    """detach_fanout on but no UI attached (headless) → spawn runs inline."""
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_spawn_once_model(), deps)
    harness.deps.detach_fanout = True
    harness.deps.interactive = False
    await harness.run_turn("go")
    assert harness.deps.jobs.list() == []


@pytest.mark.anyio
async def test_auto_detach_defaults_output_budget(tmp_path: Path):
    """When spawn_agent is auto-detached and the model passes no max_output_chars,
    run_background_agent must receive _DETACH_OUTPUT_BUDGET (not None), so the
    report is distilled + hard-capped before landing in the digest."""
    recorded: list = []

    async def _stub_background(
        type: str, task: str, mcp_names, max_output_chars, model, isolation
    ) -> str:
        recorded.append(max_output_chars)
        return "ok"

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_spawn_once_model(), deps)
    harness.deps.detach_fanout = True
    harness.deps.interactive = True
    harness.deps.services.run_background_agent = _stub_background

    await harness.run_turn("go")

    assert len(recorded) == 1, "run_background_agent was not called exactly once"
    assert recorded[0] == _DETACH_OUTPUT_BUDGET, (
        f"expected max_output_chars={_DETACH_OUTPUT_BUDGET}, got {recorded[0]}"
    )


def test_spawn_agent_accepts_a_description_param():
    """The model habitually passes a `description` (Claude Code's Task tool has
    one); spawn_agent must accept it so a fan-out doesn't fail validation."""
    import inspect

    from marim_harness.tools.provider import spawn_agent

    assert "description" in inspect.signature(spawn_agent).parameters


def _bg_described_spawn_model() -> FunctionModel:
    """Main agent: emit one explicit background spawn with a short description,
    then finish. The sub-agent itself returns immediately."""
    def fn(messages, info):
        if "sub-agent" in _last_instructions(messages):
            return ModelResponse(parts=[TextPart(content="SUB")])
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "ToolReturnPart" and \
                        getattr(p, "tool_name", "") == "spawn_agent":
                    return ModelResponse(parts=[TextPart(content="done")])
        return ModelResponse(parts=[ToolCallPart(
            tool_name="spawn_agent",
            args={"type": "explore",
                  "task": "You are doing a thorough review of the TUI.\n\n## Scope\n…",
                  "description": "Review TUI subsystem",
                  "background": True})])
    return FunctionModel(fn)


@pytest.mark.anyio
async def test_background_job_label_uses_description_not_full_task(tmp_path: Path):
    """A background spawn's job label is the short `description` (so the jobs panel
    reads `explore: Review TUI subsystem`), not the full composed task prompt."""
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_bg_described_spawn_model(), deps)
    await harness.run_turn("go")
    jobs = harness.deps.jobs.list()
    assert len(jobs) == 1
    assert jobs[0].label == "explore: Review TUI subsystem"
