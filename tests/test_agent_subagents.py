from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.runtime.harness import Harness
from marim_harness.runtime.permissions import Mode
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import (
    _edit_then_done_model,
    _last_instructions,
    _make_deps,
    _make_harness,
    _text_model,
)


def _spawn_then_done_model() -> FunctionModel:
    """Main agent: spawn an explore sub-agent, then echo its report. The same
    model backs the sub-agent, so it's told apart by its instructions."""
    def fn(messages, info):
        instr = _last_instructions(messages)
        if "sub-agent" in instr:
            return ModelResponse(parts=[TextPart(content="SUBREPORT")])
        ret = None
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "ToolReturnPart" and \
                        getattr(p, "tool_name", "") == "spawn_agent":
                    ret = str(p.content)
        if ret is not None:
            return ModelResponse(parts=[TextPart(content=f"done: {ret}")])
        return ModelResponse(parts=[ToolCallPart(
            tool_name="spawn_agent", args={"type": "explore", "task": "find X"}
        )])

    return FunctionModel(fn)


def _capture_subagent(h, report="report"):
    """Replace _build_subagent so the spawned agent's run() records the toolsets
    it was given and returns a canned report. Returns the capture dict."""
    from types import SimpleNamespace

    from pydantic_ai.usage import RunUsage

    cap: dict = {}

    class _StubAgent:
        async def run(self, task, **kwargs):
            cap["task"] = task
            cap["toolsets"] = kwargs.get("toolsets")
            return SimpleNamespace(output=report, usage=RunUsage(), all_messages=lambda: [])

    h.subagents.build = lambda *a, **k: (_StubAgent(), None)
    return cap


@pytest.mark.anyio
async def test_subagent_output_cap_spills_full_and_returns_pointer(tmp_path: Path):
    """When the spawner sets max_output_chars, an over-budget sub-agent report is
    written to a file and the main agent receives a within-budget head + pointer,
    not the raw dump — so a per-call cap actually bounds the inflow."""
    long = "CONCLUSION first. " + "filler. " * 500

    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content=long)])

    deps = _make_deps(tmp_path)
    harness = _make_harness(FunctionModel(fn), deps)
    result = await harness.subagents.run("explore", "go", "tc-1", None, 200)

    assert len(result) <= 200
    assert result.startswith("CONCLUSION first.")
    spill = tmp_path / ".marim" / "subagent-output" / "tc-1.md"
    assert spill.read_text() == long
    assert ".marim/subagent-output/tc-1.md" in result


@pytest.mark.anyio
async def test_subagent_no_cap_returns_full_output(tmp_path: Path):
    """Without a cap, the sub-agent's full report passes through unchanged and no
    spill file is created."""
    long = "x" * 5000

    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content=long)])

    deps = _make_deps(tmp_path)
    harness = _make_harness(FunctionModel(fn), deps)
    result = await harness.subagents.run("explore", "go", "tc-2", None, None)

    assert result == long
    assert not (tmp_path / ".marim" / "subagent-output").exists()


@pytest.mark.anyio
async def test_run_subagent_returns_output(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    h = _make_harness(TestModel(call_tools=[], custom_output_text="FINDINGS"), deps)
    out = await h.subagents.run("explore", "find the parser", "sid")
    assert out == "FINDINGS"


@pytest.mark.anyio
async def test_run_subagent_counts_usage_in_session_total(tmp_path: Path):
    """A foreground spawn's own token spend lands in the session total, not just
    its returned report — counted immediately as the run completes."""
    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    h = _make_harness(TestModel(call_tools=[], custom_output_text="FINDINGS"), deps)
    assert h.session.total_tokens == 0
    await h.subagents.run("explore", "find the parser", "sid")
    assert h.session.total_tokens > 0


@pytest.mark.anyio
async def test_run_subagent_restricts_tools_by_mode(tmp_path: Path):
    from marim_harness.tools.provider import NET_TOOLS, READ_TOOLS, SUBAGENT_TOOLS

    captured: dict = {}

    def fn(messages, info):
        captured["tools"] = {t.name for t in info.function_tools}
        return ModelResponse(parts=[TextPart(content="report")])

    deps = _make_deps(tmp_path, mode=Mode.ask)
    h = _make_harness(FunctionModel(fn), deps)

    # ask mode: general drops its gated tools, keeping local reads + net tools.
    # With nested sub-agents, spawn_agent is also registered at depth 0.
    out = await h.subagents.run("general", "do it", "sid")
    assert out == "report"
    assert captured["tools"] == set(READ_TOOLS | NET_TOOLS | {"spawn_agent"})

    # auto mode: the full set, including write/edit/bash and spawn_agent.
    deps.workspace.mode = Mode.auto
    await h.subagents.run("general", "do it", "sid")
    assert captured["tools"] == set(SUBAGENT_TOOLS | {"spawn_agent"})


@pytest.mark.anyio
async def test_subagent_handler_forwards_run_usage(tmp_path: Path):
    """The sub-agent event handler tags each forwarded event with the run's live
    usage (the whole RunUsage), so the UI can show the token total, cache split,
    and cost in the widget — not just a bare count."""
    from types import SimpleNamespace

    recorded: list = []

    async def cb(stream_id, event, usage):
        recorded.append((stream_id, event, usage))

    deps = _make_deps(tmp_path)
    deps.ui.on_subagent_event = cb
    h = _make_harness(_text_model(), deps)
    handler = h.subagents.handler("sid")

    async def events():
        yield "evt-a"
        yield "evt-b"

    usage = SimpleNamespace(total_tokens=4096)
    ctx = SimpleNamespace(usage=usage)
    await handler(ctx, events())

    # The full usage object is forwarded verbatim, tagged with the stream id.
    assert recorded == [
        ("sid", "evt-a", usage),
        ("sid", "evt-b", usage),
    ]


@pytest.mark.anyio
async def test_subagent_handler_none_without_listener(tmp_path: Path):
    deps = _make_deps(tmp_path)  # no on_subagent_event
    h = _make_harness(_text_model(), deps)
    assert h.subagents.handler("sid") is None


@pytest.mark.anyio
async def test_run_subagent_unknown_type(tmp_path: Path):
    deps = _make_deps(tmp_path)
    h = _make_harness(_text_model(), deps)
    out = await h.subagents.run("ghost", "do it", "sid")
    assert "No sub-agent type 'ghost'" in out
    assert "explore" in out and "general" in out  # lists what's available


@pytest.mark.anyio
async def test_agent_index_injected(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    captured: dict = {}

    def fn(messages, info):
        captured["instructions"] = _last_instructions(messages)
        return ModelResponse(parts=[TextPart(content="ok")])

    deps = _make_deps(tmp_path)
    h = Harness(model=FunctionModel(fn), provider=BuiltinToolProvider(), deps=deps,
                instructions="BASE PROMPT")
    await h.run_turn("hi")
    instr = captured["instructions"]
    assert "spawn_agent" in instr
    assert "explore" in instr
    assert "general" in instr


@pytest.mark.anyio
async def test_run_background_subagent_returns_output(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    h = _make_harness(TestModel(call_tools=[], custom_output_text="BG REPORT"), deps)
    out = await h.subagents.run_background("explore", "scan the repo")
    assert out == "BG REPORT"


@pytest.mark.anyio
async def test_run_background_output_cap_spills_full_and_returns_pointer(tmp_path: Path):
    """A background spawn's report is hard-capped too: over budget it spills to a
    file and the stored result is a within-budget head + pointer, so a giant
    background report can't flood context when it's later pulled."""
    long = "CONCLUSION first. " + "filler. " * 500

    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content=long)])

    deps = _make_deps(tmp_path)
    harness = _make_harness(FunctionModel(fn), deps)
    result = await harness.subagents.run_background("explore", "go", None, 200)

    assert len(result) <= 200
    assert result.startswith("CONCLUSION first.")
    spill_dir = tmp_path / ".marim" / "subagent-output"
    files = list(spill_dir.glob("*.md"))
    assert len(files) == 1 and files[0].read_text() == long
    assert ".marim/subagent-output/" in result


@pytest.mark.anyio
async def test_run_background_no_cap_returns_full_output(tmp_path: Path):
    """Without a cap, a background report passes through unchanged and nothing
    spills."""
    long = "y" * 5000

    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content=long)])

    deps = _make_deps(tmp_path)
    harness = _make_harness(FunctionModel(fn), deps)
    result = await harness.subagents.run_background("explore", "go")
    assert result == long
    assert not (tmp_path / ".marim" / "subagent-output").exists()


@pytest.mark.anyio
async def test_run_background_subagent_counts_and_persists_usage(tmp_path: Path):
    """A background spawn finishes off-turn, so its spend is folded into the
    session total AND persisted right away — not left for the next turn."""
    from pydantic_ai.models.test import TestModel

    from marim_harness.session import SessionManager

    deps = _make_deps(tmp_path)
    store = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data").create()
    h = Harness(
        model=TestModel(call_tools=[], custom_output_text="BG"),
        provider=BuiltinToolProvider(), deps=deps, instructions="x", store=store,
    )
    assert h.session.total_tokens == 0
    await h.subagents.run_background("explore", "scan the repo")
    assert h.session.total_tokens > 0
    # The spend reached disk immediately, without waiting for a run_turn.
    _, usage, _, _ = store.load()
    assert usage.total_tokens == h.session.total_tokens


@pytest.mark.anyio
async def test_run_background_subagent_unknown_type(tmp_path: Path):
    deps = _make_deps(tmp_path)
    h = _make_harness(_text_model(), deps)
    out = await h.subagents.run_background("ghost", "do it")
    assert "No sub-agent type 'ghost'" in out


@pytest.mark.anyio
async def test_run_background_subagent_respects_mode(tmp_path: Path):
    from marim_harness.tools.provider import NET_TOOLS, READ_TOOLS, SUBAGENT_TOOLS

    captured: dict = {}

    def fn(messages, info):
        captured["tools"] = {t.name for t in info.function_tools}
        return ModelResponse(parts=[TextPart(content="r")])

    deps = _make_deps(tmp_path, mode=Mode.ask)
    h = _make_harness(FunctionModel(fn), deps)
    await h.subagents.run_background("general", "x")
    # With nested sub-agents, spawn_agent is registered at depth 0.
    assert captured["tools"] == set(READ_TOOLS | NET_TOOLS | {"spawn_agent"})
    deps.workspace.mode = Mode.auto
    await h.subagents.run_background("general", "x")
    assert captured["tools"] == set(SUBAGENT_TOOLS | {"spawn_agent"})


@pytest.mark.anyio
async def test_run_background_streams_events_to_listener(tmp_path: Path):
    """A background spawn with a stream_id + UI listener forwards its run events,
    exactly like a foreground spawn — the Phase 2 live-streaming wiring."""
    recorded: list[str] = []

    async def cb(stream_id, event, usage):
        recorded.append(stream_id)

    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="BG ANSWER")])

    async def stream_fn(messages, info):
        yield "BG ANSWER"

    deps = _make_deps(tmp_path)
    deps.ui.on_subagent_event = cb
    h = _make_harness(FunctionModel(fn, stream_function=stream_fn), deps)
    out = await h.subagents.run_background("explore", "scan", stream_id="call_99")
    assert out == "BG ANSWER"
    assert recorded and all(sid == "call_99" for sid in recorded)


@pytest.mark.anyio
async def test_run_background_without_stream_id_does_not_forward(tmp_path: Path):
    """An empty stream_id (headless / no tool-call id) forwards nothing, even when
    a listener is wired."""
    recorded: list[str] = []

    async def cb(stream_id, event, usage):
        recorded.append(stream_id)

    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="X")])

    deps = _make_deps(tmp_path)
    deps.ui.on_subagent_event = cb
    h = _make_harness(FunctionModel(fn), deps)
    await h.subagents.run_background("explore", "scan")  # no stream_id
    assert recorded == []


def test_background_agent_runner_wired(tmp_path: Path):
    deps = _make_deps(tmp_path)
    h = _make_harness(_text_model(), deps)
    assert deps.services.run_background_agent == h.subagents.run_background


@pytest.mark.anyio
async def test_spawn_agent_tool_runs_subagent_end_to_end(tmp_path: Path):
    deps = _make_deps(tmp_path)
    h = _make_harness(_spawn_then_done_model(), deps)
    out = await h.run_turn("investigate")
    assert out == "done: SUBREPORT"


def test_parallel_tool_calls_enabled_on_main_agent(tmp_path):
    """The main agent forces parallel tool calls on, so providers that support
    it (Anthropic, OpenAI, Groq, xAI, …) run same-turn tool calls concurrently
    rather than relying on a provider default that may be off."""
    deps = _make_deps(tmp_path)
    harness = _make_harness(_edit_then_done_model(), deps)
    assert harness.agent.model_settings is not None
    assert harness.agent.model_settings.get("parallel_tool_calls") is True


def test_parallel_tool_calls_enabled_on_subagent(tmp_path):
    """Spawned sub-agents inherit the same setting — fan-out work should be as
    parallel as the main agent's."""
    deps = _make_deps(tmp_path)
    harness = _make_harness(_edit_then_done_model(), deps)
    sub, err = harness.subagents.build("explore")
    assert err is None
    assert sub.model_settings is not None
    assert sub.model_settings.get("parallel_tool_calls") is True


@pytest.mark.anyio
async def test_run_subagent_grants_named_server(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    h = _make_harness(TestModel(), deps)
    server = SimpleNamespace(tool_prefix="mddocs")
    h.mcp._live_servers = [server]
    cap = _capture_subagent(h)

    out = await h.subagents.run("explore", "read docs", "sid", ["mddocs"])
    assert out == "report"
    # Identity, not just equality: gating relies on the SAME hooked server
    # object reaching run() — a copy would silently drop the approval hook.
    assert cap["toolsets"][0] is server


@pytest.mark.anyio
async def test_run_subagent_default_grants_no_servers(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = [SimpleNamespace(tool_prefix="mddocs")]
    cap = _capture_subagent(h)

    await h.subagents.run("explore", "investigate", "sid")
    assert cap["toolsets"] == []


@pytest.mark.anyio
async def test_run_subagent_prepends_unknown_note(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = []
    _capture_subagent(h, report="FINDINGS")

    out = await h.subagents.run("explore", "investigate", "sid", ["nope"])
    assert "nope" in out
    assert out.rstrip().endswith("FINDINGS")


@pytest.mark.anyio
async def test_run_background_subagent_grants_named_server(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    h = _make_harness(TestModel(), deps)
    server = SimpleNamespace(tool_prefix="mddocs")
    h.mcp._live_servers = [server]
    cap = _capture_subagent(h)

    out = await h.subagents.run_background("general", "do it", ["mddocs"])
    assert out == "report"
    # Identity, not just equality: the background path must also forward the
    # SAME hooked server object so its approval gating is preserved.
    assert cap["toolsets"][0] is server


@pytest.mark.anyio
async def test_run_background_subagent_prepends_unknown_note(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = []
    _capture_subagent(h, report="DONE")

    out = await h.subagents.run_background("general", "do it", ["nope"])
    assert "nope" in out
    assert out.rstrip().endswith("DONE")


@pytest.mark.anyio
async def test_run_background_subagent_default_grants_no_servers(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = [SimpleNamespace(tool_prefix="mddocs")]
    cap = _capture_subagent(h)

    await h.subagents.run_background("general", "do it")
    assert cap["toolsets"] == []


@pytest.mark.anyio
async def test_run_background_isolates_task_list(tmp_path: Path):
    """A background sub-agent gets its OWN empty TaskList so its checklist never
    pollutes (or persists as) the user's session tasks. The foreground path keeps
    sharing the parent's tasks (it runs inside the turn, no race)."""
    from types import SimpleNamespace

    from pydantic_ai.usage import RunUsage

    from marim_harness.tasks import TaskList

    cap: dict = {}

    class _StubAgent:
        async def run(self, task, **kwargs):
            cap["deps"] = kwargs.get("deps")
            return SimpleNamespace(output="report", usage=RunUsage(), all_messages=lambda: [])

    deps = _make_deps(tmp_path)
    # Give the parent a non-empty checklist so a leak would be visible.
    deps.tasks.replace([{"text": "user task", "status": "in_progress"}])
    h = _make_harness(_text_model(), deps)
    h.subagents.build = lambda *a, **k: (_StubAgent(), None)

    await h.subagents.run_background("explore", "scan")
    bg_deps = cap["deps"]
    # Background got a DIFFERENT, empty TaskList object...
    assert isinstance(bg_deps.tasks, TaskList)
    assert bg_deps.tasks is not deps.tasks
    assert bg_deps.tasks.to_payload() == []
    # ...while jobs (and the rest of Deps) stay shared.
    assert bg_deps.jobs is deps.jobs
    # The parent's checklist is untouched.
    assert deps.tasks.to_payload() == [{"text": "user task", "status": "in_progress"}]


def test_harness_exposes_wake_defaults(tmp_path: Path):
    """The Harness surfaces the wake knobs so the TUI app can seed its scheduler;
    with no config passed, the defaults are on / cap 8."""
    deps = _make_deps(tmp_path)
    h = _make_harness(_text_model(), deps)
    assert h.autonomous_wake is True
    assert h.wake_depth_cap == 8


def test_harness_takes_wake_flags_from_config(tmp_path: Path):
    from marim_harness.runtime.harness import HarnessConfig

    deps = _make_deps(tmp_path)
    h = Harness(
        model=_text_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="x",
        config=HarnessConfig(autonomous_wake=False, wake_depth_cap=7),
    )
    assert h.autonomous_wake is False
    assert h.wake_depth_cap == 7


@pytest.mark.anyio
async def test_subagent_depth_propagated_via_deps(tmp_path: Path):
    """When a sub-agent is built, its Deps carry the correct subagent_depth."""
    captured: dict = {}

    def fn(messages, info):
        captured["tools"] = {t.name for t in info.function_tools}
        return ModelResponse(parts=[TextPart(content="report")])

    deps = _make_deps(tmp_path)
    h = _make_harness(FunctionModel(fn), deps)

    # The runner should be built with max_depth=3
    assert h.subagents._max_depth == 3

    # Build a depth-0 sub-agent (main agent spawning)
    sub, err = h.subagents.build("explore", depth=0)
    assert sub is not None
    # depth-0 spawn → child at depth 1, which can still spawn (1+1=2 < 3)
    # so spawn_agent should be present
    # Run the sub-agent to capture what tools it was given
    await sub.run("test task", deps=deps)
    assert "spawn_agent" in captured["tools"]
