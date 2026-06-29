import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart

from marim_harness.mcp.discovered_instructions_capability import (
    DiscoveredInstructionsCapability,
    _injected_servers,
    _instruction_messages,
)


class _FakeMcp:
    """Stand-in for McpManager: returns pairs whose server prefix appears in `discovered`."""
    def __init__(self, pairs):
        self._pairs = pairs

    def discovered_server_instructions(self, discovered):
        return [(s, t) for (s, t) in self._pairs
                if any(d.startswith(s + "_") for d in discovered)]


class _Ctx:
    def __init__(self, discovered):
        self.discovered_tool_names = set(discovered)


class _ReqCtx:
    def __init__(self, messages):
        self.messages = messages


def _base():
    return _ReqCtx([ModelRequest(parts=[UserPromptPart("hi")])])


@pytest.mark.anyio
async def test_no_discovery_is_noop():
    cap = DiscoveredInstructionsCapability(_FakeMcp([("mddocs", "guide")]))
    out = await cap.before_model_request(_Ctx(set()), _base())
    assert len(out.messages) == 1


@pytest.mark.anyio
async def test_injects_one_well_formed_pair():
    cap = DiscoveredInstructionsCapability(_FakeMcp([("mddocs", "Search first.")]))
    out = await cap.before_model_request(_Ctx({"mddocs_search_docs"}), _base())
    assert len(out.messages) == 3
    assert isinstance(out.messages[-1], ModelRequest)            # ends in ModelRequest
    env = out.messages[-1].parts[0].content
    assert "mddocs" in env and "Search first." in env
    kinds = {p.part_kind for m in out.messages for p in m.parts}
    assert kinds <= {"text", "user-prompt"}                      # no tool-call/return parts


@pytest.mark.anyio
async def test_idempotent_when_marker_present():
    cap = DiscoveredInstructionsCapability(_FakeMcp([("mddocs", "Search first.")]))
    out = await cap.before_model_request(_Ctx({"mddocs_x"}), _base())
    n = len(out.messages)
    out2 = await cap.before_model_request(_Ctx({"mddocs_x"}), out)  # marker already in history
    assert len(out2.messages) == n


@pytest.mark.anyio
async def test_self_heals_after_marker_removed():
    cap = DiscoveredInstructionsCapability(_FakeMcp([("mddocs", "Search first.")]))
    out = await cap.before_model_request(_Ctx({"mddocs_x"}), _base())
    out.messages = [ModelRequest(parts=[UserPromptPart("hi")])]     # simulate compaction
    out3 = await cap.before_model_request(_Ctx({"mddocs_x"}), out)
    assert len(out3.messages) == 3                                  # re-injected


@pytest.mark.anyio
async def test_only_uninjected_servers_added():
    cap = DiscoveredInstructionsCapability(_FakeMcp([("mddocs", "g"), ("nasa", "h")]))
    seeded = _ReqCtx(_instruction_messages("mddocs", "g") +
                     [ModelRequest(parts=[UserPromptPart("hi")])])
    out = await cap.before_model_request(_Ctx({"mddocs_x", "nasa_y"}), seeded)
    assert _injected_servers(out.messages) == {"mddocs", "nasa"}    # nasa added, mddocs not duped
    assert sum(1 for m in out.messages
               for p in m.parts if "«mcp-guidance:mddocs»" in getattr(p, "content", "")) == 1


def test_injected_servers_scan():
    msgs = _instruction_messages("mddocs", "g") + _instruction_messages("nasa", "h")
    assert _injected_servers(msgs) == {"mddocs", "nasa"}


@pytest.mark.anyio
async def test_cap_applied_to_long_instructions():
    """Instructions exceeding 2000 chars are truncated before injection."""
    long_text = "y" * 2100
    cap = DiscoveredInstructionsCapability(_FakeMcp([("bigserver", long_text)]))
    out = await cap.before_model_request(_Ctx({"bigserver_tool"}), _base())
    injected: str = out.messages[-1].parts[0].content
    assert "…(truncated)" in injected
    # The body portion (excluding the envelope label line) should not exceed the cap
    # by more than the truncation marker itself.
    assert injected.count("y") <= 2000


@pytest.mark.anyio
async def test_cap_not_applied_to_short_instructions():
    """Instructions within 2000 chars are injected verbatim (no truncation marker)."""
    short_text = "z" * 500
    cap = DiscoveredInstructionsCapability(_FakeMcp([("smallserver", short_text)]))
    out = await cap.before_model_request(_Ctx({"smallserver_tool"}), _base())
    injected: str = out.messages[-1].parts[0].content
    assert "truncated" not in injected
    assert short_text in injected
