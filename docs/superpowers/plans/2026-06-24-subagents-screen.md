# Sub-agents Screen Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the clumsy `ctrl+x` docked-widget sub-agent viewer with a discoverable, full-bleed two-pane screen (agent list + live transcript) backed by a persistent transcript host, so a fan-out is easy to navigate and its stats are visible.

**Architecture:** Approach A from the design — relocate each sub-agent's transcript out of its inline log card into a persistent, hidden `ContentSwitcher` ("detail host") that lives in the main screen. The inline card becomes a slim breadcrumb that holds only scalar status (tool count, tokens, cost, duration) and references its pane. A new full-bleed `SubAgentsView` lays out a session summary bar, a `DataTable` agent list, and the detail host; `ctrl+x` hides the main log and shows it. Because panes live in the always-mounted host, the live stream keeps mounting into them whether the view is open or not — liveness is free, with the existing "only flush the on-screen transcript" optimization preserved by keying it on the host's current pane.

**Tech Stack:** Python ≥3.10, Pydantic AI, Textual (`ContentSwitcher`, `DataTable`, `VerticalScroll`, `Binding`), pytest + Textual `Pilot`, `uv`.

**Test conventions (match the repo, don't invent):** async tests use `@pytest.mark.anyio` (NOT `asyncio`). There is no shared `tests.helpers`; build the app with a local helper mirroring `tests/test_app.py:11`:

```python
from pathlib import Path
import pytest
from marim_harness.deps import Deps
from marim_harness.interfaces.tui.app import HarnessApp
from marim_harness.permissions import Mode


def _app(tmp_path: Path) -> HarnessApp:
    from pydantic_ai.models.test import TestModel
    from marim_harness.agent import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="test"
    )
    return HarnessApp(harness)
```

Every app-level test below takes `tmp_path` and calls `_app(tmp_path)`. Widget-only tests (no harness) define a tiny throwaway `App` subclass as shown.

**Scope:** This plan covers **Phase 1 only** — the screen for *foreground* sub-agents. Detached/background agents appear in the list with their final report but show a "no live transcript" placeholder pane. **Phase 2** (live-streaming background jobs) begins with a feasibility spike and is a *separate plan*, written after the spike; do not attempt it here.

## Global Constraints

- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright`. Never bare `python`/`pip`/`pytest`.
- Ruff line length is **100**; lint set is `E,F,I` (import sorting enforced).
- `requires-python = ">=3.10"` — no 3.11+-only syntax.
- CI order is **ruff → pyright → pytest**; match it locally before claiming a task done. Coverage is on by default; use `--no-cov` only for fast single-test loops.
- Pyright runs in **basic** mode over `src` only.
- Tool docstrings / user-facing copy are product surface — write them deliberately.
- Preserve the existing long "why" comments on resumability and stream invariants when editing nearby code.

---

## File Structure

**New files:**
- `src/marim_harness/interfaces/tui/widgets/subagent_stats.py` — pure, side-effect-free helpers: per-agent row cells and session-summary aggregation. No Textual imports.
- `src/marim_harness/interfaces/tui/widgets/subagent_detail.py` — `SubAgentPane` (one agent's transcript container) and `SubAgentDetailHost` (`ContentSwitcher` of panes).
- `src/marim_harness/interfaces/tui/widgets/subagents_view.py` — `SubAgentSummary` (top roll-up bar) and `SubAgentsView` (the full-bleed container: summary + `[list | host]` + hint bar).
- `tests/test_subagent_stats.py`, `tests/test_subagent_detail.py`, `tests/test_subagents_screen.py`.

**Modified files:**
- `widgets/subagent.py` — slim `SubAgentWidget` to a breadcrumb; drop the body it constructs, reference a `SubAgentPane`, add click-to-open.
- `widgets/subagent_viewer.py` — rework `SubAgentList` into a `DataTable`; remove `SubAgentFooter`.
- `widgets/__init__.py` — update exports.
- `stream_render.py` — create a pane per spawn; retarget `_SubAgentSink` to the pane; re-key `_stream_hidden`/viewing on the host's current pane; refresh the view on scalar changes.
- `app.py` — compose `SubAgentsView`; rewire `action_toggle_subagents` + nav actions + bindings + focus.
- `styles.tcss` — layout for the two-pane view + focus highlight; retire footer rules.

---

## Task 1: Pure stats helpers

Side-effect-free row/summary computation, unit-tested directly with lightweight stand-ins (no app, no Textual). This is the only logic worth its own reviewer gate before any widget exists.

**Files:**
- Create: `src/marim_harness/interfaces/tui/widgets/subagent_stats.py`
- Test: `tests/test_subagent_stats.py`

**Interfaces:**
- Consumes: a duck-typed "agent view" with attributes `status: str`, `agent_type: str`, `tokens: int`, `cost_text: str | None`, `tool_count: int`, `detached: bool`, and methods `display_title() -> str`, `_duration() -> str`. `SubAgentWidget` already satisfies this.
- Produces:
  - `STATUS_GLYPH: dict[str, str]` and `status_glyph(status: str) -> str`
  - `row_cells(agent) -> list[str]` → `[glyph, "{type} — {title}", tools, tokens, cost, duration]`
  - `SummaryStats` dataclass: `total, running, done, failed, tokens, cost_text`
  - `aggregate(agents: list, cost_of=...) -> SummaryStats`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_subagent_stats.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_subagent_stats.py -q`
Expected: FAIL with `ModuleNotFoundError: ...subagent_stats`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/marim_harness/interfaces/tui/widgets/subagent_stats.py
"""Pure presentation helpers for the sub-agents screen — the per-agent list-row
cells and the session-summary aggregation. No Textual or app imports: this is
side-effect-free so it can be unit-tested directly (the I/O wiring lives in the
view widgets that call it)."""

from dataclasses import dataclass
from typing import Callable

from .format import format_cost, human_tokens

STATUS_GLYPH = {"done": "✓", "denied": "✕", "failed": "✕"}


def status_glyph(status: str) -> str:
    """The list glyph for a sub-agent status; running agents get a ▸ marker."""
    return STATUS_GLYPH.get(status, "▸")


def row_cells(agent) -> list[str]:
    """The six `DataTable` cells for one agent row: glyph, "{type} — {title}",
    tool count, tokens, cost, duration. A detached agent's tool tally is unknown
    (it never streamed its steps), so it shows "—" rather than a misleading "0"."""
    tools = "—" if agent.detached else str(agent.tool_count)
    tokens = human_tokens(agent.tokens) if agent.tokens else ""
    return [
        status_glyph(agent.status),
        f"{agent.agent_type} — {agent.display_title()}",
        tools,
        tokens,
        agent.cost_text or "",
        agent._duration(),
    ]


@dataclass
class SummaryStats:
    total: int
    running: int
    done: int
    failed: int
    tokens: int
    cost_text: str


def aggregate(agents: list, cost_of: Callable[[object], float]) -> SummaryStats:
    """Roll up the session's sub-agents for the summary bar. ``cost_of`` maps an
    agent to its dollar cost (injected so this stays free of usage/model wiring).
    A failed *or* denied agent counts as failed; everything not terminal is
    running. Cost is blank until at least one agent is metered."""
    running = done = failed = tokens = 0
    cost = 0.0
    for a in agents:
        tokens += a.tokens
        cost += cost_of(a)
        if a.status == "done":
            done += 1
        elif a.status in ("failed", "denied"):
            failed += 1
        else:
            running += 1
    return SummaryStats(
        total=len(agents),
        running=running,
        done=done,
        failed=failed,
        tokens=tokens,
        cost_text=format_cost(cost) if tokens else "",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_subagent_stats.py -q`
Expected: PASS (6 passed). Then `uv run ruff check src tests` and `uv run pyright` clean.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/subagent_stats.py tests/test_subagent_stats.py
git commit -m "feat(tui): pure stats helpers for the sub-agents screen"
```

---

## Task 2: Transcript pane + detail host

The persistent transcript home. `SubAgentPane` owns one agent's body header, usage line, and streamed transcript (what `SubAgentWidget.body` used to be). `SubAgentDetailHost` is a `ContentSwitcher` holding one pane per `stream_id`, showing exactly one at a time.

**Files:**
- Create: `src/marim_harness/interfaces/tui/widgets/subagent_detail.py`
- Test: `tests/test_subagent_detail.py`

**Interfaces:**
- Produces:
  - `pane_id(stream_id: str) -> str` — sanitizes a tool_call_id into a valid Textual id.
  - `SubAgentPane(VerticalScroll)`:
    - `__init__(stream_id: str, agent_type: str, model_label: str)`
    - `set_usage_line(detail: str) -> None`
    - `async add(widget) -> None` — mount a transcript child
    - `append_error(report: str) -> None` — mount the final failure text
    - `placeholder() -> None` — show the detached "no live transcript" note
    - attr `stream_id: str`
  - `SubAgentDetailHost(ContentSwitcher)`:
    - `add_pane(stream_id, agent_type, model_label) -> SubAgentPane`
    - `pane(stream_id) -> SubAgentPane | None`
    - `show(stream_id) -> None` — make that pane current
    - `current_sid() -> str | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_subagent_detail.py
import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from marim_harness.interfaces.tui.widgets.subagent_detail import (
    SubAgentDetailHost,
    SubAgentPane,
    pane_id,
)


def test_pane_id_sanitizes_tool_call_id():
    pid = pane_id("call/abc.123:x")
    assert pid.startswith("sap-")
    assert all(c.isalnum() or c in "-_" for c in pid)


class _Host(App):
    def compose(self) -> ComposeResult:
        yield SubAgentDetailHost()


@pytest.mark.anyio
async def test_host_adds_and_switches_panes():
    app = _Host()
    async with app.run_test() as pilot:
        host = app.query_one(SubAgentDetailHost)
        p1 = host.add_pane("call_1", "research", "sonnet")
        p2 = host.add_pane("call_2", "coding", "sonnet")
        await pilot.pause()
        assert isinstance(p1, SubAgentPane) and p1.stream_id == "call_1"
        assert host.pane("call_2") is p2
        host.show("call_1")
        await pilot.pause()
        assert host.current_sid() == "call_1"
        host.show("call_2")
        await pilot.pause()
        assert host.current_sid() == "call_2"


@pytest.mark.anyio
async def test_pane_streams_and_placeholder():
    app = _Host()
    async with app.run_test() as pilot:
        host = app.query_one(SubAgentDetailHost)
        pane = host.add_pane("call_1", "research", "sonnet")
        await pane.add(Static("hello transcript"))
        pane.set_usage_line("in 1.0k · out 0.2k · $0.01")
        await pilot.pause()
        assert "hello transcript" in app.screen.query_one(SubAgentPane).render_str("")  # mounted
        pane.placeholder()
        await pilot.pause()
        assert pane._placeholder.display is True
```

> Note: the `render_str` line just forces a query; if it's awkward in your Textual version, assert `len(pane.query(Static)) >= 2` instead (body header + the mounted child).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_subagent_detail.py -q`
Expected: FAIL with `ModuleNotFoundError: ...subagent_detail`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/marim_harness/interfaces/tui/widgets/subagent_detail.py
"""The persistent transcript home for the sub-agents screen.

Each spawned sub-agent gets one ``SubAgentPane`` — the scroll container its
streamed transcript mounts into — and all panes live in a single
``SubAgentDetailHost`` (a ``ContentSwitcher``) that shows exactly one at a time.
Because the host is mounted for the session's life (hidden until the screen is
open), the live stream keeps mounting into a pane whether or not it's on screen,
so opening the screen mid-run shows an already-current transcript. Nothing is
ever reparented."""

import re

from textual.containers import VerticalScroll
from textual.content import Content
from textual.widgets import ContentSwitcher, Static

_DETACHED_NOTE = "detached — ran in background, no live transcript"


def pane_id(stream_id: str) -> str:
    """A valid Textual widget id for a pane keyed by a spawn's tool_call_id.
    Textual ids must match ``[a-zA-Z_-][a-zA-Z0-9_-]*``; tool_call_ids can carry
    other characters, so replace them and prefix to guarantee a letter start."""
    return "sap-" + re.sub(r"[^a-zA-Z0-9_-]", "-", stream_id or "none")


class SubAgentPane(VerticalScroll):
    """One sub-agent's transcript: a muted ``◼ {type} · {model}`` header, an
    (initially hidden) usage line mirroring the status bar's split + cost, and the
    streamed transcript widgets mounted after them. Replaces the old
    ``SubAgentWidget.body``."""

    def __init__(self, stream_id: str, agent_type: str, model_label: str) -> None:
        self.stream_id = stream_id
        label = f"{agent_type} · {model_label}" if model_label else agent_type
        self._header = Static(Content(f"◼ {label}"), classes="subagent-bhead")
        self._usage_line = Static("", classes="subagent-usage")
        self._usage_line.display = False
        self._placeholder = Static(Content(_DETACHED_NOTE), classes="subagent-detached")
        self._placeholder.display = False
        super().__init__(
            self._header, self._usage_line, self._placeholder,
            id=pane_id(stream_id), classes="subagent-pane",
        )

    def set_usage_line(self, detail: str) -> None:
        self._usage_line.update(detail)
        self._usage_line.display = bool(detail)

    async def add(self, widget) -> None:
        """Mount a transcript child (sub-agent text or a nested tool call)."""
        await self.mount(widget)

    def append_error(self, report: str) -> None:
        """A failed spawn returns its error rather than streaming it; mount it so
        the transcript ends with the reason."""
        self.mount(Static(Content(report), classes="subagent-error"))

    def placeholder(self) -> None:
        """Show the 'no live transcript' note (a detached agent, pre-Phase 2)."""
        self._placeholder.display = True


class SubAgentDetailHost(ContentSwitcher):
    """A ``ContentSwitcher`` of ``SubAgentPane``s — the screen's right pane. One
    pane per ``stream_id``; ``current`` selects which is visible."""

    def add_pane(self, stream_id: str, agent_type: str, model_label: str) -> SubAgentPane:
        pane = SubAgentPane(stream_id, agent_type, model_label)
        self.mount(pane)
        return pane

    def pane(self, stream_id: str) -> SubAgentPane | None:
        pid = pane_id(stream_id)
        for p in self.query(SubAgentPane):
            if p.id == pid:
                return p
        return None

    def show(self, stream_id: str) -> None:
        self.current = pane_id(stream_id)

    def current_sid(self) -> str | None:
        for p in self.query(SubAgentPane):
            if p.id == self.current:
                return p.stream_id
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_subagent_detail.py -q`
Expected: PASS. Then ruff + pyright clean.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/subagent_detail.py tests/test_subagent_detail.py
git commit -m "feat(tui): SubAgentPane + SubAgentDetailHost transcript host"
```

---

## Task 3: Slim the breadcrumb card

Pull the transcript body out of `SubAgentWidget`. The card keeps its compact header, activity line, scalars (status/tool_count/tokens/cost/duration), and animation; it now references a `SubAgentPane` (set by the renderer) and redirects usage + error append to it. Click-to-open is added in Task 8 (needs the app action).

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/subagent.py`
- Modify: `tests/` (existing card tests, if any reference `.body`)

**Interfaces:**
- Consumes: `SubAgentPane` from Task 2.
- Produces (changed `SubAgentWidget`): drops `self.body`, `self._body_header`, `self._usage_line`, and `async add(...)`. Adds `self.pane: SubAgentPane | None = None` and `self.cost_value: float | None = None` (the numeric per-agent cost the summary sums — avoids re-parsing `cost_text`). `set_usage(...)` gains a trailing `cost_value` param; `finish(...)` keeps its signature. Both route body-side effects through `self.pane` when set.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_subagent_card.py  (new, or fold into an existing card test file)
from marim_harness.interfaces.tui.widgets.subagent import SubAgentWidget


def test_card_has_no_body_and_tolerates_no_pane():
    w = SubAgentWidget("research", "Map the codebase. Then summarize.", "sonnet")
    # The transcript body no longer lives on the card.
    assert not hasattr(w, "body")
    assert w.pane is None
    # Scalar updates must not blow up before a pane is attached.
    w.set_usage(1000, "$0.01", "in 0.8k · out 0.2k")
    assert w.tokens == 1000 and w.cost_text == "$0.01"
    w.finish("done report", status="done")
    assert w.status == "done"


def test_finish_failure_appends_to_pane_when_present():
    class _Pane:
        def __init__(self):
            self.usage = None
            self.errors = []
        def set_usage_line(self, d):
            self.usage = d
        def append_error(self, r):
            self.errors.append(r)
    w = SubAgentWidget("research", "task", "sonnet")
    w.pane = _Pane()
    w.set_usage(10, None, "in 10")
    assert w.pane.usage == "in 10"
    w.finish("Sub-agent 'x' failed: boom", status="failed")
    assert w.pane.errors == ["Sub-agent 'x' failed: boom"]
    assert w._fail_reason == "boom"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_subagent_card.py -q`
Expected: FAIL — `SubAgentWidget` still defines `body` / lacks `pane`.

- [ ] **Step 3: Edit `subagent.py`**

In `__init__`, **delete** the body construction block (lines that build `self._body_header`, `self._usage_line`, `self.body`) and change the `super().__init__` to mount only the two card lines. Add the pane reference. Replace:

```python
        # The transcript home: a scroll container kept mounted but hidden inline.
        # A muted body header carries "{type} · {model}"; the usage line mirrors the
        # status bar's split + cost and stays hidden until metered. Transcript
        # widgets (text, tool calls) mount after these via add().
        self._body_header = Static(self._body_header_text(), classes="subagent-bhead")
        self._usage_line = Static("", classes="subagent-usage")
        self._usage_line.display = False
        self.body = VerticalScroll(
            self._body_header, self._usage_line, classes="subagent-body"
        )
        self.body.display = False
        super().__init__(self._header, self._activity, self.body)
```

with:

```python
        # The transcript no longer lives on the card — it streams into a
        # SubAgentPane owned by the detail host. The renderer attaches that pane
        # here once both are created; scalar updates (usage/finish) redirect their
        # body-side effects through it. None until attached, and stays None for the
        # pure card unit tests, so every access guards on it.
        self.pane: "SubAgentPane | None" = None
        # The numeric cost of this agent's run, folded in by set_usage; the summary
        # bar sums these (rather than re-parsing the formatted cost_text).
        self.cost_value: float | None = None
        super().__init__(self._header, self._activity)
```

Add the type-only import near the top (under `TYPE_CHECKING` to avoid a cycle):

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .subagent_detail import SubAgentPane
```

Remove the now-unused `VerticalScroll` import (`from textual.containers import Vertical, VerticalScroll` → `from textual.containers import Vertical`) and delete the now-dead `_body_header_text` method.

Rewrite `set_usage` / `_refresh_usage_line` to push the detail string to the pane:

```python
    def set_usage(
        self, total: int, cost_text: str | None, split_text: str,
        cost_value: float | None = None,
    ) -> None:
        """Fold a full usage reading in: the running ``total`` (and ``cost_text``)
        ride on the card for the list row; ``cost_value`` (numeric) feeds the
        summary roll-up; the detailed ``split_text`` + cost land on the pane's usage
        line (the status-bar view, where there's room)."""
        self.cost_text = cost_text
        self.cost_value = cost_value
        self.split_text = split_text
        self.set_tokens(total)
        if self.pane is not None:
            detail = split_text
            if cost_text:
                detail = f"{detail} · {cost_text}" if detail else cost_text
            self.pane.set_usage_line(detail)
```

(Delete `_refresh_usage_line` and the `self._usage_line` reference; it no longer exists.)

Delete the `async def add` method (the renderer now mounts into the pane directly).

In `finish`, redirect the error append to the pane:

```python
        if status in ("failed", "denied") and report:
            self._fail_reason = failure_reason(report)
            self._full_reason = clean_reason(report)
            # The failure is returned, not streamed, so the transcript would
            # otherwise end without it — append it to the pane so the screen shows
            # the reason. Guard: a detached/pre-pane card has no pane yet.
            if self.pane is not None:
                self.pane.append_error(report)
```

Update the module docstring's reference to `self.body` to describe the pane instead (one sentence).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_subagent_card.py -q`
Expected: PASS. The card-level subagent tests pass; `stream_render`/app tests will be red until Tasks 4–8 — that's expected, do not "fix" them here.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/subagent.py tests/test_subagent_card.py
git commit -m "refactor(tui): slim SubAgentWidget to a breadcrumb, transcript moves to a pane"
```

---

## Task 4: Wire the renderer to the detail host

Create a pane per spawn, attach it to the card, retarget the sub-agent sink to mount into the pane, and re-key the "only flush the visible transcript" optimization on the host's current pane. This is the task that makes streaming land in the new host.

**Files:**
- Modify: `src/marim_harness/interfaces/tui/stream_render.py`
- Test: `tests/test_subagents_screen.py` (renderer-level assertions)

**Interfaces:**
- Consumes: `SubAgentDetailHost` / `SubAgentPane` (Task 2), slim `SubAgentWidget` (Task 3).
- Produces: `StreamRenderer.detail_host: SubAgentDetailHost | None` (set by the app at mount); pane creation in `_TopLevelSink.intercept_tool`; `_SubAgentSink` mounting into the pane; `_stream_hidden(widget, host)` re-keyed.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_subagents_screen.py — paste the `_app(tmp_path)` helper from the
# "Test conventions" section at the top of this file, plus:
from pathlib import Path

import pytest

from marim_harness.interfaces.tui.widgets.subagent_detail import SubAgentDetailHost


@pytest.mark.anyio
async def test_spawn_creates_pane_attached_to_card(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        widget = r.mount_spawn_widget({"type": "research", "description": "map it"})
        widget.stream_id = "call_1"
        r.tool_widgets["call_1"] = widget
        # Attaching the pane is what intercept_tool does at spawn; exercise the
        # helper the sink uses.
        pane = r.ensure_pane(widget)
        await pilot.pause()
        assert widget.pane is pane
        assert r.detail_host.pane("call_1") is pane
```

> The assertion of record: after `ensure_pane`, the card's `.pane` and `detail_host.pane(sid)` are the same object.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_subagents_screen.py -q`
Expected: FAIL — `StreamRenderer` has no `detail_host` / `ensure_pane`.

- [ ] **Step 3: Edit `stream_render.py`**

Add the import:

```python
from .widgets import SubAgentDetailHost, SubAgentPane  # noqa: F401 (SubAgentPane for typing)
```

In `StreamRenderer.__init__`, add the host reference (the app sets it at mount):

```python
        # The persistent transcript host (a ContentSwitcher of SubAgentPanes), set
        # by the app at mount. Panes are created here per spawn and attached to
        # their card; the sub-agent sink mounts each stream into its pane.
        self.detail_host: SubAgentDetailHost | None = None
```

Add a pane-creation helper:

```python
    def ensure_pane(self, widget: SubAgentWidget) -> "SubAgentPane | None":
        """Create (once) the detail-host pane for ``widget`` and attach it to the
        card. Returns the pane, or None if the host isn't mounted yet (headless /
        early calls) — callers tolerate None the way every UI callback does."""
        if self.detail_host is None:
            return None
        if widget.pane is not None:
            return widget.pane
        pane = self.detail_host.add_pane(
            widget.stream_id, widget.agent_type, widget.model_label
        )
        widget.pane = pane
        return pane
```

In `_TopLevelSink.intercept_tool`, after setting `widget.stream_id`, create the pane:

```python
        if event.part.tool_name == "spawn_agent":
            widget = self._r.mount_spawn_widget(args)
            widget.stream_id = event.part.tool_call_id
            self._r.tool_widgets[event.part.tool_call_id] = widget
            self._r.ensure_pane(widget)          # <-- add: build + attach the pane
            self.set_run(None, None)
            await self.container.mount(widget)
            return True
```

Retarget `_SubAgentSink`. Its `container` was `parent.body`; make it the pane, falling back safely:

```python
class _SubAgentSink(_StreamSink):
    def __init__(self, renderer: "StreamRenderer", parent: SubAgentWidget,
                 stream_id: str) -> None:
        self._r = renderer
        self._parent = parent
        self._sid = stream_id
        # Mount transcript widgets into the agent's pane in the detail host. The
        # pane is created at spawn; ensure_pane is idempotent and covers the rare
        # race where the sink runs before intercept_tool attached it.
        self.container = renderer.ensure_pane(parent)
```

Re-key `_stream_hidden` on the host's current pane instead of `viewing_sid`. Replace the function:

```python
def _stream_hidden(widget: Widget, host: "SubAgentDetailHost | None") -> bool:
    """True when ``widget`` is a sub-agent transcript that isn't the one currently
    on screen, so re-parsing its markdown every flush tick would be wasted work
    (and, ×N during a fan-out, would freeze the UI). A widget inside a SubAgentPane
    is hidden unless that pane is the host's current one; a top-level log stream
    (no pane ancestor) is never hidden. With no host, or no pane selected (screen
    closed), every sub-agent stream is hidden — they render the moment a pane is
    shown."""
    node = widget.parent
    while node is not None:
        if isinstance(node, SubAgentPane):
            return host is None or node.id != host.current
        node = node.parent
    return False
```

Update `flush_streams` to pass the host:

```python
            if _stream_hidden(m, self.detail_host):
                self.dirty_streams.add(m)
                continue
```

Remove the now-dead `viewing_sid` reads in `flush_streams`/`_stream_hidden`. Keep the `viewing_sid` field for now only if other code references it; Task 5 removes it. (Grep: `git grep viewing_sid` — every remaining hit is rewired in Task 5.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_subagents_screen.py -q`
Expected: PASS for the new test. Run the existing `tests/test_app*.py` — failures tied to `viewing_sid`/`SubAgentList`/footer are expected until Tasks 5–8; note them, don't fix yet.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/stream_render.py tests/test_subagents_screen.py
git commit -m "feat(tui): stream sub-agent transcripts into the detail host panes"
```

---

## Task 5: Rework the list into a DataTable + summary bar

Replace the plain-text `SubAgentList` with a `DataTable` driven by `subagent_stats`, and add the `SubAgentSummary` roll-up bar. Remove `SubAgentFooter`. Selection moves to a row cursor; the old `left/right` prev/next bindings move onto the table's `up/down` (Task 8 wires the app actions).

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/subagent_viewer.py`
- Create: `src/marim_harness/interfaces/tui/widgets/subagents_view.py` (the `SubAgentSummary` part; `SubAgentsView` lands in Task 7 — split the file work so each is testable)
- Test: `tests/test_subagents_screen.py` (extend)

**Interfaces:**
- Consumes: `row_cells`, `aggregate`, `SummaryStats` (Task 1).
- Produces:
  - `SubAgentList(DataTable)`: `refresh_rows(subagents: list, selected: int) -> None`; `selected_index() -> int`; emits Textual's `DataTable.RowHighlighted` for selection changes.
  - `SubAgentSummary(Static)`: `refresh_totals(stats: SummaryStats) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_subagents_screen.py  (append)
import pytest
from textual.app import App, ComposeResult

from marim_harness.interfaces.tui.widgets.subagent_viewer import SubAgentList
from marim_harness.interfaces.tui.widgets.subagents_view import SubAgentSummary
from marim_harness.interfaces.tui.widgets.subagent_stats import aggregate


class _ListApp(App):
    def compose(self) -> ComposeResult:
        yield SubAgentList()
        yield SubAgentSummary()


@pytest.mark.anyio
async def test_list_rows_and_summary(monkeypatch):
    from tests.test_subagent_stats import FakeAgent  # reuse the stand-in
    agents = [
        FakeAgent(status="pending", agent_type="research", tool_count=2, tokens=100),
        FakeAgent(status="done", agent_type="coding", tokens=200, cost_text="$0.02"),
    ]
    app = _ListApp()
    async with app.run_test() as pilot:
        lst = app.query_one(SubAgentList)
        lst.refresh_rows(agents, selected=1)
        summ = app.query_one(SubAgentSummary)
        summ.refresh_totals(aggregate(agents, cost_of=lambda a: 0.0))
        await pilot.pause()
        assert lst.row_count == 2
        assert lst.selected_index() == 1
        # summary text mentions the running/done split and total agents
        rendered = str(summ.renderable)
        assert "2" in rendered  # total agents
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_subagents_screen.py -q`
Expected: FAIL — `SubAgentList` isn't a `DataTable` / `SubAgentSummary` missing.

- [ ] **Step 3: Rewrite `subagent_viewer.py`**

Replace the whole file with the `DataTable` list (drop `SubAgentFooter`):

```python
"""The sub-agents screen's master list: one row per spawned sub-agent, with live
status/stats columns. Pure presentation — the app drives it via ``refresh_rows``
and reads the cursor via ``selected_index``; row selection (the DataTable cursor)
chooses which transcript the detail host shows."""

from textual.widgets import DataTable

from .subagent_stats import row_cells

_COLUMNS = ("", "agent", "tools", "tokens", "cost", "dur")


class SubAgentList(DataTable):
    """The left pane: a focusable row-cursor table of session sub-agents."""

    def __init__(self) -> None:
        super().__init__(id="subagent-list", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        for c in _COLUMNS:
            self.add_column(c, key=c)

    def refresh_rows(self, subagents: list, selected: int) -> None:
        """Rebuild every row from ``subagents`` and place the cursor on
        ``selected``. N is the session's sub-agent count (small), so a full rebuild
        per change is cheap and avoids per-cell key bookkeeping."""
        self.clear()
        for w in subagents:
            self.add_row(*row_cells(w))
        if self.row_count:
            self.move_cursor(row=max(0, min(selected, self.row_count - 1)))

    def selected_index(self) -> int:
        return self.cursor_row
```

Create the summary widget (its own file, shared with the Task 7 view):

```python
# src/marim_harness/interfaces/tui/widgets/subagents_view.py
"""The full-bleed sub-agents screen: a session summary bar, the agent list, and
the transcript detail host. (The container ``SubAgentsView`` is added in a later
step; this module starts with the summary bar so it can be tested on its own.)"""

from textual.content import Content
from textual.widgets import Static

from .subagent_stats import SummaryStats


class SubAgentSummary(Static):
    """The top roll-up bar: total agents (running/done/failed) + summed tokens and
    cost across the session's sub-agents."""

    def __init__(self) -> None:
        super().__init__(id="subagent-summary")

    def refresh_totals(self, stats: SummaryStats) -> None:
        left = (
            f"{stats.total} sub-agents · "
            f"{stats.running} running · {stats.done} done · {stats.failed} failed"
        )
        right = f"{stats.tokens:,} tokens"
        if stats.cost_text:
            right = f"{right} · {stats.cost_text}"
        self.update(Content(f"{left}    {right}"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_subagents_screen.py -q`
Expected: PASS. ruff + pyright clean.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/subagent_viewer.py src/marim_harness/interfaces/tui/widgets/subagents_view.py tests/test_subagents_screen.py
git commit -m "feat(tui): DataTable agent list + session summary bar"
```

---

## Task 6: Update the widgets package exports

Re-point `widgets/__init__.py` so `from .widgets import X` keeps working with the new/changed symbols, and `SubAgentFooter` is gone.

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/__init__.py`

**Interfaces:**
- Produces: `SubAgentList`, `SubAgentPane`, `SubAgentDetailHost`, `SubAgentSummary`, `SubAgentsView` (added Task 7) exported; `SubAgentFooter` removed.

- [ ] **Step 1: Edit the imports/exports**

Replace:

```python
from .subagent import SubAgentWidget
from .subagent_viewer import SubAgentFooter, SubAgentList
```

with:

```python
from .subagent import SubAgentWidget
from .subagent_detail import SubAgentDetailHost, SubAgentPane
from .subagent_viewer import SubAgentList
from .subagents_view import SubAgentSummary  # SubAgentsView added in Task 7
```

In `__all__`, replace the `"SubAgentList", "SubAgentFooter",` block under `# sub-agent` with:

```python
    # sub-agent
    "SubAgentWidget",
    "SubAgentList",
    "SubAgentPane",
    "SubAgentDetailHost",
    "SubAgentSummary",
```

- [ ] **Step 2: Verify imports resolve**

Run: `uv run python -c "from marim_harness.interfaces.tui import widgets; print(widgets.SubAgentDetailHost, widgets.SubAgentSummary)"`
Expected: prints the two classes, no `ImportError`.

- [ ] **Step 3: Run ruff (import sorting) + pyright**

Run: `uv run ruff check src tests && uv run pyright`
Expected: clean (fix any unused/order issues ruff flags).

- [ ] **Step 4: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/__init__.py
git commit -m "refactor(tui): export the new sub-agent screen widgets, drop SubAgentFooter"
```

---

## Task 7: The full-bleed SubAgentsView container

Assemble the screen body: `SubAgentSummary` on top, `[ SubAgentList | SubAgentDetailHost ]` in the middle, a hint bar on the bottom. It owns the in-view key bindings (`escape`/`ctrl+x` close, `tab` switch focus) and exposes `refresh(subagents, selected)`.

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/subagents_view.py` (add `SubAgentsView`)
- Modify: `src/marim_harness/interfaces/tui/widgets/__init__.py` (export `SubAgentsView`)
- Test: `tests/test_subagents_screen.py` (extend)

**Interfaces:**
- Consumes: `SubAgentList`, `SubAgentDetailHost`, `SubAgentSummary`, `aggregate`.
- Produces: `SubAgentsView(Vertical)`:
  - `compose()` yielding summary + a horizontal `[list | host]` + hint `Static`
  - `refresh(subagents: list, selected: int, cost_of) -> None` — repaint summary + rows
  - holds `list: SubAgentList`, `host: SubAgentDetailHost` as queried children
  - `BINDINGS`: `escape`/`ctrl+x` → `app.close_subagents`; `tab`/`shift+tab` → focus toggle

- [ ] **Step 1: Write the failing test**

```python
# tests/test_subagents_screen.py  (append)
from marim_harness.interfaces.tui.widgets.subagents_view import SubAgentsView


@pytest.mark.anyio
async def test_view_refresh_paints_list_and_host():
    from tests.test_subagent_stats import FakeAgent

    class _ViewApp(App):
        def compose(self):
            yield SubAgentsView()

    agents = [FakeAgent(status="done", tokens=100)]
    app = _ViewApp()
    async with app.run_test() as pilot:
        view = app.query_one(SubAgentsView)
        view.refresh(agents, selected=0, cost_of=lambda a: 0.0)
        await pilot.pause()
        assert view.list.row_count == 1
        assert "1 sub-agents" in str(view.query_one(SubAgentSummary).renderable)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_subagents_screen.py::test_view_refresh_paints_list_and_host -q`
Expected: FAIL — `SubAgentsView` undefined.

- [ ] **Step 3: Add `SubAgentsView` to `subagents_view.py`**

```python
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from .subagent_detail import SubAgentDetailHost
from .subagent_stats import aggregate
from .subagent_viewer import SubAgentList

_HINTS = "Esc back · ↑↓ select · Tab switch pane"


class SubAgentsView(Vertical):
    """The full-bleed sub-agents screen. Hidden until ``ctrl+x``; when shown it
    covers the main log (the app toggles ``display`` and focus). Owns the in-view
    bindings and a ``refresh`` that repaints the summary + list from the renderer's
    ``subagents`` list."""

    BINDINGS = [
        Binding("escape", "app.close_subagents", "Back", show=False),
        Binding("ctrl+x", "app.close_subagents", "Close", show=False),
        Binding("tab", "focus_next_pane", "Switch pane", show=False),
        Binding("shift+tab", "focus_next_pane", "Switch pane", show=False),
    ]

    def __init__(self) -> None:
        super().__init__(id="subagents-view")
        self.display = False

    def compose(self):
        yield SubAgentSummary()
        with Horizontal(id="subagents-body"):
            yield SubAgentList()
            yield SubAgentDetailHost(id="subagent-detail-host")
        yield Static(_HINTS, id="subagent-hints")

    @property
    def list(self) -> SubAgentList:
        return self.query_one(SubAgentList)

    @property
    def host(self) -> SubAgentDetailHost:
        return self.query_one(SubAgentDetailHost)

    def refresh(self, subagents: list, selected: int, cost_of) -> None:
        self.query_one(SubAgentSummary).refresh_totals(aggregate(subagents, cost_of))
        self.list.refresh_rows(subagents, selected)

    def action_focus_next_pane(self) -> None:
        """Toggle focus between the list and the visible transcript pane."""
        if self.list.has_focus:
            try:
                self.host.query_one(f"#{self.host.current}").focus()
            except Exception:
                pass
        else:
            self.list.focus()
```

> `Vertical.refresh` shadows Textual's `Widget.refresh`; that's intentional here since the app only calls our version, but if pyright/ruff object, rename to `repaint(...)` and update call sites in Task 8.

Export it in `widgets/__init__.py`: add `SubAgentsView` to the `from .subagents_view import ...` line and to `__all__`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_subagents_screen.py -q`
Expected: PASS. ruff + pyright clean.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/subagents_view.py src/marim_harness/interfaces/tui/widgets/__init__.py tests/test_subagents_screen.py
git commit -m "feat(tui): SubAgentsView full-bleed container (summary + list + host)"
```

---

## Task 8: Rewire the app — compose, toggle, navigation, focus

Mount `SubAgentsView` (and point the renderer at its host), rewrite `action_toggle_subagents` and the nav/close actions to drive the new view, refresh it live on selection and on scalar changes, and add click-to-open on the breadcrumb card.

**Files:**
- Modify: `src/marim_harness/interfaces/tui/app.py`
- Modify: `src/marim_harness/interfaces/tui/widgets/subagent.py` (click handler)
- Modify: `src/marim_harness/interfaces/tui/stream_render.py` (live refresh hook; drop `viewing_sid`)
- Test: `tests/test_subagents_screen.py` (extend with an app-level toggle test)

**Interfaces:**
- Consumes: `SubAgentsView`, `SubAgentDetailHost`.
- Produces: `HarnessApp.open_subagents_at(stream_id: str | None)`, `action_close_subagents`, `refresh_subagents_view()`, `subagent_cost(widget) -> float`; renderer `self.app.refresh_subagents_view()` calls.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_subagents_screen.py  (append) — app-level toggle
@pytest.mark.anyio
async def test_ctrl_x_opens_view_and_shows_selected_transcript(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        w = r.mount_spawn_widget({"type": "research", "description": "map it"})
        w.stream_id = "call_1"
        r.tool_widgets["call_1"] = w
        r.ensure_pane(w)
        await app.query_one("#log").mount(w)
        await pilot.pause()

        await pilot.press("ctrl+x")
        await pilot.pause()
        view = app.query_one("SubAgentsView")
        assert view.display is True
        assert app.query_one("#log").display is False
        assert view.host.current_sid() == "call_1"

        await pilot.press("escape")
        await pilot.pause()
        assert view.display is False
        assert app.query_one("#log").display is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_subagents_screen.py::test_ctrl_x_opens_view_and_shows_selected_transcript -q`
Expected: FAIL — old `SubAgentList`/footer compose + actions.

- [ ] **Step 3: Edit `app.py`**

Imports: drop `SubAgentFooter, SubAgentList` from the `.widgets` import; add `SubAgentsView`. Drop `from textual.css.query import NoMatches` only if it becomes unused (check first).

`compose()`: replace the two viewer-chrome yields with the view, and hide it by default. Replace:

```python
        # The full-screen sub-agent viewer chrome (hidden until ctrl+x). ...
        yield SubAgentList()
        yield SubAgentFooter(id="subagent-footer")
        yield Footer()
```

with:

```python
        # The full-bleed sub-agents screen (hidden until ctrl+x). Its detail host
        # owns the live transcript panes; the renderer mounts each spawn's stream
        # into them whether or not the screen is open, so opening mid-run shows an
        # already-current transcript.
        yield SubAgentsView()
        yield Footer()
```

In `on_mount` (early, before the first `flush_streams`), point the renderer at the host:

```python
        # Hand the renderer the persistent transcript host so spawns create their
        # panes there.
        self.stream.detail_host = self.query_one(SubAgentsView).host
```

Replace the viewer action block (`action_toggle_subagents` … `_subagent_spend`) with:

```python
    # --- Sub-agents screen (ctrl+x) ---

    def action_toggle_subagents(self) -> None:
        """Ctrl+X: open the full-bleed sub-agents screen (or close it if open)."""
        if self.subagent_viewer_open:
            self._close_subagents()
        else:
            self.open_subagents_at(None)

    def action_close_subagents(self) -> None:
        self._close_subagents()

    def open_subagents_at(self, stream_id: str | None) -> None:
        """Open the screen, selecting ``stream_id`` (or the most recent spawn when
        None — the one you most likely just watched)."""
        subs = self.stream.subagents
        if not subs:
            self.query_one("#log", VerticalScroll).mount(
                NoticeMessage("No sub-agents spawned yet — nothing to view.")
            )
            return
        index = len(subs) - 1
        if stream_id is not None:
            index = next(
                (i for i, w in enumerate(subs) if w.stream_id == stream_id), index
            )
        self.subagent_viewer_open = True
        self.subagent_index = index
        view = self.query_one(SubAgentsView)
        self.query_one("#log", VerticalScroll).display = False
        view.display = True
        self._apply_subagent_view()
        view.list.focus()

    def _close_subagents(self) -> None:
        self.subagent_viewer_open = False
        self.query_one(SubAgentsView).display = False
        self.query_one("#log", VerticalScroll).display = True
        self.query_one(PromptInput).focus()

    def _apply_subagent_view(self) -> None:
        """Repaint the list/summary and show the selected agent's pane. Clamps the
        index and closes the screen if the list emptied."""
        subs = self.stream.subagents
        if not subs:
            self._close_subagents()
            return
        self.subagent_index = max(0, min(self.subagent_index, len(subs) - 1))
        current = subs[self.subagent_index]
        view = self.query_one(SubAgentsView)
        view.refresh(subs, self.subagent_index, self.subagent_cost)
        if current.pane is not None:
            view.host.show(current.stream_id)
        # Render the just-shown transcript now (its stream was skipped while it
        # wasn't the host's current pane).
        self.stream.flush_streams()

    def refresh_subagents_view(self) -> None:
        """Repaint the screen if it's open — called from the renderer when a card's
        scalars change (tool call, usage, finish) so the list ticks live. A no-op
        when closed, so streaming pays nothing for a hidden screen."""
        if self.subagent_viewer_open:
            self._apply_subagent_view()

    def subagent_cost(self, widget) -> float:
        """The dollar cost of one sub-agent for the summary roll-up — the numeric
        cost the renderer already computed via resolve_cost and stored on the card
        (cost_value). 0.0 until metered. No re-costing here: resolve_cost needs the
        full RunUsage split, which the card doesn't keep."""
        return widget.cost_value or 0.0
```

Wire the `RowHighlighted` selection → pane switch. Add a handler on the app:

```python
    def on_data_table_row_highlighted(self, event) -> None:
        """Moving the list cursor selects that agent's transcript."""
        if self.subagent_viewer_open and event.cursor_row is not None:
            self.subagent_index = event.cursor_row
            current = self.stream.subagents[self.subagent_index]
            if current.pane is not None:
                self.query_one(SubAgentsView).host.show(current.stream_id)
            self.stream.flush_streams()
```

Delete the now-dead `action_subagent_prev` / `action_subagent_next` (the DataTable cursor owns up/down) unless other bindings reference them.

- [ ] **Step 4: Edit `subagent.py` — click-to-open**

The card's `on_click` currently only expands a failed reason. Extend it to open the screen at this agent for a non-failed card:

```python
    def on_click(self, _event) -> None:
        if self.status in ("failed", "denied") and self._full_reason != self._fail_reason:
            self._expanded = not self._expanded
            self._paint_activity()
            return
        # Otherwise, a click jumps into the sub-agents screen focused on this card.
        opener = getattr(self.app, "open_subagents_at", None)
        if opener is not None:
            opener(self.stream_id)
```

- [ ] **Step 5: Edit `stream_render.py` — live refresh + drop `viewing_sid`**

After the card scalar mutations in `on_subagent_event` (the `set_usage` call) and in the `FunctionToolResultEvent` finish branch, ask the app to repaint the screen:

```python
        # ... after parent.set_usage(...) in on_subagent_event:
        self.app.refresh_subagents_view()
```

```python
        # ... after widget.finish(...) for a SubAgentWidget in dispatch_stream_event:
        if isinstance(widget, SubAgentWidget):
            self.app.refresh_subagents_view()
```

Also call it after `note_tool` lands — simplest is to refresh once per sub-agent event at the end of `on_subagent_event`:

```python
    async def on_subagent_event(self, stream_id: str, event, usage=None) -> None:
        parent = self.tool_widgets.get(stream_id)
        if not isinstance(parent, SubAgentWidget):
            return
        if usage is not None and usage.total_tokens:
            cost, _ = resolve_cost(usage, self.app.harness.model_id)
            cost_text = _format_cost(cost) if cost is not None else None
            parent.set_usage(
                usage.total_tokens, cost_text, _format_token_split(usage),
                cost_value=cost,  # numeric cost for the summary roll-up
            )
        await self.dispatch_stream_event(event, _SubAgentSink(self, parent, stream_id))
        self.app.refresh_subagents_view()  # list/summary tick live while open
```

Remove the `self.viewing_sid` field and its assignments in `__init__`/`reset`, and `fill_finished_detached_cards` should also call `self.app.refresh_subagents_view()` so a settling background job updates the open list. (Grep `git grep viewing_sid` returns nothing after this.)

For detached panes: in `note_detached_spawn`, after marking `widget.detached`, show the placeholder if a pane exists:

```python
        widget.detached = True
        widget.activity = "running in background…"
        if widget.pane is not None:
            widget.pane.placeholder()
        self._detached_cards[job_id] = widget
```

- [ ] **Step 6: Run the test + suite**

Run: `uv run pytest --no-cov tests/test_subagents_screen.py -q` → PASS.
Then the full suite: `uv run pytest -q`. Fix any remaining references to `SubAgentFooter`/`viewing_sid`/`.body`/`subagent_prev` in existing tests (update them to the new API — the *behavior* they assert should now go through the view/host).

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/interfaces/tui/app.py src/marim_harness/interfaces/tui/widgets/subagent.py src/marim_harness/interfaces/tui/stream_render.py tests/test_subagents_screen.py
git commit -m "feat(tui): drive the sub-agents screen from ctrl+x with live list + click-to-open"
```

---

## Task 9: Styles

Lay out the full-bleed view (summary / `[list | host]` / hints), give the list a sensible width and the focused zone a visible highlight, and remove the dead footer/`.viewing`/`.subagent-body` rules.

**Files:**
- Modify: `src/marim_harness/interfaces/tui/styles.tcss`

- [ ] **Step 1: Remove dead rules**

Delete the `.subagent-body`, `.subagent-body.viewing`, `#subagent-list`, and `#subagent-footer` rule blocks (lines around the current 77–101) and the now-unused `subagents`/`subagents-top` layer names from the `Screen { layers: ... }` line if nothing else uses them (grep `layer:` first).

- [ ] **Step 2: Add the view layout**

```css
/* The full-bleed sub-agents screen (ctrl+x). Hidden via display in Python; when
   shown it covers the log. */
#subagents-view {
    display: none;            /* shown by the app */
    width: 100%;
    height: 100%;
    layer: overlay;           /* sits above the log; reuse an existing top layer */
    background: $surface;
}
#subagent-summary { height: 1; padding: 0 1; color: $text-muted; background: $panel; }
#subagents-body { height: 1fr; }
#subagent-list { width: 42; border-right: solid $panel; }
#subagent-list:focus { border-right: solid $accent; }
.subagent-pane { height: 1fr; padding: 0 1; }
.subagent-pane:focus { background: $boost; }
#subagent-hints { height: 1; padding: 0 1; color: $text-muted; background: $panel; }
.subagent-detached { color: $text-muted; text-style: italic; margin: 1 0; }
```

> If `styles.tcss` has no `overlay` layer, add one to the `Screen { layers: ... }` line (e.g. `layers: base overlay;`) or dock the view; match whatever layering convention the file already uses.

- [ ] **Step 3: Smoke-run the app**

Run the TUI (`uv run marim` in a scratch dir), spawn a sub-agent (e.g. ask it to delegate), press `ctrl+x`. Confirm: the view covers the log, the list shows the agent with stats, the transcript renders on the right, `Tab` moves focus (list border → accent), `↑/↓` selects, `Esc` returns. This is a manual check; the automated gate is Task 10.

- [ ] **Step 4: Commit**

```bash
git add src/marim_harness/interfaces/tui/styles.tcss
git commit -m "style(tui): layout for the full-bleed sub-agents screen"
```

---

## Task 10: Integration test + full gate + manual key verification

A Pilot end-to-end test for the live path, the full CI gate, and a manual tmux check that the real terminal delivers `ctrl+x` (Pilot can't prove that).

**Files:**
- Test: `tests/test_subagents_screen.py` (final integration case)

- [ ] **Step 1: Write the integration test**

```python
# tests/test_subagents_screen.py  (append)
import pytest
from textual.widgets import Static

from marim_harness.interfaces.tui.widgets.subagent import SubAgentWidget


@pytest.mark.anyio
async def test_live_stream_then_open_shows_current_transcript(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        w = r.mount_spawn_widget({"type": "research", "description": "map it"})
        w.stream_id = "call_1"
        r.tool_widgets["call_1"] = w
        r.ensure_pane(w)
        await app.query_one("#log").mount(w)
        # Stream while the screen is CLOSED: content lands in the pane regardless.
        await w.pane.add(Static("first line"))
        w.note_tool("read_file", {"path": "a.py"})
        await pilot.pause()
        # Open: the list shows the agent with its live tool count; transcript is current.
        await pilot.press("ctrl+x")
        await pilot.pause()
        view = app.query_one("SubAgentsView")
        assert view.list.row_count == 1
        assert view.host.current_sid() == "call_1"
        assert len(w.pane.query(Static)) >= 2  # body header + streamed line
        # Finish updates the row glyph live while open.
        w.finish("ok", status="done")
        app.refresh_subagents_view()
        await pilot.pause()
        assert w.status == "done"
```

- [ ] **Step 2: Run the new test**

Run: `uv run pytest --no-cov tests/test_subagents_screen.py -q`
Expected: PASS.

- [ ] **Step 3: Full CI gate (the real done-bar)**

Run, in order:
```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```
Expected: ruff clean, pyright clean, all tests pass (coverage on). Fix anything red before continuing.

- [ ] **Step 4: Manual tmux key-delivery check**

Pilot simulates the *action*, not the *keypress*. Verify the real terminal delivers `ctrl+x` (Kitty keyboard protocol can swallow/remap it):

```bash
# in a tmux pane
uv run marim
# get the agent to spawn a sub-agent (e.g. "delegate a quick file read to a sub-agent"),
# then physically press Ctrl+X.
```
Expected: the screen opens. If it doesn't, the binding is being intercepted at the terminal layer (a real bug, separate from the action working in tests) — note it and check the Kitty-protocol handling before claiming the feature done. (See the project memory note on Ctrl+G/steer keys for precedent.)

- [ ] **Step 5: Final commit**

```bash
git add tests/test_subagents_screen.py
git commit -m "test(tui): end-to-end sub-agents screen live-stream + open"
```

---

## Out of scope — Phase 2 (separate plan)

Live-streaming **background/detached** sub-agents into the host. Per the design, this starts with a **feasibility spike** (how does a detached job's output/stats reach the TUI process — subscribable event stream vs. file/buffer tail vs. final-report-only?) and only then a build. Until that plan exists, detached agents correctly appear in the list with their final report and a "no live transcript" pane placeholder. Do not implement Phase 2 as part of this plan.

---

## Self-Review

- **Spec coverage:** full-bleed two-pane screen (Tasks 7–9) ✓; fully live via persistent host + on-screen-only flush (Tasks 2, 4, 8) ✓; unified list incl. detached with placeholder (Tasks 5, 8) ✓; stats — status/duration/tools/activity/tokens/cost/model/title + session roll-up (Tasks 1, 5, 7) ✓; breadcrumb cards kept + clickable (Tasks 3, 8) ✓; discoverability via hint bar + click (Tasks 7, 9) ✓; testing split pure/Pilot + tmux caveat (Tasks 1, 10) ✓; Phase 2 deferred to a spike ✓.
- **Type consistency:** `ensure_pane`/`pane`/`add_pane`/`show`/`current_sid` names match across Tasks 2, 4, 8; `refresh_rows(subagents, selected)`, `refresh_totals(stats)`, `view.refresh(subagents, selected, cost_of)`, `aggregate(agents, cost_of=...)` are consistent. **Cost source resolved:** `set_usage(..., cost_value=cost)` stores the numeric cost on the card (Task 3); `subagent_cost(w)` returns `w.cost_value or 0.0` (Task 8); `aggregate`'s `cost_of` consumes it. No re-costing, one source.
- **Test conventions resolved:** all async tests use `@pytest.mark.anyio`; app-level tests use the local `_app(tmp_path)` helper (mirrors `tests/test_app.py:11`), not a non-existent `tests.helpers`. `resolve_cost(usage: RunUsage, model_ref)` is only called where a real `RunUsage` exists (the renderer), never reconstructed from a bare token count.
- **Open risk flagged inline (genuine API checks, not placeholders):** `Vertical.refresh` name shadow (Task 7 note — rename to `repaint` if pyright objects); `DataTable.move_cursor`/`cursor_row` and `ContentSwitcher.current` are the live Textual APIs used — verify against the installed Textual version on first run and adjust if the minor version renamed them.
