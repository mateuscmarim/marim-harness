from marim_harness.subagents.cli_backend import build_cli_argv


def test_build_cli_argv_resume_and_no_system():
    argv = build_cli_argv(
        "claude",
        "do the thing",
        "SYSTEM",
        "acceptEdits",
        [],
        None,
        resume_session_id="sess-123",
        append_system=False,
    )
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "sess-123"
    assert "--append-system-prompt" not in argv


def test_build_cli_argv_defaults_unchanged():
    # Existing sub-agent call shape must still include the system prompt and no resume.
    argv = build_cli_argv("claude", "task", "SYSTEM", "plan", ["Read"], "sonnet")
    assert "--append-system-prompt" in argv
    assert "--resume" not in argv
    assert argv[argv.index("--model") + 1] == "sonnet"
    # Safe mode is opt-in; default callers (sub-agents) keep their full environment.
    assert "--safe-mode" not in argv


def test_build_cli_argv_safe_mode():
    # The main-loop model runs claude in safe mode so the user's plugins/hooks
    # (e.g. agentmemory's cross-session context injection) don't pollute the turn.
    argv = build_cli_argv("claude", "task", "SYSTEM", "plan", [], None, safe_mode=True)
    assert "--safe-mode" in argv
