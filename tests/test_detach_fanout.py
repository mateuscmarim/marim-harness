"""Detached fan-out: spawn_agent auto-routes to a background job when the
detach-fanout mode is on and the session is interactive."""

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.tools.spawn_tools import _DETACH_OUTPUT_BUDGET
from tests.conftest import _last_instructions, _make_deps, _make_harness


def _spawn_once_model() -> FunctionModel:
    """Main agent: emit one spawn_agent (background omitted), then finish."""
    def fn(messages, info):
        # Discriminate by the sub-agent prompt's workspace line — a bare
        # "sub-agent" substring also matches the main agent's spawn index.
        if "You are operating inside the workspace at" in _last_instructions(messages):
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
    deps = _make_deps(tmp_path)
    harness = _make_harness(_spawn_once_model(), deps)
    harness.deps.ui.detach_fanout = True
    harness.deps.ui.interactive = True
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
    deps = _make_deps(tmp_path)
    harness = _make_harness(_spawn_once_model(), deps)
    harness.deps.ui.detach_fanout = True
    harness.deps.ui.interactive = False
    await harness.run_turn("go")
    assert harness.deps.jobs.list() == []


@pytest.mark.anyio
async def test_auto_detach_defaults_output_budget(tmp_path: Path):
    """When spawn_agent is auto-detached and the model passes no max_output_chars,
    run_background_agent must receive _DETACH_OUTPUT_BUDGET (not None), so the
    report is distilled + hard-capped before landing in the digest."""
    recorded: list = []

    async def _stub_background(
        type: str, task: str, mcp_names, max_output_chars, model, isolation,
        stream_id: str = "", caller_depth: int = 0, tier=None, thinking=None,
    ) -> str:
        recorded.append(max_output_chars)
        return "ok"

    deps = _make_deps(tmp_path)
    harness = _make_harness(_spawn_once_model(), deps)
    harness.deps.ui.detach_fanout = True
    harness.deps.ui.interactive = True
    harness.deps.services.run_background_agent = _stub_background

    await harness.run_turn("go")

    assert len(recorded) == 1, "run_background_agent was not called exactly once"
    assert recorded[0] == _DETACH_OUTPUT_BUDGET, (
        f"expected max_output_chars={_DETACH_OUTPUT_BUDGET}, got {recorded[0]}"
    )


@pytest.mark.anyio
async def test_subagent_unset_spawn_runs_inline_not_detached(tmp_path: Path):
    """Auto-detach (detach-fanout) is top-level-only, just like explicit
    background. A sub-agent (depth > 0) that spawns a child with `background`
    unset must run it INLINE, even with detach_fanout + interactive on — a
    detached child of a sub-agent would be orphaned when the sub-agent's turn
    ends (the job registry, never the spawner, owns the report)."""
    from types import SimpleNamespace

    from marim_harness.tools.spawn_tools import spawn_agent

    calls = {"inline": False, "bg": False}

    async def fake_runner(
        type, task, tool_call_id, mcp_names, max_output_chars=None, model=None,
        isolation=None, caller_depth: int = 0, tier=None, output_schema=None,
        thinking=None,
    ):
        calls["inline"] = True
        return "inline-ok"

    def fake_bg(*a, **k):
        calls["bg"] = True

        async def _coro():
            return "bg"

        return _coro()

    deps = _make_deps(tmp_path, subagent_depth=1, detach_fanout=True, interactive=True)
    deps.services.run_subagent = fake_runner
    deps.services.run_background_agent = fake_bg
    ctx = SimpleNamespace(deps=deps, tool_call_id="tc")

    out = await spawn_agent(ctx, "general", "do child work")

    assert out == "inline-ok"
    assert calls["inline"] is True, "sub-agent's unset spawn should run inline"
    assert calls["bg"] is False, "sub-agent's unset spawn must not auto-detach"
    assert deps.jobs.list() == []


def _nested_spawn_model() -> FunctionModel:
    """One model serving three roles by inspecting its instructions:
      - main (depth 0): spawn a `general` child INLINE (background=False), then finish
      - general (depth 1): spawn an `explore` child with background UNSET, then
        report what it returned
      - explore (depth 2): the leaf — return a marker
    """
    def fn(messages, info):
        instr = _last_instructions(messages)

        def spawn_return(msgs):
            for m in msgs:
                for p in getattr(m, "parts", []):
                    if type(p).__name__ == "ToolReturnPart" and \
                            getattr(p, "tool_name", "") == "spawn_agent":
                        return str(p.content)
            return None

        if "exploration sub-agent" in instr:  # depth-2 leaf
            return ModelResponse(parts=[TextPart(content="LEAF-OK")])

        if "general-purpose sub-agent" in instr:  # depth-1
            child = spawn_return(messages)
            if child is not None:
                return ModelResponse(parts=[TextPart(content=f"CHILD: {child}")])
            return ModelResponse(parts=[ToolCallPart(
                tool_name="spawn_agent",
                args={"type": "explore", "task": "read"})])  # background UNSET

        # main agent (depth 0)
        if spawn_return(messages) is not None:
            return ModelResponse(parts=[TextPart(content="done")])
        return ModelResponse(parts=[ToolCallPart(
            tool_name="spawn_agent",
            args={"type": "general", "task": "spawn an explore child",
                  "background": False})])  # inline, so the depth-1 spawn is observable
    return FunctionModel(fn)


@pytest.mark.anyio
async def test_e2e_nested_subagent_spawn_stays_inline(tmp_path: Path):
    """End-to-end through the real Harness + SubagentRunner: with detach_fanout +
    interactive on, a sub-agent (depth 1) spawning a child must run it INLINE.
    Before the fix the depth-1 spawn auto-detached — registering a job and handing
    the sub-agent a detach message instead of the child's report. We assert the
    leaf's marker propagates back up (inline) and that NO job was registered."""
    deps = _make_deps(tmp_path)
    harness = _make_harness(_nested_spawn_model(), deps)
    harness.deps.ui.detach_fanout = True
    harness.deps.ui.interactive = True

    await harness.run_turn("go")

    # No background job: the depth-1 child ran inline, not detached.
    assert harness.deps.jobs.list() == []
    # The leaf marker flowed depth 2 → depth 1 → main, proving the nested spawn
    # returned its report synchronously rather than being orphaned to a job.
    blob = "".join(
        str(p.content)
        for m in harness.session.history
        for p in getattr(m, "parts", [])
        if type(p).__name__ == "ToolReturnPart"
    )
    assert "LEAF-OK" in blob


def test_spawn_agent_accepts_a_description_param():
    """The model habitually passes a `description` (Claude Code's Task tool has
    one); spawn_agent must accept it so a fan-out doesn't fail validation."""
    import inspect

    from marim_harness.tools.spawn_tools import spawn_agent

    assert "description" in inspect.signature(spawn_agent).parameters


def _bg_described_spawn_model() -> FunctionModel:
    """Main agent: emit one explicit background spawn with a short description,
    then finish. The sub-agent itself returns immediately."""
    def fn(messages, info):
        if "You are operating inside the workspace at" in _last_instructions(messages):
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
    deps = _make_deps(tmp_path)
    harness = _make_harness(_bg_described_spawn_model(), deps)
    await harness.run_turn("go")
    jobs = harness.deps.jobs.list()
    assert len(jobs) == 1
    assert jobs[0].label == "explore: Review TUI subsystem"
