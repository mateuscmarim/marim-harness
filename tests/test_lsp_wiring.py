from pydantic_ai import Agent
from pydantic_ai.toolsets import FunctionToolset

from marim_harness.tools.names import LSP_TOOLS
from marim_harness.tools.provider import BuiltinToolProvider


def _registered_tool_names(register_lsp_tools: bool) -> set[str]:
    agent = Agent("test")
    BuiltinToolProvider(register_lsp_tools=register_lsp_tools).register(agent)
    # FunctionToolset backing the agent's directly-registered tools.
    return set(agent._function_toolset.tools)  # noqa: SLF001


def test_main_agent_no_longer_statically_registers_lsp():
    names = _registered_tool_names(register_lsp_tools=True)
    assert not (LSP_TOOLS & names), "LSP tools should move off static registration"
    # Non-LSP builtins are still present.
    assert "read_file" in names and "bash" in names


def test_lsp_toolset_present_when_enabled():
    ts = BuiltinToolProvider(register_lsp_tools=True).lsp_toolset()
    assert isinstance(ts, FunctionToolset)
    assert set(ts.tools) >= LSP_TOOLS


def test_lsp_toolset_none_when_disabled():
    assert BuiltinToolProvider(register_lsp_tools=False).lsp_toolset() is None
