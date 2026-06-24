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
from pydantic_ai.exceptions import ModelHTTPError

from marim_harness.deps import Deps
from marim_harness.permissions import Mode
from tests.conftest import _make_harness, _text_model


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
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    runner = _make_harness(_text_model(), deps).subagents
    sleeps: list[int] = []

    async def _record(attempt: int) -> None:
        sleeps.append(attempt)

    runner._retry_backoff = _record
    return runner, sleeps


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
async def test_retry_emits_a_ui_notice_for_a_foreground_spawn(tmp_path: Path):
    runner, _ = _runner(tmp_path)
    notices: list[tuple[str, str]] = []

    async def _notice(stream_id: str, message: str) -> None:
        notices.append((stream_id, message))

    runner.deps.on_subagent_notice = _notice
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

    runner.deps.on_subagent_notice = _notice
    sub = _FlakySub(ModelHTTPError(504, "m", body="idle timeout"), fail_times=1)
    await runner._run_to_completion(sub, "task", None, None, None, None)
    assert notices == []
