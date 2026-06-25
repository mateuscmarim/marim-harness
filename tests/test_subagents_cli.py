from typing import cast

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPartDelta,
    ToolReturnPart,
)
from pydantic_ai.usage import RunUsage

from marim_harness.subagents_cli import (
    CLI_BINARY_ENV,
    CliStreamTranslator,
    build_cli_argv,
    cli_permission_mode,
    map_tools_to_cc,
    resolve_cli_binary,
    synth_usage,
)
from marim_harness.tools.names import READ_TOOLS, SUBAGENT_TOOLS


def test_permission_mode_maps_to_auto_and_plan():
    assert cli_permission_mode(True) == "acceptEdits"
    assert cli_permission_mode(False) == "plan"


def test_tool_map_drops_unmapped_and_sorts():
    # READ_TOOLS = read_file, glob, tree, grep + LSP tools. Only read_file/glob/grep
    # map; tree and LSP names are dropped.
    assert map_tools_to_cc(READ_TOOLS) == ["Glob", "Grep", "Read"]
    assert map_tools_to_cc(SUBAGENT_TOOLS) == [
        "Bash", "Edit", "Glob", "Grep", "Read", "WebFetch", "WebSearch", "Write",
    ]


def test_build_argv_includes_required_flags():
    argv = build_cli_argv(
        "/usr/bin/claude", "do the task", "You are a worker.",
        "acceptEdits", ["Read", "Edit"], "opus",
    )
    assert argv[:3] == ["/usr/bin/claude", "-p", "do the task"]
    assert "--output-format" in argv and "stream-json" in argv and "--verbose" in argv
    assert argv[argv.index("--append-system-prompt") + 1] == "You are a worker."
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--allowedTools") + 1] == "Read,Edit"
    assert argv[argv.index("--model") + 1] == "opus"


def test_build_argv_omits_model_and_tools_when_absent():
    argv = build_cli_argv("claude", "t", "s", "plan", [], None)
    assert "--model" not in argv
    assert "--allowedTools" not in argv


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
        {"input_tokens": 10, "output_tokens": 5,
         "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1},
        num_turns=3,
    )
    assert isinstance(u, RunUsage)
    assert u.input_tokens == 10 and u.output_tokens == 5
    assert u.cache_read_tokens == 2 and u.cache_write_tokens == 1
    assert u.requests == 3


def test_synth_usage_tolerates_none():
    u = synth_usage(None, num_turns=0)
    assert u.input_tokens == 0 and u.output_tokens == 0


def test_translate_assistant_text_emits_start_then_full_delta():
    t = CliStreamTranslator()
    events = t.translate({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "Hello there"}]},
    })
    assert isinstance(events[0], PartStartEvent)
    assert isinstance(events[1], PartDeltaEvent)
    assert isinstance(events[1].delta, TextPartDelta)
    assert events[1].delta.content_delta == "Hello there"
    # start and its delta share the same part index
    assert events[0].index == events[1].index


def test_translate_tool_use_emits_call_event():
    t = CliStreamTranslator()
    events = t.translate({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"path": "x.py"}},
        ]},
    })
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, FunctionToolCallEvent)
    assert ev.part.tool_name == "Read"
    assert ev.part.tool_call_id == "toolu_1"
    assert ev.part.args_as_dict() == {"path": "x.py"}


def test_translate_tool_result_labels_from_prior_call_and_marks_failure():
    t = CliStreamTranslator()
    t.translate({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": "toolu_9", "name": "Bash", "input": {"command": "ls"}},
        ]},
    })
    events = t.translate({
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_9",
             "content": [{"type": "text", "text": "boom"}], "is_error": True},
        ]},
    })
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, FunctionToolResultEvent)
    part = cast(ToolReturnPart, ev.part)
    assert part.tool_name == "Bash"          # carried from the matching call
    assert part.tool_call_id == "toolu_9"
    assert part.content == "boom"            # list-of-blocks flattened to text
    assert part.outcome == "failed"          # is_error → failed


def test_translate_ignores_system_and_result():
    t = CliStreamTranslator()
    assert t.translate({"type": "system", "subtype": "init"}) == []
    assert t.translate({"type": "result", "result": "done"}) == []
