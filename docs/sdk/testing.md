# Testing embedders

Everything on this page runs with **no network and no API key**. The trick
is that `HarnessBuilder` accepts a constructed `Model`, so you can inject
pydantic-ai's test models through the exact same seam production uses —
you're testing the real approval loop and tool wiring, not mocks of them.

## Composition asserts with `TestModel`

For "did the builder wire what I asked for" checks, `TestModel` is enough —
it never even needs a scripted response:

```python
from pydantic_ai.models.test import TestModel

def test_my_tool_is_registered(tmp_path):
    h = (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_tool(my_tool)
         .build())
    assert "my_tool" in h.agent._function_toolset.tools
```

## Full turns with `FunctionModel`

`FunctionModel` wraps a plain function as the model: it receives the message
history and returns the next `ModelResponse`. Script it to call your tool on
the first request and produce text on the second, and you can drive a real
turn end-to-end:

```python
import pytest
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from marim_harness import Deps, HarnessBuilder

pytestmark = pytest.mark.anyio


def _scripted(tool_call_then_text: tuple[str, dict]) -> FunctionModel:
    """First request calls the tool, second returns text."""
    def call(messages, info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            name, args = tool_call_then_text
            return ModelResponse(parts=[ToolCallPart(tool_name=name, args=args)])
        return ModelResponse(parts=[TextPart("all done")])
    return FunctionModel(call)


async def test_custom_gated_tool_runs_in_auto_mode(tmp_path):
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
    assert calls == ["prod"]      # gated tool executed (auto mode approved it)
    assert out == "all done"
```

That test proves the whole chain: builder registration → model tool call →
deferred approval → `Mode.auto` resolution → tool execution → run
continuation → final text. Nothing is mocked.

`marim-harness`'s own `tests/test_builder_turns.py` is the reference file
for this pattern.

## Proving a gated write lands on disk

The most valuable single test for an unattended embedder: script the model
to call `write_file` and assert the file exists afterward — this is the test
that catches a broken approval loop, a wrong workspace root, or a path
confinement bug:

```python
async def test_scripted_write_lands(tmp_path):
    harness = HarnessBuilder(
        workspace=tmp_path,
        model=_scripted(("write_file",
                         {"path": "out/report.md", "content": "# hi\n"})),
    ).build()
    await harness.run_turn("write the report")
    assert (tmp_path / "out/report.md").read_text() == "# hi\n"
```

## Async plumbing

marim's turn loop is async. With `anyio` (what marim's own suite uses), mark
the module and provide the backend fixture if your project doesn't already:

```python
pytestmark = pytest.mark.anyio

@pytest.fixture
def anyio_backend():
    return "asyncio"
```

`pytest-asyncio` works just as well if that's your project's convention.

## Seams worth building into your embedder

Two patterns from the reference embedder that keep tests cheap:

- **A module-level turn function** (`run_agent_turn(...)`) as the single
  place your CLI touches the harness — tests monkeypatch it to assert
  orchestration logic (argument handling, exit codes, short-circuits)
  without ever building a harness.
- **A `model=None` override parameter** on your harness-building function,
  defaulting to your production model string — tests and local smoke runs
  pass `FunctionModel`/local models through it; production passes nothing.

## What not to test

Don't re-test the SDK's own guarantees (approval semantics per mode, path
confinement, builder validation) — marim-harness's suite covers those. Test
*your* composition: your tools' logic, your prompt assembly, your
orchestration around `run_turn`, and one end-to-end scripted turn proving
the wiring holds together.
