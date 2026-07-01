# Nested Sub-Agent Hierarchy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show sub-agents spawned by other sub-agents in the Ctrl+X sub-agents screen, as an indented tree, with each nested spawn rendered as a live card in its parent's transcript pane.

**Architecture:** UI-only. The backend already emits nested sub-agent streams keyed by their own `tool_call_id` (`subagents/runner.py:252`); they are dropped because `_SubAgentSink` never intercepts a nested `spawn_agent`, so no card is registered and `on_subagent_event` early-returns (`stream_render.py:678-680`). We (1) unify the `spawn_agent` claim so both the top-level and sub-agent sinks build a card + pane + registry entry, tagging each card with its `parent_id`; and (2) render/navigate the flat `subagents` list in depth-first order with tree connectors, via a pure `tree_order` helper.

**Tech Stack:** Python 3.10+, Textual (TUI), Pydantic AI, pytest (`anyio` for async widget tests). Package/run via `uv`.

## Global Constraints

- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright`. Never bare `python`/`pip`/`pytest`.
- Match CI order before claiming done: ruff → pyright → pytest.
- `requires-python >=3.10` — no 3.11+-only syntax.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM` (import sorting enforced).
- This feature touches ONLY `src/marim_harness/interfaces/tui/**` and `CLAUDE.md`. Do NOT modify `subagents/`, `tools/`, `runtime/`, or `deps` — nesting already works at the backend.
- Tool docstrings are product copy; do not weaken them.

---

## File Structure

- `src/marim_harness/interfaces/tui/widgets/subagent_stats.py` — **modify.** Add pure `TreeRow`, `tree_order`, `_row_prefix`; add `prefix` param to `row_cells`.
- `src/marim_harness/interfaces/tui/widgets/subagent.py` — **modify.** Add `parent_id` field to `SubAgentWidget`.
- `src/marim_harness/interfaces/tui/stream_render.py` — **modify.** Add shared `_claim_spawn` on `_StreamSink`; route `_TopLevelSink` and new `_SubAgentSink.intercept_tool` through it.
- `src/marim_harness/interfaces/tui/widgets/subagent_viewer.py` — **modify.** `SubAgentList.refresh_rows` renders in `tree_order` with prefixes.
- `src/marim_harness/interfaces/tui/subagents_viewer.py` — **modify.** Route the three row-index → agent sites through `tree_order`.
- `CLAUDE.md` — **modify.** Fix the stale "sub-agents cannot recurse" claim.
- Tests: `tests/test_subagent_stats.py`, `tests/test_subagents_screen.py` — **modify** (add cases).

---

## Task 1: Pure tree-ordering + row-prefix layer

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/subagent_stats.py`
- Test: `tests/test_subagent_stats.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `class TreeRow` — dataclass with `agent: object`, `depth: int`, `is_last: bool`.
  - `tree_order(agents: list) -> list[TreeRow]` — depth-first walk over `parent_id` links; an agent whose `parent_id` is falsy or not present in `agents` is a root. Requires each agent to expose `.stream_id: str` and `.parent_id: str | None`.
  - `row_cells(agent, prefix: str = "") -> list[str]` — unchanged output when `prefix=""`; otherwise the prefix is prepended to the `agent` (label) cell.
  - `_row_prefix(depth: int, is_last: bool) -> str` — `""` at depth 0; `"  " * (depth - 1) + ("└─ " if is_last else "├─ ")` at depth ≥ 1.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_subagent_stats.py`. Extend the existing `FakeAgent` usage with a minimal fake that carries the two fields `tree_order` needs:

```python
from dataclasses import dataclass

from marim_harness.interfaces.tui.widgets.subagent_stats import (
    TreeRow,
    _row_prefix,
    row_cells,
    tree_order,
)


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_subagent_stats.py -k "tree_order or row_prefix or row_cells_prefix" -v`
Expected: FAIL with `ImportError: cannot import name 'TreeRow'` (and `tree_order`/`_row_prefix`).

- [ ] **Step 3: Implement the pure helpers**

In `subagent_stats.py`, add the `TreeRow` dataclass and `tree_order`/`_row_prefix` near the top (after the imports and `STATUS_GLYPH`), and add the `prefix` param to `row_cells`.

```python
@dataclass
class TreeRow:
    """One agent placed in the display tree: the agent, its nesting ``depth``
    (0 = a top-level spawn / list root), and whether it is the last of its
    siblings (drives the └─ vs ├─ connector)."""
    agent: object
    depth: int
    is_last: bool


def tree_order(agents: list) -> list[TreeRow]:
    """Depth-first ordering of ``agents`` by ``parent_id`` links: every agent is
    emitted immediately before its own descendants, so the flat list reads as a
    tree. An agent whose ``parent_id`` is falsy — or names an agent not in this
    list (an orphan) — is treated as a root, so nothing is ever hidden. Sibling
    order (and root order) preserves ``agents``' insertion order."""
    ids = {a.stream_id for a in agents}
    children: dict[str | None, list] = {}
    for a in agents:
        pid = a.parent_id if (a.parent_id and a.parent_id in ids) else None
        children.setdefault(pid, []).append(a)
    rows: list[TreeRow] = []

    def walk(parent_id: str | None, depth: int) -> None:
        kids = children.get(parent_id, [])
        for i, a in enumerate(kids):
            rows.append(TreeRow(a, depth, i == len(kids) - 1))
            walk(a.stream_id, depth + 1)

    walk(None, 0)
    return rows


def _row_prefix(depth: int, is_last: bool) -> str:
    """The tree connector/indent for the ``agent`` cell. Root rows get no prefix;
    a nested row gets two spaces of indent per ancestor level below the root plus
    a └─ (last sibling) or ├─ connector."""
    if depth == 0:
        return ""
    return "  " * (depth - 1) + ("└─ " if is_last else "├─ ")
```

Then change `row_cells` to accept and apply the prefix (keep the `bg ·` tag inside the prefix so a nested row still reads as nested):

```python
def row_cells(agent, prefix: str = "") -> list[str]:
    """The six `DataTable` cells for one agent row: glyph, "{prefix}{type} —
    {title}", tool count, tokens, cost, duration. ``prefix`` carries the tree
    connector for a nested row (empty for a top-level spawn). A background
    (detached) agent keeps its quiet "bg · " tag between the prefix and label."""
    label = f"{agent.agent_type} — {agent.display_title()}"
    if agent.detached:
        label = f"bg · {label}"
    label = f"{prefix}{label}"
    tokens = human_tokens(agent.tokens) if agent.tokens else ""
    return [
        status_glyph(agent.status),
        label,
        str(agent.tool_count),
        tokens,
        agent.cost_text or "",
        agent._duration(),
    ]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_subagent_stats.py -v`
Expected: PASS (all existing + new cases).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/marim_harness/interfaces/tui/widgets/subagent_stats.py tests/test_subagent_stats.py
git add src/marim_harness/interfaces/tui/widgets/subagent_stats.py tests/test_subagent_stats.py
git commit -m "feat(tui): pure tree_order + row prefix for subagent list"
```

---

## Task 2: Register nested spawns as cards (sink unification)

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/subagent.py` (add `parent_id` field)
- Modify: `src/marim_harness/interfaces/tui/stream_render.py` (shared `_claim_spawn`, both sinks)
- Test: `tests/test_subagents_screen.py`

**Interfaces:**
- Consumes: `StreamRenderer.mount_spawn_widget`, `ensure_pane`, `tool_widgets`, `set_run` (existing).
- Produces:
  - `SubAgentWidget.parent_id: str | None` — the spawning card's `stream_id`; `None` for a top-level spawn.
  - `_StreamSink._claim_spawn(self, event, args: dict, container: Widget, parent_id: str | None) -> SubAgentWidget` — builds + registers + mounts a spawn card, sets `parent_id`, breaks the run; returns the card.
  - `_SubAgentSink.intercept_tool` — now claims `spawn_agent` (child card mounts into the parent's pane).

- [ ] **Step 1: Add the `parent_id` field to `SubAgentWidget`**

In `subagent.py`, in `SubAgentWidget.__init__`, right after `self.stream_id = ""` (line ~116), add:

```python
        # The stream_id of the spawn that created this card, when it was spawned
        # by another sub-agent rather than the top-level agent. None for a
        # top-level spawn. Drives the depth-first tree order + connectors in the
        # sub-agents list (see subagent_stats.tree_order). Set by the renderer's
        # _claim_spawn at registration time.
        self.parent_id: str | None = None
```

- [ ] **Step 2: Write the failing render-level test**

Add to `tests/test_subagents_screen.py` (it already has `_app(tmp_path)` and imports Textual test harness). Add these imports at the top of the file:

```python
from marim_harness.interfaces.tui.stream_render import _SubAgentSink


class _FakePart:
    def __init__(self, tool_name: str, tool_call_id: str) -> None:
        self.tool_name = tool_name
        self.tool_call_id = tool_call_id


class _FakeToolEvent:
    def __init__(self, tool_name: str, tool_call_id: str) -> None:
        self.part = _FakePart(tool_name, tool_call_id)
```

Then the test:

```python
@pytest.mark.anyio
async def test_nested_spawn_registers_child_card_in_parent_pane(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        # A top-level spawn: parent card + pane, registered like the real path.
        parent = r.mount_spawn_widget({"type": "general", "description": "parent"})
        parent.stream_id = "call-parent"
        r.tool_widgets["call-parent"] = parent
        parent_pane = r.ensure_pane(parent)
        await pilot.pause()

        # The parent's stream claims a nested spawn_agent.
        sink = _SubAgentSink(r, parent, "call-parent")
        ev = _FakeToolEvent("spawn_agent", "call-child")
        claimed = await sink.intercept_tool(
            ev, {"type": "explore", "description": "child"}, parent_pane
        )
        await pilot.pause()

        assert claimed is True
        child = r.tool_widgets["call-child"]
        assert child.parent_id == "call-parent"     # tagged for the tree
        assert child in r.subagents                  # shows in the list
        assert child.pane is not None                # its own detail pane exists
        assert child in parent_pane.children         # card mounted in parent's pane


@pytest.mark.anyio
async def test_nested_non_spawn_tool_not_claimed(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        parent = r.mount_spawn_widget({"type": "general", "description": "parent"})
        parent.stream_id = "call-parent"
        r.tool_widgets["call-parent"] = parent
        parent_pane = r.ensure_pane(parent)
        await pilot.pause()
        sink = _SubAgentSink(r, parent, "call-parent")
        claimed = await sink.intercept_tool(
            _FakeToolEvent("read_file", "call-read"), {"path": "x"}, parent_pane
        )
        assert claimed is False                      # only spawn_agent is claimed
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_subagents_screen.py -k "nested_spawn or nested_non_spawn" -v`
Expected: FAIL — `_SubAgentSink` has no `intercept_tool` override, so `claimed` is `False` in the first test (and `child` is not registered → `KeyError`).

- [ ] **Step 4: Add `_claim_spawn` to the base sink**

In `stream_render.py`, add to `class _StreamSink` (after `intercept_tool`'s default, before `on_result`):

```python
    async def _claim_spawn(
        self, event, args: dict, container: Widget, parent_id: str | None
    ) -> "SubAgentWidget":
        """Shared spawn_agent claim for both scopes: build the live card, register
        it so its own stream (forwarded by the runner under this tool_call_id) can
        find it, create its detail pane, break the current tool run, and mount the
        card into this sink's container (#log for the top-level agent, the parent's
        pane for a nested spawn). ``parent_id`` tags the card for the list's tree
        order (None for a top-level spawn)."""
        widget = self._r.mount_spawn_widget(args)
        widget.stream_id = event.part.tool_call_id
        widget.parent_id = parent_id
        self._r.tool_widgets[event.part.tool_call_id] = widget
        self._r.ensure_pane(widget)
        self.set_run(None, None)
        await container.mount(widget)
        return widget
```

- [ ] **Step 5: Route `_TopLevelSink.intercept_tool` through `_claim_spawn`**

Replace the `spawn_agent` branch body in `_TopLevelSink.intercept_tool` (lines ~232-239) with a call to the shared helper (keep the `ask_user` branch and the trailing `return False` unchanged):

```python
        if event.part.tool_name == "spawn_agent":
            await self._claim_spawn(event, args, container, parent_id=None)
            return True
```

- [ ] **Step 6: Override `intercept_tool` on `_SubAgentSink`**

In `_SubAgentSink` (which currently has no `intercept_tool`), add the override. `ask_user` and background spawns stay top-level-only, so this claims only `spawn_agent`:

```python
    async def intercept_tool(self, event, args: dict, container: Widget) -> bool:
        # A nested spawn_agent gets the same live card as a top-level one, mounted
        # into this sub-agent's pane and tagged with this agent as its parent. The
        # child's own stream is already forwarded by the runner under the nested
        # spawn's tool_call_id (subagents/runner.py); registering the card here is
        # what lets on_subagent_event find it instead of dropping the stream.
        if event.part.tool_name == "spawn_agent":
            await self._claim_spawn(
                event, args, container, parent_id=self._parent.stream_id
            )
            return True
        return False
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_subagents_screen.py -k "nested_spawn or nested_non_spawn" -v`
Expected: PASS.

- [ ] **Step 8: Full screen-test regression + lint + commit**

```bash
uv run pytest --no-cov tests/test_subagents_screen.py tests/test_subagent_detail.py -q
uv run ruff check src/marim_harness/interfaces/tui/stream_render.py src/marim_harness/interfaces/tui/widgets/subagent.py tests/test_subagents_screen.py
git add src/marim_harness/interfaces/tui/stream_render.py src/marim_harness/interfaces/tui/widgets/subagent.py tests/test_subagents_screen.py
git commit -m "feat(tui): render nested sub-agent spawns as cards in the parent pane"
```

---

## Task 3: Render + navigate the list as an indented tree

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/subagent_viewer.py` (`refresh_rows`)
- Modify: `src/marim_harness/interfaces/tui/subagents_viewer.py` (three index sites)
- Test: `tests/test_subagents_screen.py`

**Interfaces:**
- Consumes: `tree_order`, `_row_prefix`, `row_cells(agent, prefix)` (Task 1); `SubAgentWidget.parent_id` (Task 2).
- Produces: `SubAgentList.refresh_rows` renders rows in `tree_order` with prefixes; `SubAgentsViewer._ordered()` gives the same DFS agent order used to map a cursor row to an agent.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_subagents_screen.py`:

```python
@pytest.mark.anyio
async def test_list_renders_child_indented_under_parent(tmp_path):
    from marim_harness.interfaces.tui.widgets.subagent_viewer import SubAgentList

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        parent = r.mount_spawn_widget({"type": "general", "description": "parent"})
        parent.stream_id = "call-parent"
        child = r.mount_spawn_widget({"type": "explore", "description": "child"})
        child.stream_id = "call-child"
        child.parent_id = "call-parent"
        # A second top-level spawn appended AFTER the child, to prove ordering is
        # by tree, not insertion.
        sibling = r.mount_spawn_widget({"type": "coding", "description": "sib"})
        sibling.stream_id = "call-sib"

        view = app.query_one(SubAgentsView)
        view.repaint(r.subagents, cost_of := (lambda a: 0.0), selected=0)
        await pilot.pause()

        lst = app.query_one(SubAgentList)
        # Row 0 = parent, row 1 = its child (indented), row 2 = sibling.
        cell = lambda row: str(lst.get_row_at(row)[1])
        assert cell(0).startswith("general —")
        assert cell(1).startswith("└─ explore —")   # nested under parent
        assert cell(2).startswith("coding —")        # sibling root, not indented
```

Note: `view.repaint` signature is `repaint(self, subagents, cost_of, selected=None)`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest --no-cov tests/test_subagents_screen.py -k "child_indented" -v`
Expected: FAIL — row 1 cell is `"explore — child"` (no `└─ `), because `refresh_rows` still renders raw insertion order without prefixes.

- [ ] **Step 3: Make `refresh_rows` render the tree**

In `subagent_viewer.py`, update the import and `refresh_rows` body to build rows from `tree_order` with prefixes. Change the import line:

```python
from .subagent_stats import _row_prefix, row_cells, tree_order
```

Replace the row-building loop inside `refresh_rows` (the `for w in subagents:` block) with:

```python
        with self.prevent(DataTable.RowHighlighted):
            self.clear()
            for tr in tree_order(subagents):
                self.add_row(*row_cells(tr.agent, _row_prefix(tr.depth, tr.is_last)))
            if self.row_count:
                self.move_cursor(row=max(0, min(keep, self.row_count - 1)))
```

- [ ] **Step 4: Route the viewer's row-index → agent sites through `tree_order`**

In `subagents_viewer.py`, add the import and a helper, then update the three sites so a cursor row maps to the same agent the table renders at that row.

Add import:

```python
from .widgets.subagent_stats import tree_order
```

Add a helper method on `SubAgentsViewer` (e.g. after `__init__`):

```python
    def _ordered(self) -> list:
        """Sub-agents in the list's display (depth-first) order — the same order
        SubAgentList.refresh_rows renders — so a DataTable cursor row maps to the
        correct agent."""
        return [tr.agent for tr in tree_order(self.app.stream.subagents)]
```

In `open_at`, replace the index computation (the `subs = ...`/`index = len(subs) - 1`/`next(...)` block) so it indexes the ordered list — but keep the empty-check + notice on the raw list:

```python
        app = self.app
        subs = app.stream.subagents
        if not subs:
            app.query_one("#log", VerticalScroll).mount(
                NoticeMessage("No sub-agents spawned yet — nothing to view.")
            )
            return
        ordered = self._ordered()
        index = len(ordered) - 1
        if stream_id is not None:
            index = next(
                (i for i, w in enumerate(ordered) if w.stream_id == stream_id), index
            )
```

In `_repaint_list`, the table is repainted from the raw `subs` (refresh_rows re-orders internally); only the row-index → agent lookup must use the ordered list:

```python
        app = self.app
        subs = app.stream.subagents
        if not subs:
            self.close()
            return
        view = app.query_one(SubAgentsView)
        view.repaint(subs, self.cost, selected=select)
        ordered = self._ordered()
        self.index = max(0, min(view.list.cursor_row, len(ordered) - 1))
        current = ordered[self.index]
```

(The rest of `_repaint_list` — `if current.pane is not None:` onward — is unchanged.)

In `on_row_highlighted`, map the highlighted row through the ordered list:

```python
    def on_row_highlighted(self, event) -> None:
        """Moving the list cursor selects that agent's transcript."""
        if self.open and event.cursor_row is not None:
            self.index = event.cursor_row
            ordered = self._ordered()
            current = ordered[self.index]
            if current.pane is not None:
                self.app.query_one(SubAgentsView).host.show(current.stream_id)
            self.app.stream.flush_streams()
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest --no-cov tests/test_subagents_screen.py -k "child_indented" -v`
Expected: PASS.

- [ ] **Step 6: Full screen-test regression + lint + commit**

```bash
uv run pytest --no-cov tests/test_subagents_screen.py -q
uv run ruff check src/marim_harness/interfaces/tui/widgets/subagent_viewer.py src/marim_harness/interfaces/tui/subagents_viewer.py tests/test_subagents_screen.py
git add src/marim_harness/interfaces/tui/widgets/subagent_viewer.py src/marim_harness/interfaces/tui/subagents_viewer.py tests/test_subagents_screen.py
git commit -m "feat(tui): render + navigate the subagents list as an indented tree"
```

---

## Task 4: Verify the summary counts nested agents without double-counting

**Files:**
- Test: `tests/test_subagent_stats.py`

**Interfaces:**
- Consumes: `aggregate` (unchanged), `SubAgentWidget.tokens`/`cost_value`.

This task adds no production code unless the assertion below fails. It pins the spec's verification item: a parent card's `tokens` are its own run's usage, so summing every agent (parent + children) in `aggregate` is correct, not a double-count.

- [ ] **Step 1: Write the test asserting per-agent (non-cumulative) token roll-up**

```python
from marim_harness.interfaces.tui.widgets.subagent_stats import aggregate


def test_aggregate_counts_every_agent_once():
    # Simulate a parent (100 tok) with one nested child (40 tok). aggregate sums
    # each agent's own tokens; the total is 140, not 100+140 (which is what a
    # double-count of an already-cumulative parent would produce).
    parent = FakeNode("p", tokens=100)
    child = FakeNode("c", parent_id="p", tokens=40)
    stats = aggregate([parent, child], cost_of=lambda a: 0.0)
    assert stats.total == 2
    assert stats.tokens == 140
```

- [ ] **Step 2: Run it**

Run: `uv run pytest --no-cov tests/test_subagent_stats.py -k "counts_every_agent_once" -v`
Expected: PASS (aggregate already sums per-agent `.tokens`).

If this FAILS because real parent cards turn out to carry cumulative token totals that include their children (verify by inspecting how `note_subagent_usage` sets `widget.tokens` from the parent run's `RunUsage`): do NOT edit the test to pass. Instead, stop and report — the fix is to subtract descendants in `aggregate`, which is a real code change worth its own review. (Expected outcome: it passes; sub-agent runs carry their own `ctx.usage`.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_subagent_stats.py
git commit -m "test(tui): pin subagent summary counts each nested agent once"
```

---

## Task 5: Fix the stale recursion claim in CLAUDE.md

**Files:**
- Modify: `CLAUDE.md` (the `tools/` section, ~lines 96-99)

- [ ] **Step 1: Correct the sentence**

Find in `CLAUDE.md`:

```
`spawn_agent` is never granted to sub-agents, so they cannot recurse.
```

Replace with:

```
`spawn_agent` is granted to a sub-agent only when it could still nest within the
depth ceiling (`depth + 1 < SUBAGENT_MAX_DEPTH`, default 3) — see
`SubagentRunner.build`; at the leaf depth the tool is absent, so nesting is
bounded, not forbidden. Nested spawns render in the sub-agents screen as an
indented tree (a child card streams into its parent's transcript pane).
```

- [ ] **Step 2: Sanity-check the surrounding paragraph still reads correctly**

Run: `git diff CLAUDE.md`
Expected: only the one sentence changed; the paragraph about sub-agents not recursing now describes the real depth-bounded behavior.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: correct stale 'sub-agents cannot recurse' claim"
```

---

## Task 6: Full CI gate

- [ ] **Step 1: Run the full gate in CI order**

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```
Expected: ruff clean, pyright clean (src), full suite green (coverage on by default).

- [ ] **Step 2: Manual smoke (optional but recommended)**

Use the `/run` skill or launch marim, spawn a `general` sub-agent that itself spawns an `explore` child, open the sub-agents screen (Ctrl+X), and confirm: the child appears indented under its parent in the list, selecting it shows its own transcript pane, and the parent's pane shows the child as a live card.

- [ ] **Step 3: (If all green) offer to merge `subagent-hierarchy`**

Use `superpowers:finishing-a-development-branch` to choose merge / PR / cleanup.

---

## Self-Review

**Spec coverage:**
- Data model (`parent_id`) → Task 2 Step 1. (Spec's `depth` field is intentionally NOT stored: `tree_order` derives depth from `parent_id` links, which is more correct for orphans and avoids duplicated state. Documented here as a deliberate simplification of the spec.)
- Sink unification (`_claim_spawn`, both sinks, ask_user/background stay top-level) → Task 2 Steps 4-6.
- List tree rendering (`tree_order`, prefixes, both consumers same ordering) → Task 1 + Task 3.
- Summary counts / double-count verification → Task 4.
- Navigation & invariants (child pane via ContentSwitcher, `_stream_hidden`) → covered by Task 2/3 tests + no change needed (flat ContentSwitcher retained).
- CLAUDE.md fix (item a) → Task 5.

**Placeholder scan:** No TBD/TODO; every code step shows full code; every command shows expected output.

**Type consistency:** `tree_order` → `list[TreeRow]` with `.agent/.depth/.is_last`, consumed identically in `refresh_rows` (Task 3 Step 3) and `_ordered` (Task 3 Step 4). `_claim_spawn(event, args, container, parent_id)` defined in Task 2 Step 4, called with matching args in Steps 5-6. `row_cells(agent, prefix="")` defined Task 1, called with prefix in Task 3.
