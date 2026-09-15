"""Main-session policy around upstream history reduction."""

import asyncio

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness.compaction import SummarizingCompaction

from marim_harness.session.ctrl import SessionController
from tests.conftest import _make_deps


def history():
    return [
        part
        for i in range(6)
        for part in (
            ModelRequest(parts=[UserPromptPart("question " * 50)]),
            ModelResponse(parts=[TextPart("answer " * 50)]),
        )
    ]


def session(tmp_path, strategy=None):
    ctrl = SessionController(
        None,
        None,
        _make_deps(tmp_path),
        100_000,
        2,
        compaction_strategy=strategy,
        auxiliary_model=TestModel(),
    )
    ctrl.history = history()
    return ctrl


@pytest.mark.anyio
async def test_measured_gate_forces_reduction_and_resets_measurement(tmp_path):
    ctrl = session(tmp_path, SummarizingCompaction(max_tokens=1, keep_messages=2))
    ctrl.last_input_tokens = 200_000
    events = []
    ctrl.on_history_restructured = lambda: events.append("invalidate")
    ctrl.persist = lambda: events.append("persist")
    assert await ctrl.maybe_compact()
    assert events == ["invalidate", "persist"]
    assert ctrl.last_input_tokens is None
    assert ctrl.usage.requests == 1
    assert ctrl.last_compaction_details["changed"]


@pytest.mark.anyio
async def test_cancelled_summary_is_atomic_and_clears_indicator(tmp_path):
    class Cancel:
        async def compact(self, messages, ctx):
            ctx.usage.requests += 1
            ctx.usage.input_tokens += 12
            raise asyncio.CancelledError

    ctrl = session(tmp_path, Cancel())
    original = list(ctrl.history)
    events = []
    ctrl.on_compact_start = lambda: events.append("start")
    ctrl.on_compact = lambda before, after: events.append("end")
    with pytest.raises(asyncio.CancelledError):
        await ctrl.maybe_compact(trigger="manual")
    assert ctrl.history == original
    assert ctrl.usage.input_tokens == 12
    assert events == ["start", "end"]
    assert ctrl.last_compaction_details["changed"] is False


@pytest.mark.anyio
async def test_same_length_summary_invalidates_and_reports_new_text(tmp_path):
    class Replace:
        async def compact(self, messages, ctx):
            return [
                ModelRequest(parts=[SystemPromptPart("Summary of previous conversation:\n\nnew")]),
                *messages[1:],
            ]

    ctrl = session(tmp_path, Replace())
    events = []
    ctrl.on_history_restructured = lambda: events.append("invalidate")
    ctrl.persist = lambda: events.append("persist")
    assert await ctrl.maybe_compact(trigger="manual")
    assert events == ["invalidate", "persist"]
    assert ctrl.last_compaction_details["summary"] == "new"


@pytest.mark.anyio
async def test_summary_request_limit_escapes_without_committing(tmp_path):
    from pydantic_ai.exceptions import UsageLimitExceeded

    ctrl = session(tmp_path, SummarizingCompaction(max_tokens=1, keep_messages=2))
    original = list(ctrl.history)
    with pytest.raises(UsageLimitExceeded):
        await ctrl.maybe_compact(force=True, usage_limits=UsageLimits(request_limit=1))
    assert ctrl.history == original
    assert ctrl.usage.requests == 0


@pytest.mark.anyio
async def test_manual_focus_and_explicit_model_survive_switch(tmp_path):
    from pydantic_ai.models.function import FunctionModel

    prompts = []

    def summarize(messages, info):
        prompts.extend(
            str(part.content)
            for message in messages
            for part in message.parts
            if hasattr(part, "content")
        )
        return ModelResponse(parts=[TextPart("focused recap")])

    explicit = FunctionModel(summarize, model_name="summary-model")
    strategy = SummarizingCompaction(max_tokens=1, keep_messages=2, model=explicit)
    ctrl = session(tmp_path, strategy)
    ctrl.update_model(TestModel(custom_output_text="different"))
    assert await ctrl.maybe_compact(trigger="manual", instructions="Keep authentication")
    assert any("Keep authentication" in prompt for prompt in prompts)
    assert ctrl.compaction_strategy.model is explicit
    assert ctrl.usage.requests == 1


@pytest.mark.anyio
async def test_summary_failure_falls_back_and_banks_spend(tmp_path):
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    class Fail:
        async def compact(self, messages, ctx):
            ctx.usage.requests += 1
            ctx.usage.input_tokens += 40
            raise UnexpectedModelBehavior("malformed")

    ctrl = session(tmp_path, Fail())
    assert await ctrl.maybe_compact(trigger="manual")
    assert ctrl.usage.input_tokens == 40
    assert ctrl.last_compaction_details["summary"] is None
    assert ctrl.last_compaction_details["stage"] == "summary"


@pytest.mark.anyio
async def test_irreducible_history_does_not_request_or_report_success(tmp_path):
    ctrl = session(tmp_path, SummarizingCompaction(max_tokens=1, keep_messages=20))
    ctrl.history = [ModelRequest(parts=[UserPromptPart("one prompt")])]
    assert not await ctrl.maybe_compact(trigger="manual")
    assert ctrl.usage.requests == 0
    assert ctrl.last_compaction_details["changed"] is False


@pytest.mark.anyio
async def test_explicit_summary_model_ledger_attribution_excludes_main_backend(tmp_path):
    from marim_harness.stats.ledger import StatsLedger
    from marim_harness.stats.recorder import LedgerStatsRecorder

    ledger = StatsLedger(tmp_path / "stats", "workspace")
    ctrl = session(
        tmp_path,
        SummarizingCompaction(
            max_tokens=1,
            keep_messages=2,
            model=TestModel(model_name="summary-only", custom_output_text="recap"),
        ),
    )

    def forbidden_main_metadata():
        raise AssertionError("summary queried main backend metadata")

    ctrl.stats_recorder = LedgerStatsRecorder(
        ledger,
        session_id="session",
        get_model_id=lambda: "main-only",
        get_duration_seconds=lambda: 0,
        get_backend_result=forbidden_main_metadata,
    )
    assert await ctrl.maybe_compact(trigger="manual")
    events = list(ledger.iter_workspace())
    assert len(events) == 1
    assert events[0].model == "summary-only"
    assert events[0].backend_result is None


@pytest.mark.anyio
async def test_opaque_strategy_usage_has_unknown_model_attribution(tmp_path):
    class Opaque:
        async def compact(self, messages, ctx):
            ctx.usage.input_tokens += 3
            return messages

    recorded = []

    class Recorder:
        def record(self, delta, *, model_id=None):
            recorded.append(model_id)

    ctrl = session(tmp_path, Opaque())
    ctrl.stats_recorder = Recorder()
    assert not await ctrl.maybe_compact(trigger="manual")
    assert recorded == ["unknown"]


@pytest.mark.anyio
async def test_inherited_summary_uses_switched_auxiliary_model(tmp_path):
    ctrl = session(tmp_path, SummarizingCompaction(max_tokens=1, keep_messages=2))
    ctrl.update_model(TestModel(custom_output_text="new model recap"))
    assert await ctrl.maybe_compact(trigger="manual")
    assert ctrl.last_compaction_details["summary"] == "new model recap"
