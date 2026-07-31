# Session Picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `/sessions`' plain-text dump into the chat log with an interactive, filterable, deletable modal picker — matching the UX already established by `ModelPickerModal`/`ThinkingPickerModal`.

**Architecture:** A new `SessionPickerModal(ModalScreen[str | None])` in `interfaces/tui/session_picker.py`, built and tested exactly like `model_picker.py` (filter `Input` + `OptionList`, dismiss with a chosen session id or `None`). `HarnessApp.open_session_picker()` fetches sessions synchronously (already cheap — header-only JSON parse) and pushes the modal; `/sessions` now calls that instead of building a markdown string. `/switch <n|name>` is untouched. Deletion reuses the existing `SessionManager.delete()` teardown and a double-keypress confirm modeled on the app's existing quit-confirm guard — no new confirm-modal pattern.

**Tech Stack:** Python 3.10+, Textual (`ModalScreen`, `OptionList`, `Input`), pytest + pytest-anyio, existing `SessionManager`/`SessionInfo`/`format_duration`.

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM,C901` — keep functions under cyclomatic complexity 10; extract helpers rather than adding `# noqa: C901`.
- `requires-python >= 3.10` — no 3.11+-only syntax (e.g. no `match` needed here, but avoid `X | Y` in places that must run under old typing if any; this codebase already uses PEP 604 unions freely via `from __future__ import annotations` where present).
- Follow the existing three-way split (pure helpers vs. thin UI wiring) — `filter_sessions` must be a pure, side-effect-free function, unit-tested directly, independent of any widget.
- Preserve the existing "why" comment style — the codebase favors explanatory comments on non-obvious invariants (e.g. why `d` only deletes once focus is on the `OptionList`).
- Run `uv run ruff check src tests`, `uv run pyright`, `uv run pytest` (in that order) before considering any task's tests "done."

---

### Task 1: `filter_sessions` pure helper

**Files:**
- Modify: `src/marim_harness/session/store.py` (add function after the `SessionInfo` dataclass, ~line 165, before `class SessionStore:`)
- Modify: `src/marim_harness/session/__init__.py` (export it)
- Test: `tests/test_session.py` (append tests near the bottom, or in a new focused block — this file already imports `SessionInfo`/`SessionManager` at the top)

**Interfaces:**
- Produces: `filter_sessions(sessions: list[SessionInfo], query: str) -> list[SessionInfo]` — substring, case-insensitive match against `SessionInfo.name`; blank query returns the input list unchanged (same contract as `workspace.catalog.filter_entries`, which `Task 2` will NOT reuse — it filters `ModelEntry`, not `SessionInfo`, so this is a new, analogous function, not an extension of that one).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session.py`:

```python
from marim_harness.session import filter_sessions


def _sessions_for_filter():
    return [
        SessionInfo(id="a", name="Fix auth bug", updated="2026-07-01", message_count=1, tokens=0),
        SessionInfo(id="b", name="Refactor session store", updated="2026-07-02", message_count=1, tokens=0),
        SessionInfo(id="c", name="20260703-120000", updated="2026-07-03", message_count=1, tokens=0),
    ]


def test_filter_sessions_blank_query_returns_all():
    sessions = _sessions_for_filter()
    assert filter_sessions(sessions, "") == sessions
    assert filter_sessions(sessions, "   ") == sessions


def test_filter_sessions_substring_case_insensitive():
    sessions = _sessions_for_filter()
    result = filter_sessions(sessions, "AUTH")
    assert [s.id for s in result] == ["a"]


def test_filter_sessions_matches_auto_generated_name():
    sessions = _sessions_for_filter()
    result = filter_sessions(sessions, "20260703")
    assert [s.id for s in result] == ["c"]


def test_filter_sessions_no_match_returns_empty():
    sessions = _sessions_for_filter()
    assert filter_sessions(sessions, "nope-nothing-here") == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_session.py -k filter_sessions -v`
Expected: FAIL with `ImportError: cannot import name 'filter_sessions'`

- [ ] **Step 3: Implement `filter_sessions`**

In `src/marim_harness/session/store.py`, immediately after the `SessionInfo` dataclass (after its closing field, before `class SessionStore:`):

```python
def filter_sessions(sessions: list[SessionInfo], query: str) -> list[SessionInfo]:
    """Substring filter over session name (case-insensitive). Blank query keeps
    everything. Matches auto-generated timestamp names (e.g. "20260703-120000")
    the same as user-given titles — both live in the same ``name`` field."""
    q = query.strip().lower()
    if not q:
        return sessions
    return [s for s in sessions if q in s.name.lower()]
```

In `src/marim_harness/session/__init__.py`:

```python
from .ctrl import SessionController
from .store import SessionInfo, SessionManager, SessionStore, filter_sessions
from .transcripts import TranscriptStore

__all__ = [
    "SessionController",
    "SessionInfo",
    "SessionManager",
    "SessionStore",
    "TranscriptStore",
    "filter_sessions",
]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_session.py -k filter_sessions -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check src/marim_harness/session tests/test_session.py && uv run pyright src/marim_harness/session`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/session/store.py src/marim_harness/session/__init__.py tests/test_session.py
git commit -m "feat(session): add filter_sessions pure helper"
```

---

### Task 2: `SessionPickerModal` — browse, filter, select, cancel

**Files:**
- Create: `src/marim_harness/interfaces/tui/session_picker.py`
- Test: Create `tests/test_session_picker.py`

**Interfaces:**
- Consumes: `SessionInfo` (`marim_harness.session.SessionInfo`), `filter_sessions` (`marim_harness.session.filter_sessions`, Task 1), `format_duration` (`marim_harness.interfaces.durations.format_duration`).
- Produces: `SessionPickerModal(ModalScreen[str | None])` with constructor `__init__(self, sessions: list[SessionInfo], active: str | None = None) -> None`. Dismisses with the chosen session's `id`, or `None` on cancel. This task does NOT yet implement delete (`action_delete` is added in Task 3) — the `BINDINGS` list and `d` key are introduced in Task 3, not here, so this task's `SessionPickerModal` only binds `escape`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_session_picker.py`:

```python
import pytest
from textual.app import App
from textual.widgets import Input, OptionList

from marim_harness.interfaces.tui.session_picker import SessionPickerModal
from marim_harness.session import SessionInfo

_SESSIONS = [
    SessionInfo(id="s-alpha", name="Fix auth bug", updated="2026-07-03T10:00:00",
                message_count=5, tokens=1200, duration_seconds=125.0),
    SessionInfo(id="s-beta", name="Refactor session store", updated="2026-07-02T09:00:00",
                message_count=12, tokens=8300, duration_seconds=None),
    SessionInfo(id="s-gamma", name="20260701-120000", updated="2026-07-01T12:00:00",
                message_count=1, tokens=0, duration_seconds=5.0),
]


class _Host(App):
    def __init__(self, sessions, active=None):
        super().__init__()
        self.sessions = sessions
        self.active = active
        self.result = "unset"

    def on_mount(self) -> None:
        self.run_worker(self._pick())

    async def _pick(self) -> None:
        self.result = await self.push_screen_wait(
            SessionPickerModal(self.sessions, active=self.active)
        )


@pytest.mark.anyio
async def test_opens_with_all_sessions_listed():
    app = _Host(_SESSIONS)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = app.screen
        opts = modal.query_one("#session-options", OptionList)
        assert opts.option_count == len(_SESSIONS)


@pytest.mark.anyio
async def test_active_session_is_highlighted_on_open():
    app = _Host(_SESSIONS, active="s-beta")
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = app.screen
        opts = modal.query_one("#session-options", OptionList)
        assert opts.highlighted is not None
        assert opts.get_option_at_index(opts.highlighted).id == "s-beta"


@pytest.mark.anyio
async def test_typing_filters_by_name():
    app = _Host(_SESSIONS)
    async with app.run_test() as pilot:
        await pilot.pause()
        for ch in "auth":
            await pilot.press(ch)
        await pilot.pause()
        modal = app.screen
        opts = modal.query_one("#session-options", OptionList)
        assert opts.option_count == 1
        assert opts.get_option_at_index(0).id == "s-alpha"


@pytest.mark.anyio
async def test_enter_in_filter_picks_highlighted():
    app = _Host(_SESSIONS)
    async with app.run_test() as pilot:
        await pilot.pause()
        for ch in "beta":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == "s-beta"


@pytest.mark.anyio
async def test_escape_cancels_with_none():
    app = _Host(_SESSIONS)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None


@pytest.mark.anyio
async def test_option_selected_dismisses_with_id():
    app = _Host(_SESSIONS)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("tab")  # move focus from Input to OptionList
        await pilot.press("down")  # highlight s-beta
        await pilot.press("enter")  # OptionList's own enter -> select
        await pilot.pause()
    assert app.result == "s-beta"


def test_row_shows_msgs_tokens_duration_and_updated():
    from marim_harness.interfaces.tui.session_picker import _format_row

    row = _format_row(_SESSIONS[0], active=None)
    assert "5" in row and "msgs" in row
    assert "1200" in row
    assert "2m" in row  # format_duration(125.0) == "2m"
    assert "2026-07-03 10:00" in row


def test_row_marks_active_session():
    from marim_harness.interfaces.tui.session_picker import _format_row

    row = _format_row(_SESSIONS[0], active="s-alpha")
    assert "active" in row.lower()
    other = _format_row(_SESSIONS[1], active="s-alpha")
    assert "active" not in other.lower()


def test_row_shows_dash_for_missing_duration():
    from marim_harness.interfaces.tui.session_picker import _format_row

    row = _format_row(_SESSIONS[1], active=None)  # duration_seconds=None
    assert "—" in row
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_session_picker.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'marim_harness.interfaces.tui.session_picker'`

- [ ] **Step 3: Implement `SessionPickerModal`**

Create `src/marim_harness/interfaces/tui/session_picker.py`:

```python
"""A modal for browsing and switching saved sessions: a filter box over the
workspace's session list, newest-first, with the active session pre-highlighted.

Session data is fetched synchronously before the modal is constructed (listing
only parses each file's JSON header, never the full messages array — see
``session/store.py``'s ``_header_fields``), so unlike ``ModelPickerModal`` there
is no async loading state to manage here."""

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ...interfaces.durations import format_duration
from ...session import SessionInfo, filter_sessions

_NAME_WIDTH = 28


def _format_row(info: SessionInfo, active: str | None) -> str:
    name = info.name if len(info.name) <= _NAME_WIDTH else info.name[: _NAME_WIDTH - 1] + "…"
    when = info.updated[:16].replace("T", " ") if info.updated else "—"
    duration = (
        format_duration(info.duration_seconds) if info.duration_seconds is not None else "—"
    )
    marker = "  ← active" if info.id == active else ""
    return (
        f"{name:<{_NAME_WIDTH}}  {info.message_count:>3} msgs · "
        f"{info.tokens:>6} tok · {duration:>6} · {when}{marker}"
    )


class SessionPickerModal(ModalScreen[str | None]):
    """Dismisses with the chosen session id, or None if cancelled."""

    CSS = """
    SessionPickerModal {
        align: center middle;
    }
    #session-box {
        width: 90%;
        max-width: 120;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #session-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #session-status {
        color: $text-muted;
    }
    #session-options {
        height: auto;
        max-height: 20;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, sessions: list[SessionInfo], active: str | None = None) -> None:
        super().__init__()
        self.sessions = sessions
        self.active = active

    def compose(self) -> ComposeResult:
        with Vertical(id="session-box"):
            yield Static("Switch session", id="session-title")
            yield Input(placeholder="filter… (Tab to navigate, Enter to pick)",
                        id="session-filter")
            yield Static("", id="session-status")
            yield OptionList(id="session-options")

    def on_mount(self) -> None:
        self._populate(self.sessions)
        self.query_one("#session-filter", Input).focus()

    def _populate(self, sessions: list[SessionInfo]) -> None:
        options = self.query_one("#session-options", OptionList)
        options.clear_options()
        active_index = None
        for i, info in enumerate(sessions):
            options.add_option(Option(_format_row(info, self.active), id=info.id))
            if info.id == self.active:
                active_index = i
        if sessions:
            options.highlighted = active_index if active_index is not None else 0

    def _highlighted_id(self) -> str | None:
        options = self.query_one("#session-options", OptionList)
        if options.option_count and options.highlighted is not None:
            return options.get_option_at_index(options.highlighted).id
        return None

    def on_input_changed(self, event: Input.Changed) -> None:
        self._populate(filter_sessions(self.sessions, event.value))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        choice = self._highlighted_id()
        if choice is not None:
            self.dismiss(choice)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_session_picker.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check src/marim_harness/interfaces/tui/session_picker.py tests/test_session_picker.py && uv run pyright src/marim_harness/interfaces/tui/session_picker.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/session_picker.py tests/test_session_picker.py
git commit -m "feat(tui): add SessionPickerModal (browse/filter/select)"
```

---

### Task 3: Delete flow

**Files:**
- Modify: `src/marim_harness/interfaces/tui/session_picker.py`
- Test: `tests/test_session_picker.py` (append)

**Interfaces:**
- Consumes: nothing new from other tasks; deletion itself (`SessionManager.delete`) is invoked by the caller (`HarnessApp`, Task 4) reacting to a `SessionPickerModal.deleted` message — see below. The modal itself does not import `SessionManager` (it has no reference to one; it only knows the `list[SessionInfo]` it was given).
- Produces: a new Textual message `SessionPickerModal.Deleted(session_id: str)`, posted when a delete is confirmed, so `HarnessApp` (Task 4) can perform the actual `SessionManager.delete()` call and any wider app bookkeeping. The modal updates its own `OptionList` optimistically (removes the row) without waiting for that call to complete — matches this codebase's "no blocking round-trip inside a modal" style (see `ModelPickerModal`'s worker-based fetch).

Design note carried over from the spec: `d` only reaches `action_delete` once the `OptionList` (not the filter `Input`) has focus — `Input` consumes plain letters for typing. This is deliberate, not a bug to fix.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session_picker.py`:

```python
from textual.message import Message


@pytest.mark.anyio
async def test_first_d_arms_without_deleting():
    app = _Host(_SESSIONS)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = app.screen
        await pilot.press("tab")
        await pilot.press("d")
        await pilot.pause()
        opts = modal.query_one("#session-options", OptionList)
        assert opts.option_count == len(_SESSIONS)  # nothing removed yet
        assert "press d again" in str(modal.query_one("#session-status").renderable).lower()


@pytest.mark.anyio
async def test_second_d_confirms_removes_row_and_posts_deleted():
    received: list[str] = []

    class _DeleteHost(App):
        def __init__(self, sessions):
            super().__init__()
            self.sessions = sessions

        def on_mount(self) -> None:
            self.push_screen(SessionPickerModal(self.sessions))

        def on_session_picker_modal_deleted(self, message: SessionPickerModal.Deleted) -> None:
            received.append(message.session_id)

    app = _DeleteHost(_SESSIONS)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = app.screen
        await pilot.press("tab")
        await pilot.press("d")
        await pilot.press("d")
        await pilot.pause()
        opts = modal.query_one("#session-options", OptionList)
        assert opts.option_count == len(_SESSIONS) - 1
    assert received == ["s-alpha"]


@pytest.mark.anyio
async def test_active_session_cannot_be_armed():
    app = _Host(_SESSIONS, active="s-alpha")
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = app.screen
        await pilot.press("tab")  # highlighted starts on s-alpha (the active one)
        await pilot.press("d")
        await pilot.pause()
        opts = modal.query_one("#session-options", OptionList)
        assert opts.option_count == len(_SESSIONS)
        assert "can't delete the active session" in str(
            modal.query_one("#session-status").renderable
        ).lower()


@pytest.mark.anyio
async def test_moving_highlight_clears_armed_state():
    app = _Host(_SESSIONS)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = app.screen
        await pilot.press("tab")
        await pilot.press("d")  # arm s-alpha
        await pilot.press("down")  # move off it
        await pilot.press("d")  # would confirm s-alpha if still armed; must NOT
        await pilot.pause()
        opts = modal.query_one("#session-options", OptionList)
        assert opts.option_count == len(_SESSIONS)  # nothing deleted
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_session_picker.py -k "arm or confirm or active_session_cannot or highlight_clears" -v`
Expected: FAIL — `d` is not bound to any action yet (`action_delete` doesn't exist), so `pilot.press("d")` types into nothing (`OptionList` has focus, ignores the key) and no state changes; `option_count` assertions for the arm/confirm tests fail because no message is posted, and `test_active_session_cannot_be_armed`/`test_moving_highlight_clears_armed_state` fail because `#session-status` never updates.

- [ ] **Step 3: Implement the delete flow**

Modify `src/marim_harness/interfaces/tui/session_picker.py`:

Add `import time` at the top (near the other stdlib-free imports — this module currently has none, so add it as the first import line), add a `Message` import, extend `BINDINGS`, add `__init__` state, and add the delete methods.

```python
import time

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ...interfaces.durations import format_duration
from ...session import SessionInfo, filter_sessions

_NAME_WIDTH = 28
# Mirrors HarnessApp._QUIT_CONFIRM_WINDOW: a second same-row `d` within this
# window confirms; anything after it is treated as a fresh first press.
_DELETE_CONFIRM_WINDOW = 2.0
```

(`_format_row` is unchanged from Task 2.)

```python
class SessionPickerModal(ModalScreen[str | None]):
    """Dismisses with the chosen session id, or None if cancelled."""

    class Deleted(Message):
        """Posted once a delete is confirmed (second `d` within the window).
        The caller (HarnessApp) owns the actual SessionManager.delete() call —
        this modal only knows the SessionInfo list it was given, not a manager."""

        def __init__(self, session_id: str) -> None:
            self.session_id = session_id
            super().__init__()

    CSS = """
    SessionPickerModal {
        align: center middle;
    }
    #session-box {
        width: 90%;
        max-width: 120;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #session-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #session-status {
        color: $text-muted;
    }
    #session-options {
        height: auto;
        max-height: 20;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel"), ("d", "delete", "Delete")]

    def __init__(self, sessions: list[SessionInfo], active: str | None = None) -> None:
        super().__init__()
        self.sessions = sessions
        self.active = active
        self._armed: tuple[str, float] | None = None  # (session_id, armed_at)

    def compose(self) -> ComposeResult:
        with Vertical(id="session-box"):
            yield Static("Switch session", id="session-title")
            yield Input(placeholder="filter… (Tab to navigate, Enter to pick)",
                        id="session-filter")
            yield Static("", id="session-status")
            yield OptionList(id="session-options")

    def on_mount(self) -> None:
        self._populate(self.sessions)
        self.query_one("#session-filter", Input).focus()

    def _populate(self, sessions: list[SessionInfo]) -> None:
        options = self.query_one("#session-options", OptionList)
        options.clear_options()
        active_index = None
        for i, info in enumerate(sessions):
            options.add_option(Option(_format_row(info, self.active), id=info.id))
            if info.id == self.active:
                active_index = i
        if sessions:
            options.highlighted = active_index if active_index is not None else 0

    def _highlighted_id(self) -> str | None:
        options = self.query_one("#session-options", OptionList)
        if options.option_count and options.highlighted is not None:
            return options.get_option_at_index(options.highlighted).id
        return None

    def _set_status(self, text: str) -> None:
        self.query_one("#session-status", Static).update(text)

    def on_input_changed(self, event: Input.Changed) -> None:
        self._armed = None
        self._populate(filter_sessions(self.sessions, event.value))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        choice = self._highlighted_id()
        if choice is not None:
            self.dismiss(choice)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        # Any highlight change (arrow/home/end/pageup/pagedown) cancels an
        # in-progress arm — the second `d` must land on the SAME row.
        if self._armed is not None and self._armed[0] != event.option_id:
            self._armed = None
            self._set_status("")

    def action_delete(self) -> None:
        session_id = self._highlighted_id()
        if session_id is None:
            return
        if session_id == self.active:
            self._armed = None
            self._set_status("Can't delete the active session.")
            return
        now = time.monotonic()
        if self._armed is not None and self._armed[0] == session_id and \
                now - self._armed[1] <= _DELETE_CONFIRM_WINDOW:
            self._armed = None
            self.sessions = [s for s in self.sessions if s.id != session_id]
            self._populate(self.sessions)
            self._set_status(f"Deleted {session_id}.")
            self.post_message(self.Deleted(session_id))
            return
        self._armed = (session_id, now)
        self._set_status("Press d again to delete.")

    def action_cancel(self) -> None:
        self.dismiss(None)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_session_picker.py -v`
Expected: PASS (13 tests total)

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check src/marim_harness/interfaces/tui/session_picker.py tests/test_session_picker.py && uv run pyright src/marim_harness/interfaces/tui/session_picker.py`
Expected: no errors. If `action_delete`'s cyclomatic complexity trips C901 (it has several branches: no-highlight, active-session, confirm, arm — 4 branches, should be under the cap of 10, but check), leave as-is; it's well under the ceiling.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/session_picker.py tests/test_session_picker.py
git commit -m "feat(tui): add double-press delete to SessionPickerModal"
```

---

### Task 4: Wire `HarnessApp.open_session_picker()`

**Files:**
- Modify: `src/marim_harness/interfaces/tui/app.py` (add import near line 33/48, add methods near `open_thinking_picker`, ~line 957-969)
- Modify: `tests/test_app.py` (replace `test_sessions_command_lists_saved`, add new tests near the `open_model_picker`/`open_thinking_picker` test block)

**Interfaces:**
- Consumes: `SessionPickerModal` (Task 2/3), `SessionPickerModal.Deleted` message (Task 3), `app.harness.session.sessions()` (existing, `SessionController.sessions`), `app.harness.session.store.session_id` (existing, guarded for `None`), `app.harness.session.manager.delete(session_id)` (existing, `SessionManager.delete`, `session/store.py:496`), `app.switch_to_session_id(session_id: str)` (existing, `session_view.py:529`, unchanged).
- Produces: `HarnessApp.open_session_picker() -> None` (async), `HarnessApp._on_session_chosen(chosen: str | None) -> None` (async), `HarnessApp.on_session_picker_modal_deleted(message) -> None` (Textual message handler, sync).

- [ ] **Step 1: Write the failing tests**

In `tests/test_app.py`, first replace the existing `test_sessions_command_lists_saved` (it asserts on posted chat text, which no longer happens):

```python
@pytest.mark.anyio
async def test_sessions_command_opens_picker(tmp_path: Path):
    from marim_harness.interfaces.tui.session_picker import SessionPickerModal

    app = _app_with_manager(tmp_path)
    app.harness.new_session("first")
    app.harness.session.persist()
    app.harness.new_session("second")
    app.harness.session.persist()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, "/sessions")
        await pilot.pause()
        assert isinstance(app.screen, SessionPickerModal)
        opts = app.screen.query_one("#session-options")
        assert opts.option_count == 2
```

Then add near the `test_model_picker_applies_choice`/`test_model_picker_cancel_keeps_model` block:

```python
@pytest.mark.anyio
async def test_session_picker_switches_on_choice(tmp_path: Path):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    app = _app_with_manager(tmp_path)
    app.harness.new_session("alpha")
    app.harness.session.history = [ModelRequest(parts=[UserPromptPart(content="hi alpha")])]
    app.harness.session.persist()
    app.harness.new_session("beta")
    app.harness.session.persist()

    target_id = next(
        info.id for info in app.harness.session.sessions() if info.name == "alpha"
    )

    def fake_push(screen, callback=None):
        if callback is not None:
            callback(target_id)

    app.push_screen = fake_push  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.open_session_picker()
        await pilot.pause()
        assert app.harness.session.session_name == "alpha"
        assert len(app.harness.session.history) == 1


@pytest.mark.anyio
async def test_session_picker_cancel_keeps_current_session(tmp_path: Path):
    app = _app_with_manager(tmp_path)

    def fake_push(screen, callback=None):
        if callback is not None:
            callback(None)  # cancelled

    app.push_screen = fake_push  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        current = app.harness.session.session_name
        await app.open_session_picker()
        await pilot.pause()
        assert app.harness.session.session_name == current


@pytest.mark.anyio
async def test_session_picker_delete_message_removes_session(tmp_path: Path):
    app = _app_with_manager(tmp_path)
    app.harness.new_session("doomed")
    app.harness.session.persist()
    doomed_id = next(
        info.id for info in app.harness.session.sessions() if info.name == "doomed"
    )
    # Switch back to a non-doomed session so "doomed" isn't the active one.
    app.harness.new_session("keeper")
    app.harness.session.persist()

    async with app.run_test() as pilot:
        await pilot.pause()
        from marim_harness.interfaces.tui.session_picker import SessionPickerModal

        app.on_session_picker_modal_deleted(SessionPickerModal.Deleted(doomed_id))
        await pilot.pause()
        remaining_ids = {info.id for info in app.harness.session.sessions()}
        assert doomed_id not in remaining_ids
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_app.py -k "session_picker or sessions_command_opens_picker" -v`
Expected: FAIL — `open_session_picker`/`on_session_picker_modal_deleted` don't exist yet (`AttributeError`), and `/sessions` still posts text so `isinstance(app.screen, SessionPickerModal)` fails.

- [ ] **Step 3: Implement the wiring**

In `src/marim_harness/interfaces/tui/app.py`, add the import next to the existing picker imports (near line 33):

```python
from .session_picker import SessionPickerModal
```

Add the methods next to `open_thinking_picker`/`_on_thinking_chosen` (~line 957-969):

```python
async def open_session_picker(self) -> None:
    """Open the session picker and let the user browse/filter/switch/delete
    saved sessions. Sessions are fetched synchronously up front (listing is
    a cheap header-only parse — see session/store.py's _header_fields), so
    unlike the model picker there's no async fetch/loading state to manage.

    Uses the callback form of push_screen (not push_screen_wait) for the same
    reason open_model_picker does: /sessions dispatches from the command path,
    which is not a worker — push_screen_wait would raise NoActiveWorker there.
    """
    infos = self.harness.session.sessions()
    store = self.harness.session.store
    active = store.session_id if store is not None else None
    self.push_screen(SessionPickerModal(infos, active), self._on_session_chosen)

async def _on_session_chosen(self, chosen: str | None) -> None:
    """Apply a session selected in the picker. Invoked by push_screen when the
    modal is dismissed; a None result (cancelled) is a no-op."""
    if not chosen:
        return
    await self.switch_to_session_id(chosen)

def on_session_picker_modal_deleted(self, message: SessionPickerModal.Deleted) -> None:
    """The picker already removed the row optimistically; this performs the
    actual on-disk teardown via the same SessionManager.delete used by
    `marim sessions delete` (interfaces/cli/sessions.py)."""
    manager = self.harness.session.manager
    if manager is not None:
        manager.delete(message.session_id)
```

Then, no change is needed to `switch_to_session_id` (`session_view.py:529`) — it's already exactly the right terminal step.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_app.py -k "session_picker or sessions_command_opens_picker" -v`
Expected: still FAIL at this point for the `/sessions`-opens-picker test specifically, because `_cmd_sessions` (Task 5) hasn't been rewired yet — that's expected; the `open_session_picker`/delete-message tests should now PASS. Note the exact failing test in your run and confirm it's only `test_sessions_command_opens_picker`.

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check src/marim_harness/interfaces/tui/app.py tests/test_app.py && uv run pyright src/marim_harness/interfaces/tui/app.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/app.py tests/test_app.py
git commit -m "feat(tui): wire HarnessApp.open_session_picker and delete handling"
```

---

### Task 5: Rewire `/sessions` to open the picker

**Files:**
- Modify: `src/marim_harness/interfaces/tui/commands.py` (`_cmd_sessions`, ~line 125-140; `COMMANDS` entry, ~line 649)
- Modify: `tests/test_commands.py` (replace `test_sessions_marks_the_active_session`, ~line 255-267)

**Interfaces:**
- Consumes: `HarnessApp.open_session_picker()` (Task 4).
- Produces: `_cmd_sessions(app: HarnessApp, arg: str) -> None` reduced to a one-line delegation; `resolve_ref` and `_cmd_switch` are untouched.

- [ ] **Step 1: Write the failing test**

In `tests/test_commands.py`, replace `test_sessions_marks_the_active_session` (~line 255-267) with:

```python
@pytest.mark.anyio
async def test_sessions_command_opens_picker():
    app = _FakeApp()
    opened = []

    async def fake_open_session_picker():
        opened.append(True)

    app.open_session_picker = fake_open_session_picker  # type: ignore[attr-defined]
    await dispatch(app, "/sessions")
    assert opened == [True]
```

(The old test's coverage of "active session is marked" now lives in `tests/test_session_picker.py::test_row_marks_active_session`, Task 2 — the marking logic moved into `_format_row`.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest --no-cov tests/test_commands.py -k sessions_command_opens_picker -v`
Expected: FAIL — `_cmd_sessions` still builds and posts a markdown string; `app.open_session_picker` is never called (`opened == []`).

- [ ] **Step 3: Rewire `_cmd_sessions`**

In `src/marim_harness/interfaces/tui/commands.py`, replace (lines 125-140):

```python
async def _cmd_sessions(app: HarnessApp, arg: str) -> None:
    infos = app.harness.session.sessions()
    if not infos:
        await app.post_system("No saved sessions yet. Use `/new [name]` to start one.")
        return
    active = app.harness.session.session_name
    lines = ["**Sessions**", ""]
    for i, info in enumerate(infos, start=1):
        marker = " ← active" if info.name == active else ""
        when = info.updated[:16].replace("T", " ") if info.updated else "—"
        lines.append(
            f"{i}. `{info.name}` — {info.message_count} msgs, "
            f"{info.tokens} tokens, {when}{marker}"
        )
    lines += ["", "Switch with `/switch <number|name>`."]
    await app.post_system("\n".join(lines))
```

with:

```python
async def _cmd_sessions(app: HarnessApp, arg: str) -> None:
    await app.open_session_picker()
```

Then update the `COMMANDS` entry (~line 649):

```python
    Command("sessions", "browse and switch sessions (opens a picker)", _cmd_sessions, aliases=("ls",)),
```

`resolve_ref` (`commands.py:107-122`) and `_cmd_switch` (`commands.py:160-169`) are unchanged — do not touch them.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_commands.py -k sessions -v`
Expected: PASS (`test_sessions_command_opens_picker`, plus `test_resolve_ref_*` and `test_aliases_resolve_to_their_command`/`test_core_commands_present` which touch the same command registry and must still pass unchanged)

Then run the full app-level test from Task 4 that depends on this rewiring:

Run: `uv run pytest --no-cov tests/test_app.py -k test_sessions_command_opens_picker -v`
Expected: PASS (this was the one test left failing at the end of Task 4)

- [ ] **Step 5: Full-suite check for this area**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest --no-cov -k "session or command" -v`
Expected: no lint errors, no type errors, all session/command-related tests pass. Pay particular attention to any other existing test that scraped `/sessions`' old text-dump format (grep first: `grep -rn "Switch with \`/switch\|Sessions\*\*" tests/` to catch stragglers not already found).

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/commands.py tests/test_commands.py
git commit -m "feat(tui): /sessions opens the session picker instead of a text dump"
```

---

### Task 6: Full verification, CHANGELOG, manual smoke

**Files:**
- Modify: `CHANGELOG.md` (Unreleased section — check its existing heading structure first)

**Interfaces:** None (docs + verification only).

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest`
Expected: full suite passes, including coverage gate.

- [ ] **Step 2: Run lint and type-check across the whole repo**

Run: `uv run ruff check src tests && uv run pyright`
Expected: no errors.

- [ ] **Step 3: Add the CHANGELOG entry**

`CHANGELOG.md`'s `## [Unreleased]` section is a flat bullet list (no `### Added`/`### Changed` subheadings). Add a new bullet immediately under `## [Unreleased]` (top of the list, matching how the existing top entry — the `marim serve qr` one — reads as a single self-contained paragraph):

```markdown
- `/sessions` now opens an interactive picker instead of printing a text
  list: type to filter by name, Tab into the list to navigate, Enter to
  switch. Press `d` twice on a highlighted (non-active) session to delete it
  — the same teardown `marim sessions delete` already performs. `/switch
  <number|name>` is unchanged.
```

- [ ] **Step 4: Manual smoke test**

Use the `run` skill to launch the TUI against a workspace with several saved sessions (or create a few via `/new <name>` across a couple of launches first). Verify:
- `/sessions` opens the picker, not a text dump.
- Typing filters the list live.
- Tab moves focus into the list; arrow keys navigate.
- Enter (from either the filter box on a highlighted result, or directly on the list) switches sessions and closes the picker.
- Escape cancels with no change.
- On a non-active row: `d` once shows the arm status; `d` again removes it from the list and it's gone from `/sessions` on reopen; verify via `marim sessions list <workspace>` from a separate terminal (or re-open the picker) that the file is actually gone.
- Attempting `d` `d` on the currently active row is refused with a status message, and the session is not removed.
- At a narrow terminal width, confirm the row text doesn't overflow/wrap ugly — adjust `_NAME_WIDTH` or the box's `width`/`max-width` in the CSS if it does, and re-run `tests/test_session_picker.py` after any such change.

- [ ] **Step 5: Commit the CHANGELOG update**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog entry for session picker"
```
