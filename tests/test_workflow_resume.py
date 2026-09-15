"""Saved workflow results and legacy deferred calls stay resumable without replay."""

from collections import Counter

import pytest
from pydantic_ai import DeferredToolResults
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from marim_harness import BuilderError, HarnessBuilder, Mode
from tests.test_workflow_cancellation import _harness


def _paired(messages):
    calls = Counter(
        p.tool_call_id for m in messages for p in m.parts if isinstance(p, ToolCallPart)
    )
    replies = Counter(
        p.tool_call_id
        for m in messages
        for p in m.parts
        if isinstance(p, (ToolReturnPart, RetryPromptPart)) and p.tool_call_id
    )
    assert calls == replies
    assert calls["workflow"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["success", "failure", "denial"])
async def test_workflow_outcomes_persist_and_reload_without_replay(tmp_path, case):
    code = 'await worker(task="write once")'
    if case == "failure":
        code += '\nraise RuntimeError("after completed work")'

    def request(messages, info):
        if any(isinstance(p, (ToolReturnPart, RetryPromptPart)) for p in messages[-1].parts):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart("run_workflow", {"code": code}, "workflow")])

    h = _harness(tmp_path, FunctionModel(request))
    effects = []

    async def spawn(*args):
        effects.append("completed")
        return "completed result"

    h.deps.services.workflows.spawn = spawn
    if case == "denial":
        h.deps.workspace.mode = Mode.plan
    try:
        await h.run_turn("run workflow")
        store = h.session.store
        _paired(store.load()[0])
        assert effects == ([] if case == "denial" else ["completed"])
    finally:
        await h.aclose()

    def resume(messages, info):
        _paired(messages)
        return ModelResponse(parts=[TextPart("resumed")])

    h2 = _harness(tmp_path, FunctionModel(resume), store=store)
    h2.deps.services.workflows.spawn = spawn
    try:
        h2.resume()
        assert (await h2.run_turn("continue")).result == "resumed"
        assert effects == ([] if case == "denial" else ["completed"])
    finally:
        await h2.aclose()


@pytest.mark.anyio
async def test_approved_legacy_saved_call_returns_fixable_error_without_dispatch(tmp_path):
    seen = []

    def request(messages, info):
        _paired(messages)
        seen.extend(p for m in messages for p in m.parts if isinstance(p, RetryPromptPart))
        return ModelResponse(parts=[TextPart("Use the code argument next time")])

    h = _harness(tmp_path, FunctionModel(request))

    async def spawn(*args):
        pytest.fail("legacy call dispatched a worker")

    h.deps.services.workflows.spawn = spawn
    history = [
        ModelRequest(parts=[UserPromptPart("old workflow")]),
        ModelResponse(
            parts=[ToolCallPart("run_workflow", {"script": "1", "args": {}}, "workflow")]
        ),
    ]
    try:
        result = await h.agent.run(
            deps=h.deps,
            message_history=history,
            deferred_tool_results=DeferredToolResults(approvals={"workflow": True}),
        )
        assert "code" in str(seen)
        h.session.store.save(result.all_messages(), result.usage)
        _paired(h.session.store.load()[0])
    finally:
        await h.aclose()


def test_custom_workflow_tool_collision_is_a_build_error(tmp_path):
    async def run_workflow(code: str) -> str:
        """Conflicting embedder tool."""
        return code

    with pytest.raises(BuilderError, match="run_workflow.*collides"):
        (
            HarnessBuilder(workspace=tmp_path, model=TestModel())
            .with_defaults()
            .with_tool(run_workflow)
            .build()
        )
