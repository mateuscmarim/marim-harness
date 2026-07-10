"""SessionStore.load runs pydantic-ai's sanitize_messages as an inbound
hardening layer: the session file is marim's own, but it sits on disk where
anything can rewrite it between runs. The layer must strip what could make the
next request dangerous (non-HTTP FileUrls the provider would fetch
server-side) while NOT touching what marim's own resumability machinery owns:
trailing unanswered tool calls (repaired, not discarded, at the next turn's
start) and marim's own system prompt parts."""

import logging
from pathlib import Path

from pydantic_ai.messages import (
    ImageUrl,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.usage import RunUsage

from marim_harness.session import SessionManager


def _manager(tmp_path: Path) -> SessionManager:
    return SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")


def _round_trip(tmp_path: Path, history: list) -> list:
    mgr = _manager(tmp_path)
    store = mgr.create("sanitize")
    store.save(history, RunUsage())
    messages, _, _, _, _ = mgr.store(store.session_id).load()
    return messages


def test_trailing_unanswered_tool_call_survives_load(tmp_path: Path):
    """sanitize_messages' default strips dangling tail tool calls; marim must
    NOT — the partial work they carry is preserved by synthesizing a return
    (_repair_unanswered_tool_calls) at the next turn's start, which needs to
    still see the call. The store passes every tool-call id as resolved to
    disable exactly that strip."""
    history = [
        ModelRequest(parts=[UserPromptPart(content="go")]),
        ModelResponse(
            parts=[
                TextPart(content="working on it"),
                ToolCallPart(tool_name="edit_file", args="{}", tool_call_id="tc1"),
            ]
        ),
    ]
    messages = _round_trip(tmp_path, history)
    assert len(messages) == 2
    calls = [p for p in messages[-1].parts if isinstance(p, ToolCallPart)]
    assert [c.tool_call_id for c in calls] == ["tc1"]  # kept for the repair


def test_non_http_file_url_is_stripped_and_logged(tmp_path: Path, caplog):
    """A tampered session file smuggling an s3:// URL into history would make
    the model provider fetch the object server-side under its own credentials;
    load drops it and logs (never warns to stderr — that paints over the TUI)."""
    history = [
        ModelRequest(
            parts=[UserPromptPart(content=["look at this", ImageUrl(url="s3://bucket/x.png")])]
        ),
        ModelResponse(parts=[TextPart(content="ok")]),
    ]
    with caplog.at_level(logging.WARNING, logger="marim_harness.session.store"):
        messages = _round_trip(tmp_path, history)
    assert messages[0].parts[0].content == ["look at this"]  # URL part dropped
    assert any("s3" in r.message for r in caplog.records)


def test_https_file_url_survives_load(tmp_path: Path):
    history = [
        ModelRequest(
            parts=[UserPromptPart(content=["see", ImageUrl(url="https://example.com/x.png")])]
        ),
        ModelResponse(parts=[TextPart(content="ok")]),
    ]
    messages = _round_trip(tmp_path, history)
    (kept_url,) = [c for c in messages[0].parts[0].content if isinstance(c, ImageUrl)]
    assert kept_url.url == "https://example.com/x.png"


def test_system_prompt_part_survives_load(tmp_path: Path, caplog):
    """strip_system_prompts is deliberately off: a system part in a saved
    transcript is marim's own, and stripping it would silently mutate a
    resumed conversation (and warn on every load)."""
    history = [
        ModelRequest(
            parts=[SystemPromptPart(content="be helpful"), UserPromptPart(content="hi")]
        ),
        ModelResponse(parts=[TextPart(content="hello")]),
    ]
    with caplog.at_level(logging.WARNING, logger="marim_harness.session.store"):
        messages = _round_trip(tmp_path, history)
    assert any(isinstance(p, SystemPromptPart) for p in messages[0].parts)
    assert not caplog.records  # a clean transcript loads silently
