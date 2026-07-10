"""Regression guard for the bundled ``examples/embedding`` sample: keep the
embedder composable and its custom tools wired as advertised, driven through a
real turn with a scripted `FunctionModel` (no network, no API key) so the SDK
example can't silently rot as the builder surface evolves.

This mirrors `tests/test_builder_turns.py` — the point is that an embedder gets
the exact approval loop and tool wiring the CLI does, composed explicitly.
"""

import importlib.util
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

pytestmark = pytest.mark.anyio

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "embedding" / "assistant.py"


def _load_assistant():
    # Load the example by path — examples/ is not an importable package, and
    # loading it here proves the file itself imports and composes cleanly.
    spec = importlib.util.spec_from_file_location("_embedding_assistant", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scripted(tool_call_then_text: tuple[str, dict]) -> FunctionModel:
    """First request calls the tool; the next returns final text."""

    def call(messages, info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            name, args = tool_call_then_text
            return ModelResponse(parts=[ToolCallPart(tool_name=name, args=args)])
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(call)


def test_both_custom_tools_are_registered(tmp_path: Path):
    assistant = _load_assistant()
    # A never-invoked model — this test only exercises composition.
    stub = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("")]))
    harness = assistant.build_assistant(tmp_path, model=stub)
    tools = harness.agent._function_toolset.tools.keys()
    assert "record_decision" in tools
    assert "list_decisions" in tools


async def test_gated_record_decision_writes_the_log(tmp_path: Path):
    assistant = _load_assistant()
    reply = await assistant.run_agent_turn(
        "record a decision",
        tmp_path,
        model=_scripted(
            ("record_decision", {"title": "Use SQLite", "rationale": "Zero-ops."})
        ),
    )
    assert reply == "done"
    # Mode.auto approves the gated tool, so the decision reaches disk.
    log = tmp_path / assistant.DECISIONS_FILE
    assert log.exists()
    assert "## Use SQLite" in log.read_text()
    assert "Zero-ops." in log.read_text()


async def test_record_decision_rejects_empty_input(tmp_path: Path):
    assistant = _load_assistant()
    tool = assistant.make_record_decision_tool(assistant.DECISIONS_FILE)

    class _Deps:
        workspace = type("W", (), {"root": tmp_path})()

    class _Ctx:
        deps = _Deps()

    assert tool(_Ctx(), "  ", "has rationale").startswith("error:")
    assert not (tmp_path / assistant.DECISIONS_FILE).exists()
