# tests/test_lsp_tools.py
import pytest

from marim_harness.runtime.deps import Deps, HarnessServices
from marim_harness.tools import names, provider


class _FakeLsp:
    def __init__(self):
        self.calls = []

    async def goto_definition(self, path, line, col):
        self.calls.append(("def", path, line, col))
        return "target.py:10:5"

    async def find_references(self, path, line, col):
        self.calls.append(("ref", path, line, col))
        return "a.py:1:1"

    async def hover(self, path, line, col):
        return "def foo() -> int"

    async def document_symbols(self, path):
        return "function foo  :4"

    async def workspace_symbols(self, query):
        return f"foo  a.py:4 ({query})"

    async def diagnostics(self, path, *, settle=1.5, deep=False):
        return f"{path}: no diagnostics"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _Ctx:
    def __init__(self, deps):
        self.deps = deps


@pytest.mark.anyio
async def test_goto_definition_tool_delegates(tmp_path):
    lsp = _FakeLsp()
    ctx = _Ctx(Deps(workspace_root=tmp_path, services=HarnessServices(lsp=lsp)))
    out = await provider.goto_definition(ctx, "m.py", 10, 5)
    assert out == "target.py:10:5"
    assert lsp.calls == [("def", "m.py", 10, 5)]


@pytest.mark.anyio
async def test_tools_report_unavailable_without_lsp(tmp_path):
    ctx = _Ctx(Deps(workspace_root=tmp_path, services=HarnessServices(lsp=None)))
    out = await provider.find_references(ctx, "m.py", 1, 1)
    assert "not available" in out.lower()


@pytest.mark.anyio
async def test_diagnostics_tool_delegates(tmp_path):
    ctx = _Ctx(Deps(workspace_root=tmp_path, services=HarnessServices(lsp=_FakeLsp())))
    out = await provider.diagnostics(ctx, "m.py")
    assert "no diagnostics" in out


def test_lsp_tool_names_are_read_tools():
    expected = {"goto_definition", "find_references", "hover",
                "document_symbols", "workspace_symbols", "diagnostics"}
    assert expected <= names.LSP_TOOLS
    assert expected <= names.READ_TOOLS  # subagents granted read get LSP too


def test_lsp_tools_registered_on_main_agent(tmp_path):
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    agent = Agent(TestModel(), deps_type=Deps)
    provider.BuiltinToolProvider().register(agent)
    with agent.override(model=TestModel(call_tools=[])):
        result = agent.run_sync("hi", deps=Deps(workspace_root=tmp_path))
    assert result is not None  # smoke: registration doesn't break agent build


def test_lsp_tools_in_subagent_fns():
    for name in ("goto_definition", "find_references", "hover",
                 "document_symbols", "workspace_symbols", "diagnostics"):
        assert name in provider._SUBAGENT_FNS


class _DiagLsp:
    def __init__(self, report):
        self.report = report
        self.seen = []

    async def diagnostics(self, path, *, settle=1.5, deep=False):
        self.seen.append((path, settle))
        return self.report


@pytest.mark.anyio
async def test_edit_appends_diagnostics(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("x = 1\n")
    lsp = _DiagLsp("m.py:1:1: error: bad")
    ctx = _Ctx(Deps(workspace_root=tmp_path, services=HarnessServices(lsp=lsp)))
    from marim_harness.tools import fs
    out = await provider.edit_file(ctx, "m.py", [fs.Edit(old_string="x = 1", new_string="y = 2")])
    assert "edited m.py" in out
    assert "m.py:1:1: error: bad" in out
    assert lsp.seen and lsp.seen[0][0] == "m.py"


@pytest.mark.anyio
async def test_write_appends_diagnostics(tmp_path):
    lsp = _DiagLsp("n.py:2:3: warning: meh")
    ctx = _Ctx(Deps(workspace_root=tmp_path, services=HarnessServices(lsp=lsp)))
    out = await provider.write_file(ctx, "n.py", "z = 3\n")
    assert "wrote n.py" in out
    assert "n.py:2:3: warning: meh" in out


@pytest.mark.anyio
async def test_edit_no_diagnostics_block_when_clean(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("x = 1\n")
    lsp = _DiagLsp("m.py: no diagnostics")
    ctx = _Ctx(Deps(workspace_root=tmp_path, services=HarnessServices(lsp=lsp)))
    from marim_harness.tools import fs
    out = await provider.edit_file(ctx, "m.py", [fs.Edit(old_string="x = 1", new_string="y = 2")])
    # A clean file adds no noise.
    assert "no diagnostics" not in out
    assert out.strip().endswith("edit)")


@pytest.mark.anyio
async def test_write_without_lsp_is_unchanged(tmp_path):
    ctx = _Ctx(Deps(workspace_root=tmp_path, services=HarnessServices(lsp=None)))
    out = await provider.write_file(ctx, "n.py", "z = 3\n")
    assert out == "wrote n.py (6 bytes, 6 chars)"


@pytest.mark.anyio
async def test_diagnostics_exception_returns_unchanged_result(tmp_path):
    """Exception in lsp.diagnostics must not fail the write/edit."""
    class _FailingLsp:
        async def diagnostics(self, path, *, settle=1.5, deep=False):
            raise RuntimeError("boom")

    ctx = _Ctx(Deps(workspace_root=tmp_path, services=HarnessServices(lsp=_FailingLsp())))
    out = await provider.write_file(ctx, "n.py", "z = 3\n")
    # No diagnostics block; result unchanged
    assert out == "wrote n.py (6 bytes, 6 chars)"
    assert "boom" not in out


@pytest.mark.anyio
async def test_real_diagnostic_not_suppressed_by_path_containing_disabled(tmp_path):
    """Path/message containing 'disabled' must not suppress real diagnostics."""
    # Real diagnostic line for a path containing "disabled"
    lsp = _DiagLsp("feature_disabled.py:3:1: error: undefined name")
    ctx = _Ctx(Deps(workspace_root=tmp_path, services=HarnessServices(lsp=lsp)))
    out = await provider.write_file(ctx, "feature_disabled.py", "bad code\n")
    # The real diagnostic must be appended
    assert "diagnostics:" in out
    assert "undefined name" in out


@pytest.mark.anyio
async def test_clean_report_suppresses_diagnostics_block(tmp_path):
    """A 'no diagnostics' clean report must not append a diagnostics block."""
    lsp = _DiagLsp("n.py: no diagnostics")
    ctx = _Ctx(Deps(workspace_root=tmp_path, services=HarnessServices(lsp=lsp)))
    out = await provider.write_file(ctx, "n.py", "z = 3\n")
    # No diagnostics block appended
    assert "diagnostics:" not in out
    assert "no diagnostics" not in out
