import pytest

from marim_harness.mcp.manager import McpManager, should_defer


@pytest.mark.parametrize(
    ("policy", "count", "threshold", "expected"),
    [
        ("off", 100, 15, False),
        ("on", 0, 15, True),
        ("on", 100, 15, True),
        ("auto", 15, 15, False),   # at threshold -> not deferred (strictly greater)
        ("auto", 16, 15, True),
        ("auto", 3, 15, False),
        ("bogus", 100, 15, False),  # unknown policy is conservative: no deferral
    ],
)
def test_should_defer(policy, count, threshold, expected):
    assert should_defer(policy, count, threshold) is expected


class _FakeServer:
    def __init__(self, name, n_tools):
        self.id = name
        self._n = n_tools

    async def list_tools(self):
        return list(range(self._n))


def _manager_with(servers):
    m = McpManager.__new__(McpManager)  # bypass connect plumbing
    m._live_servers = list(servers)
    m.disabled = set()
    return m


@pytest.mark.anyio
async def test_live_tool_count_sums_servers():
    m = _manager_with([_FakeServer("a", 4), _FakeServer("b", 6)])
    assert await m.live_tool_count() == 10


@pytest.mark.anyio
async def test_live_tool_count_tolerates_failures():
    class _Bad:
        id = "bad"

        async def list_tools(self):
            raise RuntimeError("boom")

    m = _manager_with([_FakeServer("a", 4), _Bad()])
    assert await m.live_tool_count() == 4  # bad server contributes 0, no raise


@pytest.mark.anyio
async def test_toolsets_for_off_returns_live_unwrapped():
    servers = [_FakeServer("a", 50)]
    m = _manager_with(servers)
    result = await m.toolsets_for("off", 15)
    assert result == servers  # unchanged


@pytest.mark.anyio
async def test_toolsets_for_on_wraps_in_deferred():
    from pydantic_ai import DeferredLoadingToolset

    m = _manager_with([_FakeServer("a", 50)])
    result = await m.toolsets_for("on", 15)
    assert len(result) == 1
    assert isinstance(result[0], DeferredLoadingToolset)


@pytest.mark.anyio
async def test_toolsets_for_auto_defers_only_above_threshold():
    from pydantic_ai import DeferredLoadingToolset

    below = _manager_with([_FakeServer("a", 5)])
    assert await below.toolsets_for("auto", 15) == below.live_toolsets()

    above = _manager_with([_FakeServer("a", 50)])
    deferred = await above.toolsets_for("auto", 15)
    assert isinstance(deferred[0], DeferredLoadingToolset)


@pytest.mark.anyio
async def test_deferred_toolsets_empty_when_no_servers():
    m = _manager_with([])
    assert m.deferred_toolsets() == []
    assert await m.toolsets_for("on", 15) == []


class _NamedTool:
    def __init__(self, name):
        self.name = name


class _NamedServer:
    def __init__(self, sid, names):
        self.id = sid
        self._names = names

    async def list_tools(self):
        return [_NamedTool(n) for n in self._names]


@pytest.mark.anyio
async def test_live_tools_by_server_groups_sorted_names():
    m = _manager_with([
        _NamedServer("mddocs", ["mddocs_b", "mddocs_a"]),
        _NamedServer("nasa", ["nasa_x"]),
    ])
    groups = await m.live_tools_by_server()
    assert groups == {"mddocs": ["mddocs_a", "mddocs_b"], "nasa": ["nasa_x"]}


@pytest.mark.anyio
async def test_live_tools_by_server_best_effort_on_failure():
    class _Bad:
        id = "bad"

        async def list_tools(self):
            raise RuntimeError("boom")

    m = _manager_with([_NamedServer("ok", ["t1"]), _Bad()])
    assert await m.live_tools_by_server() == {"ok": ["t1"]}


@pytest.mark.anyio
async def test_live_tool_count_still_counts_after_refactor():
    # The existing int-tool fakes (no .name) must still count by length.
    m = _manager_with([_FakeServer("a", 4), _FakeServer("b", 6)])
    assert await m.live_tool_count() == 10
