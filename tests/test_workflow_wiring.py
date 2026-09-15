"""Exercise workflow construction and live controls through public agent calls."""

import asyncio
import json
import sys

import pytest
from pydantic_ai import DeferredToolRequests, DeferredToolResults
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from marim_harness.runtime.builder import HarnessBuilder
from marim_harness.runtime.deps import HarnessServices, UIHooks
from marim_harness.workflows.catalog import WorkflowBinding
from tests.conftest import _make_deps, _make_harness


def _workflow_model(code, schemas=None):
    def request(messages, info):
        if schemas is not None:
            schemas.append([tool for tool in info.function_tools if tool.name == "run_workflow"])
        if any(isinstance(part, ToolReturnPart) for part in messages[-1].parts):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart("run_workflow", {"code": code}, "wf")])

    return FunctionModel(request)


async def _approve(harness, pending):
    assert isinstance(pending.output, DeferredToolRequests)
    assert len(pending.output.approvals) == 1
    completed = await harness.agent.run(
        deps=harness.deps,
        message_history=pending.all_messages(),
        deferred_tool_results=DeferredToolResults(approvals={"wf": True}),
    )
    returns = [
        part
        for message in completed.all_messages()
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == "run_workflow"
    ]
    assert len(returns) == 1
    return str(returns[0].content)


def _harness(tmp_path, code='await worker(task="review")', **kwargs):
    return (
        HarnessBuilder(workspace=tmp_path, model=_workflow_model(code))
        .with_defaults()
        .with_lsp(enabled=False)
        .with_deps(_make_deps(tmp_path))
        .with_config_overrides(workflow_bindings=(WorkflowBinding("worker", "general"),), **kwargs)
        .build()
    )


def test_services_default_workflows_none():
    assert HarnessServices().workflows is None


def test_ui_hooks_default_workflow_callbacks_none():
    ui = UIHooks()
    assert ui.on_workflow_spawn is None
    assert ui.on_workflow_log is None  # retained for historical stream events
    assert ui.on_workflow_spawn_done is None
    assert ui.on_workflow_start is None
    assert ui.on_workflow_done is None


@pytest.mark.anyio
async def test_registered_workflow_defers_then_dispatches_once(tmp_path):
    schemas = []
    h = _harness(tmp_path)
    calls = []
    starts = []

    async def spawn(role, task, stream_id, *args, **kwargs):
        calls.append((role, task, stream_id))
        return "reviewed"

    integration = h.deps.services.workflows
    assert integration is not None
    integration.spawn = spawn
    h.deps.ui.on_workflow_start = lambda *args: starts.append(args)
    with h.agent.override(model=_workflow_model('await worker(task="review")', schemas)):
        pending = await h.agent.run("run", deps=h.deps)
        assert calls == []
        assert starts == []
        result = await _approve(h, pending)
    assert json.loads(result) == "reviewed"
    assert calls == [("general", "review", "wf::wf1")]
    assert len(starts) == 1
    assert all(len(schema) == 1 for schema in schemas)
    schema = schemas[0][0].parameters_json_schema
    assert set(schema["properties"]) == {"code"}
    assert schema["required"] == ["code"]


@pytest.mark.anyio
async def test_harness_threads_wall_deadline_and_drains_workers(tmp_path):
    h = _harness(tmp_path, workflow_timeout_secs=0.03)
    cleaned = asyncio.Event()

    async def spawn(*args, **kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            cleaned.set()

    h.deps.services.workflows.spawn = spawn
    pending = await h.agent.run("run", deps=h.deps)
    result = await _approve(h, pending)
    assert "timed out after 0.03s" in result
    assert cleaned.is_set()


@pytest.mark.anyio
@pytest.mark.parametrize("initially_enabled", [True, False])
async def test_workflows_toggle_applies_live_including_initially_disabled(
    tmp_path, initially_enabled
):
    h = _harness(tmp_path, workflows_enabled=initially_enabled)
    calls = []

    async def spawn(*args, **kwargs):
        calls.append(args)
        return "enabled"

    integration = h.deps.services.workflows
    assert integration is not None
    integration.spawn = spawn
    assert h.set_workflows_enabled(False)
    pending = await h.agent.run("run", deps=h.deps)
    assert "unavailable" in await _approve(h, pending)
    assert calls == []
    assert h.set_workflows_enabled(True)
    pending = await h.agent.run("run", deps=h.deps)
    assert json.loads(await _approve(h, pending)) == "enabled"
    assert len(calls) == 1


@pytest.mark.anyio
async def test_missing_monty_keeps_one_gated_fallback_schema(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pydantic_monty", None)
    schemas = []
    h = _make_harness(_workflow_model("1 + 1", schemas), _make_deps(tmp_path))
    assert h.deps.services.workflows is None
    assert not h.set_workflows_enabled(True)
    assert h.set_workflows_enabled(False)
    pending = await h.agent.run("run", deps=h.deps)
    result = await _approve(h, pending)
    assert "marim-harness[workflows]" in result
    assert "spawn_agent" in result
    assert all(len(schema) == 1 for schema in schemas)
    assert set(schemas[0][0].parameters_json_schema["properties"]) == {"code"}


@pytest.mark.anyio
async def test_group_off_omits_workflow_and_cannot_live_enable(tmp_path):
    model = TestModel(call_tools=[])
    h = HarnessBuilder(workspace=tmp_path, model=model).build()
    assert h.deps.services.workflows is None
    assert not h.set_workflows_enabled(True)
    await h.agent.run("inspect tools", deps=h.deps)
    assert model.last_model_request_parameters is not None
    assert "run_workflow" not in {
        tool.name for tool in model.last_model_request_parameters.function_tools
    }
