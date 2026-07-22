import pytest

from marim_harness.lsp.provider import (
    LspProviderError,
    LspRegistry,
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


def _prov(language, exts, *, command=None, backend=None, probe=None,
          diagnostics="lsp", source="global"):
    block = {"language": language, "extensions": exts, "diagnostics": diagnostics}
    if command:
        block["command"] = command
    if backend:
        block["backend"] = backend
    if probe is not None:
        block["probe"] = probe
    (p,) = parse_lsp_providers(
        block, bundled=backend is not None, source=source,
        plugin_root=None, strict=True,
    )
    return p


def test_registry_language_for():
    reg = LspRegistry([_prov("go", [".go"], command="gopls")])
    assert reg.language_for("main.go") == "go"
    assert reg.language_for("main.py") is None
    assert reg.language_for("src.v2/Makefile") is None  # dotted dir, no ext


def test_registry_availability_probe_present(monkeypatch):
    reg = LspRegistry([_prov("go", [".go"], command="gopls", probe=["gopls"])])
    monkeypatch.setattr(
        "marim_harness.lsp.registry.shutil.which", lambda b: "/usr/bin/gopls"
    )
    assert reg.availability("go").available is True


def test_registry_availability_probe_missing():
    reg = LspRegistry(
        [_prov("go", [".go"], command="gopls", probe=["definitely-not-on-path-xyz"])]
    )
    a = reg.availability("go")
    assert a.available is False


def test_registry_empty_probe_always_available():
    reg = LspRegistry([_prov("java", [".java"], backend="multilspy:java", probe=[])])
    assert reg.availability("java").available is True


def test_registry_unknown_language():
    reg = LspRegistry([])
    assert reg.availability("nope").available is False


def test_registry_provider_for():
    reg = LspRegistry([_prov("go", [".go"], command="gopls")])
    assert reg.provider_for("go").command == "gopls"
    assert reg.provider_for("py") is None


def test_registry_workspace_languages(tmp_path):
    (tmp_path / "a.go").write_text("package main")
    (tmp_path / "b.go").write_text("package main")
    reg = LspRegistry([_prov("go", [".go"], command="gopls")])
    assert reg.workspace_languages(tmp_path) == {"go"}


def test_registry_last_provider_wins_on_extension_conflict():
    # A later provider (e.g. project plugin) overriding the same extension wins.
    reg = LspRegistry([
        _prov("python", [".py"], backend="basedpyright", source="bundled"),
        _prov("python2", [".py"], command="custom-py-ls", source="project"),
    ])
    assert reg.language_for("x.py") == "python2"
