"""Subagent safety: tool-use hooks fire for a sub-agent's autonomous tool calls,
a sub-agent crash is contained (it never takes down the spawning turn), and a
runaway sub-agent is bounded by a model-request cap."""

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.agent import Harness, HarnessConfig
from marim_harness.deps import Deps
from marim_harness.hooks import events as hook_events
from marim_harness.permissions import Mode
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import _make_harness, _text_model


class _RecordingHooks:
    """Stands in for a configured HookRunner: records every (event, tool_name)
    dispatched so a test can assert the sub-agent's tool calls reached the engine."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    async def dispatch(self, event: str, payload: dict):
        self.events.append((event, str(payload.get("tool_name", ""))))
        return None


def _probe_agent(tmp_path: Path) -> None:
    """Write a custom sub-agent whose only tool is glob, so a streaming TestModel
    drives exactly one read-only tool call (no network tools to fire)."""
    agents_dir = tmp_path / ".marim" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "probe.md").write_text(
        "---\nname: probe\ndescription: test probe\ntools: glob\n---\nYou are a probe.\n"
    )


@pytest.mark.anyio
async def test_foreground_subagent_tool_calls_fire_hooks(tmp_path: Path):
    """A foreground sub-agent's tool calls go through the same Pre/PostToolUse
    hooks engine as the main agent's — so user-configured guardrails apply to a
    sub-agent's autonomous actions, not just the orchestrator's."""
    from pydantic_ai.models.test import TestModel

    _probe_agent(tmp_path)
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    recorder = _RecordingHooks()
    deps.hooks = recorder
    h = _make_harness(TestModel(), deps)

    await h.subagents.run("probe", "investigate", "sid")
    assert (hook_events.PRE_TOOL_USE, "glob") in recorder.events
    assert (hook_events.POST_TOOL_USE, "glob") in recorder.events


@pytest.mark.anyio
async def test_background_subagent_tool_calls_fire_hooks(tmp_path: Path):
    """The background path fires Pre/PostToolUse too, even though it does not
    stream to the UI — a detached sub-agent still runs under the hooks engine."""
    from pydantic_ai.models.test import TestModel

    _probe_agent(tmp_path)
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    recorder = _RecordingHooks()
    deps.hooks = recorder
    h = _make_harness(TestModel(), deps)

    await h.subagents.run_background("probe", "investigate")
    assert (hook_events.PRE_TOOL_USE, "glob") in recorder.events
    assert (hook_events.POST_TOOL_USE, "glob") in recorder.events


@pytest.mark.anyio
async def test_foreground_subagent_error_is_contained(tmp_path: Path):
    """A sub-agent that raises mid-run does not propagate into the spawning turn:
    run() returns an error string so sibling spawns in a fan-out survive, and the
    crash never corrupts the session token total."""

    class _BoomAgent:
        async def run(self, task, **kwargs):
            raise RuntimeError("boom")

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(_text_model(), deps)
    h.subagents.build = lambda type, max_output_chars=None, model=None, workspace_root=None: (
        _BoomAgent(), None
    )
    assert h.session.total_tokens == 0

    out = await h.subagents.run("explore", "do it", "sid")
    assert "boom" in out
    assert h.session.total_tokens == 0


@pytest.mark.anyio
async def test_subagent_request_limit_bounds_runaway(tmp_path: Path):
    """A sub-agent stuck in a tool-call loop is bounded by the model-request cap:
    run() returns an error string instead of looping forever or raising into the
    turn."""

    def fn(messages, info):  # never produces a final text — always calls a tool
        return ModelResponse(parts=[ToolCallPart(tool_name="glob", args={"pattern": "*"})])

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = Harness(
        model=FunctionModel(fn), provider=BuiltinToolProvider(), deps=deps,
        instructions="x", config=HarnessConfig(subagent_request_limit=2),
    )
    out = await h.subagents.run("explore", "loop forever", "sid")
    assert isinstance(out, str)
    assert "limit" in out.lower() or "exceed" in out.lower()


@pytest.mark.anyio
async def test_build_returns_exactly_one_of_sub_or_err(tmp_path: Path):
    """SubagentRunner.build must return (sub, None) XOR (None, err) — never
    (None, None) and never (sub, err). The XOR contract is what the runners'
    cleanup logic relies on; a future change that violates it would silently
    leave worktrees orphaned."""
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(_text_model(), deps)

    # Unknown type → (None, err)
    sub, err = h.subagents.build("nonexistent-type")
    assert sub is None and err is not None

    # Valid built-in (explore) → (sub, None)
    sub, err = h.subagents.build("explore")
    assert sub is not None and err is None
