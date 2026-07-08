import pytest
from pydantic_ai import DeferredLoadingToolset
from pydantic_ai.toolsets import FunctionToolset

from marim_harness.runtime.toolsets import compose_turn_toolsets


class _FakeMcp:
    def __init__(self, toolsets, count):
        self._toolsets = list(toolsets)
        self._count = count

    def live_toolsets(self):
        return list(self._toolsets)

    async def live_tool_count(self):
        return self._count


def _lsp():
    ts = FunctionToolset()
    ts.add_function(lambda x: str(x), name="goto_definition")
    return ts


@pytest.mark.anyio
async def test_lsp_none_reproduces_live_when_under_threshold():
    a, b = FunctionToolset(), FunctionToolset()
    mcp = _FakeMcp([a, b], count=3)
    out = await compose_turn_toolsets(mcp, None, 6, "auto", 5)
    assert out == [a, b]


@pytest.mark.anyio
async def test_lsp_none_defers_live_when_over_threshold():
    a = FunctionToolset()
    mcp = _FakeMcp([a], count=10)
    out = await compose_turn_toolsets(mcp, None, 6, "auto", 5)
    assert len(out) == 1 and isinstance(out[0], DeferredLoadingToolset)


@pytest.mark.anyio
async def test_lsp_inline_when_combined_under_threshold():
    a = FunctionToolset()
    lsp = _lsp()
    mcp = _FakeMcp([a], count=3)  # 3 + 6 = 9 <= 10
    out = await compose_turn_toolsets(mcp, lsp, 6, "auto", 10)
    assert out == [a, lsp]  # lsp present inline


@pytest.mark.anyio
async def test_lsp_count_tips_combined_over_threshold():
    a = FunctionToolset()
    lsp = _lsp()
    mcp = _FakeMcp([a], count=5)  # mcp alone (5) <= 8, but 5 + 6 = 11 > 8
    out = await compose_turn_toolsets(mcp, lsp, 6, "auto", 8)
    assert len(out) == 1 and isinstance(out[0], DeferredLoadingToolset)


@pytest.mark.anyio
async def test_lsp_only_empty_mcp_inline_under_threshold():
    lsp = _lsp()
    mcp = _FakeMcp([], count=0)
    out = await compose_turn_toolsets(mcp, lsp, 6, "auto", 10)
    assert out == [lsp]


@pytest.mark.anyio
async def test_all_empty_returns_empty():
    mcp = _FakeMcp([], count=0)
    out = await compose_turn_toolsets(mcp, None, 6, "auto", 10)
    assert out == []


@pytest.mark.anyio
async def test_policy_on_always_defers():
    a = FunctionToolset()
    lsp = _lsp()
    mcp = _FakeMcp([a], count=1)
    out = await compose_turn_toolsets(mcp, lsp, 6, "on", 999)
    assert len(out) == 1 and isinstance(out[0], DeferredLoadingToolset)


# Reference-behavior cases migrated from the removed McpManager.toolsets_for
# tests: compose_turn_toolsets is the sole per-turn path, so the policy
# pass-through it inherits from should_defer lives here now.
@pytest.mark.anyio
async def test_policy_off_returns_live_inline():
    # "off" never defers, even for a large surface — the live toolsets pass
    # through unwrapped (was test_toolsets_for_off_returns_live_unwrapped).
    a, b = FunctionToolset(), FunctionToolset()
    mcp = _FakeMcp([a, b], count=50)
    out = await compose_turn_toolsets(mcp, None, 6, "off", 15)
    assert out == [a, b]


@pytest.mark.anyio
async def test_policy_on_empty_short_circuits_to_empty():
    # Empty combined surface returns [] before the policy check — so even "on"
    # yields [] (was the on+empty half of test_deferred_toolsets_empty…).
    mcp = _FakeMcp([], count=0)
    out = await compose_turn_toolsets(mcp, None, 6, "on", 15)
    assert out == []
