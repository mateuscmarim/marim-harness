from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.messages import ToolReturnPart

from marim_harness.claude.approvals import HEADLESS_DENY_MESSAGE, ClaudeApprovalBroker
from marim_harness.runtime.permissions import Mode, Reach, UiSeams
from marim_harness.subagents.cli_backend import ClaudeCliRunner, CliRunError, synth_usage
from marim_harness.usage import COST_DETAIL_KEY
from tests.fakes import fake_claude_bin, read_claude_argv, read_claude_log


def _run_kwargs(binary: str, cwd: Path, **overrides) -> dict:
    kwargs = dict(
        binary=binary,
        prompt="do the task",
        system_prompt="role",
        cwd=str(cwd),
        allowed_tools=["read_file", "write_file"],
        model="opus",
        stream_id="s1",
    )
    kwargs.update(overrides)
    return kwargs


def _broker(mode: Mode, root: Path, *, panel=None) -> ClaudeApprovalBroker:
    return ClaudeApprovalBroker(
        mode_getter=lambda: mode,
        reach=Reach(root=root, scratchpad=lambda: None),
        ui=UiSeams(request_approval=panel, ask_user=None),
        label="worker",
    )


@pytest.mark.anyio
async def test_run_streams_events_captures_session_and_checkpoints(tmp_path):
    scenario = {
        "session_id": "SID-1",
        "turns": [
            [
                {"text": "hi"},
                {"tool_use": {"id": "t1", "name": "Read", "input": {"file_path": "/a"}}},
                {"tool_result": {"id": "t1", "content": "x"}},
                {"text": "Done: report"},
                # The fake's auto-result text concatenates every `text` step, so
                # pin the terminal result explicitly — the runner's output must
                # come from the result object, not from the streamed text.
                {"result": {"result": "Done: report"}},
            ]
        ],
    }
    binary = fake_claude_bin(tmp_path, scenario)
    events: list = []
    ckpts: list = []

    async def on_event(sid, ev, usage):
        events.append((sid, ev))

    def checkpoint(transcript, session_id):
        ckpts.append((len(transcript), session_id))

    runner = ClaudeCliRunner(on_event, None)
    result = await runner.run(**_run_kwargs(binary, tmp_path, checkpoint=checkpoint))
    assert result.output == "Done: report" and result.session_id == "SID-1"
    assert result.usage.input_tokens == 7 and result.usage.details[COST_DETAIL_KEY] == 1000
    assert events and all(sid == "s1" for sid, _ in events)
    assert ckpts and ckpts[-1][1] == "SID-1"
    argv = read_claude_argv(tmp_path)
    assert argv[argv.index("--append-system-prompt") + 1] == "role"
    assert argv[argv.index("--tools") + 1] == "Read,Write"
    assert argv[argv.index("--model") + 1] == "opus"
    assert "--resume" not in argv and "--disallowedTools" not in argv
    assert "--permission-mode" not in argv and "--safe-mode" in argv


@pytest.mark.anyio
async def test_resume_omits_system_prompt_and_threads_the_id(tmp_path):
    binary = fake_claude_bin(tmp_path, {"known_sessions": ["OLD"], "turns": [[{"text": "back"}]]})
    result = await ClaudeCliRunner(None, None).run(
        **_run_kwargs(
            binary,
            tmp_path,
            resume_session_id="OLD",
            disallowed_tools=["WebFetch", "WebSearch"],
        )
    )
    assert result.output == "back"
    argv = read_claude_argv(tmp_path)
    assert argv[argv.index("--resume") + 1] == "OLD"
    assert "--append-system-prompt" not in argv
    assert argv[argv.index("--disallowedTools") + 1] == "WebFetch,WebSearch"


@pytest.mark.anyio
async def test_run_raises_when_claude_exits_without_result(tmp_path):
    binary = fake_claude_bin(
        tmp_path, {"turns": [[{"text": "a"}, {"exit": {"code": 2, "stderr": "bad"}}]]}
    )
    with pytest.raises(CliRunError) as exc:
        await ClaudeCliRunner(None, None).run(**_run_kwargs(binary, tmp_path))
    assert "no result" in str(exc.value) and "claude exited (code 2): bad" in str(exc.value)


@pytest.mark.anyio
async def test_run_times_out_on_silence(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_CLAUDE_CLI_TIMEOUT", "0.3")
    binary = fake_claude_bin(tmp_path, {"turns": [[{"text": "a"}, {"sleep": 30}]]})
    with pytest.raises(CliRunError) as exc:
        await ClaudeCliRunner(None, None).run(**_run_kwargs(binary, tmp_path))
    assert "timed out" in str(exc.value)


@pytest.mark.anyio
async def test_prompts_go_through_the_broker(tmp_path):
    step = {
        "can_use_tool": {
            "tool_name": "Write",
            "input": {"file_path": str(tmp_path / "out.txt"), "content": "x"},
        }
    }
    binary = fake_claude_bin(tmp_path, {"turns": [[step]]})
    seen: list = []

    async def panel(call):
        seen.append(call)
        return True

    result = await ClaudeCliRunner(None, None).run(
        **_run_kwargs(binary, tmp_path, broker=_broker(Mode.ask, tmp_path, panel=panel))
    )
    assert result.output == "Write done"
    assert seen[0].tool_name == "write_file" and seen[0].args["label"] == "worker"


@pytest.mark.anyio
async def test_headless_broker_denies_mutations(tmp_path):
    step = {
        "can_use_tool": {
            "tool_name": "Write",
            "input": {"file_path": str(tmp_path / "out.txt"), "content": "x"},
        }
    }
    binary = fake_claude_bin(tmp_path, {"turns": [[step]]})
    # ask mode escalates a mutation to a prompt; with no approver bound (headless)
    # the broker answers the deny itself rather than hanging.
    result = await ClaudeCliRunner(None, None).run(
        **_run_kwargs(binary, tmp_path, broker=_broker(Mode.ask, tmp_path))
    )
    assert result.output == "denied: " + HEADLESS_DENY_MESSAGE


@pytest.mark.anyio
async def test_run_without_broker_answers_prompts_with_an_error(tmp_path):
    step = {"can_use_tool": {"tool_name": "Read", "input": {"file_path": "/a"}}}
    binary = fake_claude_bin(tmp_path, {"turns": [[step]]})
    await ClaudeCliRunner(None, None).run(**_run_kwargs(binary, tmp_path))
    replies = [m for m in read_claude_log(tmp_path) if m.get("type") == "control_response"]
    assert replies and replies[0]["response"]["subtype"] == "error"


@pytest.mark.anyio
async def test_stream_events_do_not_reach_the_transcript(tmp_path):
    binary = fake_claude_bin(tmp_path, {"turns": [[{"text": "abcdef"}]]})
    events: list = []

    async def on_event(sid, ev, usage):
        events.append(ev)

    result = await ClaudeCliRunner(on_event, None).run(**_run_kwargs(binary, tmp_path))
    # The fake emits 3-char deltas then one assistant block: the transcript
    # holds the single whole message, not the deltas.
    assert result.output == "abcdef"
    texts = [getattr(p, "content", None) for m in result.transcript for p in m.parts]
    assert texts.count("abcdef") == 1


def test_synth_usage_rounds_micro_usd():
    # round(), not int(): a billed 1.001 USD is 1000999.9999999999 as a float, so
    # int() truncates to 1000999 — one micro-USD short — while round() gives the
    # exact 1001000, matching config/openrouter_cost.py's rounding.
    assert int(1.001 * 1_000_000) == 1000999  # pins the truncation the fix avoids
    usage = synth_usage({"input_tokens": 1, "output_tokens": 1}, 1, total_cost_usd=1.001)
    assert usage.details[COST_DETAIL_KEY] == 1001000


# --- background Agents of the spawn's own claude (issue #130) -----------------

_NOTIFICATION = {
    "type": "system",
    "subtype": "task_notification",
    "tool_use_id": "tu1",
    "status": "completed",
    "summary": "pong",
}


def _spawn_turn(after_turn: dict | None = None) -> list[dict]:
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
        {"result": {"result": "Launched.", "total_cost_usd": 0.002}},
    ]
    if after_turn is not None:
        steps.append({"after_turn": after_turn})
    return steps


def _card_returns(events: list) -> list[tuple[str, str]]:
    """(stream, content) of every spawn_agent return the sink saw — the
    demux settling a Claude-side sub-agent's card."""
    out = []
    for sid, ev in events:
        part = getattr(ev, "part", None)
        if isinstance(part, ToolReturnPart) and part.tool_name == "spawn_agent":
            out.append((sid, str(part.content)))
    return out


@pytest.mark.anyio
async def test_run_waits_for_the_background_agent_and_reports_the_reaction(tmp_path):
    """The spawn's claude launched a background Agent and ended its turn:
    the runner keeps the process open until Claude reacts to the report and
    that reaction is the spawn's report (last result), with the card settled
    and both turns' usage summed."""
    own_turn = {
        "prelude": [_NOTIFICATION],
        "steps": [
            {"text": "Agent completed: pong"},
            {"result": {"result": "Agent completed: pong", "total_cost_usd": 0.005}},
        ],
        "delay": 0.3,
    }
    binary = fake_claude_bin(tmp_path, {"turns": [_spawn_turn(own_turn)]})
    events: list = []

    async def on_event(sid, ev, usage):
        events.append((sid, ev))

    result = await ClaudeCliRunner(on_event, None).run(**_run_kwargs(binary, tmp_path))
    assert result.output == "Agent completed: pong"
    assert result.usage.input_tokens == 14 and result.usage.details[COST_DETAIL_KEY] == 5000
    assert _card_returns(events) == [("s1", "pong")]
    # One process, two turns, no second user message: the reaction was
    # Claude's own, not something the runner sent.
    users = [m for m in read_claude_log(tmp_path) if m.get("type") == "user"]
    assert len(users) == 1


@pytest.mark.anyio
async def test_run_settles_a_report_the_cli_never_reacted_to(tmp_path):
    """The agent reports but Claude runs no turn on it: the sent turn's
    result stands, and the notification still settles the card on the way
    out instead of dying in the process's prelude."""
    binary = fake_claude_bin(
        tmp_path, {"turns": [_spawn_turn({"prelude": [_NOTIFICATION], "delay": 0.2})]}
    )
    events: list = []

    async def on_event(sid, ev, usage):
        events.append((sid, ev))

    result = await ClaudeCliRunner(on_event, None).run(**_run_kwargs(binary, tmp_path))
    assert result.output == "Launched." and result.usage.details[COST_DETAIL_KEY] == 2000
    assert _card_returns(events) == [("s1", "pong")]


@pytest.mark.anyio
async def test_run_gives_up_on_an_agent_that_never_reports(tmp_path, monkeypatch):
    """Bounded by the silence timeout: the sent turn's result is the report
    and the spawn ends (killing the agent with the process) rather than
    pinning its slot forever."""
    monkeypatch.setenv("MARIM_CLAUDE_CLI_TIMEOUT", "0.3")
    binary = fake_claude_bin(tmp_path, {"turns": [_spawn_turn({"delay": 30})]})
    result = await ClaudeCliRunner(None, None).run(**_run_kwargs(binary, tmp_path))
    assert result.output == "Launched."


@pytest.mark.anyio
async def test_run_keeps_the_report_when_the_reaction_goes_silent(tmp_path, monkeypatch):
    """A reaction turn that hangs is interrupted like any silent turn, but it
    does not fail the spawn: the sent turn already produced the report."""
    monkeypatch.setenv("MARIM_CLAUDE_CLI_TIMEOUT", "0.3")
    own_turn = {"prelude": [_NOTIFICATION], "steps": [{"text": "hmm"}, {"await_interrupt": True}]}
    binary = fake_claude_bin(tmp_path, {"turns": [_spawn_turn(own_turn)]})
    result = await ClaudeCliRunner(None, None).run(**_run_kwargs(binary, tmp_path))
    assert result.output == "Launched."
