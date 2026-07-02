# Inline Interaction Panels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert `AskUserModal` and `ApprovalModal` into inline panel widgets mounted above the status bar, so the transcript stays visible and scrollable while the agent waits for user input.

**Architecture:** A new `InteractionPanel` base widget (a `Vertical` carrying an `asyncio.Future` and priority scroll-forwarding bindings) plus a `run_panel` helper that mounts a panel above `#status-bar`, awaits its future, and always removes it in a `finally`. The two modals become subclasses of that base; `app._ask_user` / `app._request_approval` swap `push_screen_wait(Modal)` for `run_panel(self, Panel)`. No modal layer means mouse wheel over the transcript just works.

**Tech Stack:** Python ≥3.10, Textual 8.2.7 (`textual>=0.80` floor), pytest + anyio + Textual Pilot, uv.

**Spec:** `docs/superpowers/specs/2026-07-01-inline-interaction-panels-design.md`

## Global Constraints

- Use `uv` for everything: `uv run pytest …`, `uv run ruff …`, `uv run pyright`. Never bare `python`/`pytest`/`pip`.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM` (import sorting enforced).
- `requires-python >=3.10` — no 3.11+-only syntax.
- Verification order before claiming done: `uv run ruff check src tests` → `uv run pyright` → `uv run pytest`.
- Result contracts must not change: ask-user resolves `dict | None`, approval resolves `bool`; the runtime layer (`Deps.ui` callbacks) is untouched.
- Preserve the existing long "why" comments when editing nearby code (CLAUDE.md convention).

---

### Task 1: `InteractionPanel` base + `run_panel` helper

**Files:**
- Create: `src/marim_harness/interfaces/tui/interaction_panel.py`
- Test: `tests/test_interaction_panel.py`

**Interfaces:**
- Consumes: nothing new (Textual only).
- Produces:
  - `class InteractionPanel(Vertical)` — attributes: `result: asyncio.Future`; methods: `resolve(value) -> None`, `action_scroll_transcript(direction: str) -> None`. Priority bindings: `pageup`/`pagedown`/`ctrl+up`/`ctrl+down` → transcript scroll.
  - `async def run_panel(app, panel) -> Any` — mounts `panel` before `#status-bar`, awaits `panel.result`, removes the panel in a `finally`, restores prior focus. Tasks 2–3 rely on these exact names.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_interaction_panel.py`:

```python
"""The InteractionPanel base: future-resolution lifecycle, teardown on worker
cancel, and scroll-key forwarding to the transcript."""

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from marim_harness.interfaces.tui.interaction_panel import InteractionPanel, run_panel


class _PanelApp(App):
    """Minimal stand-in for the main screen: a scrollable #log above a
    #status-bar, matching where run_panel mounts panels in the real app."""

    def __init__(self) -> None:
        super().__init__()
        self.result = "unset"
        self.panel = InteractionPanel()
        self.worker = None

    def compose(self) -> ComposeResult:
        yield VerticalScroll(Static("line\n" * 200), id="log")
        yield Static("status", id="status-bar")

    def on_mount(self) -> None:
        self.worker = self.run_worker(self._ask())

    async def _ask(self) -> None:
        self.result = await run_panel(self, self.panel)


@pytest.mark.anyio
async def test_resolve_returns_value_and_removes_panel():
    app = _PanelApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.panel.is_attached  # mounted while pending
        app.panel.resolve({"answer": 42})
        await pilot.pause()
        assert app.result == {"answer": 42}
        assert not app.panel.is_attached  # removed after resolution


@pytest.mark.anyio
async def test_panel_mounts_above_status_bar():
    app = _PanelApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        children = list(app.screen.children)
        assert children.index(app.panel) < children.index(
            app.query_one("#status-bar")
        )


@pytest.mark.anyio
async def test_worker_cancel_removes_panel():
    app = _PanelApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.panel.is_attached
        app.worker.cancel()
        await pilot.pause()
        assert not app.panel.is_attached
        assert app.result == "unset"  # never resolved


@pytest.mark.anyio
async def test_double_resolve_is_harmless():
    app = _PanelApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.panel.resolve("first")
        app.panel.resolve("second")  # must not raise InvalidStateError
        await pilot.pause()
        assert app.result == "first"


@pytest.mark.anyio
async def test_scroll_keys_forward_to_transcript():
    app = _PanelApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        # Focus inside the panel: scroll keys must still reach the transcript.
        app.panel.can_focus = True
        app.panel.focus()
        await pilot.pause()
        log = app.query_one("#log", VerticalScroll)
        assert log.scroll_y == 0
        await pilot.press("pagedown")
        await pilot.pause()
        assert log.scroll_y > 0
        await pilot.press("pageup")
        await pilot.pause()
        assert log.scroll_y == 0
        await pilot.press("ctrl+down")
        await pilot.pause()
        assert log.scroll_y > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_interaction_panel.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.interfaces.tui.interaction_panel'`

- [ ] **Step 3: Write the implementation**

Create `src/marim_harness/interfaces/tui/interaction_panel.py`:

```python
"""Inline interaction panels: widgets mounted above the status bar that put a
question to the user while the turn worker awaits the answer. Unlike the
ModalScreens they replaced, the transcript stays visible and scrollable —
mouse wheel reaches it natively (no modal layer eats events), and the panel
forwards PageUp/PageDown and Ctrl+Up/Down to it for keyboard users.

The awaiting side goes through :func:`run_panel`: mount, await the panel's
``result`` future, and always remove the panel in a ``finally`` — so a turn
cancelled while a panel is up (Esc/Ctrl-C) tears it down too."""

import asyncio
from typing import Any

from textual.app import App
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll


class InteractionPanel(Vertical):
    """Base for the inline ask-user/approval panels.

    Owns the ``result`` future the turn worker awaits; subclasses call
    :meth:`resolve` everywhere the old modals called ``dismiss``."""

    # priority=True: the focused child (OptionList/SelectionList) has its own
    # PageUp/PageDown bindings for paging options, which would otherwise shadow
    # these. While a panel is up, paging is for reading the transcript the
    # question refers to — that's the whole point of being inline.
    BINDINGS = [
        Binding("pageup", "scroll_transcript('page_up')", "Scroll transcript",
                priority=True, show=False),
        Binding("pagedown", "scroll_transcript('page_down')", "Scroll transcript",
                priority=True, show=False),
        Binding("ctrl+up", "scroll_transcript('up')", "Scroll transcript",
                priority=True, show=False),
        Binding("ctrl+down", "scroll_transcript('down')", "Scroll transcript",
                priority=True, show=False),
    ]

    DEFAULT_CSS = """
    InteractionPanel {
        height: auto;
        max-height: 50%;
        padding: 1 2;
        background: $surface;
        border: round $accent;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        # Panels are always constructed inside the app's event loop (the turn
        # worker or a test coroutine), so get_running_loop is safe and avoids
        # get_event_loop's 3.12 deprecation path.
        self.result: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

    def resolve(self, value: Any) -> None:
        """Resolve the awaited future once; later calls are no-ops (a double
        Enter/click must not raise InvalidStateError)."""
        if not self.result.done():
            self.result.set_result(value)

    def action_scroll_transcript(self, direction: str) -> None:
        """Forward a scroll key to the transcript. ``direction`` is the suffix
        of the Widget scroll method: page_up / page_down / up / down.
        animate=False so the position is deterministic for tests and snappy
        for readers."""
        log = self.app.query_one("#log", VerticalScroll)
        getattr(log, f"scroll_{direction}")(animate=False)


async def run_panel(app: App, panel: InteractionPanel) -> Any:
    """Mount ``panel`` above the status bar, await its result, remove it.

    Removal lives in a ``finally`` and is deliberately not awaited: when the
    turn worker is cancelled the CancelledError propagates out of the result
    await, and awaiting the removal here could be interrupted by that same
    cancellation — scheduling it is enough. Focus is restored to whatever had
    it before the panel appeared (the modals got this for free from screen
    push/pop)."""
    previous = app.focused
    await app.mount(panel, before="#status-bar")
    try:
        return await panel.result
    finally:
        panel.remove()
        if previous is not None and previous.is_attached:
            previous.focus()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_interaction_panel.py -v`
Expected: 5 passed

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/interfaces/tui/interaction_panel.py tests/test_interaction_panel.py
git commit -m "feat(tui): InteractionPanel base and run_panel helper for inline prompts"
```

---

### Task 2: Convert `AskUserModal` → `AskUserPanel`

**Files:**
- Modify: `src/marim_harness/interfaces/tui/ask_user.py` (full rewrite below)
- Modify: `src/marim_harness/interfaces/tui/app.py` (import line 19; `_ask_user` at ~line 804)
- Modify: `tests/test_app.py` (~line 3097, test rename only)
- Rename + modify: `tests/test_ask_user_modal.py` → `tests/test_ask_user_panel.py`

**Interfaces:**
- Consumes: `InteractionPanel`, `run_panel` from Task 1; `Question`/`Choice` from `marim_harness.ask_user` (unchanged).
- Produces: `class AskUserPanel(InteractionPanel)` — constructor `AskUserPanel(questions: list[Question])`; resolves its `result` future with `dict | None` (`{header: str | list[str]}`, or `None` on Escape). App wiring for ask-user is completed inside this task so the commit stays green (app.py imports the class at module load — renaming it without rewiring would break every test that imports app.py).

- [ ] **Step 1: Rename the test file and rewrite it against the panel API**

```bash
git mv tests/test_ask_user_modal.py tests/test_ask_user_panel.py
```

Replace the harness class and imports at the top of `tests/test_ask_user_panel.py` (all existing test bodies stay, with the two adjustments noted below):

```python
# tests/test_ask_user_panel.py
import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Button, SelectionList, Static

from marim_harness.ask_user import Choice, Question
from marim_harness.interfaces.tui.ask_user import AskUserPanel
from marim_harness.interfaces.tui.interaction_panel import run_panel


class _Harness(App):
    """Mimics the main screen's stack: scrollable #log, then #status-bar —
    run_panel mounts the panel between them."""

    def __init__(self, questions):
        super().__init__()
        self._questions = questions
        self.result = "unset"

    def compose(self) -> ComposeResult:
        yield VerticalScroll(Static("line\n" * 100), id="log")
        yield Static("", id="status-bar")

    def on_mount(self) -> None:
        self.run_worker(self._ask())

    async def _ask(self) -> None:
        self.result = await run_panel(self, AskUserPanel(self._questions))
```

Adjustments to the existing test bodies:
1. Every `app.screen.query_one(...)` → `app.query_one(...)`, and delete the
   comment about Textual 8.x modal screen querying (the panel lives in the
   base screen now, so the workaround is obsolete).
2. No other body changes — key presses, clicks, and expected results are
   identical.

Append two new tests at the end of the file:

```python
@pytest.mark.anyio
async def test_transcript_scrolls_while_question_pending():
    """The whole point of the panel: with focus on the panel's OptionList,
    PageDown scrolls the transcript (priority binding beats the OptionList's
    own paging) and the question stays pending."""
    qs = [Question("Pick one", "Pick", [Choice("Alpha"), Choice("Beta")])]
    app = _Harness(qs)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        log = app.query_one("#log", VerticalScroll)
        assert log.scroll_y == 0
        await pilot.press("pagedown")
        await pilot.pause()
        assert log.scroll_y > 0
        assert app.result == "unset"


@pytest.mark.anyio
async def test_panel_removed_after_answer():
    qs = [Question("Pick one", "Pick", [Choice("Alpha")])]
    app = _Harness(qs)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert not app.query(AskUserPanel)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_ask_user_panel.py -v`
Expected: FAIL — `ImportError: cannot import name 'AskUserPanel'`

- [ ] **Step 3: Rewrite `src/marim_harness/interfaces/tui/ask_user.py`**

Full new content (the question-stepping logic is byte-for-byte the modal's;
what changes: base class, CSS placement, `dismiss(...)` → `self.resolve(...)`,
and the `#ask-box` wrapper dissolves into the panel itself):

```python
"""The inline panel behind the ``ask_user`` tool: steps the user through a
prompt's questions one at a time and resolves with a ``{header: answer}``
mapping (or None if cancelled). Single-select uses an OptionList; multi-select
a SelectionList with a Confirm button; a free-text Input is always visible so
"Other" is offered on every question. Mounted above the status bar (not a
modal) so the transcript stays scrollable while the question is pending."""


from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Input, OptionList, SelectionList, Static
from textual.widgets.option_list import Option

from ...ask_user import Choice, Question
from .interaction_panel import InteractionPanel


def _option_prompt(choice: Choice) -> Text:
    """An option's rendered prompt: the label, with any description dim beneath."""
    text = Text(choice.label)
    if choice.description:
        text.append(f"\n  {choice.description}", style="dim")
    return text


class AskUserPanel(InteractionPanel):
    """Resolves with ``{header: str | list[str]}`` for every question, or None
    if the user pressed Escape."""

    DEFAULT_CSS = """
    #ask-progress {
        color: $text-muted;
    }
    #ask-question {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #ask-body {
        height: auto;
        max-height: 18;
    }
    #ask-other-label {
        color: $text-muted;
        margin-top: 1;
    }
    #ask-confirm {
        margin-top: 1;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, questions: list[Question]) -> None:
        super().__init__()
        self._questions = questions
        self._index = 0
        self._answers: dict = {}

    def compose(self) -> ComposeResult:
        yield Static("", id="ask-progress")
        yield Static("", id="ask-question")
        yield Vertical(id="ask-body")
        yield Static("Or type your own answer:", id="ask-other-label")
        yield Input(placeholder="type a custom answer…", id="ask-other")
        yield Button("Confirm selection", id="ask-confirm", variant="primary")

    def on_mount(self) -> None:
        self.run_worker(self._show_question())

    async def _show_question(self) -> None:
        """Render the current question: progress line, prompt, the option widget
        (OptionList for single-select, SelectionList for multi), and toggle the
        Confirm button (multi-select only)."""
        q = self._questions[self._index]
        total = len(self._questions)
        progress = f"Question {self._index + 1}/{total}" if total > 1 else ""
        self.query_one("#ask-progress", Static).update(progress)
        self.query_one("#ask-question", Static).update(q.question)

        body = self.query_one("#ask-body", Vertical)
        await body.remove_children()
        other = self.query_one("#ask-other", Input)
        other.value = ""
        confirm = self.query_one("#ask-confirm", Button)
        confirm.display = q.multi

        if q.multi:
            sel: SelectionList[int] = SelectionList(id="ask-select")
            await body.mount(sel)
            for i, opt in enumerate(q.options):
                sel.add_option((_option_prompt(opt), i))
            sel.highlighted = 0
            sel.focus()
        else:
            options = OptionList(id="ask-options")
            await body.mount(options)
            for i, opt in enumerate(q.options):
                options.add_option(Option(_option_prompt(opt), id=str(i)))
            options.highlighted = 0
            options.focus()

    def _record(self, answer: str | list[str]) -> None:
        """Store the current question's answer, then advance or resolve."""
        q = self._questions[self._index]
        self._answers[q.header] = answer
        self._index += 1
        if self._index >= len(self._questions):
            self.resolve(self._answers)
        else:
            self.run_worker(self._show_question())

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        q = self._questions[self._index]
        if q.multi or event.option.id is None:
            return
        self._record(q.options[int(event.option.id)].label)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        q = self._questions[self._index]
        if q.multi:
            self._confirm_multi()
            return
        text = event.value.strip()
        if text:
            self._record(text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ask-confirm":
            self._confirm_multi()

    def _confirm_multi(self) -> None:
        """Collect the checked labels plus any free-text, then advance.

        If nothing is checked and no free-text is present the submission is
        ignored: the user must select at least one option, type free-text, or
        press Escape to cancel.
        """
        q = self._questions[self._index]
        sel = self.query_one("#ask-select", SelectionList)
        labels = [q.options[i].label for i in sel.selected]
        other = self.query_one("#ask-other", Input).value.strip()
        if not labels and not other:
            return
        if other:
            labels.append(other)
        self._record(labels)

    def action_cancel(self) -> None:
        self.resolve(None)
```

- [ ] **Step 4: Wire `_ask_user` through the panel in app.py**

Change the import at line 19 from:

```python
from .ask_user import AskUserModal
```

to:

```python
from .ask_user import AskUserPanel
from .interaction_panel import run_panel
```

(line 18's `from .approval import ApprovalModal` stays until Task 3; run
`uv run ruff check --fix src` if isort complains about ordering.)

Replace `_ask_user` (app.py:804–810) with:

```python
    async def _ask_user(self, questions):
        """Put a structured question to the user and return their {header:
        answer} mapping, or None if they dismissed it. Inline panel, not a
        modal: the transcript stays scrollable while the agent waits, and a
        cancelled turn removes the panel via run_panel's finally."""
        prompt = questions[0].question if questions else ""
        self._notify("Question from agent", prompt, "ask_user")
        return await run_panel(self, AskUserPanel(questions))
```

In `tests/test_app.py` (~line 3097), rename
`test_ask_user_callback_shows_modal_and_returns_answer` →
`test_ask_user_callback_shows_panel_and_returns_answer`. The body is already
API-agnostic (worker + `pilot.press("enter")`) and passes unchanged.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_ask_user_panel.py -v`
Expected: 10 passed (8 converted + 2 new).

Run: `uv run pytest --no-cov tests/test_app.py -k ask_user -v`
Expected: all passed.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/interfaces/tui/ask_user.py \
        src/marim_harness/interfaces/tui/app.py \
        tests/test_ask_user_panel.py tests/test_app.py
git commit -m "feat(tui): ask-user renders inline, transcript stays scrollable"
```

---

### Task 3: Convert `ApprovalModal` → `ApprovalPanel`

**Files:**
- Modify: `src/marim_harness/interfaces/tui/approval.py`
- Modify: `src/marim_harness/interfaces/tui/app.py` (import line 18; `_request_approval` at ~line 793)
- Modify: `tests/test_approval.py`

**Interfaces:**
- Consumes: `InteractionPanel`, `run_panel` from Task 1 (`run_panel` is already imported in app.py by Task 2).
- Produces: `class ApprovalPanel(InteractionPanel)` — constructor `ApprovalPanel(tool_name: str, args: dict)`; resolves `bool` (True = approve). `format_detail`, `ADDED_STYLE`, `REMOVED_STYLE` stay module-level and unchanged. App wiring for approvals is completed inside this task (same green-commit reasoning as Task 2).

- [ ] **Step 1: Update `tests/test_approval.py`**

Replace the imports and harness class (the `format_detail` tests and
`_styled_text` helper are untouched):

```python
import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from marim_harness.interfaces.tui.approval import (
    ADDED_STYLE,
    REMOVED_STYLE,
    ApprovalPanel,
    format_detail,
)
from marim_harness.interfaces.tui.interaction_panel import run_panel


class _Harness(App):
    def __init__(self):
        super().__init__()
        self.result = "unset"

    def compose(self) -> ComposeResult:
        yield VerticalScroll(Static("line\n" * 100), id="log")
        yield Static("", id="status-bar")

    def on_mount(self) -> None:
        self.run_worker(self._ask())

    async def _ask(self) -> None:
        self.result = await run_panel(
            self, ApprovalPanel("edit_file", {"path": "a.txt"})
        )
```

The three interaction tests (`test_approve_returns_true`,
`test_deny_returns_false`, `test_escape_denies`) keep their bodies verbatim.
Append two new tests:

```python
@pytest.mark.anyio
async def test_panel_removed_after_decision():
    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert not app.query(ApprovalPanel)


@pytest.mark.anyio
async def test_transcript_scrolls_while_approval_pending():
    app = _Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        log = app.query_one("#log", VerticalScroll)
        assert log.scroll_y == 0
        await pilot.press("pagedown")
        await pilot.pause()
        assert log.scroll_y > 0
        assert app.result == "unset"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_approval.py -v`
Expected: FAIL — `ImportError: cannot import name 'ApprovalPanel'`

- [ ] **Step 3: Rewrite the modal class in `src/marim_harness/interfaces/tui/approval.py`**

Keep lines 1–46 (`_append_diff`, `format_detail`, the style constants) exactly
as they are, but swap the imports block at the top to:

```python
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Static

from .interaction_panel import InteractionPanel
```

Then replace the entire `ApprovalModal` class (line 49 to end of file) with:

```python
class ApprovalPanel(InteractionPanel):
    """Asks the user to approve or deny a tool call, inline above the status
    bar so the transcript stays readable. Resolves with True/False."""

    # The panel itself takes focus on mount so the a/d/Esc bindings are live
    # immediately (the modal got this from the screen's focus scope).
    can_focus = True

    DEFAULT_CSS = """
    ApprovalPanel {
        border: round $warning;
    }
    #approval-title {
        text-style: bold;
        color: $warning;
        margin-bottom: 1;
    }
    #approval-detail {
        height: auto;
        max-height: 20;
        margin-bottom: 1;
    }
    #approval-buttons {
        height: auto;
        align-horizontal: right;
    }
    #approval-buttons Button {
        margin-left: 2;
    }
    """

    # Esc denies — backing out of an approval is a deny, and it keeps the panel
    # consistent with ask-user, which binds Esc to cancel. Without it a
    # reflexive Esc would fall through to the app binding and cancel the whole
    # turn.
    BINDINGS = [
        ("a", "approve", "Approve"),
        ("d", "deny", "Deny"),
        ("escape", "deny", "Cancel"),
    ]

    def __init__(self, tool_name: str, args: dict) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.args = args

    def compose(self) -> ComposeResult:
        yield Static(f"Approve  {self.tool_name}?", id="approval-title")
        yield Static(format_detail(self.tool_name, self.args), id="approval-detail")
        with Horizontal(id="approval-buttons"):
            yield Button("Deny (d)", id="deny", variant="error")
            yield Button("Approve (a)", id="approve", variant="success")

    def on_mount(self) -> None:
        self.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.resolve(event.button.id == "approve")

    def action_approve(self) -> None:
        self.resolve(True)

    def action_deny(self) -> None:
        self.resolve(False)
```

Also update the module docstring reference on line 7 ("Diff highlighting
styles for the approval preview.") — no change needed, it's a comment on the
constants, leave it.

- [ ] **Step 4: Wire `_request_approval` through the panel in app.py**

Change the import at line 18 from:

```python
from .approval import ApprovalModal
```

to:

```python
from .approval import ApprovalPanel
```

Replace `_request_approval` (app.py:793–802) with:

```python
    async def _request_approval(self, call) -> DeferredToolApprovalResult | bool:
        self._notify(
            "Approval needed",
            f"Tool: {call.tool_name}",
            "approval_needed",
        )
        approved = await run_panel(
            self, ApprovalPanel(call.tool_name, call.args_as_dict())
        )
        return True if approved else ToolDenied("denied by user")
```

Other `push_screen_wait`/`push_screen` call sites (model picker ~line 722/746,
sudo modal ~line 943) are unaffected; do not touch `_handle_bang`'s worker
comment — sudo's modal still needs a worker and that comment stays true.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_approval.py -v`
Expected: 12 passed (3 interaction + 7 format_detail + 2 new).

- [ ] **Step 6: Full verification gate (CI order)**

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```

Expected: all three green. If any `test_app.py` test fails on focus or timing,
the likely cause is the focus-restore in `run_panel` racing `pilot.pause()` —
add one extra `await pilot.pause()` in the affected test rather than changing
`run_panel`.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/interfaces/tui/approval.py \
        src/marim_harness/interfaces/tui/app.py tests/test_approval.py
git commit -m "feat(tui): approval prompt renders inline, transcript stays scrollable"
```

---

## Verification against spec

| Spec section | Covered by |
|---|---|
| §1 Modal → panel conversion | Tasks 2, 3 |
| §2 Shared await helper (`finally` teardown) | Task 1 (`run_panel`); wiring in Tasks 2, 3 |
| §3 Layout (`max-height: 50%`, internal scroll) | Task 1 CSS; Tasks 2/3 keep body max-heights |
| §4 Keyboard scrolling with panel focus | Task 1 bindings; scroll tests in Tasks 1, 2, 3 |
| §5 Esc semantics | Panel-level Esc bindings (Tasks 2, 3); app Esc untouched |
| §6 Prompt input coexistence | No code change needed — queueing during busy turn already exists; PromptInput stays in compose |
| §7 Tests | Every task carries its tests; Task 3 runs the full gate |
