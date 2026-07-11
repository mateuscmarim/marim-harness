"""Orchestration coverage for ``TurnController._handle_run_failure``.

The overflow/contention *classifiers* (``is_context_overflow_error``,
``overflow_is_contention``) are pinned in test_provider_errors.py. These exercise
the recovery ORCHESTRATION the classifiers feed: usage banking of the failed
round, the one-shot ``_RunRetry`` latch, the contention backoff-and-retry, and
the dirty-continuation guard that must never force-compact a round whose
in-memory history ends in unanswered tool calls.
"""

import httpx
import pytest
from openai import APIError
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RunUsage

from marim_harness.config.context_limits import ContextLimits
from marim_harness.runtime.controller import TurnController, _RunRetry
from marim_harness.runtime.errors import ContextWindowExceededError
from marim_harness.runtime.harness import HarnessConfig, build_collaborators
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import _make_deps

pytestmark = pytest.mark.anyio


def _overflow_exc() -> APIError:
    """An overflow rejection shaped like a provider's context-length error."""
    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    return APIError(
        "too long", req,
        body={"error": {"code": "context_length_exceeded", "message": "too long"}},
    )


def _make_tc(tmp_path):
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])

    model = FunctionModel(fn)
    deps = _make_deps(tmp_path)
    collabs = build_collaborators(
        model, BuiltinToolProvider(), deps, "You are a coding agent.",
        HarnessConfig(), get_model=lambda: model,
    )
    return TurnController(
        agent=collabs.agent, session=collabs.session, checkpoints=collabs.checkpoints,
        hooks=collabs.hooks, mcp=collabs.mcp, deps=deps, get_model=lambda: model,
    )


def _spy_compact(tc):
    """Replace ``_maybe_compact`` with a recorder that reports a successful
    compaction, so the banking/latch logic is exercised without real history
    surgery. Returns the list of ``force`` flags it was called with."""
    calls: list[bool] = []

    async def fake(*, force: bool = False) -> bool:
        calls.append(force)
        return True

    tc._maybe_compact = fake
    return calls


# --- (a) overflow → force-compact + retry, banking round_usage exactly once ---


async def test_overflow_banks_round_usage_once_and_returns_compacted(tmp_path):
    tc = _make_tc(tmp_path)
    compacts = _spy_compact(tc)
    assert tc.session.usage.input_tokens == 0

    round_usage = RunUsage(requests=1, input_tokens=100, output_tokens=5)
    retried: set[_RunRetry] = set()
    result = await tc._handle_run_failure(
        _overflow_exc(), [], [], None, round_usage, retried
    )

    assert result is _RunRetry.COMPACTED
    assert _RunRetry.COMPACTED in retried
    assert compacts == [True]  # forced compaction fired exactly once
    # The failed round's spend is banked ONCE here; the success-path retry banks
    # its own fresh round's ``result.usage`` in the loop — so no double count.
    assert tc.session.usage.input_tokens == 100
    assert tc.session.usage.requests == 1


async def test_overflow_retry_is_one_shot(tmp_path):
    """With COMPACTED already latched, a second overflow does not compact-and-retry
    again — it falls through to the failure path and raises the diagnostic."""
    tc = _make_tc(tmp_path)
    compacts = _spy_compact(tc)

    retried = {_RunRetry.COMPACTED}
    with pytest.raises(ContextWindowExceededError):
        await tc._handle_run_failure(
            _overflow_exc(), [], [], None, RunUsage(requests=1, input_tokens=50), retried
        )
    assert compacts == []  # latch spent → no second forced compaction
    assert tc.session.usage.input_tokens == 50  # still banked once


# --- (b) contention → backoff + retry in place, never force-compacting --------


def _make_contention_tc(tmp_path):
    tc = _make_tc(tmp_path)
    # A KNOWN large window with the last measured request far below it — the
    # pool-contention shape (a small request rejected because parallel spawns
    # exhausted a shared KV pool).
    tc.session.limits = ContextLimits(window_override=102_206)
    tc.session.last_input_tokens = 16_118
    backoffs: list[int] = []

    async def _no_backoff() -> None:
        backoffs.append(1)

    tc._contention_backoff = _no_backoff
    return tc, backoffs


async def test_contention_backs_off_and_retries_without_compacting(tmp_path):
    tc, backoffs = _make_contention_tc(tmp_path)
    compacts = _spy_compact(tc)

    retried: set[_RunRetry] = set()
    result = await tc._handle_run_failure(
        _overflow_exc(), [], [], None, RunUsage(requests=1, input_tokens=200), retried
    )

    assert result is _RunRetry.CONTENTION
    assert _RunRetry.CONTENTION in retried
    assert backoffs == [1]           # backed off once before retrying in place
    assert compacts == []            # history left untouched — never force-compacted
    assert tc.session.usage.input_tokens == 200  # spend banked once


async def test_contention_retry_is_one_shot(tmp_path):
    """A second contention-classified overflow after the latch is spent gives up
    with the contention diagnostic rather than backing off forever."""
    tc, backoffs = _make_contention_tc(tmp_path)
    _spy_compact(tc)

    retried = {_RunRetry.CONTENTION}
    with pytest.raises(ContextWindowExceededError):
        await tc._handle_run_failure(
            _overflow_exc(), [], [], None, RunUsage(requests=1), retried
        )
    assert backoffs == []  # latch spent → no second backoff


# --- (c) the dirty-continuation guard: never compact a deferred round ---------


async def test_dirty_continuation_round_is_not_force_compacted(tmp_path):
    """On an approval-continuation round the in-memory history deliberately ends
    with the round's unanswered tool calls. ``_handle_run_failure`` must NOT
    force-compact it (that would persist exactly the dirty state the approval loop
    promises never reaches disk) — it raises through the normal failure path."""
    tc = _make_tc(tmp_path)
    compacts = _spy_compact(tc)

    deferred_results = object()  # a non-None continuation payload
    with pytest.raises(ContextWindowExceededError):
        await tc._handle_run_failure(
            _overflow_exc(), [], [], deferred_results,
            RunUsage(requests=1, input_tokens=75), set(),
        )
    assert compacts == []  # guard short-circuits before any compaction
    assert tc.session.usage.input_tokens == 75  # spend still banked once


async def test_non_overflow_failure_reraises_original_and_banks_usage(tmp_path):
    """A plain (non-overflow) failure re-raises the original exception unchanged
    after banking the round's spend — no compaction, no retry directive."""
    tc = _make_tc(tmp_path)
    compacts = _spy_compact(tc)

    boom = RuntimeError("render boom")
    with pytest.raises(RuntimeError, match="render boom"):
        await tc._handle_run_failure(
            boom, [], [], None, RunUsage(requests=1, input_tokens=42), set()
        )
    assert compacts == []
    assert tc.session.usage.input_tokens == 42
