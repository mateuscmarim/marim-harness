import os
from typing import cast

import pytest
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.usage import RunUsage

from marim_harness.subagents.cli_backend import (
    CLI_BINARY_ENV,
    ClaudeCliRunner,
    CliRunError,
    CliStreamTranslator,
    map_tools_to_cc,
    resolve_cli_binary,
    sum_result_usages,
    synth_usage,
)
from marim_harness.tools.names import READ_TOOLS, SUBAGENT_TOOLS
from tests.fakes import fake_claude_bin


def test_tool_map_drops_unmapped_and_sorts():
    # READ_TOOLS = read_file, glob, tree, grep + LSP tools. Only read_file/glob/grep
    # map; tree and LSP names are dropped.
    assert map_tools_to_cc(READ_TOOLS) == ["Glob", "Grep", "Read"]
    assert map_tools_to_cc(SUBAGENT_TOOLS) == [
        "Bash",
        "Edit",
        "Glob",
        "Grep",
        "Read",
        "WebFetch",
        "WebSearch",
        "Write",
    ]


def test_resolve_binary_prefers_env(monkeypatch, tmp_path):
    fake = tmp_path / "myclaude"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv(CLI_BINARY_ENV, str(fake))
    assert resolve_cli_binary() == str(fake)


def test_resolve_binary_none_when_missing(monkeypatch):
    monkeypatch.setenv(CLI_BINARY_ENV, "definitely-not-a-real-binary-xyz")
    assert resolve_cli_binary() is None


def test_synth_usage_maps_token_fields():
    u = synth_usage(
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 2,
            "cache_creation_input_tokens": 1,
        },
        num_turns=3,
    )
    assert isinstance(u, RunUsage)
    # input_tokens is made inclusive of cache (10 uncached + 2 read + 1 write = 13),
    # matching the harness/pydantic-ai convention; the cache buckets stay separate.
    assert u.input_tokens == 13 and u.output_tokens == 5
    assert u.cache_read_tokens == 2 and u.cache_write_tokens == 1
    assert u.requests == 3
    # total_tokens now includes cache (13 + 5), not just uncached + output.
    assert u.total_tokens == 18


def test_synth_usage_uncached_split_is_nonzero():
    """Regression: with cache folded in, split_tokens recovers the real uncached
    input (the ``↑`` value) instead of underflowing to 0."""
    from marim_harness.usage import split_tokens

    u = synth_usage(
        {
            "input_tokens": 35000,
            "output_tokens": 15000,
            "cache_read_input_tokens": 200000,
            "cache_creation_input_tokens": 48000,
        },
        num_turns=1,
    )
    s = split_tokens(u)
    assert s.uncached_input == 35000  # the genuine uncached prompt, not 0
    assert s.cache_read == 200000 and s.cache_write == 48000
    assert s.output == 15000


def test_synth_usage_captures_billed_cost():
    from marim_harness.usage import COST_DETAIL_KEY, exact_cost

    u = synth_usage({"input_tokens": 10, "output_tokens": 5}, num_turns=1, total_cost_usd=0.001)
    # Stored as integer micro-USD so exact_cost() returns the billed amount.
    assert u.details.get(COST_DETAIL_KEY) == 1000
    assert exact_cost(u) == pytest.approx(0.001, rel=1e-6)


def test_synth_usage_omits_cost_key_when_absent():
    from marim_harness.usage import COST_DETAIL_KEY

    u = synth_usage({"input_tokens": 5, "output_tokens": 2}, num_turns=1)
    assert COST_DETAIL_KEY not in u.details


def test_synth_usage_tolerates_none():
    u = synth_usage(None, num_turns=0)
    assert u.input_tokens == 0 and u.output_tokens == 0


def test_sum_result_usages_sums_tokens_and_keeps_last_cumulative_cost():
    r1 = {
        "num_turns": 2,
        "total_cost_usd": 0.04,
        "usage": {
            "input_tokens": 18,
            "output_tokens": 1083,
            "cache_read_input_tokens": 44348,
            "cache_creation_input_tokens": 10455,
            "cache_creation": {"ephemeral_1h_input_tokens": 10455},
        },
    }
    r2 = {
        "num_turns": 1,
        "total_cost_usd": 0.05,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 48,
            "cache_read_input_tokens": 28039,
            "cache_creation_input_tokens": 1942,
        },
    }
    summed, turns, cost = sum_result_usages([r1, r2])
    assert summed["input_tokens"] == 28 and summed["output_tokens"] == 1131
    assert summed["cache_read_input_tokens"] == 44348 + 28039
    assert "cache_creation" not in summed  # nested dicts skipped
    assert turns == 3
    assert cost == 0.05  # total_cost_usd is cumulative — last wins


def test_sum_result_usages_tolerates_missing_fields():
    summed, turns, cost = sum_result_usages([{"usage": None}, {}])
    assert summed == {} and turns == 0 and cost is None


def test_translate_assistant_text_emits_start_then_full_delta():
    t = CliStreamTranslator()
    events = t.translate(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Hello there"}]},
        }
    )
    assert isinstance(events[0], PartStartEvent)
    assert isinstance(events[1], PartDeltaEvent)
    assert isinstance(events[1].delta, TextPartDelta)
    assert events[1].delta.content_delta == "Hello there"
    # start and its delta share the same part index
    assert events[0].index == events[1].index


def test_translate_tool_use_normalizes_to_harness_name():
    # A Claude Code `Read` (file_path) is normalized to the harness `read_file`
    # (path) so the TUI renders it with the native read widget.
    t = CliStreamTranslator()
    events = t.translate(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Read",
                        "input": {"file_path": "x.py"},
                    },
                ]
            },
        }
    )
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, FunctionToolCallEvent)
    assert ev.part.tool_name == "read_file"
    assert ev.part.tool_call_id == "toolu_1"
    assert ev.part.args_as_dict() == {"path": "x.py"}


def test_translate_edit_maps_to_edit_file_with_edits_list():
    # The crux: a CLI `Edit` must become a harness `edit_file` carrying an
    # `edits` list of {old_string,new_string,replace_all} so the inline diff
    # renders instead of a raw-args dump.
    t = CliStreamTranslator()
    events = t.translate(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_2",
                        "name": "Edit",
                        "input": {
                            "file_path": "a.py",
                            "old_string": "foo",
                            "new_string": "bar",
                            "replace_all": True,
                        },
                    },
                ]
            },
        }
    )
    ev = events[0]
    assert ev.part.tool_name == "edit_file"
    args = ev.part.args_as_dict()
    assert args["path"] == "a.py"
    assert args["edits"] == [
        {"old_string": "foo", "new_string": "bar", "replace_all": True},
    ]


def test_translate_write_maps_to_write_file_path():
    t = CliStreamTranslator()
    events = t.translate(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_3",
                        "name": "Write",
                        "input": {"file_path": "b.py", "content": "x = 1"},
                    },
                ]
            },
        }
    )
    ev = events[0]
    assert ev.part.tool_name == "write_file"
    assert ev.part.args_as_dict() == {"path": "b.py", "content": "x = 1"}


def test_translate_unmapped_tool_passes_through():
    # A Claude Code tool with no harness equivalent keeps its name + args.
    t = CliStreamTranslator()
    events = t.translate(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_4",
                        "name": "TodoWrite",
                        "input": {"todos": []},
                    },
                ]
            },
        }
    )
    assert events[0].part.tool_name == "TodoWrite"


def test_translate_tool_result_labels_from_prior_call_and_marks_failure():
    t = CliStreamTranslator()
    t.translate(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_9",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    },
                ]
            },
        }
    )
    events = t.translate(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_9",
                        "content": [{"type": "text", "text": "boom"}],
                        "is_error": True,
                    },
                ]
            },
        }
    )
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, FunctionToolResultEvent)
    part = cast(ToolReturnPart, ev.part)
    # The result reuses the call's normalized harness name (Bash → bash) so the
    # result widget matches the call widget.
    assert part.tool_name == "bash"
    assert part.tool_call_id == "toolu_9"
    assert part.content == "boom"  # list-of-blocks flattened to text
    assert part.outcome == "failed"  # is_error → failed


def test_translate_ignores_system_and_result():
    t = CliStreamTranslator()
    assert t.translate({"type": "system", "subtype": "init"}) == []
    assert t.translate({"type": "result", "result": "done"}) == []


def test_translator_accumulates_transcript_messages():
    t = CliStreamTranslator()
    t.translate(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "reading"},
                    {
                        "type": "tool_use",
                        "id": "c1",
                        "name": "Read",
                        "input": {"file_path": "x.py"},
                    },
                ]
            },
        }
    )
    t.translate(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "c1", "content": "file body"},
                ]
            },
        }
    )
    msgs = t.transcript()
    assert isinstance(msgs[0], ModelResponse)
    # tool_use was normalized to the harness name + arg shape.
    call = [p for p in msgs[0].parts if isinstance(p, ToolCallPart)][0]
    assert call.tool_name == "read_file"
    assert call.args_as_dict() == {"path": "x.py"}
    assert isinstance(msgs[1], ModelRequest)
    ret = msgs[1].parts[0]
    assert isinstance(ret, ToolReturnPart)
    assert ret.tool_name == "read_file" and ret.content == "file body"


def test_translate_thinking_block_emits_thinking_events():
    t = CliStreamTranslator()
    events = t.translate(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "pondering...", "signature": "sig"},
                ]
            },
        }
    )
    assert isinstance(events[0], PartStartEvent)
    assert isinstance(events[0].part, ThinkingPart)
    assert isinstance(events[1], PartDeltaEvent)
    assert isinstance(events[1].delta, ThinkingPartDelta)
    assert events[1].delta.content_delta == "pondering..."
    # the transcript carries the thought too
    parts = t.transcript()[0].parts
    assert isinstance(parts[0], ThinkingPart) and parts[0].content == "pondering..."


def test_record_call_and_return_append_transcript_pair():
    t = CliStreamTranslator()
    t.record_call(
        ToolCallPart(tool_name="spawn_agent", args={"type": "Explore"}, tool_call_id="t1")
    )
    from datetime import datetime, timezone

    t.record_return(
        ToolReturnPart(
            tool_name="spawn_agent",
            content="4",
            tool_call_id="t1",
            timestamp=datetime.now(tz=timezone.utc),
            outcome="success",
        )
    )
    msgs = t.transcript()
    assert isinstance(msgs[0], ModelResponse)
    call = cast(ToolCallPart, msgs[0].parts[0])
    assert call.tool_name == "spawn_agent"
    assert isinstance(msgs[1], ModelRequest)
    ret = cast(ToolReturnPart, msgs[1].parts[0])
    assert ret.content == "4"


# ---------------------------------------------------------------------------
# ClaudeCliRunner — fake-binary integration tests
# ---------------------------------------------------------------------------


def _run(binary: str, tmp_path, runner: ClaudeCliRunner, **overrides):
    kwargs = dict(
        binary=binary,
        prompt="p",
        system_prompt="s",
        cwd=str(tmp_path),
        allowed_tools=["read_file"],
        model=None,
        stream_id="s1",
    )
    kwargs.update(overrides)
    return runner.run(**kwargs)


@pytest.mark.anyio
async def test_runner_streams_events_and_returns_result(tmp_path):
    binary = fake_claude_bin(
        tmp_path,
        {
            "turns": [
                [
                    {"text": "hi"},
                    {"tool_use": {"id": "t1", "name": "Read", "input": {"file_path": "/a"}}},
                    {"tool_result": {"id": "t1", "content": "x"}},
                    {"text": "Done"},
                    # The fake joins every `text` step into its auto result; pin
                    # the terminal result so output is read from the result object.
                    {"result": {"result": "Done"}},
                ]
            ]
        },
    )
    seen: list = []

    async def on_event(sid, ev, usage):
        seen.append((sid, type(ev).__name__))

    result = await _run(binary, tmp_path, ClaudeCliRunner(on_event, None))
    assert result.output == "Done" and result.usage.input_tokens == 7
    assert ("s1", "PartStartEvent") in seen and ("s1", "FunctionToolCallEvent") in seen


@pytest.mark.anyio
async def test_runner_surfaces_real_model_from_init_event(tmp_path):
    binary = fake_claude_bin(tmp_path, {"model": "claude-opus-4-1", "turns": [[{"text": "ok"}]]})
    models: list = []

    async def on_model(sid, m):
        models.append((sid, m))

    await _run(binary, tmp_path, ClaudeCliRunner(None, None, on_model))
    assert models[0] == ("s1", "claude-opus-4-1")


@pytest.mark.anyio
async def test_runner_skips_model_callback_without_stream_id(tmp_path):
    binary = fake_claude_bin(tmp_path, {"turns": [[{"text": "ok"}]]})
    models: list = []

    async def on_model(sid, m):
        models.append(m)

    await _run(binary, tmp_path, ClaudeCliRunner(None, None, on_model), stream_id=None)
    assert models == []


@pytest.mark.anyio
async def test_runner_raises_when_no_result(tmp_path):
    binary = fake_claude_bin(
        tmp_path, {"turns": [[{"text": "a"}, {"exit": {"code": 0, "stderr": ""}}]]}
    )
    with pytest.raises(CliRunError, match="no result"):
        await _run(binary, tmp_path, ClaudeCliRunner(None, None))


@pytest.mark.anyio
async def test_runner_kills_subprocess_when_event_callback_raises(tmp_path):
    binary = fake_claude_bin(tmp_path, {"turns": [[{"text": "a"}, {"sleep": 30}]]})

    async def boom(sid, ev, usage):
        raise RuntimeError("sink failed")

    runner = ClaudeCliRunner(boom, None)
    with pytest.raises(RuntimeError, match="sink failed"):
        await _run(binary, tmp_path, runner)
    # `run`'s finally closed the process (SIGTERM to its group): the pid the
    # fake recorded is gone from the process table.
    pid = int((tmp_path / "claude.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.anyio
async def test_runner_returns_transcript(tmp_path):
    binary = fake_claude_bin(
        tmp_path,
        {
            "turns": [
                [
                    {"tool_use": {"id": "t1", "name": "Read", "input": {"file_path": "/a"}}},
                    {"tool_result": {"id": "t1", "content": "x"}},
                    {"text": "Done"},
                    # The fake joins every `text` step into its auto result; pin
                    # the terminal result so output is read from the result object.
                    {"result": {"result": "Done"}},
                ]
            ]
        },
    )
    result = await _run(binary, tmp_path, ClaudeCliRunner(None, None))
    kinds = [type(p).__name__ for m in result.transcript for p in m.parts]
    assert kinds == ["ToolCallPart", "ToolReturnPart", "TextPart"]


# One recorded Claude turn that spawns a Claude-side Agent sub-agent: the
# parent's tool_use, the task_started/task_notification system objects, the
# child's own assistant message (tagged with parent_tool_use_id) and the
# turn's terminal result. Replayed verbatim through the fake's `raw` steps.
_AGENT_STREAM: list[dict] = [
    {"type": "system", "subtype": "init", "model": "claude-opus-4-8"},
    {
        "type": "assistant",
        "message": {
            "model": "claude-opus-4-8",
            "id": "msg_p1",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tsub",
                    "name": "Agent",
                    "input": {
                        "description": "Answer 2+2",
                        "subagent_type": "Explore",
                        "prompt": "What is 2+2?",
                    },
                }
            ],
        },
    },
    {
        "type": "system",
        "subtype": "task_started",
        "task_id": "af41",
        "tool_use_id": "tsub",
        "description": "Answer 2+2",
        "subagent_type": "Explore",
        "prompt": "What is 2+2?",
    },
    {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tsub",
                    "content": [{"type": "text", "text": "Async agent launched..."}],
                }
            ]
        },
    },
    {
        "type": "assistant",
        "parent_tool_use_id": "tsub",
        "message": {
            "model": "claude-haiku-4-5",
            "id": "msg_c1",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_creation_input_tokens": 10066,
            },
            "content": [{"type": "text", "text": "4"}],
        },
    },
    {
        "type": "system",
        "subtype": "task_updated",
        "task_id": "af41",
        "patch": {"status": "completed"},
    },
    {
        "type": "system",
        "subtype": "task_notification",
        "task_id": "af41",
        "tool_use_id": "tsub",
        "status": "completed",
        "summary": "4",
        "usage": {"total_tokens": 10086, "tool_uses": 0, "duration_ms": 2073},
    },
    {
        "type": "assistant",
        "message": {
            "model": "claude-opus-4-8",
            "id": "msg_p2",
            "content": [{"type": "text", "text": "Four."}],
        },
    },
    {
        "type": "result",
        "subtype": "success",
        "result": "Four.",
        "num_turns": 1,
        "total_cost_usd": 0.05,
        "usage": {"input_tokens": 10, "output_tokens": 48},
    },
]


def _agent_scenario() -> dict:
    """Replay `_AGENT_STREAM` through the fake: every object is a `raw` step in
    order, except the terminal `result`, which becomes the fake's own auto
    result (so its `result`/`usage`/`total_cost_usd` land on the real one), and
    `system/init`, which the fake emits itself from the scenario's model."""
    init = [o for o in _AGENT_STREAM if o.get("type") == "system" and o.get("subtype") == "init"][0]
    body = [o for o in _AGENT_STREAM if o is not init]
    final = body.pop()
    assert final["type"] == "result"
    steps: list[dict] = [{"raw": o} for o in body]
    steps.append({"result": {k: v for k, v in final.items() if k != "type"}})
    return {"model": init["model"], "turns": [steps]}


@pytest.mark.anyio
async def test_runner_demuxes_claude_side_subagents(tmp_path):
    binary = fake_claude_bin(tmp_path, _agent_scenario())
    events: list[tuple[str, object, object]] = []
    models: list[tuple[str, str]] = []

    async def on_event(stream_id, event, usage):
        events.append((stream_id, event, usage))

    async def on_model(stream_id, model):
        models.append((stream_id, model))

    runner = ClaudeCliRunner(on_event, None, on_model)
    result = await runner.run(
        binary=binary,
        prompt="t",
        system_prompt="s",
        cwd=str(tmp_path),
        allowed_tools=[],
        model=None,
        stream_id="parent",
    )
    # The report is the turn's terminal result — one per turn now that the turn
    # runs on a live process (multi-result folding stays covered by the
    # sum_result_usages unit tests above).
    assert result.output == "Four."
    assert result.usage.output_tokens == 48
    from marim_harness.usage import COST_DETAIL_KEY

    assert result.usage.details[COST_DETAIL_KEY] == 50_000  # $0.05 in micro-USD

    # The spawn surfaced on the PARENT stream as a spawn_agent call…
    spawn_calls = [
        (sid, ev)
        for sid, ev, _ in events
        if isinstance(ev, FunctionToolCallEvent) and ev.part.tool_name == "spawn_agent"
    ]
    assert spawn_calls and spawn_calls[0][0] == "parent"
    assert spawn_calls[0][1].part.tool_call_id == "tsub"
    # …child text streamed on the CHILD stream, carrying live usage…
    child_events = [(sid, ev, u) for sid, ev, u in events if sid == "tsub"]
    assert child_events
    assert any(u is not None and u.output_tokens == 5 for _, _, u in child_events)
    # …and the notification settled the card with the summary.
    finishes = [
        ev
        for sid, ev, _ in events
        if sid == "parent"
        and isinstance(ev, FunctionToolResultEvent)
        and ev.part.tool_name == "spawn_agent"
    ]
    assert finishes and finishes[0].part.content == "4"

    # The child's model reached the card; the run's own model still surfaced.
    assert ("tsub", "claude-haiku-4-5") in models
    assert any(m == "claude-opus-4-8" for _, m in models)

    # Parent transcript pairs the synthesized spawn call with its return;
    # the child transcript is captured for sidecar persistence.
    parent_parts = [p for m in result.transcript for p in m.parts]
    assert any(
        getattr(p, "tool_name", "") == "spawn_agent" and isinstance(p, ToolCallPart)
        for p in parent_parts
    )
    assert any(
        getattr(p, "tool_name", "") == "spawn_agent" and isinstance(p, ToolReturnPart)
        for p in parent_parts
    )
    assert "tsub" in result.child_transcripts
