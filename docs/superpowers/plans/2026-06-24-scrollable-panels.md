# Scrollable Jobs & Tasks Panels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the Jobs and Tasks TUI panels scrollable with a count badge in the header.

**Architecture:** Upgrade both panels from `Static` to `VerticalScroll` containers with pinned header + scrollable body. CSS switches from `height: auto; max-height: 8` to fixed `height: 8`.

**Tech Stack:** Textual, Python

## Global Constraints
- Python 3.14, Textual TUI framework
- Follow existing code patterns in `src/marim_harness/interfaces/tui/`
- No new dependencies

---

### Task 1: Update CSS for scrollable panel layout

**Files:**
- Modify: `src/marim_harness/interfaces/tui/styles.tcss:15-23`

- [ ] **Step 1: Replace panel CSS rules**

Replace lines 15-23 in `styles.tcss`:

```css
#task-panel {
    height: 8; background: $panel; color: $text;
    padding: 0 1; border-top: tall $background;
}
#task-header { height: 1; }
#task-body { height: 1fr; }

#job-panel {
    height: 8; background: $panel; color: $text;
    padding: 0 1; border-top: tall $background;
}
#job-header { height: 1; }
#job-body { height: 1fr; }
```

- [ ] **Step 2: Commit**

```bash
git add src/marim_harness/interfaces/tui/styles.tcss
git commit -m "style(tui): make jobs/tasks panels fixed-height with scrollable body"
```

---

### Task 2: Upgrade TaskPanel to VerticalScroll container

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/panels.py:8-28`

**Interfaces:**
- Consumes: `render_tasks(items)` from `src/marim_harness/tasks.py` (returns `str`)
- Produces: `TaskPanel.show_tasks(items: list)` — same signature, new internals

- [ ] **Step 1: Rewrite TaskPanel**

```python
from textual.containers import VerticalScroll
from textual.widgets import Static
from textual.content import Content


class TaskPanel(VerticalScroll):
    """The agent's live checklist, pinned above the status bar. Hidden whenever
    the list is empty so it takes no space when unused."""

    def __init__(self) -> None:
        super().__init__(id="task-panel")
        self.display = False
        self._header = Static(id="task-header")
        self._body = Static(id="task-body")
        self.mount(self._header, self._body)

    def show_tasks(self, items: list) -> None:
        """Render the current checklist, or hide the panel when there are none."""
        from ....tasks import render_tasks

        if not items:
            self.display = False
            self._header.update("")
            self._body.update("")
            return
        self.display = True
        count = len(items)
        self._header.update(
            Content.from_markup(f"[b $accent]Tasks[/] [dim]({count})[/]")
        )
        self._body.update(Content(render_tasks(items)))
```

- [ ] **Step 2: Run existing tests**

Run: `uv run python -m pytest tests/ -q --override-ini="addopts="`
Expected: All pass (no behavior change yet — panel just renders differently)

- [ ] **Step 3: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/panels.py
git commit -m "feat(tui): upgrade TaskPanel to scrollable VerticalScroll with count badge"
```

---

### Task 3: Upgrade JobPanel to VerticalScroll container

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/panels.py:31-51`

**Interfaces:**
- Consumes: `render_jobs(jobs)` from `src/marim_harness/jobs.py` (returns `str`)
- Produces: `JobPanel.show_jobs(jobs: list)` — same signature, new internals

- [ ] **Step 1: Rewrite JobPanel**

```python
class JobPanel(VerticalScroll):
    """The session's live background jobs, pinned above the status bar. Hidden
    whenever there are no jobs."""

    def __init__(self) -> None:
        super().__init__(id="job-panel")
        self.display = False
        self._header = Static(id="job-header")
        self._body = Static(id="job-body")
        self.mount(self._header, self._body)

    def show_jobs(self, jobs: list) -> None:
        """Render the current jobs, or hide the panel when there are none."""
        from ....jobs import render_jobs

        if not jobs:
            self.display = False
            self._header.update("")
            self._body.update("")
            return
        self.display = True
        count = len(jobs)
        self._header.update(
            Content.from_markup(f"[b $accent]Jobs[/] [dim]({count})[/]")
        )
        self._body.update(Content(render_jobs(jobs)))
```

- [ ] **Step 2: Run full test suite**

Run: `uv run python -m pytest --override-ini="addopts=" -q`
Expected: All 1226 tests pass

- [ ] **Step 3: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/panels.py
git commit -m "feat(tui): upgrade JobPanel to scrollable VerticalScroll with count badge"
```

---

### Task 4: Verify and clean up

- [ ] **Step 1: Run full test suite one final time**

Run: `uv run python -m pytest --override-ini="addopts=" -q`
Expected: All 1226 pass, no new warnings

- [ ] **Step 2: Manual smoke test**

Launch the TUI, trigger several jobs/tasks, verify:
- Count badge shows correct number
- Scroll appears when hovering over a full panel
- Panels hide when empty
- Header stays pinned while body scrolls

- [ ] **Step 3: Final commit if any cleanup needed**
