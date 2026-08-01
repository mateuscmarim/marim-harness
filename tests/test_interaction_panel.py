"""The InteractionPanel base: future-resolution lifecycle, teardown on worker
cancel, and scroll-key forwarding to the transcript."""

import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from marim_harness.interfaces.tui.interactions.base import InteractionPanel, run_panel


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


class _ManualPanelApp(App):
    """Like _PanelApp, but doesn't auto-start run_panel on mount — the test
    controls exactly when the panel is requested, so it can push a modal
    screen first and prove run_panel still finds the base screen."""

    def __init__(self) -> None:
        super().__init__()
        self.result = "unset"
        self.panel = InteractionPanel()

    def compose(self) -> ComposeResult:
        yield VerticalScroll(Static("line\n" * 200), id="log")
        yield Static("status", id="status-bar")

    async def ask(self) -> None:
        self.result = await run_panel(self, self.panel)


@pytest.mark.anyio
async def test_mounts_into_base_screen_with_modal_on_top():
    """App.mount delegates to app.screen — the TOP of the screen stack. The
    settings screen and model picker are ModalScreens the user can open
    mid-turn; if an approval/ask_user then fires, run_panel must still find
    #status-bar on the BASE screen, not the modal (which raises NoMatches and
    kills the turn if targeted via app.mount)."""
    app = _ManualPanelApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        base = app.screen_stack[0]
        app.push_screen(ModalScreen())
        await pilot.pause()
        assert len(app.screen_stack) == 2  # modal is now on top

        app.run_worker(app.ask())
        await pilot.pause()

        assert app.panel.is_attached
        assert app.panel.parent is base
        bar = base.query_one("#status-bar")
        assert list(base.children).index(app.panel) < list(base.children).index(bar)

        # Resolution still works even with the modal on top (only its
        # keyboard reachability is affected, which is out of scope here).
        app.panel.resolve("done")
        await pilot.pause()
        assert app.result == "done"


def _harness_app(root: Path):
    """A real HarnessApp (not the bare-App stand-ins above), needed for these
    two tests because they exercise app.on_descendant_focus, which only
    exists on HarnessApp."""
    from pydantic_ai.models.test import TestModel

    from marim_harness.interfaces.tui.app import HarnessApp
    from marim_harness.runtime.harness import Harness
    from marim_harness.tools.provider import BuiltinToolProvider
    from tests.conftest import _make_deps

    deps = _make_deps(root)
    harness = Harness(TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="test")
    return HarnessApp(harness)


@asynccontextmanager
async def _app_with_two_panels():
    """Two InteractionPanels mounted concurrently via run_panel, mirroring the
    real hazard: pydantic-ai's concurrent tool calls (or the trust prompt,
    which isn't gated on turn_busy) can put a second panel up while the first
    is still pending."""
    with tempfile.TemporaryDirectory() as tmp:
        app = _harness_app(Path(tmp))
        async with app.run_test() as pilot:
            await pilot.pause()
            panel_a = InteractionPanel()
            panel_a.can_focus = True
            panel_b = InteractionPanel()
            panel_b.can_focus = True
            app.run_worker(run_panel(app, panel_a))
            await pilot.pause()
            panel_a.focus()
            app.run_worker(run_panel(app, panel_b))
            await pilot.pause()
            yield pilot, panel_a, panel_b


@asynccontextmanager
async def _app_with_pending_panel():
    """One pending InteractionPanel, plus a seeded sub-agent card so ctrl+x
    doesn't no-op (SubAgentsScreen.open_at no-ops with no cards — see
    tests/test_subagents_screen.py)."""
    with tempfile.TemporaryDirectory() as tmp:
        app = _harness_app(Path(tmp))
        async with app.run_test() as pilot:
            await pilot.pause()
            r = app.stream
            w = r.mount_spawn_widget({"type": "research", "description": "map it"})
            w.stream_id = "call_1"
            r.tool_widgets["call_1"] = w
            r.ensure_pane(w)
            await app.query_one("#log").mount(w)
            await pilot.pause()

            panel = InteractionPanel()
            panel.can_focus = True
            app.run_worker(run_panel(app, panel))
            await pilot.pause()
            panel.focus()
            await pilot.pause()
            yield pilot, panel


@pytest.mark.anyio
async def test_resolving_one_panel_refocuses_a_still_pending_sibling():
    """Two panels can coexist (concurrent tool calls; the trust prompt is not
    gated on turn_busy). Resolving the first must hand focus to the one still
    waiting — otherwise 'a'/'d' type into the prompt and Esc cancels the turn."""
    async with _app_with_two_panels() as (pilot, panel_a, panel_b):
        panel_a.resolve(True)
        await pilot.pause()
        assert pilot.app.focused is panel_b


@pytest.mark.anyio
async def test_refocus_falls_back_to_a_non_focusable_siblings_descendant():
    """AskUserPanel/PlanCard don't set can_focus on themselves — they focus a
    SelectionList/OptionList descendant in on_mount instead. sibling.focus()
    would silently no-op for those (Textual only moves focus onto a
    `.focusable` widget), leaving the panel just as keyboard-dead as the bug
    this task fixes. Use the real subclasses, not the bare test double, so
    this actually exercises that shape."""
    from textual.widgets import OptionList

    from marim_harness.ask_user import Choice, Question
    from marim_harness.interfaces.tui.interactions.approval import ApprovalPanel
    from marim_harness.interfaces.tui.interactions.ask_user import AskUserPanel

    with tempfile.TemporaryDirectory() as tmp:
        app = _harness_app(Path(tmp))
        async with app.run_test() as pilot:
            await pilot.pause()
            questions = [Question("Pick one", "Pick", [Choice("Alpha"), Choice("Beta")])]
            ask = AskUserPanel(questions)
            approval = ApprovalPanel("bash", {"command": "echo hi"})

            # ask mounts first, its on_mount worker focuses its OptionList —
            # then approval mounts and, being focusable, steals focus onto
            # itself in its own on_mount. This is the scenario that actually
            # exercises the fallback: when approval resolves, focus is on
            # approval itself, NOT already sitting on ask's descendant.
            app.run_worker(run_panel(app, ask))
            await pilot.pause()  # lets AskUserPanel's on_mount worker mount+focus its list
            assert pilot.app.focused in ask.query(OptionList)  # sanity: ask had focus first

            app.run_worker(run_panel(app, approval))
            await pilot.pause()
            assert pilot.app.focused is approval  # approval stole focus on mount

            assert ask.focusable is False  # the shape this test is pinning
            approval.resolve(True)
            await pilot.pause()

            assert isinstance(pilot.app.focused, OptionList)
            assert pilot.app.focused in ask.query(OptionList)


@pytest.mark.anyio
async def test_opening_the_subagents_view_does_not_strand_a_pending_panel():
    """run_panel's docstring names this hazard and closes it before mounting, but
    nothing stopped ctrl+x afterward: the panel was covered and keyboard-dead
    while the turn appeared wedged."""
    async with _app_with_pending_panel() as (pilot, panel):
        await pilot.press("ctrl+x")
        await pilot.pause()
        assert pilot.app.focused is panel


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
