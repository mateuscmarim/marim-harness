from marim_harness.hooks import events


def test_event_constants_match_claude_code_names():
    assert events.SESSION_START == "SessionStart"
    assert events.USER_PROMPT_SUBMIT == "UserPromptSubmit"
    assert events.PRE_TOOL_USE == "PreToolUse"
    assert events.POST_TOOL_USE == "PostToolUse"
    assert events.PRE_COMPACT == "PreCompact"
    assert events.SUBAGENT_START == "SubagentStart"
    assert events.SUBAGENT_STOP == "SubagentStop"
    assert events.STOP == "Stop"
    assert events.SESSION_END == "SessionEnd"


def test_only_session_start_and_user_prompt_inject():
    assert events.INJECTING_EVENTS == frozenset(
        {events.SESSION_START, events.USER_PROMPT_SUBMIT}
    )
