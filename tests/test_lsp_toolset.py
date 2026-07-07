from marim_harness.tools.lsp_tools import build_lsp_toolset

_EXPECTED = {
    "goto_definition", "find_references", "hover",
    "document_symbols", "workspace_symbols", "diagnostics",
}


def test_build_lsp_toolset_has_the_six_tools():
    ts = build_lsp_toolset()
    assert set(ts.tools) == _EXPECTED


def test_lsp_tools_are_ungated():
    ts = build_lsp_toolset()
    for name in _EXPECTED:
        assert ts.tools[name].requires_approval is not True
