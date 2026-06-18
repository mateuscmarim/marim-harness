# tests/test_lsp_tools.py
import pytest

from marim_harness.deps import Deps
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

    async def diagnostics(self, path, *, settle=1.5):
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
    ctx = _Ctx(Deps(workspace_root=tmp_path, lsp=lsp))
    out = await provider.goto_definition(ctx, "m.py", 10, 5)
    assert out == "target.py:10:5"
    assert lsp.calls == [("def", "m.py", 10, 5)]


@pytest.mark.anyio
async def test_tools_report_unavailable_without_lsp(tmp_path):
    ctx = _Ctx(Deps(workspace_root=tmp_path, lsp=None))
    out = await provider.find_references(ctx, "m.py", 1, 1)
    assert "not available" in out.lower()


@pytest.mark.anyio
async def test_diagnostics_tool_delegates(tmp_path):
    ctx = _Ctx(Deps(workspace_root=tmp_path, lsp=_FakeLsp()))
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
