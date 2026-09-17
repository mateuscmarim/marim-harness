"""Turn boundaries, shared spend and safe cancellation for upstream advice."""

import asyncio
from decimal import Decimal

import anyio
import pytest
from pydantic_ai.exceptions import ModelHTTPError, UsageLimitExceeded
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage, RunUsage, UsageLimits

from marim_harness.runtime.builder import HarnessBuilder
from marim_harness.runtime.permissions import Mode
from marim_harness.session import SessionManager
from marim_harness.usage import resolve_cost
from tests.test_upstream_advisor import consulting_executor, make_harness, returns


def persisted_harness(tmp_path, executor, advisor):
    h = (
        HarnessBuilder(workspace=tmp_path, model=executor)
        .with_sessions(tmp_path / "sessions", stats=False)
        .with_mode(Mode.auto)
        .with_advisor("old")
        .build()
    )
    h._build_advisor_model = lambda _: advisor
    h.session.titler = None
    return h


@pytest.mark.parametrize("saved,expected", [("saved", "saved"), ("off", None), (None, "default")])
def test_session_selection_precedence(tmp_path, saved, expected):
    from marim_harness.runtime.deps import Deps, WorkspaceConfig
    from marim_harness.runtime.harness import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    manager = SessionManager(tmp_path)
    store = manager.create()
    store.advisor_model = saved
    h = Harness(
        TestModel(),
        BuiltinToolProvider(),
        Deps(workspace=WorkspaceConfig(root=tmp_path)),
        "help",
        store=store,
        manager=manager,
        advisor_model="default",
    )
    assert h.advisor_model_id == expected


@pytest.mark.anyio
@pytest.mark.parametrize("next_choice", ["new", None])
@pytest.mark.parametrize("boundary", ["approval", "retry", "dictionary"])
async def test_selection_frozen_across_turn_rounds(tmp_path, next_choice, boundary):
    calls = []
    requests = 0

    async def executor(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            if boundary != "approval":
                h.set_advisor_model(next_choice)
            first = {
                "dictionary": ToolCallPart("final_result", {"a": "bad"}),
                "retry": ToolCallPart("advisor", {}),
                "approval": ToolCallPart("write_file", {"path": "gate.txt", "content": "ok"}),
            }
            return ModelResponse(parts=[first[boundary]])
        if requests == 2:
            return ModelResponse(parts=[ToolCallPart("advisor", {"prompt": "check"})])
        if boundary == "dictionary":
            return ModelResponse(parts=[ToolCallPart("final_result", {"a": 1})])
        return ModelResponse(parts=[TextPart("done")])

    builder = HarnessBuilder(workspace=tmp_path, model=FunctionModel(executor)).with_advisor("old")
    builder.with_mode(Mode.ask)
    if boundary == "dictionary":
        builder.with_output_type(
            {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
        )
    h = builder.build()

    def model_source(model_id):
        calls.append(model_id)
        return TestModel(custom_output_text=model_id)

    h._build_advisor_model = model_source

    @h.agent.tool_plain(requires_approval=True)
    def gate():
        return "approved"

    async def approve(*args, **kwargs):
        h.set_advisor_model(next_choice)
        return True

    h.bind_ui(request_approval=approve)
    outcome = await h.run_turn("go")
    assert outcome.subtype == "success"
    assert calls == ["old"]
    assert [r.content for r in returns(h.session.history) if r.tool_name == "advisor"] == ["old"]
    assert h.advisor_model_id == next_choice


@pytest.mark.anyio
@pytest.mark.parametrize("before,after", [(None, "new"), ("old", "new"), ("old", None)])
async def test_selection_applies_next_turn_without_rebuild(tmp_path, before, after):
    seen = []

    def executor(messages, info):
        # A fresh user prompt starts each turn; the second model request receives
        # this turn's advice. Prior turns' returns must not suppress consultation.
        if any(isinstance(part, ToolReturnPart) for part in messages[-1].parts):
            return ModelResponse(parts=[TextPart("done")])
        available = [t.name for t in info.function_tools].count("advisor")
        seen.append(available)
        if available:
            return ModelResponse(parts=[ToolCallPart("advisor", {"prompt": "check selection"})])
        return ModelResponse(parts=[TextPart("done")])

    h = make_harness(tmp_path, FunctionModel(executor), TestModel())
    advisors = {
        "old": TestModel(custom_output_text="advice from old"),
        "new": TestModel(custom_output_text="advice from new"),
    }
    h._build_advisor_model = advisors.__getitem__
    h.set_advisor_model(before)
    original = h.agent
    await h.run_turn("first")
    first_advice = [r.content for r in returns(h.session.history) if r.tool_name == "advisor"]
    first_turn_end = len(h.session.history)
    h.set_advisor_model(after)
    await h.run_turn("second")
    second_advice = [
        r.content for r in returns(h.session.history[first_turn_end:]) if r.tool_name == "advisor"
    ]
    assert h.agent is original
    assert seen == [int(before is not None), int(after is not None)]
    assert first_advice == ([f"advice from {before}"] if before is not None else [])
    assert second_advice == ([f"advice from {after}"] if after is not None else [])


def test_selection_persists_metadata_only(tmp_path):
    h = persisted_harness(tmp_path, TestModel(), TestModel())
    h.session.persist()
    baseline = h.session.store.load()[0]
    h.session.history.append(ModelResponse(parts=[ToolCallPart("advisor", {"prompt": "dirty"})]))
    h.set_advisor_model("new")
    assert h.session.store.load()[0] == baseline
    reopened = h.session.manager.store(h.session.store.session_id)
    assert reopened.advisor_model == "new"


def test_historical_advisor_exchange_round_trips(tmp_path):
    store = SessionManager(tmp_path).create()
    result = "Historical advice\n\n[advisor usage: 17 in, 9 out tokens]"
    messages = [
        ModelResponse(parts=[ToolCallPart("advisor", {}, "old-id")]),
        ModelRequest(parts=[ToolReturnPart("advisor", result, "old-id")]),
    ]
    store.save(messages, RunUsage())
    loaded = store.load()[0]
    store.save(loaded, RunUsage())
    assert store.load()[0] == messages
    assert loaded[0].parts[0].args == {}
    assert loaded[1].parts[0].content == result


@pytest.mark.anyio
@pytest.mark.parametrize("approval", [False, True])
async def test_advisor_usage_banked_once(tmp_path, approval):
    count = 0

    def executor(messages, info):
        nonlocal count
        count += 1
        parts = [TextPart("done")]
        if count < 3:
            parts = [ToolCallPart("advisor", {"prompt": "review"}, f"a{count}")]
        if count == 1 and approval:
            parts.append(ToolCallPart("gate", {}, "g"))
        return ModelResponse(parts=parts, usage=RequestUsage(input_tokens=11, output_tokens=5))

    def advise(messages, info):
        return ModelResponse(
            parts=[TextPart("advice")], usage=RequestUsage(input_tokens=7, output_tokens=3)
        )

    h = persisted_harness(tmp_path, FunctionModel(executor), FunctionModel(advise))

    @h.agent.tool_plain(requires_approval=True)
    def gate():
        return "approved"

    outcome = await h.run_turn("go")
    for usage in (outcome.usage, h.session.usage, h.session.store.load()[1]):
        assert (usage.requests, usage.input_tokens, usage.output_tokens) == (5, 47, 21)


@pytest.mark.anyio
@pytest.mark.parametrize("approval", [False, True])
async def test_advisor_shares_remaining_turn_limits(tmp_path, approval):
    calls = []

    def advise(messages, info):
        calls.append(1)
        return ModelResponse(parts=[TextPart("unexpected")])

    def executor(messages, info):
        if approval and not returns(messages):
            return ModelResponse(parts=[ToolCallPart("gate", {})])
        return ModelResponse(parts=[ToolCallPart("advisor", {"prompt": "check"})])

    h = persisted_harness(tmp_path, FunctionModel(executor), FunctionModel(advise))
    h.turn_controller._usage_limits = UsageLimits(request_limit=2 if approval else 1)

    @h.agent.tool_plain(requires_approval=True)
    def gate():
        return "approved"

    with pytest.raises(UsageLimitExceeded):
        await h.run_turn("go")
    assert calls == []
    assert h.session.usage.requests == (2 if approval else 1)


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["provider", "cancel"])
async def test_failed_or_cancelled_advice_banks_usage_once(tmp_path, failure):
    entered = asyncio.Event()
    calls = 0

    async def advise(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[TextPart("advice")], usage=RequestUsage(input_tokens=7, output_tokens=3)
            )
        entered.set()
        if failure == "provider":
            raise ModelHTTPError(503, "advisor", "failed")
        await asyncio.Event().wait()

    def executor(messages, info):
        return ModelResponse(
            parts=[ToolCallPart("advisor", {"prompt": "review"})],
            usage=RequestUsage(input_tokens=11, output_tokens=5),
        )

    h = persisted_harness(tmp_path, FunctionModel(executor), FunctionModel(advise))
    task = asyncio.create_task(h.run_turn("go"))
    await asyncio.wait_for(entered.wait(), 5)
    if failure == "cancel":
        task.cancel()
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else ModelHTTPError):
        await task
    usage = h.session.usage
    assert (usage.input_tokens, usage.output_tokens) == (29, 13)
    assert h.turn_controller._turn_usage.input_tokens == 29
    assert h.session.store.load()[1].input_tokens == 29


def assert_paired(messages):
    calls = {p.tool_call_id for m in messages for p in m.parts if isinstance(p, ToolCallPart)}
    results = {p.tool_call_id for m in messages for p in m.parts if isinstance(p, ToolReturnPart)}
    assert calls <= results


@pytest.mark.anyio
async def test_advisor_failure_leaves_resumable_history(tmp_path):
    def broken(messages, info):
        raise ModelHTTPError(503, "reviewer", "unavailable")

    h = persisted_harness(tmp_path, consulting_executor(), FunctionModel(broken))
    with pytest.raises(ModelHTTPError):
        await h.run_turn("go")
    assert_paired(h.session.store.load()[0])
    h.set_advisor_model(None)
    h.current_model = TestModel(call_tools=[], custom_output_text="recovered")
    assert (await h.run_turn("continue")).result == "recovered"


@pytest.mark.anyio
async def test_advisor_cancellation_retains_claim_until_cleanup(tmp_path):
    from marim_harness.session.claim import try_acquire

    entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

    async def advise(messages, info):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            with anyio.CancelScope(shield=True):
                await release.wait()
                cleaned.set()

    h = persisted_harness(tmp_path, consulting_executor(), FunctionModel(advise))
    path = h.session.store.path
    h.adopt_claim(try_acquire(path, kind="headless"), kind="headless")

    async def owned_turn():
        try:
            await h.run_turn("go")
        finally:
            await h.aclose()

    task = asyncio.create_task(owned_turn())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert try_acquire(path, kind="headless") is None
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert cleaned.is_set()
        claim = try_acquire(path, kind="headless")
        assert claim is not None
        claim.release()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("cost", [None, Decimal("0.0123")])
@pytest.mark.anyio
async def test_mixed_advisor_cost_is_not_falsely_exact(tmp_path, cost):
    import json

    from marim_harness.server.host import _dump_usage
    from marim_harness.usage import preserve_usage_cost, usage_from_dump

    usage = RunUsage(
        input_tokens=123,
        output_tokens=45,
        cost=cost,
        details={"cost_micro_usd": 17, "advisor_mixed_cost": 1},
    )
    expected = (float(cost) if cost is not None else None, False)
    assert resolve_cost(usage, "anthropic:claude-sonnet-4-6") == expected
    preserve_usage_cost(usage)
    store = SessionManager(tmp_path).create()
    store.save([], usage)
    assert resolve_cost(store.load()[1], "anthropic:claude-sonnet-4-6") == expected
    wire = json.loads(json.dumps(_dump_usage(usage)))
    assert resolve_cost(usage_from_dump(wire), "anthropic:claude-sonnet-4-6") == expected
    per_request = cost / 3 if cost is not None else None

    def executor(messages, info):
        parts = (
            [TextPart("done")]
            if returns(messages)
            else [ToolCallPart("advisor", {"prompt": "check"})]
        )
        return ModelResponse(
            parts=parts,
            usage=RequestUsage(
                input_tokens=11,
                output_tokens=5,
                cost=per_request,
                details={"cost_micro_usd": 17},
            ),
        )

    def advisor(messages, info):
        return ModelResponse(
            parts=[TextPart("advice")],
            usage=RequestUsage(
                input_tokens=7,
                output_tokens=3,
                cost=per_request,
            ),
        )

    h = persisted_harness(tmp_path, FunctionModel(executor), FunctionModel(advisor))
    outcome = await h.run_turn("go")
    assert outcome.usage.details["advisor_mixed_cost"] == 1
    assert resolve_cost(outcome.usage, "anthropic:claude-sonnet-4-6") == expected
    assert resolve_cost(h.session.store.load()[1], "anthropic:claude-sonnet-4-6") == expected


@pytest.mark.anyio
@pytest.mark.parametrize("first_cost,expected", [(Decimal("0.0123"), 0.0168), (None, None)])
async def test_mixed_advisor_cost_accumulates_after_resume(tmp_path, first_cost, expected):
    import json

    from marim_harness.server.host import _dump_usage
    from marim_harness.usage import usage_from_dump

    per_request = first_cost / 3 if first_cost is not None else None

    def executor(messages, info):
        # Prior turns' advice must not suppress consultation on the next turn.
        finished = any(isinstance(part, ToolReturnPart) for part in messages[-1].parts)
        return ModelResponse(
            parts=[TextPart("done")]
            if finished
            else [ToolCallPart("advisor", {"prompt": "check"})],
            usage=RequestUsage(input_tokens=11, output_tokens=5, cost=per_request),
        )

    def advisor(messages, info):
        return ModelResponse(
            parts=[TextPart("advice")],
            usage=RequestUsage(input_tokens=7, output_tokens=3, cost=per_request),
        )

    h = persisted_harness(tmp_path, FunctionModel(executor), FunctionModel(advisor))
    try:
        await h.run_turn("first")
        assert resolve_cost(h.session.usage, None) == (
            float(first_cost) if first_cost is not None else None,
            False,
        )
        h.resume()
        assert h.session.usage.cost is None  # Legacy session format stores cost in details.

        per_request = Decimal("0.0015")
        outcome = await h.run_turn("after resume")
        assert resolve_cost(outcome.usage, None) == (0.0045, False)
        assert resolve_cost(h.session.usage, None) == (expected, False)
        wire = json.loads(json.dumps(_dump_usage(h.session.usage)))
        assert resolve_cost(usage_from_dump(wire), None) == (expected, False)
        assert resolve_cost(h.session.store.load()[1], None) == (expected, False)
    finally:
        await h.aclose()
