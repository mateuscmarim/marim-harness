"""End-to-end turn tests for a builder-built Harness.

These drive `HarnessBuilder(...).build()` through real turns with pydantic-ai's
`FunctionModel` (a scripted callable model, no network/API key needed) rather
than mocking any marim internals. That's the point of the SDK surface: an
embedder gets the exact same approval loop, tool wiring, and session behavior
that the CLI does, just composed explicitly. See `tests/test_builder.py` for
the composition-only tests these build on.
"""

from pathlib import Path

import pytest
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from marim_harness import Deps, HarnessBuilder

pytestmark = pytest.mark.anyio  # tests/test_turn_controller.py uses the same marker


def _scripted(tool_call_then_text: tuple[str, dict]) -> FunctionModel:
    """FunctionModel script: first request calls the tool, second returns text."""

    def call(messages, info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            name, args = tool_call_then_text
            return ModelResponse(parts=[ToolCallPart(tool_name=name, args=args)])
        return ModelResponse(parts=[TextPart("all done")])

    return FunctionModel(call)


async def test_custom_gated_tool_runs_in_auto_mode(tmp_path: Path):
    calls: list[str] = []

    def deploy(ctx: RunContext[Deps], target: str) -> str:
        """Deploy the app to `target`."""
        calls.append(target)
        return f"deployed {target}"

    harness = (
        HarnessBuilder(workspace=tmp_path, model=_scripted(("deploy", {"target": "prod"})))
        .with_tool(deploy, requires_approval=True)
        .build()
    )
    out = await harness.run_turn("deploy to prod")
    assert calls == ["prod"]  # gated tool executed (auto mode approves)
    assert out.result == "all done"


async def test_bare_build_reads_files(tmp_path: Path):
    (tmp_path / "hello.txt").write_text("hi")
    harness = HarnessBuilder(
        workspace=tmp_path,
        model=_scripted(("read_file", {"path": "hello.txt"})),
    ).build()
    out = await harness.run_turn("read hello.txt")
    assert out.result == "all done"


async def test_in_memory_session_round_trips(tmp_path: Path):
    def echo(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(f"turn {sum(1 for m in messages)}")])

    harness = HarnessBuilder(workspace=tmp_path, model=FunctionModel(echo)).build()
    first = await harness.run_turn("one")
    second = await harness.run_turn("two")
    assert first.result != second.result  # second turn saw a longer history


async def test_with_capability_attaches_after_builtins(tmp_path: Path):
    """An embedder capability (here a plain ProcessHistory, the same shape a
    pydantic-ai-harness module has) runs on every model request of a
    builder-built Harness. This is the with_capability seam end-to-end: the
    processor sees the history both on the initial request and on the
    post-tool-call continuation."""
    from pydantic_ai.capabilities import ProcessHistory

    seen: list[int] = []

    def observe(messages):
        seen.append(len(messages))
        return messages

    harness = (
        HarnessBuilder(workspace=tmp_path, model=_scripted(("list_dir", {"path": "."})))
        .with_capability(ProcessHistory(observe))
        .build()
    )
    out = await harness.run_turn("what's here?")
    assert out.result == "all done"
    # Once for the initial request, once for the tool-return continuation.
    assert len(seen) == 2


def _always_tool(name: str, args: dict) -> FunctionModel:
    """FunctionModel script that never concludes: every request calls the tool."""

    def call(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(tool_name=name, args=args)])

    return FunctionModel(call)


async def test_usage_request_limit_ends_a_runaway_turn_with_spend_readable(tmp_path: Path):
    """§3.1 acceptance: a model that keeps calling a tool, built with
    request_limit=2, ends the turn after two requests — as a raise, since a
    tripped limit is a failed attempt, not a result — and the spend is still
    readable on the session afterwards."""
    from pydantic_ai.exceptions import UsageLimitExceeded

    (tmp_path / "hello.txt").write_text("hi")
    harness = (
        HarnessBuilder(workspace=tmp_path, model=_always_tool("read_file", {"path": "hello.txt"}))
        .with_usage_limits(request_limit=2)
        .build()
    )
    with pytest.raises(UsageLimitExceeded):
        await harness.run_turn("loop forever")
    assert harness.session.usage.requests == 2


async def test_usage_limit_spans_approval_rounds(tmp_path: Path):
    """The limit is per TURN, not per agent.run round. A gated tool splits a
    turn into rounds (request → deferred → approval → continuation request);
    with request_limit=2 the continuation after the first approval is the
    second request, so the model's next tool call — a third request — trips
    the limit. A per-round limit would have reset at the continuation and
    let the turn run on."""
    from pydantic_ai.exceptions import UsageLimitExceeded

    calls: list[str] = []

    def deploy(ctx: RunContext[Deps], target: str) -> str:
        """Deploy the app to `target`."""
        calls.append(target)
        return f"deployed {target}"

    harness = (
        HarnessBuilder(workspace=tmp_path, model=_always_tool("deploy", {"target": "prod"}))
        .with_tool(deploy, requires_approval=True)
        .with_usage_limits(request_limit=2)
        .build()
    )
    with pytest.raises(UsageLimitExceeded):
        await harness.run_turn("deploy in a loop")
    assert harness.session.usage.requests == 2
    # Both deferred calls were approved and executed before the cap hit.
    assert calls == ["prod", "prod"]


async def test_usage_limit_not_tripped_by_a_bounded_turn(tmp_path: Path):
    """A limit that isn't reached is invisible: the tool-call-then-text script
    needs exactly two requests, so request_limit=2 lets it finish."""
    (tmp_path / "hello.txt").write_text("hi")
    harness = (
        HarnessBuilder(workspace=tmp_path, model=_scripted(("read_file", {"path": "hello.txt"})))
        .with_usage_limits(request_limit=2)
        .build()
    )
    out = await harness.run_turn("read hello.txt")
    assert out.result == "all done"
    assert out.usage.requests == 2


async def test_outcome_usage_is_the_turn_slice_of_session_usage(tmp_path: Path):
    """§3.4: TurnOutcome.usage is this turn's total across rounds, while
    session.usage stays cumulative across turns."""
    (tmp_path / "hello.txt").write_text("hi")

    def call(messages, info: AgentInfo) -> ModelResponse:
        # The first request of each turn is the one whose last message is the
        # user prompt; every other request ends the turn with text.
        last = messages[-1]
        if any(getattr(p, "part_kind", "") == "user-prompt" for p in last.parts):
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "hello.txt"})])
        return ModelResponse(parts=[TextPart("all done")])

    harness = HarnessBuilder(workspace=tmp_path, model=FunctionModel(call)).build()
    first = await harness.run_turn("one")
    assert first.usage.requests == 2
    assert first.usage.total_tokens > 0
    assert harness.session.usage.requests == 2
    second = await harness.run_turn("two")
    assert second.usage.requests == 2
    assert harness.session.usage.requests == 4
    # The first outcome's usage is a distinct object, untouched by turn two.
    assert first.usage is not second.usage
    assert first.usage.requests == 2


async def test_bare_build_never_sends_workspace_agents_md(tmp_path: Path):
    """§3.2 acceptance: a workspace AGENTS.md carrying a sentinel, built bare,
    never reaches the model — asserted on the instructions the model actually
    received, not on closure identity."""
    sentinel = "SENTINEL-do-not-inject-8f3c"
    (tmp_path / "AGENTS.md").write_text(f"Always say {sentinel}.\n")
    seen: list[str | None] = []

    def call(messages, info: AgentInfo) -> ModelResponse:
        seen.append(info.instructions)
        return ModelResponse(parts=[TextPart("ok")])

    bare = HarnessBuilder(workspace=tmp_path, model=FunctionModel(call)).build()
    await bare.run_turn("hello")
    assert seen and all(sentinel not in (i or "") for i in seen)

    seen.clear()
    opted_in = (
        HarnessBuilder(workspace=tmp_path, model=FunctionModel(call))
        .with_instructions(project=True)
        .build()
    )
    await opted_in.run_turn("hello")
    assert seen and all(sentinel in (i or "") for i in seen)


async def test_files_write_off_turn_still_reads(tmp_path: Path):
    """§3.3: a read-only build runs a normal turn with the read tools and
    exposes neither write tool to the model."""
    (tmp_path / "hello.txt").write_text("hi")
    offered: list[set[str]] = []

    def call(messages, info: AgentInfo) -> ModelResponse:
        offered.append({t.name for t in info.function_tools})
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "hello.txt"})])
        return ModelResponse(parts=[TextPart("all done")])

    harness = (
        HarnessBuilder(workspace=tmp_path, model=FunctionModel(call))
        .with_files_write(False)
        .build()
    )
    out = await harness.run_turn("read hello.txt")
    assert out.result == "all done"
    assert offered and all({"write_file", "edit_file"}.isdisjoint(t) for t in offered)
    assert all("read_file" in t for t in offered)
