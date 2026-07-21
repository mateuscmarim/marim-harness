import pytest

from marim_harness.lsp.provider import (
    LspProviderError,
    parse_lsp_providers,
)


def test_parse_declarative_single():
    block = {
        "language": "go",
        "extensions": [".go"],
        "command": "gopls",
        "args": [],
        "rootMarkers": ["go.mod"],
        "env": {"GOFLAGS": "-mod=mod"},
        "probe": ["gopls"],
        "installHint": "install gopls",
    }
    (p,) = parse_lsp_providers(
        block, bundled=False, source="global", plugin_root=None, strict=True
    )
    assert p.language == "go"
    assert p.extensions == (".go",)
    assert p.command == "gopls"
    assert p.root_markers == ("go.mod",)
    assert p.env == (("GOFLAGS", "-mod=mod"),)
    assert p.probe == ("gopls",)
    assert p.diagnostics == "lsp"
    assert p.backend is None
    assert p.source == "global"


def test_probe_defaults_to_command():
    block = {"language": "go", "extensions": [".go"], "command": "gopls"}
    (p,) = parse_lsp_providers(
        block, bundled=False, source="global", plugin_root=None, strict=True
    )
    assert p.probe == ("gopls",)


def test_list_form():
    block = [
        {"language": "go", "extensions": [".go"], "command": "gopls"},
        {"language": "zig", "extensions": [".zig"], "command": "zls"},
    ]
    ps = parse_lsp_providers(
        block, bundled=False, source="global", plugin_root=None, strict=True
    )
    assert [p.language for p in ps] == ["go", "zig"]


def test_backend_rejected_for_third_party_strict():
    block = {"language": "python", "extensions": [".py"], "backend": "basedpyright"}
    with pytest.raises(LspProviderError):
        parse_lsp_providers(
            block, bundled=False, source="global", plugin_root=None, strict=True
        )


def test_backend_ignored_for_third_party_lenient():
    block = {"language": "python", "extensions": [".py"], "backend": "basedpyright"}
    assert parse_lsp_providers(
        block, bundled=False, source="global", plugin_root=None, strict=False
    ) == []


def test_backend_allowed_for_bundled():
    block = {
        "language": "python",
        "extensions": [".py"],
        "backend": "basedpyright",
        "diagnostics": "python-checks",
        "probe": ["basedpyright-langserver", "jedi-language-server"],
        "installHint": "install basedpyright",
    }
    (p,) = parse_lsp_providers(
        block, bundled=True, source="bundled", plugin_root=None, strict=True
    )
    assert p.backend == "basedpyright"
    assert p.diagnostics == "python-checks"


def test_command_and_backend_mutually_exclusive():
    block = {
        "language": "python",
        "extensions": [".py"],
        "backend": "basedpyright",
        "command": "basedpyright-langserver",
    }
    with pytest.raises(LspProviderError):
        parse_lsp_providers(
            block, bundled=True, source="bundled", plugin_root=None, strict=True
        )


def test_missing_language_or_extensions_rejected_strict():
    with pytest.raises(LspProviderError):
        parse_lsp_providers(
            {"extensions": [".go"], "command": "gopls"},
            bundled=False, source="global", plugin_root=None, strict=True,
        )
    with pytest.raises(LspProviderError):
        parse_lsp_providers(
            {"language": "go", "command": "gopls"},
            bundled=False, source="global", plugin_root=None, strict=True,
        )


def test_no_launch_rejected_strict():
    # Neither command nor backend: nothing to launch.
    with pytest.raises(LspProviderError):
        parse_lsp_providers(
            {"language": "go", "extensions": [".go"]},
            bundled=False, source="global", plugin_root=None, strict=True,
        )


def test_extension_normalized_to_lowercase_with_dot():
    block = {"language": "go", "extensions": ["GO", ".Go"], "command": "gopls"}
    (p,) = parse_lsp_providers(
        block, bundled=False, source="global", plugin_root=None, strict=True
    )
    assert p.extensions == (".go", ".go")
