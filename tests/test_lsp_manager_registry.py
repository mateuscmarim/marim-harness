# tests/test_lsp_manager_registry.py
import pytest

from marim_harness.lsp.manager import LspManager
from marim_harness.lsp.provider import LspRegistry, parse_lsp_providers


def _reg(*blocks_bundled):
    provs = []
    for block, bundled in blocks_bundled:
        provs += parse_lsp_providers(
            block, bundled=bundled, source="bundled" if bundled else "global",
            plugin_root=None, strict=True,
        )
    return LspRegistry(provs)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_unsupported_file_message(tmp_path):
    mgr = LspManager(tmp_path, registry=_reg())
    out = await mgr.goto_definition("x.zzz", 1, 1)
    assert "unsupported file type" in out


@pytest.mark.anyio
async def test_disabled_language_message(tmp_path):
    reg = _reg((
        {"language": "go", "extensions": [".go"], "command": "gopls"}, False,
    ))
    mgr = LspManager(tmp_path, registry=reg, disabled=frozenset({"go"}))
    out = await mgr.hover("x.go", 1, 1)
    assert "disabled for go" in out


@pytest.mark.anyio
async def test_python_diagnostics_routes_to_checks(tmp_path, monkeypatch):
    reg = _reg((
        {"language": "python", "extensions": [".py"], "backend": "basedpyright",
         "diagnostics": "python-checks", "probe": ["basedpyright-langserver"]}, True,
    ))
    called = {}

    async def fake_python_diagnostics(root, path, *, deep):
        called["hit"] = (path, deep)
        return []

    monkeypatch.setattr(
        "marim_harness.lsp.manager.checks.python_diagnostics", fake_python_diagnostics
    )
    monkeypatch.setattr(
        "marim_harness.lsp.manager.checks.format_checks", lambda path, diags: "clean"
    )
    mgr = LspManager(tmp_path, registry=reg)
    out = await mgr.diagnostics("m.py")
    assert out == "clean"
    assert called["hit"] == ("m.py", False)
