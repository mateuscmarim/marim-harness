"""Tests for fuzzy 'did you mean' suggestions on unknown tool-name calls."""

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    UserPromptPart,
)

from marim_harness.tools.suggest import (
    nearest_tool_name,
    suggest_unknown_tool_retry,
)


def _retry_content(messages: list[ModelMessage], index: int = 0) -> str:
    """Pull the text content out of the first RetryPromptPart of ``messages[index]``."""
    part = messages[index].parts[0]
    assert isinstance(part, RetryPromptPart)
    assert isinstance(part.content, str)
    return part.content

# The available tool list from the real incident, abbreviated.
_AVAILABLE = [
    "activate_skill",
    "agentmemory_memory_consolidate",
    "agentmemory_memory_recall",
    "agentmemory_memory_save",
    "agentmemory_memory_smart_search",
    "ask_user",
    "bash",
    "read_file",
    "write_file",
]


def _unknown_msg(unknown: str, available: list[str]) -> str:
    """Reproduce Pydantic AI's ToolManager rejection string verbatim."""
    listing = ", ".join(f"{n!r}" for n in available)
    return f"Unknown tool name: {unknown!r}. Available tools: {listing}"


def _retry_request(content: str) -> ModelRequest:
    return ModelRequest(parts=[RetryPromptPart(content=content, tool_name=None)])


class TestNearestToolName:
    def test_recovers_the_motivating_case(self):
        # The exact garble from the incident.
        assert (
            nearest_tool_name("agents_memory_smart_search", _AVAILABLE)
            == "agentmemory_memory_smart_search"
        )

    def test_underscore_insensitive(self):
        assert nearest_tool_name("readfile", ["read_file", "write_file"]) == "read_file"

    def test_returns_none_when_nothing_close(self):
        assert nearest_tool_name("xyzzy", _AVAILABLE) is None

    def test_returns_none_for_empty_available(self):
        assert nearest_tool_name("read_file", []) is None

    def test_ties_keep_first_in_order(self):
        # Two equally-distant candidates (both share no letters): the earlier
        # one wins. min_ratio=0 so the match isn't rejected for being weak.
        assert nearest_tool_name("ab", ["xy", "zw"], min_ratio=0.0) == "xy"

    def test_threshold_is_respected(self):
        # A high threshold rejects a merely-okay match.
        assert nearest_tool_name("read", ["read_file"], min_ratio=0.99) is None


class TestSuggestUnknownToolRetry:
    def test_appends_hint_to_unknown_tool_rejection(self):
        msg = _unknown_msg("agents_memory_smart_search", _AVAILABLE)
        out = suggest_unknown_tool_retry([_retry_request(msg)])
        content = _retry_content(out)
        assert content.startswith(msg)
        assert "Did you mean 'agentmemory_memory_smart_search'?" in content

    def test_is_idempotent(self):
        msg = _unknown_msg("agents_memory_smart_search", _AVAILABLE)
        once = suggest_unknown_tool_retry([_retry_request(msg)])
        twice = suggest_unknown_tool_retry(once)
        # Hint appears exactly once, not stacked.
        assert _retry_content(twice).count("Did you mean") == 1

    def test_does_not_mutate_input_objects(self):
        msg = _unknown_msg("agents_memory_smart_search", _AVAILABLE)
        original = _retry_request(msg)
        suggest_unknown_tool_retry([original])
        # The caller's part is untouched; a copy was returned instead.
        assert _retry_content([original]) == msg

    def test_no_suggestion_when_no_close_match(self):
        msg = _unknown_msg("totally_unrelated_zzz", ["bash", "read_file"])
        out = suggest_unknown_tool_retry([_retry_request(msg)])
        assert _retry_content(out) == msg

    def test_ignores_non_rejection_retry_parts(self):
        # A validation-feedback retry (no "Unknown tool name:") is left alone.
        msg = "Validation feedback: argument 'path' is required"
        out = suggest_unknown_tool_retry([_retry_request(msg)])
        assert _retry_content(out) == msg

    def test_only_inspects_trailing_request(self):
        # A rejection buried earlier in history is not the pending one; ignore it.
        old = _retry_request(_unknown_msg("agents_memory_smart_search", _AVAILABLE))
        resp = ModelResponse(parts=[TextPart(content="ok")])
        tail = ModelRequest(parts=[UserPromptPart(content="next")])
        out = suggest_unknown_tool_retry([old, resp, tail])
        assert "Did you mean" not in _retry_content(out)

    def test_empty_history_is_noop(self):
        assert suggest_unknown_tool_retry([]) == []
