"""``ClaudeProcess`` against the scenario fake: argv shape, turn routing,
silence/idle timeouts, interrupt, mid-turn user messages, death handling."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from marim_harness.claude.process import (
    CLOSED,
    ClaudeProcess,
    ProcessOptions,
    build_process_argv,
    child_env,
    next_turn_object,
    turn_objects,
)
from marim_harness.config.external_cli import CliModelError
from tests.fakes import fake_claude_bin, read_claude_argv, read_claude_log

pytestmark = pytest.mark.anyio


# --- pure helpers -----------------------------------------------------------


def test_argv_carries_isolation_and_stream_json_flags():
    argv = build_process_argv(ProcessOptions(binary="/bin/claude", cwd="/ws"))
    assert argv[0] == "/bin/claude"
    for flag in (
        "--strict-mcp-config",
        "--safe-mode",
        "--permission-prompt-tool",
        "--include-partial-messages",
        "--replay-user-messages",
        "--verbose",
    ):
        assert flag in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--permission-prompts") + 1] == "host"
    assert argv[argv.index("--permission-prompt-tool") + 1] == "stdio"
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    # Never: the old one-shot launch flags, and --bare (breaks subscription auth).
    assert "-p" not in argv and "--permission-mode" not in argv and "--bare" not in argv
    assert "--model" not in argv and "--resume" not in argv
    assert "--append-system-prompt" not in argv and "--no-session-persistence" not in argv


def test_argv_optional_flags():
    argv = build_process_argv(
        ProcessOptions(
            binary="claude",
            cwd="/ws",
            model="opus",
            resume_id="S1",
            tools=("Read", "Write"),
            disallowed_tools=("Task", "Agent"),
            append_system="SYS",
            persist=False,
        )
    )
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--resume") + 1] == "S1"
    assert argv[argv.index("--tools") + 1] == "Read,Write"
    assert argv[argv.index("--disallowedTools") + 1] == "Task,Agent"
    assert argv[argv.index("--append-system-prompt") + 1] == "SYS"
    assert "--no-session-persistence" in argv


def test_child_env_strips_nested_claude_markers():
    base = {
        "PATH": "/bin",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_SSE_PORT": "1234",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
        "HOME": "/h",
    }
    env = child_env(base)
    assert env == {"PATH": "/bin", "HOME": "/h"}
    assert child_env({"X": "1"}) == {"X": "1"}


# --- process ----------------------------------------------------------------


def _process(tmp_path: Path, scenario: dict, **kwargs) -> ClaudeProcess:
    binary = fake_claude_bin(tmp_path, scenario)
    return ClaudeProcess(ProcessOptions(binary=binary, cwd=str(tmp_path)), **kwargs)


async def _collect(process: ClaudeProcess, text: str) -> list[dict]:
    handle = await process.send_turn(text)
    return [obj async for obj in turn_objects(process, handle)]


async def test_turn_streams_until_result_and_captures_init(tmp_path: Path):
    process = _process(
        tmp_path, {"session_id": "S7", "version": "2.1.261", "turns": [[{"text": "Hello"}]]}
    )
    await process.start()
    try:
        objs = await _collect(process, "hi")
    finally:
        await process.aclose()
    kinds = [o["type"] for o in objs]
    assert kinds[0] == "user" and objs[0]["isReplay"] is True
    assert kinds[1] == "system" and kinds[-1] == "result"
    assert "stream_event" in kinds and "assistant" in kinds
    assert objs[-1]["result"] == "Hello"
    assert process.session_id == "S7"
    assert process.init_info["claude_code_version"] == "2.1.261"
    argv = read_claude_argv(tmp_path)
    assert "--input-format" in argv and "--resume" not in argv
    sent = read_claude_log(tmp_path)
    assert sent[0]["request"]["subtype"] == "initialize"
    assert sent[1]["type"] == "user"
    assert sent[1]["message"]["content"] == [{"type": "text", "text": "hi"}]


async def test_second_turn_reuses_the_same_process(tmp_path: Path):
    process = _process(tmp_path, {"turns": [[{"text": "one"}], [{"text": "two"}]]})
    await process.start()
    try:
        pid = process.pid
        first = await _collect(process, "a")
        second = await _collect(process, "b")
    finally:
        await process.aclose()
    assert first[-1]["result"] == "one" and second[-1]["result"] == "two"
    assert process.pid is None and pid is not None  # closed now; was one pid throughout
    assert [o["type"] for o in second].count("system") == 0  # init only once per process
    assert len(read_claude_log(tmp_path)) == 3  # initialize + two user messages


async def test_death_mid_turn_delivers_closed_with_stderr(tmp_path: Path):
    scenario = {"turns": [[{"text": "a"}, {"exit": {"code": 3, "stderr": "boom"}}]]}
    process = _process(tmp_path, scenario)
    await process.start()
    try:
        objs = await _collect(process, "go")
    finally:
        await process.aclose()
    assert objs[-1]["type"] == CLOSED
    assert "boom" in objs[-1]["stderr"] and objs[-1]["returncode"] == 3
    assert process.alive is False
    assert "boom" in process.last_stderr


async def test_closed_is_delivered_even_when_settling_the_exit_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`_settle_exit` is best-effort polish (stderr tail + exit code). If it
    raises — `proc.wait()` on an already-reaped pid, a stored exception on the
    shielded stderr task — the open turn must still get its CLOSED object;
    otherwise the consumer blocks for the whole silence timeout (600 s by
    default) while `alive` still reads True."""

    async def boom(self) -> None:
        raise OSError("reaper lost the child")

    monkeypatch.setattr(ClaudeProcess, "_settle_exit", boom)
    scenario = {"turns": [[{"text": "a"}, {"exit": {"code": 3, "stderr": "boom"}}]]}
    process = _process(tmp_path, scenario)
    await process.start()
    try:
        objs = await asyncio.wait_for(_collect(process, "go"), 5.0)
    finally:
        await process.aclose()
    assert objs[-1]["type"] == CLOSED
    assert process.closed.is_set() and process.alive is False


async def test_send_turn_rejects_a_second_turn_while_one_is_open(tmp_path: Path):
    process = _process(tmp_path, {"turns": [[{"text": "a"}, {"sleep": 30}]]})
    await process.start()
    try:
        await process.send_turn("one")
        with pytest.raises(AssertionError):
            await process.send_turn("two")
    finally:
        await process.aclose()


async def test_send_turn_on_dead_process_yields_closed(tmp_path: Path):
    binary = fake_claude_bin(tmp_path, {"known_sessions": ["S1"], "turns": [[{"text": "a"}]]})
    process = ClaudeProcess(ProcessOptions(binary=binary, cwd=str(tmp_path), resume_id="NOPE"))
    await process.start()  # the fake exits 1 before reading stdin
    try:
        handle = await process.send_turn("hi")
        first = await next_turn_object(process, handle)
    finally:
        await process.aclose()
    assert first["type"] == CLOSED
    assert "No conversation found with session ID: NOPE" in first["stderr"]
    assert handle.open is False


async def test_silence_timeout_interrupts_and_raises(tmp_path: Path):
    process = _process(tmp_path, {"turns": [[{"text": "a"}, {"sleep": 30}]]}, silence_timeout=0.3)
    await process.start()
    start = time.monotonic()
    try:
        with pytest.raises(CliModelError) as exc:
            await _collect(process, "go")
    finally:
        await process.aclose()
    assert "timed out" in str(exc.value)
    assert time.monotonic() - start < 5


async def test_silence_clock_pauses_while_a_prompt_is_open(tmp_path: Path):
    ask = {"tool_name": "Write", "input": {"file_path": "x"}, "tool_use_id": "tu1"}
    step = {"can_use_tool": ask}

    async def slow_allow(request_id: str, request: dict) -> dict:
        await asyncio.sleep(0.6)  # longer than the silence timeout below
        return {"behavior": "allow", "updatedInput": request["input"]}

    process = _process(tmp_path, {"turns": [[step]]}, on_request=slow_allow, silence_timeout=0.3)
    await process.start()
    try:
        objs = await _collect(process, "write it")
    finally:
        await process.aclose()
    assert objs[-1]["type"] == "result" and objs[-1]["result"] == "Write done"
    assert objs[-1]["permission_denials"] == []


async def test_request_handler_errors_become_error_responses(tmp_path: Path):
    step = {"can_use_tool": {"tool_name": "Write", "input": {}, "tool_use_id": "tu1"}}

    async def broken(request_id: str, request: dict) -> dict:
        raise RuntimeError("panel exploded")

    process = _process(tmp_path, {"turns": [[step]]}, on_request=broken)
    await process.start()
    try:
        objs = await _collect(process, "write it")
    finally:
        await process.aclose()
    # The fake treats an error response as a deny carrying the error text.
    assert objs[-1]["result"] == "denied: panel exploded"


async def test_interrupt_ends_turn_with_aborted_result(tmp_path: Path):
    process = _process(tmp_path, {"turns": [[{"text": "a"}, {"await_interrupt": True}]]})
    await process.start()
    try:
        handle = await process.send_turn("go")
        seen = []
        while True:
            obj = await next_turn_object(process, handle)
            seen.append(obj)
            if obj["type"] == "assistant":
                break
        await process.interrupt(handle)
        async for obj in turn_objects(process, handle):
            seen.append(obj)
    finally:
        await process.aclose()
    result = seen[-1]
    assert result["type"] == "result" and result["is_error"] is True
    assert result["terminal_reason"] == "aborted_streaming"
    assert handle.open is False
    sent = read_claude_log(tmp_path)
    assert any(m.get("request", {}).get("subtype") == "interrupt" for m in sent)


async def test_cancelled_consumer_interrupts_the_turn(tmp_path: Path):
    process = _process(tmp_path, {"turns": [[{"text": "a"}, {"await_interrupt": True}]]})
    await process.start()
    try:
        handle = await process.send_turn("go")

        async def consume():
            async for _obj in turn_objects(process, handle):
                pass

        task = asyncio.ensure_future(consume())
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(handle.closed.wait(), 3.0)
    finally:
        await process.aclose()
    sent = read_claude_log(tmp_path)
    assert any(m.get("request", {}).get("subtype") == "interrupt" for m in sent)


async def test_send_user_mid_turn_folds_into_the_live_turn(tmp_path: Path):
    process = _process(tmp_path, {"turns": [[{"text": "a"}, {"await_user": True}]]})
    await process.start()
    try:
        handle = await process.send_turn("go")
        while (await next_turn_object(process, handle))["type"] != "assistant":
            pass
        assert process.turn_open is True
        await process.send_user("go left")
        rest = [obj async for obj in turn_objects(process, handle)]
    finally:
        await process.aclose()
    assert rest[-1]["result"] == "aheard: go left"
    assert process.turn_open is False


async def test_idle_timeout_closes_process_between_turns(tmp_path: Path):
    process = _process(tmp_path, {"session_id": "S3", "turns": [[{"text": "a"}]]}, idle_timeout=0.2)
    await process.start()
    try:
        await _collect(process, "go")
        assert process.alive is True
        await asyncio.wait_for(process.closed.wait(), 3.0)
    finally:
        await process.aclose()
    assert process.alive is False
    assert process.session_id == "S3"  # kept for the next turn's --resume


async def test_idle_timer_is_disarmed_by_the_next_turn(tmp_path: Path):
    # Generous margins on purpose: the point is that the second turn re-arms
    # the timer, and a loaded CI leg must not be able to turn "the old timer
    # did not fire" into a scheduling flake.
    process = _process(tmp_path, {"turns": [[{"text": "a"}]]}, idle_timeout=1.5)
    await process.start()
    try:
        await _collect(process, "one")
        await asyncio.sleep(0.6)
        await _collect(process, "two")  # re-arms: the old timer must not fire
        await asyncio.sleep(0.6)
        assert process.alive is True
    finally:
        await process.aclose()


async def test_aclose_kills_a_hung_child_promptly(tmp_path: Path):
    process = _process(tmp_path, {"turns": [[{"text": "a"}, {"sleep": 30}]]})
    await process.start()
    handle = await process.send_turn("go")
    await next_turn_object(process, handle)
    pid = process.pid
    start = time.monotonic()
    await process.aclose()
    assert time.monotonic() - start < 4
    assert process.alive is False and process.closed.is_set()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # type: ignore[arg-type]
    assert handle.open is False
    # Objects the CLI already produced stay queued (a dying turn keeps its
    # prose); the synthetic CLOSED is appended last. Asserting on the *first*
    # queued object instead would only be testing reader timing.
    queued = []
    while not handle.events.empty():
        queued.append(handle.events.get_nowait())
    assert queued[-1]["type"] == CLOSED


async def test_missing_binary_raises_cli_model_error(tmp_path: Path):
    process = ClaudeProcess(ProcessOptions(binary=str(tmp_path / "nope"), cwd=str(tmp_path)))
    with pytest.raises(CliModelError) as exc:
        await process.start()
    assert "claude" in str(exc.value)


# --- turns the CLI runs on its own (background sub-agents) -------------------

_NOTIFICATION = {
    "type": "system",
    "subtype": "task_notification",
    "tool_use_id": "tu1",
    "status": "completed",
    "summary": "pong",
}


def _spawn_turn(after_turn: dict | list | None = None) -> list[dict]:
    """A turn that launches a background Agent and ends before it reports."""
    steps: list[dict] = [
        {"tool_use": {"id": "tu1", "name": "Agent", "input": {"description": "Explore"}}},
        {
            "raw": {
                "type": "system",
                "subtype": "task_started",
                "tool_use_id": "tu1",
                "is_backgrounded": True,
            }
        },
        {"tool_result": {"id": "tu1", "content": "Async agent launched successfully"}},
        {"text": "Launched."},
    ]
    if after_turn is not None:
        steps.append({"after_turn": after_turn})
    return steps


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition never held"
        await asyncio.sleep(0.02)


async def _drain_turn(process: ClaudeProcess, handle) -> list[dict]:
    return [obj async for obj in turn_objects(process, handle)]


async def test_cli_own_turn_is_buffered_and_reported(tmp_path: Path):
    """The notification lands with no turn open, the CLI runs a turn on it:
    both are buffered on one unsolicited handle (notification first, then the
    init and the reaction) and the hook sees the opening objects."""
    own_turn = {"prelude": [_NOTIFICATION], "steps": [{"text": "Agent completed: pong"}]}
    openings: list[list[dict]] = []
    process = _process(tmp_path, {"turns": [_spawn_turn(own_turn)]}, on_unsolicited=openings.append)
    await process.start()
    try:
        first = await _collect(process, "summarize")
        assert first[-1]["result"] == "Launched."
        await _wait_for(lambda: process.has_unsolicited)
        handle = process.take_unsolicited()
        assert handle is not None and process.take_unsolicited() is None
        objs = await _drain_turn(process, handle)
    finally:
        await process.aclose()
    assert objs[0] == _NOTIFICATION
    assert objs[1]["type"] == "system" and objs[1]["subtype"] == "init"
    assert objs[-1]["type"] == "result" and objs[-1]["result"] == "Agent completed: pong"
    assert process.background_tasks == frozenset()
    assert [[o.get("subtype") for o in opening] for opening in openings] == [
        ["task_notification", "init"]
    ]
    assert process.turn_open is False


async def test_notification_without_a_reaction_rides_into_the_next_turn(tmp_path: Path):
    """A notification the CLI never reacts to is not a turn: it waits in the
    prelude and is the first object of the next turn marim sends, so the
    demux settles the card before the new prose."""
    openings: list[list[dict]] = []
    scenario = {"turns": [_spawn_turn({"prelude": [_NOTIFICATION]}), [{"text": "two"}]]}
    process = _process(tmp_path, scenario, on_unsolicited=openings.append)
    await process.start()
    try:
        await _collect(process, "one")
        await _wait_for(lambda: not process.background_tasks)
        assert process.has_unsolicited is False
        second = await _collect(process, "again")
    finally:
        await process.aclose()
    assert second[0] == _NOTIFICATION
    assert second[1]["type"] == "user" and second[-1]["result"] == "two"
    assert openings == []


async def test_stray_objects_with_no_turn_open_are_dropped(tmp_path: Path):
    """Only lifecycle events wait between turns: a stray result/assistant
    replay must not pollute the next turn (it would end it early)."""
    stray = {"type": "result", "subtype": "success", "result": "stale", "is_error": False}
    scenario = {"turns": [[{"text": "a"}, {"after_turn": {"prelude": [stray]}}], [{"text": "b"}]]}
    process = _process(tmp_path, scenario)
    await process.start()
    try:
        await _collect(process, "one")
        await asyncio.sleep(0.2)
        assert process.has_unsolicited is False
        second = await _collect(process, "two")
    finally:
        await process.aclose()
    assert second[0]["type"] == "user" and second[-1]["result"] == "b"


async def test_send_turn_waits_for_the_cli_own_turn_to_finish(tmp_path: Path):
    """A marim turn sent while the CLI's own turn streams would receive that
    turn's result and end early: `send_turn` lets it finish first."""
    own_turn = {"prelude": [_NOTIFICATION], "steps": [{"sleep": 0.4}, {"text": "reaction"}]}
    scenario = {"turns": [_spawn_turn(own_turn), [{"text": "two"}]]}
    process = _process(tmp_path, scenario)
    await process.start()
    try:
        await _collect(process, "one")
        await _wait_for(lambda: process.has_unsolicited)
        own = process.take_unsolicited()
        assert own is not None and own.open is True
        handle = await process.send_turn("two")
        assert own.open is False  # finished before the user message went out
        second = await _drain_turn(process, handle)
        own_objs = await _drain_turn(process, own)
    finally:
        await process.aclose()
    assert own_objs[-1]["result"] == "reaction"
    assert second[0]["type"] == "user" and second[-1]["result"] == "two"


async def test_cli_own_turn_that_hangs_is_interrupted_before_the_next_turn(tmp_path: Path):
    """The CLI must really stop its own turn, not just marim's view of it:
    otherwise its late ``result`` would end the next marim turn instead."""
    own_turn = {"prelude": [_NOTIFICATION], "steps": [{"text": "x"}, {"await_interrupt": True}]}
    scenario = {"turns": [_spawn_turn(own_turn), [{"text": "Two."}]]}
    process = _process(tmp_path, scenario, silence_timeout=0.3)
    await process.start()
    try:
        await _collect(process, "one")
        await _wait_for(lambda: process.has_unsolicited)
        own = process.take_unsolicited()
        assert own is not None
        started = time.monotonic()
        handle = await process.send_turn("two")
        assert time.monotonic() - started < 3.0
        own_objs = await _drain_turn(process, own)
        assert own_objs[-1]["type"] == "result" and own_objs[-1]["is_error"] is True
        assert own_objs[-1]["terminal_reason"] == "aborted_streaming"
        objs = [obj async for obj in turn_objects(process, handle)]
        assert [o["type"] for o in objs][-1] == "result" and not objs[-1].get("is_error")
        assert process.alive is True
    finally:
        await process.aclose()


async def test_cli_own_turn_that_ignores_the_interrupt_is_killed(tmp_path: Path):
    own_turn = {"prelude": [_NOTIFICATION], "steps": [{"text": "x"}, {"sleep": 30}]}
    scenario = {"turns": [_spawn_turn(own_turn)]}
    process = _process(tmp_path, scenario, silence_timeout=0.3)
    await process.start()
    try:
        await _collect(process, "one")
        await _wait_for(lambda: process.has_unsolicited)
        own = process.take_unsolicited()
        assert own is not None
        handle = await process.send_turn("two")
        own_objs = await _drain_turn(process, own)
        assert own_objs[-1]["type"] == CLOSED
        assert process.alive is False and handle.open is False
    finally:
        await process.aclose()


async def test_death_during_the_cli_own_turn_delivers_closed(tmp_path: Path):
    own_turn = {
        "prelude": [_NOTIFICATION],
        "steps": [{"text": "x"}, {"exit": {"code": 2, "stderr": "boom"}}],
    }
    process = _process(tmp_path, {"turns": [_spawn_turn(own_turn)]})
    await process.start()
    try:
        await _collect(process, "one")
        await _wait_for(lambda: process.has_unsolicited)
        own = process.take_unsolicited()
        assert own is not None
        objs = await asyncio.wait_for(_drain_turn(process, own), 5.0)
    finally:
        await process.aclose()
    assert objs[-1]["type"] == CLOSED and objs[-1]["returncode"] == 2
    assert process.background_tasks == frozenset()


async def test_idle_reaper_holds_while_a_background_agent_runs(tmp_path: Path):
    """An idle close would kill the running sub-agent and lose its report:
    the normal clock starts only when its notification lands."""
    scenario = {"turns": [_spawn_turn({"delay": 0.6, "prelude": [_NOTIFICATION]})]}
    process = _process(tmp_path, scenario, idle_timeout=0.2)
    await process.start()
    try:
        await _collect(process, "one")
        await asyncio.sleep(0.4)
        assert process.alive is True and process.background_tasks == frozenset({"tu1"})
        await asyncio.wait_for(process.closed.wait(), 3.0)
    finally:
        await process.aclose()
    assert process.alive is False and process.background_tasks == frozenset()


async def test_idle_hold_for_an_agent_that_never_reports_is_bounded(tmp_path: Path):
    """A sub-agent whose notification never comes must not pin the process
    forever: the hold is the idle timeout stretched, not switched off."""
    scenario = {"turns": [_spawn_turn()]}
    process = _process(tmp_path, scenario, idle_timeout=0.1)
    await process.start()
    try:
        await _collect(process, "one")
        await asyncio.sleep(0.4)
        assert process.alive is True and process.background_tasks == frozenset({"tu1"})
        await asyncio.wait_for(process.closed.wait(), 3.0)
    finally:
        await process.aclose()
    assert process.alive is False


async def test_wait_background_hands_over_the_cli_reaction_turn(tmp_path: Path):
    """A one-turn consumer (a spawn, a headless run) waits the background
    agent out: the reaction turn is handed over once it opens, and the wait
    ends at once — no grace — when nothing is left to hear from."""
    own_turn = {
        "prelude": [_NOTIFICATION],
        "steps": [{"text": "Agent completed: pong"}],
        "delay": 0.3,
    }
    process = _process(tmp_path, {"turns": [_spawn_turn(own_turn)]})
    await process.start()
    try:
        await _collect(process, "one")
        assert process.background_tasks == frozenset({"tu1"})
        assert await process.wait_background(3.0) is True
        handle = process.take_unsolicited()
        assert handle is not None and handle.own
        reaction = await _drain_turn(process, handle)
        started = time.monotonic()
        assert await process.wait_background(3.0) is False
        assert time.monotonic() - started < 0.5
    finally:
        await process.aclose()
    assert reaction[0] == _NOTIFICATION and reaction[-1]["result"] == "Agent completed: pong"
    assert process.take_prelude() == []


async def test_wait_background_gives_a_notification_only_grace_then_yields_it(tmp_path: Path):
    """A notification the CLI does not react to: the wait gives the reaction
    a short grace, then reports nothing to consume and leaves the
    notification in the prelude for the consumer to settle the card with."""
    scenario = {"turns": [_spawn_turn({"prelude": [_NOTIFICATION], "delay": 0.2})]}
    process = _process(tmp_path, scenario)
    await process.start()
    try:
        await _collect(process, "one")
        assert await process.wait_background(3.0, grace=0.3) is False
        assert process.background_tasks == frozenset()
        assert process.take_prelude() == [_NOTIFICATION]
    finally:
        await process.aclose()


async def test_wait_background_times_out_on_an_agent_that_never_reports(tmp_path: Path):
    """Bounded: an agent whose notification never comes ends the wait at the
    timeout, with the agent still on the books (the close will kill it)."""
    process = _process(tmp_path, {"turns": [_spawn_turn()]})
    await process.start()
    try:
        await _collect(process, "one")
        started = time.monotonic()
        assert await process.wait_background(0.3) is False
        assert 0.25 < time.monotonic() - started < 2.0
        assert process.background_tasks == frozenset({"tu1"})
    finally:
        await process.aclose()


async def test_wait_background_returns_at_once_without_background_work(tmp_path: Path):
    process = _process(tmp_path, {"turns": [[{"text": "plain"}]]})
    await process.start()
    try:
        await _collect(process, "one")
        started = time.monotonic()
        assert await process.wait_background(3.0) is False
        assert time.monotonic() - started < 0.2
    finally:
        await process.aclose()
    # A closed process has nothing to wait for either.
    assert await process.wait_background(3.0) is False
