from pydantic_ai.usage import RunUsage

from marim_harness.subagents_cli import (
    CLI_BINARY_ENV,
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
