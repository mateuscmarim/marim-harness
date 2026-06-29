from marim_harness.mcp.catalog import _CATALOG_PER_SERVER_CAP, render_tool_catalog


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
