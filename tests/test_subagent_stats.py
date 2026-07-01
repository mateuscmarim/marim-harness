from dataclasses import dataclass

from marim_harness.interfaces.tui.widgets.subagent_stats import (
    _row_prefix,
    aggregate,
    row_cells,
    status_glyph,
    tree_order,
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
    stream_id: str = ""
    parent_id: str | None = None

    def display_title(self) -> str:
        return self._title

    def _duration(self) -> str:
        return self._dur


@dataclass
class FakeNode:
    stream_id: str
    parent_id: str | None = None
    # row_cells needs these too:
    status: str = "done"
    agent_type: str = "explore"
    tokens: int = 0
    cost_text: str | None = None
    tool_count: int = 0
    detached: bool = False
    _title: str = "t"
    _dur: str = "1s"

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


def test_row_cells_detached_shows_bg_tag_and_real_tally():
    # Phase 2: a background agent streams its steps, so it shows its real tool
    # tally; a "bg · " tag marks it as an off-turn (background) run.
    a = FakeAgent(status="pending", detached=True, tool_count=4)
    cells = row_cells(a)
    assert cells[1] == "bg · research — map the codebase"
    assert cells[2] == "4"


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


def test_tree_order_nests_children_after_parent_depth_first():
    a = FakeNode("a")
    b = FakeNode("b", parent_id="a")       # child of a
    c = FakeNode("c", parent_id="b")       # grandchild
    d = FakeNode("d")                      # second root
    # Insertion order interleaves roots and descendants:
    rows = tree_order([a, d, b, c])
    assert [(r.agent.stream_id, r.depth) for r in rows] == [
        ("a", 0), ("b", 1), ("c", 2), ("d", 0),
    ]


def test_tree_order_marks_last_sibling():
    a = FakeNode("a")
    b = FakeNode("b", parent_id="a")
    c = FakeNode("c", parent_id="a")
    rows = tree_order([a, b, c])
    by_id = {r.agent.stream_id: r for r in rows}
    assert by_id["b"].is_last is False
    assert by_id["c"].is_last is True
    assert by_id["a"].is_last is True      # only root


def test_tree_order_orphan_parent_becomes_root():
    # parent_id points at an agent not in the list → treated as a root, never hidden.
    orphan = FakeNode("x", parent_id="missing")
    rows = tree_order([orphan])
    assert [(r.agent.stream_id, r.depth) for r in rows] == [("x", 0)]


def test_tree_order_empty():
    assert tree_order([]) == []


def test_row_prefix_shape():
    assert _row_prefix(0, True) == ""
    assert _row_prefix(1, True) == "└─ "
    assert _row_prefix(1, False) == "├─ "
    assert _row_prefix(2, True) == "  └─ "


def test_row_cells_prefix_prepended_to_label():
    n = FakeNode("b", parent_id="a", agent_type="explore", _title="read file")
    cells = row_cells(n, prefix="└─ ")
    assert cells[1] == "└─ explore — read file"


def test_row_cells_default_prefix_unchanged():
    n = FakeNode("a", agent_type="research", _title="map it")
    assert row_cells(n)[1] == "research — map it"


def test_aggregate_counts_every_agent_once():
    # Simulate a parent (100 tok) with one nested child (40 tok). aggregate sums
    # each agent's own tokens; the total is 140, not 100+140 (which is what a
    # double-count of an already-cumulative parent would produce).
    parent = FakeNode("p", tokens=100)
    child = FakeNode("c", parent_id="p", tokens=40)
    stats = aggregate([parent, child], cost_of=lambda a: 0.0)
    assert stats.total == 2
    assert stats.tokens == 140
