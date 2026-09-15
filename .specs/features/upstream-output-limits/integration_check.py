"""Replay only in a tree combining output limits and upstream compaction.

Run alongside tests/test_output_limits.py from that combined tree; the exact
command is recorded in qualification.md (the test helpers come from that tree).
The compaction branch supplies session.compaction; this file is an integration
artifact, outside the ordinary test collection on the output-only branch.
"""

import pytest
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from pydantic_ai_harness.compaction import SummarizingCompaction

from marim_harness.runtime.output_limits import OutputStorage
from marim_harness.session.compaction import ReductionOptions, reduce_history
from tests.test_output_limits import _harness, _read, _returns


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_spill_clear_summarize_save_resume_retrieve(tmp_path):
    produced = []

    def model(messages, info):
        if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(
            parts=[ToolCallPart("sample", {}, tool_call_id=f"call-{len(produced)}")]
        )

    h = _harness(tmp_path, FunctionModel(model))
    h.deps.services.get_session_id = lambda: "persisted"

    @h.agent.tool_plain
    def sample():
        produced.append(1)
        return "complete output\n" * 2000

    history = []
    handles = []
    for i in range(5):
        result = await h.agent.run(f"read {i}", deps=h.deps)
        history.extend(result.all_messages())
        handles.append(_returns(result)[0].metadata["overflow_handle"])
    summary = SummarizingCompaction(
        model=TestModel(custom_output_text="Saved output: " + handles[0]),
        max_tokens=1,
        keep_messages=4,
    )
    reduction = await reduce_history(
        history,
        ReductionOptions(
            summary=summary,
            target_tokens=1,
            keep_messages=4,
            keep_pairs=1,
            clear=True,
            force=True,
            focus=None,
        ),
        model=TestModel(),
        usage=RunUsage(),
    )
    assert "clear" in reduction.stages
    assert "summary" in reduction.stages
    saved = tmp_path / "saved.json"
    saved.write_bytes(ModelMessagesTypeAdapter.dump_json(reduction.messages))
    restored = ModelMessagesTypeAdapter.validate_json(saved.read_bytes())
    assert restored == reduction.messages
    assert handles[0] in saved.read_text()
    fresh = _harness(tmp_path)
    fresh.deps.services.get_session_id = lambda: "persisted"
    cap = OutputStorage.capture(fresh.deps).capability()
    assert "complete output" in await _read(cap, handles[0], limit=2)
    (OutputStorage.capture(fresh.deps).root / handles[0]).unlink()
    assert "No stored tool result" in await _read(cap, handles[0])
    assert len(produced) == 5
