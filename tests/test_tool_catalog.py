import pytest

from marim_harness.mcp.catalog import (
    _CATALOG_PER_SERVER_CAP,
    render_tool_catalog,
    tool_catalog_text,
)


def test_empty_groups_render_to_empty_string():
    assert render_tool_catalog({}) == ""


def test_renders_servers_sorted_with_names():
    out = render_tool_catalog({"zeta": ["z_b", "z_a"], "alpha": ["a_one"]})
    lines = out.splitlines()
    # preamble first, then servers alphabetically
    assert "search_tools" in lines[0]
    assert lines[1] == "- alpha: a_one"
    # names within a server are rendered in the order given (caller pre-sorts)
    assert lines[2] == "- zeta: z_b, z_a"


def test_per_server_cap_truncates_with_more_suffix():
    names = [f"t{i:02d}" for i in range(_CATALOG_PER_SERVER_CAP + 5)]  # 17 names
    out = render_tool_catalog({"big": names})
    row = [ln for ln in out.splitlines() if ln.startswith("- big:")][0]
    assert "(+5 more)" in row
    assert "t00" in row and "t11" in row  # first 12 shown (t00..t11)
    assert "t12" not in row               # capped


def test_no_more_suffix_when_under_cap():
    out = render_tool_catalog({"small": ["a", "b"]})
    assert "more)" not in out
    assert "- small: a, b" in out


class _FakeMcp:
    def __init__(self, groups):
        self._groups = groups

    async def live_tools_by_server(self):
        return self._groups


@pytest.mark.anyio
async def test_catalog_text_shown_when_policy_on():
    mcp = _FakeMcp({"mddocs": ["mddocs_doc_index", "mddocs_grep_docs"]})
    text = await tool_catalog_text(mcp, "on", 15)
    assert "mddocs_doc_index" in text
    assert "search_tools" in text


@pytest.mark.anyio
async def test_catalog_text_empty_when_off():
    mcp = _FakeMcp({"mddocs": ["mddocs_doc_index"]})
    assert await tool_catalog_text(mcp, "off", 15) == ""


@pytest.mark.anyio
async def test_catalog_text_empty_when_auto_below_threshold():
    mcp = _FakeMcp({"mddocs": ["a", "b", "c"]})  # 3 tools
    assert await tool_catalog_text(mcp, "auto", 15) == ""  # 3 <= 15 -> not deferred


@pytest.mark.anyio
async def test_catalog_text_shown_when_auto_above_threshold():
    mcp = _FakeMcp({"s": [f"t{i}" for i in range(20)]})  # 20 tools
    text = await tool_catalog_text(mcp, "auto", 15)  # 20 > 15 -> deferred
    assert text.startswith("Additional MCP tools")



