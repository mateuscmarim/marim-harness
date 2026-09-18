"""Child accounting follows the executed model, including partial failed runs."""

from unittest.mock import AsyncMock

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage, RunUsage

from marim_harness.config.model import ModelConfig, ModelSource, MultiModelSource
from marim_harness.runtime.builder import HarnessBuilder
from marim_harness.session import TranscriptStore
from marim_harness.stats.ledger import iter_turns
from marim_harness.usage import usage_summary
from marim_harness.workspace.agents import AgentDef
from tests._codex_subscription import MODEL, source, sse
from tests._codex_subscription import wire as wire  # noqa: F401


def accounting_harness(wire, tmp_path, monkeypatch, child_provider, fails):
    from marim_harness import usage

    monkeypatch.setattr(
        usage, "estimate_cost", lambda delta, model: 2.5 if model == "paid-child" else 42.0
    )
    (tmp_path / "input.txt").write_text("child read")
    paid = ModelSource(
        ModelConfig(provider="openrouter", model="anthropic/claude-sonnet-4-6", api_key="fake")
    )
    main = paid.build("anthropic/claude-sonnet-4-6")
    subscription = source()
    if child_provider == "subscription":
        child = subscription.build(MODEL)
        child.provider.client.max_retries = 0  # Exercise Marim's retry exhaustion directly.
        wire.replies.append(
            sse(input_tokens=321, tool=("read_file", {"path": "input.txt"}))
            if fails
            else sse(input_tokens=321, text="child done")
        )
        if fails:
            wire.replies.extend([(503, {"error": "overloaded"})] * 2)
        selection = "openai-codex:gpt-6-astra"
    else:
        calls = 0

        def respond(messages, info):
            nonlocal calls
            calls += 1
            if fails and calls > 1:
                raise ModelHTTPError(503, "paid-child", body="overloaded")
            part = (
                ToolCallPart("read_file", {"path": "input.txt"})
                if fails
                else TextPart("child done")
            )
            return ModelResponse(
                parts=[part], usage=RequestUsage(input_tokens=321, output_tokens=10)
            )

        child = FunctionModel(respond, model_name="paid-child")
        monkeypatch.setattr(paid, "build", lambda model_id: child)
        selection = "openrouter:paid-child"
    h = (
        HarnessBuilder(workspace=tmp_path, model=main)
        .with_sessions(tmp_path / "sessions")
        .with_subagent(AgentDef("reader", "Read", "Read only", frozenset({"read_file"}), "test"))
        .with_config_overrides(
            model_source=MultiModelSource(
                {"openrouter": paid, "openai-codex": subscription}, "openrouter"
            ),
            model_id="openrouter:anthropic/claude-sonnet-4-6",
            titler=None,
            compaction_strategy=None,
            subagent_retry_attempts=1,
            subagent_concurrency=1,
        )
        .build()
    )
    monkeypatch.setattr(h.subagents._driver, "backoff", AsyncMock())
    return h, selection


async def run_child(h, selection, mode, fails):
    if mode == "resume":
        store = h.session.store
        TranscriptStore(store.path, store.session_id).write(
            "child",
            [ModelRequest(parts=[UserPromptPart("previous child task")])],
            2000,
            meta={"status": "running", "type": "reader", "task": "inspect", "model": selection},
        )
        job_id, message = await h.subagents.resume_spawn("child")
        assert job_id is not None, message
        result = await h.deps.jobs.wait(job_id)
        assert h.deps.jobs.get(job_id).status == ("failed" if fails else "done")
        return result
    if mode == "background" and fails:
        with pytest.raises(Exception, match="503"):
            await h.subagents.run_background(
                "reader", "inspect", model=selection, stream_id="child"
            )
        return "failed"
    run = h.subagents.run_background if mode == "background" else h.subagents.run
    return await run("reader", "inspect", model=selection, stream_id="child")


@pytest.mark.anyio
@pytest.mark.parametrize("child_provider", ["subscription", "api"])
@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("mode", ["foreground", "background", "resume"])
async def test_native_child_usage_identity(
    wire, tmp_path, monkeypatch, child_provider, fails, mode
):
    h, selection = accounting_harness(wire, tmp_path, monkeypatch, child_provider, fails)
    try:
        result = await run_child(h, selection, mode, fails)
        if not fails:
            assert "child done" in result
        events = list(iter_turns(tmp_path / "stats" / "global" / "turns.jsonl"))
        assert len(events) == 1
        assert events[0].input_tokens == 321
        assert events[0].output_tokens == 10
        if child_provider == "subscription":
            assert events[0].model == "openai-codex:gpt-6-astra"
            assert events[0].cost_usd is None
        else:
            assert events[0].model == "paid-child"
            assert events[0].cost_usd == 2.5
        assert events[0].cost_is_exact is False
        assert h.subagents._driver.backoff.await_count == (1 if fails else 0)
    finally:
        await h.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("fails", [False, True])
async def test_subscription_child_aggregate_usage(wire, tmp_path, monkeypatch, fails):
    h, selection = accounting_harness(wire, tmp_path, monkeypatch, "subscription", fails)
    try:
        await run_child(h, selection, "foreground", fails)
        # A later paid main request keeps its own price; child provenance must
        # affect only the combined total and the child's own ledger record.
        main_delta = RunUsage(input_tokens=20, output_tokens=5)
        h.session.add_usage(main_delta)
        assert "subscription_cost_unknown" not in main_delta.details
        events = list(iter_turns(tmp_path / "stats" / "global" / "turns.jsonl"))
        assert len(events) == 2
        assert events[-1].model == "anthropic/claude-sonnet-4-6"
        assert events[-1].cost_usd == 42.0
        summary = usage_summary(h.session.usage, h.model_id)
        assert summary["input_tokens"] == 341
        assert summary["output_tokens"] == 15
        assert summary["cost_usd"] is None
        assert summary["cost_is_exact"] is False
        h.session.persist(force=True)
        reloaded = h.session.store.load()[1]
        assert reloaded.details["subscription_cost_unknown"] == 1
        assert usage_summary(reloaded, h.model_id)["cost_usd"] is None
    finally:
        await h.aclose()
