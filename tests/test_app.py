from pathlib import Path

import pytest

from marim_harness.deps import Deps
from marim_harness.permissions import Mode
from marim_harness.tui.app import HarnessApp


def _app(tmp_path: Path) -> HarnessApp:
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="test"
    )
    return HarnessApp(harness)


@pytest.mark.anyio
async def test_status_bar_shows_mode(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one("#status-bar")
        text = str(bar.render())
        assert "ask" in text or "auto" in text


@pytest.mark.anyio
async def test_status_bar_shows_token_count(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one("#status-bar")
        assert "0 tokens" in str(bar.render())  # starts at zero
        app.harness.usage.input_tokens = 12
        app.harness.usage.output_tokens = 8
        app._refresh_status()
        await pilot.pause()
        assert "20 tokens" in str(bar.render())


def _submit(app, text):
    from textual.widgets import Input

    inp = app.query_one(Input)
    inp.value = text
    return app.on_input_submitted(Input.Submitted(inp, text))


@pytest.mark.anyio
@pytest.mark.parametrize("cmd", ["/exit", "/quit", "  /exit  "])
async def test_exit_command_quits_app(tmp_path: Path, cmd: str):
    app = _app(tmp_path)
    exited = []
    app.exit = lambda *a, **k: exited.append(True)  # type: ignore[method-assign]
    started = []
    app.run_worker = lambda *a, **k: started.append(a)  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, cmd)
        await pilot.pause()
    assert exited == [True]
    assert started == []  # never sent to the model as a prompt


@pytest.mark.anyio
async def test_failed_turn_shows_error_and_keeps_running(tmp_path: Path):
    from marim_harness.tui.widgets import ErrorMessage

    app = _app(tmp_path)

    async def boom(*a, **k):
        raise RuntimeError("upstream exploded")

    app.harness.run_turn = boom  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._run_turn("hello")
        await pilot.pause()
        # the app survives the failure
        assert app.is_running is True
        # busy indicator is cleared
        assert "working" not in str(app.query_one("#status-bar").render()).lower()
        # an error is rendered in the log
        errors = list(app.query(ErrorMessage))
        assert len(errors) == 1
        assert "upstream exploded" in str(errors[0].render())


@pytest.mark.anyio
async def test_status_bar_shows_busy_indicator(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one("#status-bar")
        assert "working" not in str(bar.render()).lower()
        app._set_busy(True)
        await pilot.pause()
        assert "working" in str(bar.render()).lower()
        app._set_busy(False)
        await pilot.pause()
        assert "working" not in str(bar.render()).lower()


@pytest.mark.anyio
async def test_log_and_input_both_visible(tmp_path: Path):
    """Status bar and input must not collide; both render at non-zero size."""
    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        from textual.widgets import Footer, Input

        status = app.query_one("#status-bar")
        inp = app.query_one(Input)
        footer = app.query_one(Footer)
        assert status.size.height >= 1
        assert inp.size.height >= 1
        # they occupy different rows
        assert status.region.y != inp.region.y
        # the input must not overlap the footer (its last row stays above it)
        assert inp.region.bottom <= footer.region.y


@pytest.mark.anyio
async def test_welcome_message_shown_on_start(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        log = app.query_one("#log")
        text = " ".join(str(c.render()) for c in log.walk_children() if hasattr(c, "render"))
        assert "ctrl+t" in text.lower() or "welcome" in text.lower()


@pytest.mark.anyio
async def test_mode_keybinding_cycles(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        start = app.harness.deps.mode
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.harness.deps.mode is not start


@pytest.mark.anyio
async def test_on_events_mounts_and_finishes_tool_widget(tmp_path: Path):
    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        ToolCallPart,
        ToolReturnPart,
    )

    from marim_harness.tui.widgets import ToolCallWidget

    call = FunctionToolCallEvent(
        part=ToolCallPart(
            tool_name="read_file",
            args={"path": "a.txt"},
            tool_call_id="call-1",
        )
    )
    result = FunctionToolResultEvent(
        part=ToolReturnPart(
            tool_name="read_file",
            content="1\tfoo",
            tool_call_id="call-1",
        )
    )

    async def gen():
        yield call
        yield result

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._on_events(None, gen())
        await pilot.pause()

        widget = app._tool_widgets.get("call-1")
        assert isinstance(widget, ToolCallWidget)
        log = app.query_one("#log")
        assert widget in log.walk_children()
        assert widget.status == "done"
        assert "1\tfoo" in widget.result_text
