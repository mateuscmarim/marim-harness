"""Sub-agent transient-error retries.

A sub-agent run that dies on a transient provider error (a 504 gateway timeout, a
429, an OpenRouter-wrapped upstream 5xx) used to be a hard failure — the whole
run's context was lost and the orchestrator had to re-spawn or do the work itself.
The runner now retries transient failures with backoff before giving up, while a
permanent error (a genuine bad request) still fails fast. These tests pin that on
the inner ``_run_to_completion`` loop so they don't depend on a live model.
"""

from pathlib import Path

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from tests.conftest import _make_deps, _make_harness, _text_model


class _Result:
    output = "ok"


class _FlakySub:
    """A stand-in for a built sub-agent whose ``run`` fails ``fail_times`` times
    with ``error`` before succeeding."""

    def __init__(self, error: Exception, fail_times: int) -> None:
        self.error = error
        self.fail_times = fail_times
        self.calls = 0

    async def run(self, task, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error
        return _Result()


def _runner(tmp_path: Path):
    """A real SubagentRunner with its backoff replaced by a recorder, so retries
    don't actually sleep. Returns ``(runner, sleeps)``."""
    deps = _make_deps(tmp_path)
    runner = _make_harness(_text_model(), deps).subagents
    sleeps: list[int] = []

    async def _record(attempt: int) -> None:
        sleeps.append(attempt)

    runner._retry_backoff = _record
    return runner, sleeps


def test_subagent_gets_the_same_tool_retry_budget_as_main_agent(tmp_path: Path):
    """A built sub-agent must carry retries=2 (the main agent's tool-arg budget),
    not pydantic-ai's default of 1. At budget 1 a single malformed tool argument —
    e.g. a model applying Claude Code's grep (-i/output_mode) or bash (ms timeout)
    interface to marim's tools — kills the whole sub-agent with
    UnexpectedModelBehavior before it can self-correct on a retry."""
    runner, _ = _runner(tmp_path)
    sub, err = runner.build("general")
    assert err is None, err
    assert sub is not None
    assert sub._max_tool_retries == 2


@pytest.mark.anyio
async def test_retries_a_transient_error_then_succeeds(tmp_path: Path):
    runner, sleeps = _runner(tmp_path)
    sub = _FlakySub(ModelHTTPError(504, "m", body="idle timeout"), fail_times=1)
    result = await runner._run_to_completion(sub, "task", None, None, None)
    assert result.output == "ok"
    assert sub.calls == 2          # one failure, one success
    assert sleeps == [1]           # backed off once before the retry


@pytest.mark.anyio
async def test_gives_up_after_the_retry_budget_and_reraises(tmp_path: Path):
    runner, sleeps = _runner(tmp_path)
    err = ModelHTTPError(503, "m", body="overloaded")
    sub = _FlakySub(err, fail_times=99)
    with pytest.raises(ModelHTTPError):
        await runner._run_to_completion(sub, "task", None, None, None)
    # default budget is 2 retries → 3 attempts total, 2 backoffs
    assert sub.calls == 3
    assert sleeps == [1, 2]


@pytest.mark.anyio
async def test_does_not_retry_a_permanent_error(tmp_path: Path):
    runner, sleeps = _runner(tmp_path)
    body = {"message": "invalid request: unsupported parameter", "code": 400}
    sub = _FlakySub(ModelHTTPError(400, "m", body=body), fail_times=99)
    with pytest.raises(ModelHTTPError):
        await runner._run_to_completion(sub, "task", None, None, None)
    assert sub.calls == 1          # failed once, never retried
    assert sleeps == []


@pytest.mark.anyio
async def test_resumes_after_transient_error_without_re_running_work(tmp_path: Path):
    """A transient error deep in a multi-step run resumes from the captured
    conversation instead of restarting it — the tool call already completed in the
    failed attempt is NOT run a second time."""
    runner, sleeps = _runner(tmp_path)
    state = {"tool_runs": 0, "raised": False}

    def fn(messages, info):
        parts = [p for m in messages for p in getattr(m, "parts", [])]
        has_return = any(type(p).__name__ == "ToolReturnPart" for p in parts)
        if not has_return:
            # Step 1: call the tool.
            return ModelResponse(parts=[ToolCallPart(
                tool_name="counter", args={}, tool_call_id="t1")])
        if not state["raised"]:
            # Step 2 of the first attempt: fail transiently *after* the tool ran.
            state["raised"] = True
            raise ModelHTTPError(429, "rate limited", body={"e": 1})
        # Resumed: the tool result is already in history → finish.
        return ModelResponse(parts=[TextPart(content="done")])

    sub = Agent(FunctionModel(fn))

    @sub.tool_plain
    def counter() -> str:
        state["tool_runs"] += 1
        return "counted"

    result = await runner._run_to_completion(sub, "go", None, None, None)
    assert result.output == "done"
    assert state["tool_runs"] == 1     # resumed, not restarted from scratch
    assert sleeps == [1]               # one transient retry


@pytest.mark.anyio
async def test_subagent_strips_a_nameless_tool_call_before_the_next_request(tmp_path: Path):
    """A sub-agent's model can emit a structurally-broken tool call LIVE mid-run (a
    nameless call, or args that aren't valid JSON). The main agent scrubs these
    before every request via a ProcessHistory capability; a sub-agent built without
    it carries the broken call into the next request, which the provider 400s with
    'tool_calls[i] is missing a function name'. The built sub-agent must get the
    same scrub, so the malformed call never reaches the next request."""
    seen: dict = {}
    calls = {"n": 0}

    def fn(messages, info):
        calls["n"] += 1
        if calls["n"] == 1:
            # A flaky provider streams a tool call whose function name never lands.
            return ModelResponse(
                parts=[ToolCallPart(tool_name="", args={}, tool_call_id="bad")]
            )
        seen["messages"] = messages
        return ModelResponse(parts=[TextPart(content="done")])

    deps = _make_deps(tmp_path)
    runner = _make_harness(FunctionModel(fn), deps).subagents
    sub, err = runner.build("general")
    assert err is None, err
    assert sub is not None

    result = await runner._run_to_completion(sub, "go", deps, None, None)

    assert result.output == "done"
    # The continuation request the model saw must not carry the nameless call.
    nameless = [
        p
        for m in seen["messages"]
        for p in getattr(m, "parts", [])
        if isinstance(p, ToolCallPart) and not p.tool_name
    ]
    assert nameless == []


@pytest.mark.anyio
async def test_retry_emits_a_ui_notice_for_a_foreground_spawn(tmp_path: Path):
    runner, _ = _runner(tmp_path)
    notices: list[tuple[str, str]] = []

    async def _notice(stream_id: str, message: str) -> None:
        notices.append((stream_id, message))

    runner.deps.ui.on_subagent_notice = _notice
    sub = _FlakySub(ModelHTTPError(504, "m", body="idle timeout"), fail_times=1)
    await runner._run_to_completion(sub, "task", None, None, None, "sid-1")
    assert len(notices) == 1
    stream_id, message = notices[0]
    assert stream_id == "sid-1"
    assert "retry" in message.lower()


@pytest.mark.anyio
async def test_no_ui_notice_when_there_is_no_stream(tmp_path: Path):
    # A background spawn (stream_id None) has no card to annotate; the notice
    # callback must not fire.
    runner, _ = _runner(tmp_path)
    notices: list[tuple[str, str]] = []

    async def _notice(stream_id: str, message: str) -> None:
        notices.append((stream_id, message))

    runner.deps.ui.on_subagent_notice = _notice
    sub = _FlakySub(ModelHTTPError(504, "m", body="idle timeout"), fail_times=1)
    await runner._run_to_completion(sub, "task", None, None, None, None)
    assert notices == []


def _overflow() -> ModelHTTPError:
    return ModelHTTPError(
        400, "m", body={"message": "This model's maximum context length is "
                                   "8192 tokens; your request used more."}
    )


@pytest.mark.anyio
async def test_overflow_sheds_stale_observations_and_resumes(tmp_path: Path):
    """A context overflow mid-run is recovered ONCE: the captured conversation is
    resumed with stale tool observations masked, so the run finishes instead of
    dying — and without re-running the tools (same resume contract as the
    transient path)."""
    runner, sleeps = _runner(tmp_path)
    state = {"raised": False}
    seen: dict = {}

    def fn(messages, info):
        parts = [p for m in messages for p in getattr(m, "parts", [])]
        returns = [p for p in parts if type(p).__name__ == "ToolReturnPart"]
        if len(returns) < 2:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="blob", args={}, tool_call_id=f"t{len(returns)}")])
        if not state["raised"]:
            state["raised"] = True
            raise _overflow()
        seen["messages"] = messages
        return ModelResponse(parts=[TextPart(content="done")])

    sub = Agent(FunctionModel(fn))

    @sub.tool_plain
    def blob() -> str:
        return "x" * 500

    result = await runner._run_to_completion(sub, "go", None, None, None)
    assert result.output == "done"
    assert sleeps == []  # overflow recovery resumes immediately, no backoff

    from marim_harness.compaction import MASKED_OBSERVATION
    contents = [
        str(p.content) for m in seen["messages"]
        for p in getattr(m, "parts", [])
        if type(p).__name__ == "ToolReturnPart"
    ]
    assert contents[0] == MASKED_OBSERVATION   # stale observation shed
    assert contents[1] == "x" * 500            # newest spared (keep_recent=1)


@pytest.mark.anyio
async def test_overflow_with_nothing_to_shed_raises(tmp_path: Path):
    """When masking can free nothing (only one observation, which is spared), a
    resume would fail identically — the overflow must surface, not loop."""
    runner, _ = _runner(tmp_path)
    calls = {"n": 0}

    def fn(messages, info):
        calls["n"] += 1
        parts = [p for m in messages for p in getattr(m, "parts", [])]
        if not any(type(p).__name__ == "ToolReturnPart" for p in parts):
            return ModelResponse(parts=[ToolCallPart(
                tool_name="blob", args={}, tool_call_id="t0")])
        raise _overflow()

    sub = Agent(FunctionModel(fn))

    @sub.tool_plain
    def blob() -> str:
        return "x" * 500

    with pytest.raises(ModelHTTPError):
        await runner._run_to_completion(sub, "go", None, None, None)
    assert calls["n"] == 2  # tool round + the failing request; no resume attempt


@pytest.mark.anyio
async def test_overflow_gives_up_after_one_shed(tmp_path: Path):
    """A second overflow after a successful shed-and-resume surfaces: masking
    already freed everything it could."""
    runner, _ = _runner(tmp_path)
    calls = {"n": 0}

    def fn(messages, info):
        calls["n"] += 1
        parts = [p for m in messages for p in getattr(m, "parts", [])]
        returns = [p for p in parts if type(p).__name__ == "ToolReturnPart"]
        if len(returns) < 2:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="blob", args={}, tool_call_id=f"t{len(returns)}")])
        raise _overflow()

    sub = Agent(FunctionModel(fn))

    @sub.tool_plain
    def blob() -> str:
        return "x" * 500

    with pytest.raises(ModelHTTPError):
        await runner._run_to_completion(sub, "go", None, None, None)
    # 2 tool rounds + overflow, then exactly ONE resumed request that overflows
    # again and surfaces: 4 model calls total.
    assert calls["n"] == 4


@pytest.mark.anyio
async def test_overflow_shed_emits_a_ui_notice_for_a_foreground_spawn(tmp_path: Path):
    runner, _ = _runner(tmp_path)
    notices: list[tuple[str, str]] = []

    async def _notice(stream_id: str, message: str) -> None:
        notices.append((stream_id, message))

    runner.deps.ui.on_subagent_notice = _notice
    state = {"raised": False}

    def fn(messages, info):
        parts = [p for m in messages for p in getattr(m, "parts", [])]
        returns = [p for p in parts if type(p).__name__ == "ToolReturnPart"]
        if len(returns) < 2:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="blob", args={}, tool_call_id=f"t{len(returns)}")])
        if not state["raised"]:
            state["raised"] = True
            raise _overflow()
        return ModelResponse(parts=[TextPart(content="done")])

    sub = Agent(FunctionModel(fn))

    @sub.tool_plain
    def blob() -> str:
        return "x" * 500

    await runner._run_to_completion(sub, "go", None, None, None, "sid-1")
    assert len(notices) == 1
    stream_id, message = notices[0]
    assert stream_id == "sid-1"
    assert "overflow" in message.lower()
