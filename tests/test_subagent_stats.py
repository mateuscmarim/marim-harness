from dataclasses import dataclass

from marim_harness.interfaces.tui.widgets.subagent_stats import (
    aggregate,
    row_cells,
    status_glyph,
)


@dataclass
class FakeAgent:
    status: str = "pending"
    agent_type: str = "research"
    tokens: int = 0
    cost_text: str | None = None
    tool_count: int = 0
    detached: bool = False
    _title: str = "map the codebase"
    _dur: str = "12s"

    def display_title(self) -> str:
        return self._title

    def _duration(self) -> str:
        return self._dur


def test_status_glyph_covers_each_state():
    assert status_glyph("done") == "✓"
    assert status_glyph("failed") == "✕"
    assert status_glyph("denied") == "✕"
    assert status_glyph("pending") == "▸"


def test_row_cells_running_agent():
    a = FakeAgent(status="pending", tool_count=3, tokens=8100, cost_text="$0.04")
    cells = row_cells(a)
    assert cells[0] == "▸"
    assert cells[1] == "research — map the codebase"
    assert cells[2] == "3"
    assert cells[3] == "8.1k"
    assert cells[4] == "$0.04"
    assert cells[5] == "12s"


def test_row_cells_detached_has_no_tool_count():
    a = FakeAgent(status="pending", detached=True, tool_count=0)
    # A background run never streamed its steps, so don't show a misleading "0".
    assert row_cells(a)[2] == "—"


def test_row_cells_blank_cost_when_unmetered():
    assert row_cells(FakeAgent())[4] == ""


def test_aggregate_counts_and_totals():
    agents = [
        FakeAgent(status="pending", tokens=100),
        FakeAgent(status="done", tokens=200),
        FakeAgent(status="failed", tokens=50),
    ]
    s = aggregate(agents, cost_of=lambda a: 0.01)
    assert (s.total, s.running, s.done, s.failed) == (3, 1, 1, 1)
    assert s.tokens == 350
    assert s.cost_text == "$0.03"


def test_aggregate_empty_is_blank_cost():
    s = aggregate([], cost_of=lambda a: 0.0)
    assert s.total == 0
    assert s.cost_text == ""
