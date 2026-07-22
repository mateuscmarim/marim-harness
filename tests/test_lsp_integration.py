import pytest

from marim_harness.lsp.bundled import bundled_lsp_providers
from marim_harness.lsp.manager import LspManager
from marim_harness.lsp.provider import LspRegistry

_reg = LspRegistry(bundled_lsp_providers())

pytestmark = pytest.mark.skipif(
    "python" not in _reg.locally_installed_languages(),
    reason="no local Python language server (pyright) installed",
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_definition_and_diagnostics_real_pyright(tmp_path):
    (tmp_path / "lib.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "main.py").write_text("from lib import add\n\nadd(1, 2)\n")
    mgr = LspManager(tmp_path, registry=_reg, start_timeout=120.0, request_timeout=60.0)
    try:
        # goto_definition on `add` usage (main.py line 3, col 1, 1-based) → lib.py
        defn = await mgr.goto_definition("main.py", 3, 1)
        assert "lib.py" in defn
        # diagnostics on a file with an undefined name → at least one error
        (tmp_path / "bad.py").write_text("print(undefined_name)\n")
        diag = await mgr.diagnostics("bad.py", settle=3.0)
        assert "bad.py" in diag
    finally:
        await mgr.aclose()
