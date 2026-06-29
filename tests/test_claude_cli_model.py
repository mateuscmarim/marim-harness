
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)

from marim_harness.config.claude_cli_model import (
    extract_system,
    flatten_history,
    latest_user_text,
    permission_mode_for,
    request_usage_from_cli,
)


def test_permission_mode_mapping():
    assert permission_mode_for("auto") == "acceptEdits"
    assert permission_mode_for("ask") == "acceptEdits"
    assert permission_mode_for("plan") == "plan"
    # Unknown falls back to the safe read-only plan mode.
    assert permission_mode_for("weird") == "plan"


def test_latest_user_text_takes_newest_request():
    msgs = [
        ModelRequest(parts=[UserPromptPart(content="first")]),
        ModelResponse(parts=[TextPart(content="answer one")]),
        ModelRequest(parts=[UserPromptPart(content="second")]),
    ]
    assert latest_user_text(msgs) == "second"


def test_latest_user_text_joins_list_content():
    msgs = [ModelRequest(parts=[UserPromptPart(content=["a", "b"])])]
    assert latest_user_text(msgs) == "a\nb"


def test_extract_system_prefers_instructions_then_system_parts():
    msgs = [
        ModelRequest(
            parts=[SystemPromptPart(content="sys-part")],
            instructions="the-instructions",
        ),
    ]
    assert extract_system(msgs) == "the-instructions"
    msgs2 = [ModelRequest(parts=[SystemPromptPart(content="sys-only")])]
    assert extract_system(msgs2) == "sys-only"


def test_flatten_history_labels_roles():
    msgs = [
        ModelRequest(parts=[UserPromptPart(content="hello")]),
        ModelResponse(parts=[TextPart(content="hi there")]),
        ModelRequest(parts=[UserPromptPart(content="more")]),
    ]
    out = flatten_history(msgs)
    assert "User: hello" in out
    assert "Assistant: hi there" in out
    assert out.rstrip().endswith("User: more")


def test_request_usage_folds_cache_and_cost():
    u = request_usage_from_cli(
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 7,
        },
        total_cost_usd=0.25,
    )
    assert u.input_tokens == 117  # 10 + 100 + 7, inclusive of cache
    assert u.output_tokens == 5
    assert u.cache_read_tokens == 100
    assert u.cache_write_tokens == 7
    from marim_harness.usage import COST_DETAIL_KEY

    assert u.details[COST_DETAIL_KEY] == 250_000
