"""Round-trip test: ToolSearchCallPart / ToolSearchReturnPart survive session persistence.

Pydantic AI's tool-search feature introduces two new message-history part types.
This test verifies they survive a save→load cycle through marim's real SessionStore
so that resuming a session mid-tool-search doesn't drop or corrupt the history.
"""

from pathlib import Path

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolSearchCallPart,
    ToolSearchReturnPart,
)
from pydantic_ai.usage import RunUsage

from marim_harness.session import SessionManager


def _manager(tmp_path: Path) -> SessionManager:
    return SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")


def test_tool_search_parts_round_trip(tmp_path: Path) -> None:
    """ToolSearchCallPart and ToolSearchReturnPart must survive a save→load cycle
    through the real SessionStore so that resuming a session mid-tool-search
    doesn't drop or corrupt the history."""
    call_id = "ts-abc123"
    call_part = ToolSearchCallPart(
        args={"queries": ["email"]},
        tool_call_id=call_id,
    )
    return_part = ToolSearchReturnPart(
        content={"discovered_tools": [{"name": "send_email", "description": "Send an email"}]},
        tool_call_id=call_id,
    )
    history = [
        ModelResponse(parts=[call_part]),
        ModelRequest(parts=[return_part]),
    ]

    mgr = _manager(tmp_path)
    store = mgr.create("tool-search-rt")
    store.save(history, RunUsage())

    messages, _, _, _ = mgr.store(store.session_id).load()

    all_parts = [p for msg in messages for p in getattr(msg, "parts", [])]
    calls = [p for p in all_parts if isinstance(p, ToolSearchCallPart)]
    returns = [p for p in all_parts if isinstance(p, ToolSearchReturnPart)]

    assert calls, "ToolSearchCallPart did not survive persistence"
    assert returns, "ToolSearchReturnPart did not survive persistence"

    c = calls[0]
    assert c.tool_name == "search_tools"
    assert c.tool_kind == "tool-search"
    assert c.tool_call_id == call_id

    r = returns[0]
    assert r.tool_name == "search_tools"
    assert r.tool_kind == "tool-search"
    assert r.tool_call_id == call_id
    assert r.content["discovered_tools"][0]["name"] == "send_email"
