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


def test_human_tokens_formatting():
    from marim_harness.tui.app import _human_tokens

    assert _human_tokens(0) == "0"
    assert _human_tokens(950) == "950"
    assert _human_tokens(1500) == "1.5k"
    assert _human_tokens(100_000) == "100k"


@pytest.mark.anyio
async def test_status_bar_shows_context_usage(tmp_path: Path):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one("#status-bar")
        assert "ctx" in str(bar.render()).lower()  # shown even when empty
        # ~500 tokens of content against a 1000-token window -> 50%
        app.harness.max_context_tokens = 1000
        app.harness.history = [
            ModelRequest(parts=[UserPromptPart(content="x" * 2000)])
        ]
        app._refresh_status()
        await pilot.pause()
        assert "50%" in str(bar.render())


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


def _log_text(app) -> str:
    log = app.query_one("#log")
    parts = []
    for c in log.walk_children():
        if hasattr(c, "text"):  # AssistantMessage accumulates into .text
            parts.append(c.text)
        elif hasattr(c, "render"):
            parts.append(str(c.render()))
    return " ".join(parts)


@pytest.mark.anyio
async def test_slash_help_lists_commands(tmp_path: Path):
    app = _app(tmp_path)
    started = []
    app.run_worker = lambda *a, **k: started.append(a)  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, "/help")
        await pilot.pause()
        text = _log_text(app)
        assert "/mode" in text and "/clear" in text
        assert "AGENTS.md" in text  # project-instructions discoverability
        assert started == []  # never sent to the model


@pytest.mark.anyio
async def test_slash_unknown_command_reports_error(tmp_path: Path):
    app = _app(tmp_path)
    started = []
    app.run_worker = lambda *a, **k: started.append(a)  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, "/wat")
        await pilot.pause()
        assert "unknown command" in _log_text(app).lower()
        assert started == []


@pytest.mark.anyio
@pytest.mark.parametrize("arg,expected", [("plan", "plan"), ("auto", "auto")])
async def test_slash_mode_sets_mode(tmp_path: Path, arg: str, expected: str):
    from marim_harness.permissions import Mode

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, f"/mode {arg}")
        await pilot.pause()
        assert app.harness.deps.mode is Mode(expected)


@pytest.mark.anyio
async def test_slash_mode_no_arg_cycles(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        start = app.harness.deps.mode
        await _submit(app, "/mode")
        await pilot.pause()
        assert app.harness.deps.mode is start.cycle()


@pytest.mark.anyio
async def test_slash_clear_resets_conversation(tmp_path: Path):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from marim_harness.session import SessionManager

    app = _app(tmp_path)
    store = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data").create()
    app.harness.store = store
    app.harness.history = [ModelRequest(parts=[UserPromptPart(content="old")])]
    app.harness._persist()
    assert store.path.exists()

    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, "/clear")
        await pilot.pause()
        assert app.harness.history == []
        assert not store.path.exists()  # saved session wiped
        # the banner is back
        assert app.query_one("#banner") is not None


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
async def test_cancel_turn_aborts_and_shows_message(tmp_path: Path):
    import asyncio

    from textual.widgets import Input

    from marim_harness.tui.widgets import ErrorMessage

    app = _app(tmp_path)
    started = asyncio.Event()

    async def hang(*a, **k):
        started.set()
        await asyncio.sleep(3600)

    app.harness.run_turn = hang  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one(Input)
        await app.on_input_submitted(Input.Submitted(inp, "do something slow"))
        for _ in range(50):
            await pilot.pause()
            if started.is_set():
                break
        assert app._busy is True

        app.action_cancel_turn()
        for _ in range(50):
            await pilot.pause()
            if not app._busy:
                break

        assert app._busy is False
        assert app.is_running is True
        errors = list(app.query(ErrorMessage))
        assert any("cancel" in str(e.render()).lower() for e in errors)


@pytest.mark.anyio
async def test_cancel_when_idle_is_a_noop(tmp_path: Path):
    from marim_harness.tui.widgets import ErrorMessage

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_cancel_turn()  # nothing running
        await pilot.pause()
        assert app.is_running is True
        assert list(app.query(ErrorMessage)) == []


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
async def test_ascii_banner_shown_on_start(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        banner = app.query_one("#banner")
        text = str(banner.render())
        assert "█" in text  # the ASCII art rendered
        assert "h a r n e s s" in text  # letter-spaced subtitle


@pytest.mark.anyio
async def test_welcome_message_shown_on_start(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        log = app.query_one("#log")
        text = " ".join(str(c.render()) for c in log.walk_children() if hasattr(c, "render"))
        assert "ctrl+t" in text.lower() or "welcome" in text.lower()


@pytest.mark.anyio
async def test_resumed_session_shows_banner(tmp_path: Path):
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    app = _app(tmp_path)
    # Simulate a resumed conversation: history populated before mount.
    result = await Agent(TestModel(), instructions="x").run("hi")
    app.harness.history = result.all_messages()
    async with app.run_test() as pilot:
        await pilot.pause()
        log = app.query_one("#log")
        text = " ".join(
            str(c.render()) for c in log.walk_children() if hasattr(c, "render")
        )
        assert "resumed" in text.lower()


@pytest.mark.anyio
async def test_resume_replays_history_into_log(tmp_path: Path):
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    from marim_harness.tui.widgets import (
        AssistantMessage,
        ToolCallWidget,
        UserMessage,
    )

    app = _app(tmp_path)
    app.harness.history = [
        ModelRequest(parts=[UserPromptPart(content="read app.py")]),
        ModelResponse(
            parts=[
                TextPart(content="Let me look."),
                ToolCallPart(
                    tool_name="read_file", args={"path": "app.py"}, tool_call_id="t1"
                ),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="read_file", content="1\tprint(1)", tool_call_id="t1"
                )
            ]
        ),
        ModelResponse(parts=[TextPart(content="It prints 1.")]),
    ]
    async with app.run_test() as pilot:
        await pilot.pause()

        users = [str(w.render()) for w in app.query(UserMessage)]
        assert any("read app.py" in u for u in users)

        tools = list(app.query(ToolCallWidget))
        assert len(tools) == 1
        assert tools[0].status == "done"
        assert "print(1)" in tools[0].result_text

        assistant = " ".join(w.text for w in app.query(AssistantMessage))
        assert "Let me look." in assistant
        assert "It prints 1." in assistant
        assert "resumed" in assistant.lower()  # banner still shown


@pytest.mark.anyio
async def test_compaction_shows_notice_in_log(tmp_path: Path):
    from marim_harness.tui.widgets import NoticeMessage

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Simulate the harness reporting a compaction mid-turn.
        app.harness.on_compact(40, 10)
        await pilot.pause()
        notices = list(app.query(NoticeMessage))
        assert len(notices) == 1
        text = str(notices[0].render())
        assert "40" in text and "10" in text
        assert "compact" in text.lower()


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


def _app_with_manager(tmp_path: Path) -> HarnessApp:
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.session import SessionManager
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    store = manager.create("main")
    harness = Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps,
        instructions="test", store=store, manager=manager,
    )
    return HarnessApp(harness)


@pytest.mark.anyio
async def test_new_command_starts_named_session(tmp_path: Path):
    app = _app_with_manager(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, "/new project-x")
        await pilot.pause()
        assert app.harness.session_name == "project-x"
        assert "new session" in _log_text(app).lower()


@pytest.mark.anyio
async def test_sessions_command_lists_saved(tmp_path: Path):
    app = _app_with_manager(tmp_path)
    app.harness.new_session("first")
    app.harness._persist()
    app.harness.new_session("second")
    app.harness._persist()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, "/sessions")
        await pilot.pause()
        text = _log_text(app)
        assert "first" in text
        assert "second" in text


@pytest.mark.anyio
async def test_switch_command_loads_session(tmp_path: Path):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    app = _app_with_manager(tmp_path)
    app.harness.new_session("alpha")
    app.harness.history = [ModelRequest(parts=[UserPromptPart(content="hello alpha")])]
    app.harness._persist()
    app.harness.new_session("beta")
    app.harness._persist()
    assert app.harness.history == []

    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, "/switch alpha")
        await pilot.pause()
        assert app.harness.session_name == "alpha"
        assert len(app.harness.history) == 1
        assert "switched to" in _log_text(app).lower()


@pytest.mark.anyio
async def test_switch_unknown_reports_error(tmp_path: Path):
    app = _app_with_manager(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, "/switch nope")
        await pilot.pause()
        assert "no session matches" in _log_text(app).lower()


def _autoname_app(tmp_path: Path) -> HarnessApp:
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.session import SessionManager
    from marim_harness.tools.provider import BuiltinToolProvider

    async def titler(messages):
        return "Auto Title"

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    harness = Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps,
        instructions="test", store=manager.create(), manager=manager, titler=titler,
    )
    return HarnessApp(harness)


@pytest.mark.anyio
async def test_name_command_sets_title(tmp_path: Path):
    app = _app_with_manager(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, "/name My Project")
        await pilot.pause()
        assert app.harness.session_name == "My Project"
        assert "My Project" in str(app.query_one("#status-bar").render())
        assert "renamed" in _log_text(app).lower()


@pytest.mark.anyio
async def test_name_command_regenerates_with_titler(tmp_path: Path):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    app = _autoname_app(tmp_path)
    app.harness.history = [ModelRequest(parts=[UserPromptPart(content="do work")])]
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, "/name")  # blank -> regenerate from conversation
        await pilot.pause()
        assert app.harness.session_name == "Auto Title"


@pytest.mark.anyio
async def test_autoname_posts_notice_after_first_turn(tmp_path: Path):
    app = _autoname_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.harness.run_turn("hello")
        await pilot.pause()
        assert app.harness.session_name == "Auto Title"
        assert "Auto Title" in _log_text(app)
        assert "Auto Title" in str(app.query_one("#status-bar").render())
