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
from pydantic_ai import Agent, capture_run_messages
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


def test_fresh_capture_is_fresh_even_inside_a_used_capture():
    """Pin the private pydantic-ai names ``_fresh_capture`` relies on AND its core
    semantics: inside an already-*used* ``capture_run_messages`` context (the state
    a foreground spawn actually runs in — the main turn's run set the flag), the
    helper must yield a brand-new list bound as the current capture state, and
    restore the outer state on exit. If a pydantic-ai bump renames
    ``_messages_ctx_var``/``_RunMessages`` or changes the used-flag protocol, this
    fails loudly instead of sub-agent resumes silently picking up the
    orchestrator's conversation."""
    from pydantic_ai import _agent_graph

    from marim_harness.subagents.runner import _fresh_capture

    with capture_run_messages() as outer:
        # Simulate the main turn's agent.run having bound this context.
        _agent_graph._messages_ctx_var.get().used = True
        with _fresh_capture() as inner:
            assert inner is not outer
            state = _agent_graph._messages_ctx_var.get()
            assert state.messages is inner
            assert state.used is False  # a run inside will bind to `inner`
        # On exit the outer capture state is restored untouched.
        restored = _agent_graph._messages_ctx_var.get()
        assert restored.messages is outer
        assert restored.used is True


@pytest.mark.anyio
async def test_overflow_resume_inside_main_turn_capture_uses_the_subs_history(tmp_path: Path):
    """THE production topology: a foreground spawn runs inside the main turn's tool
    execution, where the TurnController already holds a capture_run_messages
    context that the main run has bound (``used=True``). pydantic-ai's public
    ``capture_run_messages`` REUSES that state instead of nesting, so a capture
    opened in ``_run_to_completion`` would alias the MAIN turn's message list —
    and an overflow shed (or transient resume) would resume the sub-agent with
    the orchestrator's conversation. This pins that the sub-agent's resume history
    is its OWN conversation and the outer capture stays untouched."""
    runner, _ = _runner(tmp_path)
    state = {"raised": False}
    seen: dict = {}

    def sub_fn(messages, info):
        parts = [p for m in messages for p in getattr(m, "parts", [])]
        returns = [p for p in parts if type(p).__name__ == "ToolReturnPart"]
        if len(returns) < 2:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="blob", args={}, tool_call_id=f"t{len(returns)}")])
        if not state["raised"]:
            state["raised"] = True
            raise _overflow()
        seen["messages"] = messages
        return ModelResponse(parts=[TextPart(content="sub-done")])

    sub = Agent(FunctionModel(sub_fn))

    @sub.tool_plain
    def blob() -> str:
        return "x" * 500

    def outer_fn(messages, info):
        parts = [p for m in messages for p in getattr(m, "parts", [])]
        if not any(type(p).__name__ == "ToolReturnPart" for p in parts):
            return ModelResponse(parts=[ToolCallPart(
                tool_name="delegate", args={}, tool_call_id="outer-t1")])
        return ModelResponse(parts=[TextPart(content="outer-done")])

    outer = Agent(FunctionModel(outer_fn))
    inner_output: dict = {}

    @outer.tool_plain
    async def delegate() -> str:
        result = await runner._run_to_completion(sub, "sub task", None, None, None)
        inner_output["out"] = result.output
        return result.output

    # The outer capture context, bound by the outer run — exactly what
    # TurnController._run_agent_loop holds around the main agent's run.
    with capture_run_messages() as outer_captured:
        result = await outer.run("orchestrate")

    # (a) The spawn recovered and both agents finished.
    assert result.output == "outer-done"
    assert inner_output["out"] == "sub-done"

    def _texts(messages) -> str:
        return " ".join(
            str(getattr(p, "content", ""))
            for m in messages for p in getattr(m, "parts", [])
        )

    def _tool_names(messages) -> set:
        return {
            getattr(p, "tool_name", None)
            for m in messages for p in getattr(m, "parts", [])
        } - {None}

    # (b) The resumed history the sub's model saw is the SUB's conversation:
    # its task and its (shed-masked) tool round — none of the outer agent's.
    from marim_harness.compaction import MASKED_OBSERVATION
    sub_seen = seen["messages"]
    assert "sub task" in _texts(sub_seen)
    assert "orchestrate" not in _texts(sub_seen)
    assert "delegate" not in _tool_names(sub_seen)
    contents = [
        str(p.content) for m in sub_seen for p in getattr(m, "parts", [])
        if type(p).__name__ == "ToolReturnPart"
    ]
    assert contents[0] == MASKED_OBSERVATION   # stale observation shed
    assert contents[1] == "x" * 500            # newest spared (keep_recent=1)

    # (c) The outer captured list still holds the OUTER conversation, untouched
    # by the sub-agent's run and resume.
    assert "orchestrate" in _texts(outer_captured)
    assert "sub task" not in _texts(outer_captured)
    assert "blob" not in _tool_names(outer_captured)
    assert "delegate" in _tool_names(outer_captured)


@pytest.mark.anyio
async def test_overflow_shed_resume_accumulates_usage_across_attempts(tmp_path: Path):
    """The failed (overflowed) attempt's spend must not be dropped: one usage
    accumulator rides every ``sub.run`` call, so the result the callers fold into
    ``session.usage`` covers BOTH attempts — and the shed-and-resumed attempt is
    the largest request of the whole run, exactly the one worth billing."""
    runner, _ = _runner(tmp_path)
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

    result = await runner._run_to_completion(sub, "go", None, None, None)
    assert result.output == "done"
    # First attempt completed 2 tool-round requests before overflowing; the
    # resumed attempt made 1 more. All three must be in the usage the foreground/
    # background tails bank via `session.usage += result.usage`.
    assert result.usage.requests == 3


@pytest.mark.anyio
async def test_give_up_after_retries_banks_partial_usage_into_session(tmp_path: Path):
    """When ``_run_to_completion`` exhausts its budget and re-raises, the spend
    from the failed attempts must land in ``session.usage`` anyway — there is no
    result for the callers to fold, but the provider billed those tokens."""
    runner, _ = _runner(tmp_path)

    def fn(messages, info):
        parts = [p for m in messages for p in getattr(m, "parts", [])]
        if not any(type(p).__name__ == "ToolReturnPart" for p in parts):
            return ModelResponse(parts=[ToolCallPart(
                tool_name="counter", args={}, tool_call_id="t1")])
        raise ModelHTTPError(503, "m", body="overloaded")

    sub = Agent(FunctionModel(fn))

    @sub.tool_plain
    def counter() -> str:
        return "counted"

    before = runner.session.usage.requests
    with pytest.raises(ModelHTTPError):
        await runner._run_to_completion(sub, "go", None, None, None)
    # The first attempt's completed tool-round request survives the give-up.
    assert runner.session.usage.requests == before + 1
    assert runner.session.usage.input_tokens > 0


@pytest.mark.anyio
async def test_foreground_overflow_failure_tells_orchestrator_to_split(tmp_path: Path):
    """When the shed-and-resume backstop is exhausted, the contained foreground
    error must tell the orchestrator what to DO (split/narrow the task) — the
    tool result is model-facing product surface, not a stack trace."""
    runner, _ = _runner(tmp_path)

    async def _boom(*args, **kwargs):
        raise ModelHTTPError(
            400, "m", body={"message": "maximum context length exceeded"}
        )

    runner._run_to_completion = _boom
    out = await runner.run("general", "task", "sid-1")
    assert "overflowed its context window" in out
    assert "split the task" in out.lower()


@pytest.mark.anyio
async def test_contention_overflow_retries_as_transient_instead_of_shedding(tmp_path: Path):
    """A shared-KV-pool rejection (parallel spawns exhausting a local server's
    unified cache) is not this sub-agent's fault: its run is far below the KNOWN
    window, so masking its observations would destroy context for nothing and
    the resumed request would meet the same contention anyway. The overflow must
    take the transient path — backoff, then resume — with nothing masked."""
    import dataclasses

    from marim_harness.config.context_limits import ContextLimits

    runner, sleeps = _runner(tmp_path)
    runner._masking = dataclasses.replace(
        runner._masking, limits=ContextLimits(window_override=200_000)
    )
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
    assert sleeps == [1]  # backed off like a transient error, not an instant shed

    contents = [
        str(p.content) for m in seen["messages"]
        for p in getattr(m, "parts", [])
        if type(p).__name__ == "ToolReturnPart"
    ]
    assert contents == ["x" * 500, "x" * 500]  # nothing masked
