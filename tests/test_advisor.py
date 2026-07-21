"""The advisor core: make_advisor (the advice callable) and its prompt/text
constants. No live models — TestModel/FunctionModel only."""

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from marim_harness.advisor import (
    ADVISOR_GUIDANCE,
    ADVISOR_OFF,
    _advise_prompt,
    make_advisor,
)


def test_advisor_off_sentinel_value():
    # Persisted into session JSON; changing it orphans saved sessions.
    assert ADVISOR_OFF == "off"


def test_advise_prompt_wraps_transcript_and_restates_task():
    prompt = _advise_prompt("User: hi\nAssistant: hello")
    assert "=== TRANSCRIPT START ===" in prompt
    assert "=== TRANSCRIPT END ===" in prompt
    assert "User: hi" in prompt
    # The task must be restated in the user turn (claude-cli appends our
    # instructions to Claude Code's own prompt, so system-only rules drift).
    assert "advice" in prompt.lower() or "guidance" in prompt.lower()


def test_guidance_mentions_the_tool_and_weighing():
    assert "advisor" in ADVISOR_GUIDANCE
    assert "transcript" in ADVISOR_GUIDANCE


@pytest.mark.anyio
async def test_make_advisor_returns_advice_with_usage_trailer():
    advise = make_advisor(
        lambda mid: TestModel(custom_output_text="Refactor the parser first."),
        lambda: "test:model",
        cwd=".",
        max_tokens=64,
    )
    out = await advise([])
    assert out.startswith("Refactor the parser first.")
    assert "[advisor usage:" in out


@pytest.mark.anyio
async def test_no_model_configured_returns_error_string():
    advise = make_advisor(lambda mid: TestModel(), lambda: None, cwd=".")
    out = await advise([])
    assert out.startswith("Advisor unavailable")
    assert "Continue without advice" in out


@pytest.mark.anyio
async def test_build_failure_returns_error_string():
    def broken(mid):
        raise ValueError("no credentials for provider 'nope'")

    advise = make_advisor(broken, lambda: "nope:model", cwd=".")
    out = await advise([])
    assert out.startswith("Advisor unavailable")
    assert "no credentials" in out
    assert "Continue without advice" in out


@pytest.mark.anyio
async def test_run_failure_retries_once_with_tighter_transcript_then_succeeds():
    calls = []

    def flaky(messages, info):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("context overflow")
        return ModelResponse(parts=[TextPart("second try")])

    advise = make_advisor(lambda mid: FunctionModel(flaky), lambda: "m", cwd=".")
    out = await advise([])
    assert out.startswith("second try")
    assert len(calls) == 2


@pytest.mark.anyio
async def test_run_failure_twice_returns_error_string():
    def always_broken(messages, info):
        raise RuntimeError("boom")

    advise = make_advisor(lambda mid: FunctionModel(always_broken), lambda: "m", cwd=".")
    out = await advise([])
    assert out.startswith("Advisor unavailable")
    assert "boom" in out
