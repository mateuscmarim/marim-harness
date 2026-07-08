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

from marim_harness import HarnessBuilder
from marim_harness.runtime.deps import Deps

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
        HarnessBuilder(workspace=tmp_path,
                        model=_scripted(("deploy", {"target": "prod"})))
        .with_tool(deploy, requires_approval=True)
        .build()
    )
    out = await harness.run_turn("deploy to prod")
    assert calls == ["prod"]          # gated tool executed (auto mode approves)
    assert out == "all done"


async def test_bare_build_reads_files(tmp_path: Path):
    (tmp_path / "hello.txt").write_text("hi")
    harness = HarnessBuilder(
        workspace=tmp_path,
        model=_scripted(("read_file", {"path": "hello.txt"})),
    ).build()
    out = await harness.run_turn("read hello.txt")
    assert out == "all done"


async def test_in_memory_session_round_trips(tmp_path: Path):
    def echo(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(f"turn {sum(1 for m in messages)}")])

    harness = HarnessBuilder(workspace=tmp_path, model=FunctionModel(echo)).build()
    first = await harness.run_turn("one")
    second = await harness.run_turn("two")
    assert first != second            # second turn saw a longer history
