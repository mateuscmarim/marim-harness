# Tool & tool-group rendering redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render every tool call through one shared `summarize()` helper so tool rows, tool-group headers, and sub-agent cards read consistently as `{glyph} {Label} · {target}  {badges}`, and make finished tool groups fold to a one-line summary.

**Architecture:** Introduce `interfaces/tui/widgets/tool_summary.py` — a pure module with a per-tool descriptor registry that turns `(tool_name, args)` into a `ToolSummary(label, target, badges)`. The three renderers (`ToolCallWidget`, `ToolGroupWidget`, `SubAgentWidget` card) all build their display from it, deleting the old arg-count branch. Tool groups gain a finish-collapse driven from the stream renderer's result handler.

**Tech Stack:** Python ≥3.10, Textual 8.2.7, `textual.content.Content`, pytest (coverage ≥90%).

## Global Constraints

- Python `>=3.10` — no 3.11+-only syntax (the spec / `requires-python`).
- Use `uv` for everything: `uv run pytest`, `uv run ruff check`, `uv run pyright`. Never bare `python`/`pip`/`pytest`.
- Ruff line length 100; lint set `E,F,I` (import sorting enforced).
- CI order, run locally before claiming done: `uv run ruff check src tests` → `uv run pyright` → `uv run pytest`. Coverage gate is 90%.
- Untrusted tool args/results are **never** markup-parsed: render targets via literal `Content(...)` / plain-string args to `Content.assemble`, never `Content.from_markup`.
- Commit message trailer (every commit):
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01NtRbyuNFL1mJyx5DnoBotL
  ```

---

### Task 1: `tool_summary.py` — the shared summary helper

**Files:**
- Create: `src/marim_harness/interfaces/tui/widgets/tool_summary.py`
- Modify: `src/marim_harness/interfaces/tui/widgets/subagent.py:23-39` (drop the local `_TOOL_LABELS` + `humanize_tool`, import them from the new module)
- Test: `tests/test_tool_summary.py`

**Interfaces:**
- Produces:
  - `humanize_tool(name: str) -> str`
  - `@dataclass(frozen=True) class ToolSummary: label: str; target: str; badges: tuple[str, ...] = ()`
  - `summarize(tool_name: str, args: dict, *, cap: int = 100) -> ToolSummary`
  - `_clip(text: str, limit: int = 100) -> str`, `_clip_middle(text: str, limit: int) -> str`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tool_summary.py`:

```python
from marim_harness.interfaces.tui.widgets.tool_summary import (
    ToolSummary,
    _clip_middle,
    humanize_tool,
    summarize,
)


def test_single_arg_tool_targets_its_value():
    s = summarize("read_file", {"path": ".marim/test_output.txt"})
    assert s == ToolSummary(label="Read", target=".marim/test_output.txt", badges=())


def test_multi_arg_tool_uses_registered_target_not_repr():
    # The old code rendered this as wait_for_job(id='job-6', timeout=600).
    s = summarize("wait_for_job", {"id": "job-6", "timeout": 600})
    assert s.label == "Wait"
    assert s.target == "job-6"
    assert s.badges == ()  # timeout is dropped as default-noise


def test_bash_background_becomes_a_badge():
    s = summarize("bash", {"command": "uv run pytest", "background": True})
    assert s.label == "Bash"
    assert s.target == "uv run pytest"
    assert s.badges == ("bg",)


def test_bash_command_clips_middle_keeping_the_tail():
    cmd = "uv run pytest --no-cov -q 2>&1 | grep -E '^[0-9]+ ' | tail -1"
    s = summarize("bash", {"command": cmd}, cap=30)
    assert s.target.startswith("uv run pytest")
    assert s.target.endswith("tail -1")
    assert "…" in s.target and len(s.target) <= 30


def test_grep_path_becomes_an_in_badge():
    s = summarize("grep", {"pattern": "build_harness", "path": "src/"})
    assert s.label == "Grep"
    assert s.target == "build_harness"
    assert s.badges == ("in src/",)


def test_unknown_tool_falls_back_to_humanized_name_and_first_arg():
    s = summarize("frobnicate_thing", {"widget": "gizmo", "level": 9})
    assert s.label == "Frobnicate Thing"
    assert s.target == "gizmo"
    assert s.badges == ()


def test_empty_args_gives_label_only():
    s = summarize("tree", {})
    assert s == ToolSummary(label="Tree", target="", badges=())


def test_humanize_tool_maps_known_and_titlecases_unknown():
    assert humanize_tool("read_file") == "Read"
    assert humanize_tool("spawn_agent") == "Spawn Agent"


def test_clip_middle_noop_when_short():
    assert _clip_middle("short", 30) == "short"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_tool_summary.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named '...tool_summary'`.

- [ ] **Step 3: Create the module**

Create `src/marim_harness/interfaces/tui/widgets/tool_summary.py`:

```python
"""One shared tool-call summary used by every renderer (the main ``ToolCallWidget``
row, the ``ToolGroupWidget`` header, and the sub-agent card's ``↳`` line) so a call
reads the same everywhere: ``{Label} · {target}  {badges}``.

Each tool resolves to a humanized verb (``Read``/``Bash``/``Wait``), the *salient*
argument as the target (the command/path/pattern/id — picked per tool, not by
position or arg-count), and zero or more compact badges for the flags that would
otherwise be noise (``bg`` for a backgrounded bash; ``in <path>`` for a scoped
grep). Unknown tools fall back to a title-cased name + their first meaningful arg,
so nothing ever degrades to raw ``key=value`` repr."""

from dataclasses import dataclass

# Default cap for the main tool-row target; the sub-agent card passes a tighter cap.
_PREVIEW_CAP = 100

# Friendly verbs; unknown tools title-case their raw name (spawn_agent → "Spawn Agent").
_TOOL_LABELS = {
    "read_file": "Read", "write_file": "Write", "edit_file": "Edit", "bash": "Bash",
    "grep": "Grep", "glob": "Glob", "tree": "Tree", "web_search": "Search",
    "fetch_url": "Fetch", "wait_for_job": "Wait", "spawn_agent": "Spawn",
    "goto_definition": "Definition", "find_references": "References", "hover": "Hover",
    "document_symbols": "Symbols", "workspace_symbols": "Symbols",
    "diagnostics": "Diagnostics",
}

# The salient argument per tool — the one worth showing as the target. Tools absent
# here use the generic "first meaningful arg" fallback.
_TARGET_ARG = {
    "read_file": "path", "write_file": "path", "edit_file": "path",
    "bash": "command", "grep": "pattern", "glob": "pattern", "tree": "path",
    "wait_for_job": "id", "web_search": "query", "fetch_url": "url",
}


def humanize_tool(name: str) -> str:
    """A short, friendly verb for a tool call (``read_file`` → ``Read``)."""
    return _TOOL_LABELS.get(name) or name.replace("_", " ").title()


def _clip(text: str, limit: int = _PREVIEW_CAP) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clip_middle(text: str, limit: int) -> str:
    """Clip from the middle, keeping head and tail — for shell pipelines whose tail
    (``… | tail -1``) is as informative as their head."""
    if len(text) <= limit:
        return text
    keep = limit - 1  # one char for the ellipsis
    tail = keep // 3
    head = keep - tail
    return text[:head] + "…" + (text[-tail:] if tail else "")


@dataclass(frozen=True)
class ToolSummary:
    label: str
    target: str
    badges: tuple[str, ...] = ()


def _meaningful(args: dict) -> list:
    return [v for v in args.values() if v not in (None, "", [], {})]


def _raw_target(tool_name: str, args: dict) -> str:
    key = _TARGET_ARG.get(tool_name)
    if key is not None:
        v = args.get(key)
        if v not in (None, "", [], {}):
            return " ".join(str(v).split())
    items = _meaningful(args)
    return " ".join(str(items[0]).split()) if items else ""


def _badges(tool_name: str, args: dict) -> tuple[str, ...]:
    out: list[str] = []
    if tool_name == "bash" and args.get("background"):
        out.append("bg")
    if tool_name == "grep" and args.get("path"):
        out.append(f"in {args['path']}")
    return tuple(out)


def summarize(tool_name: str, args: dict, *, cap: int = _PREVIEW_CAP) -> ToolSummary:
    raw = _raw_target(tool_name, args)
    clip = _clip_middle if tool_name == "bash" else _clip
    return ToolSummary(
        label=humanize_tool(tool_name),
        target=clip(raw, cap),
        badges=_badges(tool_name, args),
    )
```

- [ ] **Step 4: Point subagent.py at the shared `humanize_tool`**

In `src/marim_harness/interfaces/tui/widgets/subagent.py`, delete the `_TOOL_LABELS` dict (lines 27-34) and the `humanize_tool` function (lines 37-39), and add to the imports near the top (after the existing `from textual.widgets import Static`):

```python
from .tool_summary import humanize_tool
```

(Leave `note_tool`'s body using `humanize_tool` for now — it is rewired in Task 4.)

- [ ] **Step 5: Run tests + lint + types**

Run: `uv run pytest tests/test_tool_summary.py -q && uv run pytest tests/test_widgets.py -q && uv run ruff check src tests && uv run pyright`
Expected: all PASS (subagent still imports `humanize_tool`, now from `tool_summary`).

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/tool_summary.py \
        src/marim_harness/interfaces/tui/widgets/subagent.py \
        tests/test_tool_summary.py
git commit -m "feat(tui): add shared tool-summary helper (label/target/badges)

$(printf 'Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>\nClaude-Session: https://claude.ai/code/session_01NtRbyuNFL1mJyx5DnoBotL')"
```

---

### Task 2: `ToolCallWidget` renders via `summarize()` + animated glyph

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/tools.py` (imports; `_clip`/`_PREVIEW_CAP` removal lines 18-30; `_summary`/`_summary_body` lines 68-89; add `_glyph`, `on_mount`, `_tick`; `finish` line 201-212)
- Test: `tests/test_widgets.py`

**Interfaces:**
- Consumes: `summarize`, `ToolSummary` from `tool_summary`; `_SPINNER`, `_SPINNER_TICK_INTERVAL` from `..status`.
- Produces: `ToolCallWidget._summary() -> Content` (unchanged signature); `_glyph() -> tuple[str, str]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_widgets.py` (near the other `ToolCallWidget` tests):

```python
async def test_toolcall_header_uses_summary_shape():
    from textual.app import App
    from marim_harness.interfaces.tui.widgets import ToolCallWidget

    class _A(App):
        def compose(self):
            yield ToolCallWidget("wait_for_job", {"id": "job-6", "timeout": 600})

    app = _A()
    async with app.run_test():
        w = app.query_one(ToolCallWidget)
        # No raw repr — the registered target only, no key= / quotes / timeout.
        assert "Wait · job-6" in w.title.plain
        assert "timeout" not in w.title.plain
        assert "id='job-6'" not in w.title.plain


async def test_toolcall_bash_background_shows_bg_badge():
    from textual.app import App
    from marim_harness.interfaces.tui.widgets import ToolCallWidget

    class _A(App):
        def compose(self):
            yield ToolCallWidget("bash", {"command": "uv run pytest", "background": True})

    app = _A()
    async with app.run_test():
        w = app.query_one(ToolCallWidget)
        assert "Bash · uv run pytest" in w.title.plain
        assert "bg" in w.title.plain


def test_toolcall_pending_glyph_is_spinner_done_is_check():
    from marim_harness.interfaces.tui.status import _SPINNER
    from marim_harness.interfaces.tui.widgets import ToolCallWidget

    w = ToolCallWidget("read_file", {"path": "a.py"})
    assert w._glyph()[0] == _SPINNER[0]  # pending → spinner frame, not "·"
    w.status = "done"
    assert w._glyph()[0] == "✓"
    w.status = "failed"
    assert w._glyph()[0] == "✗"
```

Also update the existing edit_file title assertion (search `test_widgets.py` for `edit_file(` — the test that asserts the old `edit_file(path) +N -M` title). Change the expected substring from `f"edit_file({path})"` to `f"Edit · {path}"` (the `+N -M` suffix stays).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_widgets.py -k "toolcall or edit_file" -q`
Expected: FAIL — current headers are `wait_for_job(id='job-6', timeout=600)` and `_glyph` does not exist.

- [ ] **Step 3: Rewrite the header rendering**

In `src/marim_harness/interfaces/tui/widgets/tools.py`:

Replace the imports block / `_PREVIEW_CAP` / `_clip` (lines 18-30) — delete `_PREVIEW_CAP` and `_clip` (now in `tool_summary`), and add imports:

```python
from ..status import _SPINNER, _SPINNER_TICK_INTERVAL
from .tool_summary import summarize
```

Replace `_summary` and `_summary_body` (lines 68-89) with:

```python
    def _glyph(self) -> tuple[str, str]:
        """The status glyph and its style: an animated spinner while pending (so
        ``·`` is freed up to mean 'separator' only), then ✓/✕/✗."""
        if self.status == "failed":
            return "✗", _FAIL_FG
        if self.status == "denied":
            return "✕", ""
        if self.status == "done":
            return "✓", ""
        return _SPINNER[self._spin], ""

    def _summary(self) -> Content:
        glyph, gstyle = self._glyph()
        s = summarize(self.tool_name, self.args)
        target = s.target
        # edit_file appends a +N -M line stat to its path (the diff is the body).
        if self.tool_name == "edit_file":
            _, added, removed = self._edit_diff(cap=None)
            target = f"{target} +{added} -{removed}" if target else f"+{added} -{removed}"
        head = f"{s.label} · {target}" if target else s.label
        # Glyph carries the status colour; the (untrusted) head is a literal span so
        # markup in a path/command is never parsed; badges trail dim.
        parts: list = [(f"{glyph} ", gstyle), head]
        for b in s.badges:
            parts.extend(("   ", (b, "dim")))
        return Content.assemble(*parts)
```

Add `self._spin = 0` in `__init__` (after `self.status = "pending"`, line 46), then add the spinner lifecycle methods (place them right after `_summary`):

```python
    def on_mount(self) -> None:
        # Animate the working glyph while pending; the timer is stopped at finish so
        # a finished session isn't left with hundreds of 10Hz no-op ticks.
        self._spinner_timer = self.set_interval(_SPINNER_TICK_INTERVAL, self._tick)

    def _tick(self) -> None:
        if self.status != "pending":
            return
        self._spin = (self._spin + 1) % len(_SPINNER)
        self.title = self._summary()
```

In `finish` (lines 201-212), after the existing body update, stop the timer. Append at the end of `finish`:

```python
        timer = getattr(self, "_spinner_timer", None)
        if timer is not None:
            timer.stop()
```

- [ ] **Step 4: Run tests + lint + types**

Run: `uv run pytest tests/test_widgets.py -q && uv run ruff check src tests && uv run pyright`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/tools.py tests/test_widgets.py
git commit -m "feat(tui): render tool rows via summarize() with animated glyph

$(printf 'Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>\nClaude-Session: https://claude.ai/code/session_01NtRbyuNFL1mJyx5DnoBotL')"
```

---

### Task 3: `ToolGroupWidget` humanized header + fold-on-finish

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/tools.py` (`ToolGroupWidget` lines 215-251)
- Modify: `src/marim_harness/interfaces/tui/stream_render.py` (result handler lines 476-491; add `_group_of` helper)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `humanize_tool` from `tool_summary`; `format_duration` from `..status`.
- Produces: `ToolGroupWidget.note_child_finished(self, failed: bool = False) -> None`; `StreamRenderer._group_of(widget) -> ToolGroupWidget | None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_app.py`:

```python
async def test_tool_group_folds_to_summary_when_all_children_finish():
    from textual.app import App
    from marim_harness.interfaces.tui.widgets import ToolCallWidget, ToolGroupWidget

    class _A(App):
        def compose(self):
            yield ToolGroupWidget()

    app = _A()
    async with app.run_test():
        g = app.query_one(ToolGroupWidget)
        a = ToolCallWidget("read_file", {"path": "a.py"})
        b = ToolCallWidget("read_file", {"path": "b.py"})
        await g.add_tool(a)
        await g.add_tool(b)
        # Header humanizes names with a multiplier; open while running.
        assert "Read ×2" in g.title.plain
        assert g.collapsed is False
        g.note_child_finished()
        assert g.collapsed is False  # one child still pending
        g.note_child_finished()
        assert g.collapsed is True   # all done → fold
        assert "·" in g.title.plain  # duration appended


async def test_tool_group_with_failed_child_stays_open():
    from textual.app import App
    from marim_harness.interfaces.tui.widgets import ToolCallWidget, ToolGroupWidget

    class _A(App):
        def compose(self):
            yield ToolGroupWidget()

    app = _A()
    async with app.run_test():
        g = app.query_one(ToolGroupWidget)
        await g.add_tool(ToolCallWidget("bash", {"command": "false"}))
        await g.add_tool(ToolCallWidget("read_file", {"path": "a.py"}))
        g.note_child_finished(failed=True)
        g.note_child_finished()
        assert g.collapsed is False  # an error must stay visible
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_app.py -k "tool_group_folds or failed_child" -q`
Expected: FAIL — `note_child_finished` does not exist; header shows `read_file ×2` (not humanized); group starts collapsed.

- [ ] **Step 3: Implement the group changes**

In `tools.py`, add to the imports added in Task 2:

```python
import time
from ..status import _SPINNER, _SPINNER_TICK_INTERVAL, format_duration
```

(`time` goes with the stdlib imports at the top, next to `import re`.)

Replace `ToolGroupWidget.__init__` and `_summary` (lines 224-242) with:

```python
    def __init__(self) -> None:
        # Insertion-ordered count per tool name, for the title breakdown.
        self._counts: dict[str, int] = {}
        self._finished = 0
        self._any_failed = False
        self._t0 = time.monotonic()
        self._t_end: float | None = None
        self.body = Vertical(classes="tool-group-body")
        # Open while the run is in flight (live rows visible); folds on finish.
        super().__init__(
            self.body, title=self._summary(), collapsed=False  # pyright: ignore[reportArgumentType]
        )

    def _summary(self) -> Content:
        total = sum(self._counts.values())
        label = "1 tool" if total == 1 else f"{total} tools"
        # "Read ×3 · Grep" — humanized, multiplier only when it repeats.
        parts = [
            f"{humanize_tool(n)} ×{c}" if c > 1 else humanize_tool(n)
            for n, c in self._counts.items()
        ]
        breakdown = " · ".join(parts)
        text = f"≡ {label} · {breakdown}" if breakdown else f"≡ {label}"
        if self._t_end is not None:
            text = f"{text} · {format_duration(self._t_end - self._t0)}"
        return Content(text)

    def note_child_finished(self, failed: bool = False) -> None:
        """A child call reached a terminal status. Once every child is done, freeze
        the duration into the header and fold the group — unless a child failed, in
        which case stay open so the error stays visible."""
        self._finished += 1
        self._any_failed = self._any_failed or failed
        if self._finished >= sum(self._counts.values()):
            self._t_end = time.monotonic()
            self.title = self._summary()
            self.collapsed = not self._any_failed
```

(Update the class docstring's "Starts collapsed" sentence to "Starts expanded while the run is live and folds to a one-line summary once every child finishes.")

In `stream_render.py`, add a helper method on `StreamRenderer` (near `add_tool_to_run`):

```python
    def _group_of(self, widget) -> ToolGroupWidget | None:
        """The ToolGroupWidget a tool widget lives in, if any (its body's parent)."""
        node = widget.parent
        while node is not None:
            if isinstance(node, ToolGroupWidget):
                return node
            node = node.parent
        return None
```

In the `FunctionToolResultEvent` branch (lines 476-491), after `widget.finish(content, status=status)` and inside the `if widget is not None:` block, add:

```python
                if isinstance(widget, ToolCallWidget):
                    group = self._group_of(widget)
                    if group is not None:
                        group.note_child_finished(failed=status == "failed")
```

- [ ] **Step 4: Run tests + lint + types**

Run: `uv run pytest tests/test_app.py tests/test_widgets.py -q && uv run ruff check src tests && uv run pyright`
Expected: PASS. If a pre-existing group test asserts `collapsed is True` at creation or the lowercase `read_file ×2` header, update it to the new expanded-while-running / humanized header behavior (search `test_app.py` around lines 2153-2206).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/tools.py \
        src/marim_harness/interfaces/tui/stream_render.py tests/test_app.py
git commit -m "feat(tui): fold finished tool groups to a one-line summary

$(printf 'Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>\nClaude-Session: https://claude.ai/code/session_01NtRbyuNFL1mJyx5DnoBotL')"
```

---

### Task 4: Sub-agent card `↳` line via `summarize()`; remove `tool_preview`

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/subagent.py` (`note_tool` lines 233-238)
- Modify: `src/marim_harness/interfaces/tui/widgets/format.py` (delete `tool_preview` + its `_PREVIEW_CAP`, lines 3-16)
- Modify: `src/marim_harness/interfaces/tui/widgets/__init__.py` (drop `tool_preview` from the `format` import and `__all__`)
- Modify: `src/marim_harness/interfaces/tui/stream_render.py` (line 27 import; `_SubAgentSink.on_tool` line 220-221)
- Test: `tests/test_widgets.py`

**Interfaces:**
- Consumes: `summarize` from `tool_summary`.
- Produces: `SubAgentWidget.note_tool(self, tool_name: str = "", args: dict | None = None) -> None` (2nd param changes from `preview: str` to `args: dict`).

- [ ] **Step 1: Update the card tests to the new signature/shape**

In `tests/test_widgets.py`, the existing `note_tool` calls pass a preview string (lines ~668-706). Change them to pass args dicts and assert the unified shape. Replace those calls:

```python
        w.note_tool("read_file", {"path": "src/foo.py"})
        assert "Read · src/foo.py" in w._activity.render().plain
        w.note_tool("grep", {"pattern": "needle"})
        assert "Grep · needle" in w._activity.render().plain
        w.note_tool("bash", {"command": "ls", "background": True})
        assert "Bash · ls" in w._activity.render().plain
        assert "bg" in w._activity.render().plain
```

(Keep the surrounding test setup; only the `note_tool(...)` lines and their assertions change. For any assertion that read the old `"Grep needle"` space-joined form, switch to the `·` form above.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_widgets.py -k "note_tool or activity" -q`
Expected: FAIL — `note_tool` still expects a preview string; activity reads `Grep needle` (space, not `·`), no badges.

- [ ] **Step 3: Rewire `note_tool`**

In `subagent.py`, add to imports: `from .tool_summary import humanize_tool, summarize` (replace the Task-1 `from .tool_summary import humanize_tool` line). Replace `note_tool` (lines 233-238):

```python
    def note_tool(self, tool_name: str = "", args: dict | None = None) -> None:
        """Record that the sub-agent just called ``tool_name`` (with its ``args``):
        bump the tally and show it as the current tool on the ↳ line, using the same
        ``label · target  badges`` shape as the main log."""
        self.tool_count += 1
        s = summarize(tool_name, args or {}, cap=60)
        line = f"{s.label} · {s.target}" if s.target else s.label
        if s.badges:
            line = f"{line}  {' '.join(s.badges)}"
        self.activity = line
        self._paint_activity()
```

- [ ] **Step 4: Remove `tool_preview` and fix its callers**

In `format.py`, delete the module-level `_PREVIEW_CAP` (lines 3-5) and the `tool_preview` function (lines 8-16). Leave the token/cost helpers.

In `widgets/__init__.py`, change the format import line to:
```python
from .format import format_cost, format_token_split, human_tokens
```
and remove `"tool_preview",` from `__all__`.

In `stream_render.py`: remove `tool_preview,` from the widgets import (line 27), and change `_SubAgentSink.on_tool` (lines 220-221) to:

```python
    def on_tool(self, tool_name: str, args: dict) -> None:
        self._parent.note_tool(tool_name, args)
```

- [ ] **Step 5: Confirm no stray `tool_preview` references remain**

Run: `grep -rn "tool_preview" src tests`
Expected: no output. (If any remain, update them — they should all be gone.)

- [ ] **Step 6: Run tests + lint + types**

Run: `uv run pytest -q && uv run ruff check src tests && uv run pyright`
Expected: PASS (full suite, coverage ≥90%).

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/subagent.py \
        src/marim_harness/interfaces/tui/widgets/format.py \
        src/marim_harness/interfaces/tui/widgets/__init__.py \
        src/marim_harness/interfaces/tui/stream_render.py tests/test_widgets.py
git commit -m "refactor(tui): sub-agent card uses summarize(); drop tool_preview

$(printf 'Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>\nClaude-Session: https://claude.ai/code/session_01NtRbyuNFL1mJyx5DnoBotL')"
```

---

### Task 5: Transparent tool rows matching the sub-agent cards

**Files:**
- Modify: `src/marim_harness/interfaces/tui/styles.tcss` (the `ToolCallWidget` rule at line 40 and the `ToolGroupWidget` rules at lines 94-101)

**Interfaces:** none (CSS only).

- [ ] **Step 1: Strip the Collapsible band**

In `styles.tcss`, update the tool widget rules so both families render transparent with only the `border-left` rail (matching the sub-agent card). Replace line 40 and add group rules:

```css
ToolCallWidget { margin: 0 0 1 0; border-left: tall $primary; padding-left: 1; background: transparent; }
ToolCallWidget > CollapsibleTitle, ToolGroupWidget > CollapsibleTitle { background: transparent; }
ToolGroupWidget { background: transparent; }
```

Keep the existing `.tool-group-body` rules (lines 94-101) as-is.

- [ ] **Step 2: Manual verification (CSS is not unit-tested)**

Run `uv run marim`, then drive a turn that fans out tools (e.g. ask it to read a few files and run a command). Confirm:
- Tool rows and groups have **no** filled background band — just the left accent rail, matching the sub-agent cards directly above/below them.
- A running group shows live rows with spinners; when the run finishes it folds to one line `▶ ≡ N tools · … · 1.4s`.
- A `bash(..., background=True)` row reads `✓ Bash · <cmd>   bg` with the badge dim.

If a band persists, the offending selector is Textual's `CollapsibleTitle` default — widen the rule to `ToolCallWidget CollapsibleTitle, ToolGroupWidget CollapsibleTitle { background: transparent; }` and re-check.

- [ ] **Step 3: Final CI pass**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: all PASS, coverage ≥90%.

- [ ] **Step 4: Commit**

```bash
git add src/marim_harness/interfaces/tui/styles.tcss
git commit -m "style(tui): transparent tool rows to match sub-agent cards

$(printf 'Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>\nClaude-Session: https://claude.ai/code/session_01NtRbyuNFL1mJyx5DnoBotL')"
```

---

## Self-Review

**Spec coverage:**
- Spec §1 (one summary helper) → Task 1 (`tool_summary.py`), consumed in Tasks 2-4.
- Spec §2 (descriptor registry, generic fallback, middle-clip bash, markup safety) → Task 1 (`_TARGET_ARG`, `_badges`, `_clip_middle`, fallback) + literal `Content` in Task 2.
- Spec §3 (status-only glyph: spinner pending / ✓ / ✕ / ✗; `·` freed; dim badges; transparent background) → Task 2 (`_glyph`, dim badge spans) + Task 5 (transparent).
- Spec §4 (group humanized header + run-open / fold-on-finish + aggregate duration; lone call stays bare) → Task 3 (group changes leave `add_tool_to_run` promotion untouched).
- Spec §5 (edges: failed ✗ literal body; empty-args label-only; gated re-emit dedup; tests; CI order) → Task 2 (failed/empty), existing `tool_widgets` dedup untouched, Tasks 1-4 tests, every task ends on CI order.

**Placeholder scan:** No TBD/TODO/"handle edge cases"/"add validation" — every code step shows complete code; every test step shows full test bodies.

**Type consistency:** `summarize(tool_name, args, *, cap=100) -> ToolSummary` is defined in Task 1 and called with that exact signature in Tasks 2 (`summarize(self.tool_name, self.args)`), 3 (via `humanize_tool`), and 4 (`summarize(tool_name, args or {}, cap=60)`). `ToolSummary(label, target, badges)` fields are read consistently. `note_child_finished(failed=...)` defined in Task 3 and called with `failed=status == "failed"` in the same task. `note_tool(tool_name, args)` redefined in Task 4 with its sole caller (`_SubAgentSink.on_tool`) updated in the same task. `_SPINNER`/`_SPINNER_TICK_INTERVAL`/`format_duration` are imported from `..status` (verified to exist at `status.py:20-21,29`).
