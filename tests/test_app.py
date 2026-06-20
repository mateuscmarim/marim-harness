from pathlib import Path

import pytest

from marim_harness.deps import Deps
from marim_harness.interfaces.tui.app import HarnessApp
from marim_harness.interfaces.tui.widgets import NoticeMessage
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


@pytest.mark.anyio
async def test_status_bar_shows_mode(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one("#status-bar")
        text = str(bar.render())
        assert "ask" in text or "auto" in text


@pytest.mark.anyio
async def test_status_bar_shows_token_split(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one("#status-bar")
        assert "0↑" in str(bar.render())  # starts at zero
        app.harness.session.usage.input_tokens = 12
        app.harness.session.usage.output_tokens = 8
        app._refresh_status()
        await pilot.pause()
        text = str(bar.render())
        assert "12↑" in text  # uncached input
        assert "8↓" in text   # output


@pytest.mark.anyio
async def test_status_bar_shows_estimated_cost_when_model_priced(tmp_path: Path):
    """When the active model is priced, the status bar appends an estimated $cost
    next to the live token counter."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.harness.model_id = "claude-sonnet-4-6"
        app.harness.session.usage.input_tokens = 50000
        app.harness.session.usage.output_tokens = 2000
        app._refresh_status()
        await pilot.pause()
        assert "$" in str(app.query_one("#status-bar").render())


@pytest.mark.anyio
async def test_status_bar_omits_cost_for_unpriced_model(tmp_path: Path):
    """An unknown/local model has no price, so no $cost is shown — just tokens."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.harness.model_id = "some-local-unpriced-model"
        app.harness.session.usage.input_tokens = 5000
        app._refresh_status()
        await pilot.pause()
        text = str(app.query_one("#status-bar").render())
        assert "$" not in text
        assert "5k↑" in text  # split still shown without a price


@pytest.mark.anyio
async def test_status_bar_includes_live_run_tokens_while_streaming(tmp_path: Path):
    """While a turn streams, the bar shows the in-flight run's tokens (not yet
    committed to session usage) as a live ``+N`` delta beside the committed
    split, so spend climbs live instead of only jumping when the turn ends."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.harness.session.usage.input_tokens = 100  # committed from prior turns
        app._set_busy(True)
        app._live_run_tokens = 50  # in-flight this turn
        app._refresh_status()
        await pilot.pause()
        text = str(app.query_one("#status-bar").render())
        assert "100↑" in text  # committed split
        assert "+50" in text   # live in-flight delta


@pytest.mark.anyio
async def test_on_events_tracks_live_run_tokens_from_ctx_usage(tmp_path: Path):
    """The main event handler reads the run's live token total off ctx.usage —
    the same source the sub-agent handler already uses."""
    import types

    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        ToolCallPart,
    )

    ctx = types.SimpleNamespace(usage=types.SimpleNamespace(total_tokens=1234))

    async def gen():
        yield FunctionToolCallEvent(
            part=ToolCallPart(tool_name="read_file", args={}, tool_call_id="c1")
        )

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._on_events(ctx, gen())
        assert app._live_run_tokens == 1234


@pytest.mark.anyio
async def test_live_run_tokens_reset_when_turn_ends(tmp_path: Path):
    """At turn end the harness commits the run into session usage; the in-flight
    counter must reset so the committed tokens aren't shown twice."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._live_run_tokens = 500
        app._set_busy(False)
        assert app._live_run_tokens == 0


@pytest.mark.anyio
async def test_flush_refreshes_status_while_busy(tmp_path: Path):
    """The shared streaming flush tick repaints the status bar while busy, so the
    live token counter advances without waiting for the turn to finish."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._set_busy(True)  # paints the initial split
        app.harness.session.usage.input_tokens = 200
        app._live_run_tokens = 99
        app._flush_streams()  # the per-frame tick picks up the live delta
        await pilot.pause()
        text = str(app.query_one("#status-bar").render())
        assert "200↑" in text  # committed split
        assert "+99" in text   # live in-flight delta picked up by the tick


@pytest.mark.anyio
async def test_flush_only_touches_buffered_messages(tmp_path: Path):
    """The per-tick flush visits only streams that buffered new deltas, not the
    whole message tree — a finished, already-rendered message is left alone so the
    tick stays O(active streams) rather than O(every message ever shown)."""
    from textual.containers import VerticalScroll

    from marim_harness.interfaces.tui.widgets import AssistantMessage

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        log = app.query_one("#log", VerticalScroll)
        clean = AssistantMessage()
        dirty = AssistantMessage()
        await log.mount(clean)
        await log.mount(dirty)
        await pilot.pause()

        flushed: list[str] = []
        clean.flush = lambda: flushed.append("clean")  # type: ignore[assignment]
        dirty.flush = lambda: flushed.append("dirty")  # type: ignore[assignment]

        app._append_stream(dirty, "hello")  # buffers a delta and marks it dirty
        app._flush_streams()

        assert flushed == ["dirty"]  # the clean, untouched message is skipped
        # The dirty set drains each tick, so a second flush with no new deltas is
        # a no-op (nothing re-flushed).
        flushed.clear()
        app._flush_streams()
        assert flushed == []


def test_human_tokens_formatting():
    from marim_harness.interfaces.tui.app import _human_tokens

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
        app.harness.session.max_context_tokens = 1000
        app.harness.session.history = [
            ModelRequest(parts=[UserPromptPart(content="x" * 2000)])
        ]
        app._refresh_status()
        await pilot.pause()
        assert "50%" in str(bar.render())


@pytest.mark.anyio
async def test_title_survives_markup_like_session_name(tmp_path: Path, monkeypatch):
    """A session name is model-generated and may contain bracket sequences like
    `[edit(x="…`. It now lives in the terminal title (a plain string assignment,
    not markup-parsed), so the app must carry it literally and not crash."""
    bomb = '[/] and [edit(old_string="unterminated'
    monkeypatch.setattr(
        type(_app(tmp_path).harness.session), "session_name", property(lambda self: bomb)
    )
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._refresh_title()
        await pilot.pause()
        # The literal name (including its brackets) is in the title, intact.
        assert "[edit(old_string=" in app.title


def _submit(app, text):
    from marim_harness.interfaces.tui.widgets import PromptInput

    pi = app.query_one(PromptInput)
    pi.text = text
    return app.on_prompt_input_submitted(PromptInput.Submitted(text))


@pytest.mark.anyio
async def test_submitting_records_prompt_history(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.history import PromptHistory
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="test"
    )
    hist = PromptHistory()
    app = HarnessApp(harness, history=hist)

    def _swallow(coro, *a, **k):  # don't actually run the turn worker
        coro.close()

    app.run_worker = _swallow  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, "remember this")
        assert hist.entries == ["remember this"]
        # The PromptInput navigates over the very same history.
        from marim_harness.interfaces.tui.widgets import PromptInput

        assert app.query_one(PromptInput).prompt_history is hist


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
    app.harness.session.store = store
    app.harness.session.history = [ModelRequest(parts=[UserPromptPart(content="old")])]
    app.harness.session.persist()
    assert store.path.exists()

    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, "/clear")
        await pilot.pause()
        assert app.harness.session.history == []
        assert not store.path.exists()  # saved session wiped
        # the banner is back
        assert app.query_one("#banner") is not None


@pytest.mark.anyio
async def test_failed_turn_shows_error_and_keeps_running(tmp_path: Path):
    from marim_harness.interfaces.tui.widgets import ErrorMessage

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

    from marim_harness.interfaces.tui.widgets import ErrorMessage, PromptInput

    app = _app(tmp_path)
    started = asyncio.Event()

    async def hang(*a, **k):
        started.set()
        await asyncio.sleep(3600)

    app.harness.run_turn = hang  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.on_prompt_input_submitted(PromptInput.Submitted("do something slow"))
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
    from marim_harness.interfaces.tui.widgets import ErrorMessage

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
async def test_set_busy_survives_missing_status_bar(tmp_path: Path):
    """Regression: a turn finishing while the app tears down (e.g. /exit fired
    mid-turn) runs `_run_turn`'s finally -> `_set_busy(False)` -> `_refresh_status`
    after the status bar has already been removed. It must not raise NoMatches."""
    from textual.widgets import Static

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#status-bar", Static).remove()
        await pilot.pause()
        # No status bar in the DOM; updating status must be a quiet no-op.
        app._set_busy(False)
        app._refresh_status()
        assert app.is_running is True


@pytest.mark.anyio
async def test_log_and_input_both_visible(tmp_path: Path):
    """Status bar and input must not collide; both render at non-zero size."""
    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        from textual.widgets import Footer

        from marim_harness.interfaces.tui.widgets import PromptInput

        status = app.query_one("#status-bar")
        inp = app.query_one(PromptInput)
        footer = app.query_one(Footer)
        assert status.size.height >= 1
        assert inp.size.height >= 1
        # they occupy different rows
        assert status.region.y != inp.region.y
        # the input must not overlap the footer (its last row stays above it)
        assert inp.region.bottom <= footer.region.y


@pytest.mark.anyio
async def test_input_is_focused_on_start(tmp_path: Path):
    """The prompt box should hold focus the moment the app opens, so the user
    can type without first clicking or tabbing into it."""
    from marim_harness.interfaces.tui.widgets import PromptInput

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.focused is app.query_one(PromptInput)


@pytest.mark.anyio
async def test_task_panel_hidden_until_tasks_then_live_updates(tmp_path: Path):
    from marim_harness.interfaces.tui.widgets import TaskPanel

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.query_one(TaskPanel)
        assert panel.display is False  # nothing to show yet

        # The update_tasks tool path fires on_change -> the panel appears live.
        app.harness.deps.tasks.replace([
            {"text": "read the code", "status": "done"},
            {"text": "write the test", "status": "in_progress"},
        ])
        await pilot.pause()
        assert panel.display is True
        text = str(panel.render())
        assert "read the code" in text and "write the test" in text
        assert "✔" in text and "▸" in text

        # Clearing the list hides the panel again (e.g. agent emptied it).
        app.harness.deps.tasks.replace([])
        await pilot.pause()
        assert panel.display is False


@pytest.mark.anyio
async def test_task_panel_reflects_restored_tasks_on_mount(tmp_path: Path):
    from marim_harness.interfaces.tui.widgets import TaskPanel

    app = _app(tmp_path)
    # Simulate a session whose checklist was restored before mount.
    app.harness.deps.tasks.load([{"text": "resumed item", "status": "pending"}])
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.query_one(TaskPanel)
        assert panel.display is True
        assert "resumed item" in str(panel.render())


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
    app.harness.session.history = result.all_messages()
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

    from marim_harness.interfaces.tui.widgets import (
        AssistantMessage,
        ToolCallWidget,
        UserMessage,
    )

    app = _app(tmp_path)
    app.harness.session.history = [
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
async def test_resume_hides_injected_turn_context(tmp_path: Path):
    """A resumed session whose user prompt has a turn-context envelope (job
    digests, SessionStart/UserPromptSubmit hook output) must show only what the
    user typed in the log — not the injected context."""
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

    from marim_harness.agent import wrap_turn_context
    from marim_harness.interfaces.tui.widgets import UserMessage

    wrapped = wrap_turn_context(
        "<agentmemory-context>pinned slots, files, …</agentmemory-context>",
        "I want to implement a fetch tool similar to the one available at claude",
    )
    app = _app(tmp_path)
    app.harness.session.history = [
        ModelRequest(parts=[UserPromptPart(content=wrapped)]),
        ModelResponse(parts=[TextPart(content="ok")]),
    ]
    async with app.run_test() as pilot:
        await pilot.pause()
        users = [str(w.render()) for w in app.query(UserMessage)]
        assert any("implement a fetch tool" in u for u in users)
        # The injected context must not leak into the displayed message.
        assert not any("agentmemory-context" in u for u in users)
        assert not any("pinned slots" in u for u in users)


@pytest.mark.anyio
async def test_gated_tool_renders_one_widget_not_two(tmp_path: Path):
    """A gated tool (bash) goes through the deferred-approval flow, which makes
    pydantic_ai emit the call event twice for one tool_call_id (approval pass +
    execution pass). The log must still show a single finished ToolCallWidget,
    not an orphaned 'pending' entry plus a finished one."""
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    from marim_harness.agent import Harness
    from marim_harness.interfaces.tui.widgets import ToolCallWidget
    from marim_harness.tools.provider import BuiltinToolProvider

    state = {"n": 0}

    async def stream_fn(messages, info):
        state["n"] += 1
        if state["n"] == 1:
            yield {0: DeltaToolCall(
                name="bash", json_args='{"command": "echo hi"}', tool_call_id="b1")}
        else:
            yield "done"

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(FunctionModel(stream_function=stream_fn),
                      BuiltinToolProvider(), deps, instructions="test")
    app = HarnessApp(harness)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._run_turn("run echo")
        await pilot.pause()
        tools = list(app.query(ToolCallWidget))
        assert len(tools) == 1
        assert tools[0].status == "done"
        assert "hi" in tools[0].result_text


@pytest.mark.anyio
async def test_compaction_shows_notice_in_log(tmp_path: Path):
    from marim_harness.interfaces.tui.widgets import NoticeMessage

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Simulate the harness reporting a compaction mid-turn.
        app.harness.session.on_compact(40, 10)
        await pilot.pause()
        notices = list(app.query(NoticeMessage))
        assert len(notices) == 1
        text = str(notices[0].render())
        assert "40" in text and "10" in text
        assert "compact" in text.lower()


@pytest.mark.anyio
async def test_compacting_indicator_shown_then_replaced(tmp_path: Path):
    from marim_harness.interfaces.tui.widgets import NoticeMessage

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Compaction begins: the live "compacting…" indicator appears.
        app.harness.session.on_compact_start()
        await pilot.pause()
        live = [str(n.render()) for n in app.query(NoticeMessage)]
        assert any("compacting" in t.lower() for t in live)
        # Compaction finishes: the indicator is replaced by the result line.
        app.harness.session.on_compact(40, 10)
        await pilot.pause()
        final = [str(n.render()) for n in app.query(NoticeMessage)]
        assert not any("compacting conversation" in t.lower() for t in final)
        assert any("40" in t and "10" in t for t in final)


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

    from marim_harness.interfaces.tui.widgets import ToolCallWidget

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


@pytest.mark.anyio
async def test_spawn_agent_mounts_subagent_widget(tmp_path: Path):
    """A spawn_agent tool call gets a SubAgentWidget (not a generic ToolCallWidget),
    keyed by its tool_call_id, and is finished by the result event."""
    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        ToolCallPart,
        ToolReturnPart,
    )

    from marim_harness.interfaces.tui.widgets import SubAgentWidget

    call = FunctionToolCallEvent(
        part=ToolCallPart(
            tool_name="spawn_agent",
            args={"type": "explore", "task": "find the config loader"},
            tool_call_id="spawn-1",
        )
    )
    result = FunctionToolResultEvent(
        part=ToolReturnPart(
            tool_name="spawn_agent",
            content="found it in config.py",
            tool_call_id="spawn-1",
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

        widget = app._tool_widgets.get("spawn-1")
        assert isinstance(widget, SubAgentWidget)
        assert widget.agent_type == "explore"
        assert "find the config loader" in widget.agent_task
        log = app.query_one("#log")
        assert widget in log.walk_children()
        assert widget.status == "done"
        assert widget.report == "found it in config.py"


@pytest.mark.anyio
async def test_subagent_event_routes_stream_into_widget(tmp_path: Path):
    """The sub-agent's own text and nested tool calls land inside its
    SubAgentWidget body, not the top-level log."""
    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        PartStartEvent,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
    )

    from marim_harness.interfaces.tui.widgets import (
        AssistantMessage,
        SubAgentWidget,
        ToolCallWidget,
    )

    spawn = FunctionToolCallEvent(
        part=ToolCallPart(
            tool_name="spawn_agent",
            args={"type": "explore", "task": "look around"},
            tool_call_id="s1",
        )
    )

    async def spawn_gen():
        yield spawn

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._on_events(None, spawn_gen())
        await pilot.pause()

        parent = app._tool_widgets["s1"]
        assert isinstance(parent, SubAgentWidget)

        # The sub-agent emits text, then a nested read_file call + result.
        await app._on_subagent_event(
            "s1", PartStartEvent(index=0, part=TextPart(content="checking files"))
        )
        await app._on_subagent_event(
            "s1",
            FunctionToolCallEvent(
                part=ToolCallPart(
                    tool_name="read_file",
                    args={"path": "x.py"},
                    tool_call_id="nested-1",
                )
            ),
        )
        await app._on_subagent_event(
            "s1",
            FunctionToolResultEvent(
                part=ToolReturnPart(
                    tool_name="read_file", content="1\tcode", tool_call_id="nested-1"
                )
            ),
        )
        await pilot.pause()

        # Both nested widgets live inside the SubAgentWidget body.
        body_children = list(parent.body.walk_children())
        sub_texts = [c for c in body_children if isinstance(c, AssistantMessage)]
        sub_tools = [c for c in body_children if isinstance(c, ToolCallWidget)]
        assert any("checking files" in m.text for m in sub_texts)
        assert len(sub_tools) == 1
        assert sub_tools[0].status == "done"
        assert "code" in sub_tools[0].result_text


@pytest.mark.anyio
async def test_subagent_event_without_widget_is_noop(tmp_path: Path):
    """A sub-agent event for an unknown stream id must not raise."""
    from pydantic_ai.messages import PartStartEvent, TextPart

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._on_subagent_event(
            "ghost", PartStartEvent(index=0, part=TextPart(content="orphan"))
        )
        await pilot.pause()
        assert app.is_running is True


@pytest.mark.anyio
async def test_subagent_event_usage_populates_title_total_and_body_split(tmp_path: Path):
    """A sub-agent event carrying a RunUsage drives the widget: the collapsed
    title shows the running total, and the expanded body shows the full split."""
    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        PartStartEvent,
        TextPart,
        ToolCallPart,
    )
    from pydantic_ai.usage import RunUsage
    from textual.widgets import Static

    from marim_harness.interfaces.tui.widgets import SubAgentWidget

    spawn = FunctionToolCallEvent(
        part=ToolCallPart(
            tool_name="spawn_agent",
            args={"type": "explore", "task": "look around"},
            tool_call_id="s1",
        )
    )

    async def spawn_gen():
        yield spawn

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._on_events(None, spawn_gen())
        await pilot.pause()
        parent = app._tool_widgets["s1"]
        assert isinstance(parent, SubAgentWidget)

        usage = RunUsage(
            input_tokens=56000, output_tokens=2000,
            cache_read_tokens=50000, cache_write_tokens=5000,
        )
        await app._on_subagent_event(
            "s1",
            PartStartEvent(index=0, part=TextPart(content="checking")),
            usage,
        )
        await pilot.pause()

        # 56k in + 2k out = 58k total, shown compactly in the title.
        assert "58k" in str(parent.title)
        # The full split lands in the body (cost may be absent if unpriced).
        usage_line = parent.body.query_one(".subagent-usage", Static)
        assert "1k↑ 55k⚡ 2k↓" in str(usage_line.visual)


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
        assert app.harness.session.session_name == "project-x"
        assert "new session" in _log_text(app).lower()


@pytest.mark.anyio
async def test_sessions_command_lists_saved(tmp_path: Path):
    app = _app_with_manager(tmp_path)
    app.harness.new_session("first")
    app.harness.session.persist()
    app.harness.new_session("second")
    app.harness.session.persist()
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
    app.harness.session.history = [ModelRequest(parts=[UserPromptPart(content="hello alpha")])]
    app.harness.session.persist()
    app.harness.new_session("beta")
    app.harness.session.persist()
    assert app.harness.session.history == []

    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, "/switch alpha")
        await pilot.pause()
        assert app.harness.session.session_name == "alpha"
        assert len(app.harness.session.history) == 1
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
        assert app.harness.session.session_name == "My Project"
        assert "My Project" in app.title  # name now lives in the terminal title
        assert "renamed" in _log_text(app).lower()


@pytest.mark.anyio
async def test_name_command_regenerates_with_titler(tmp_path: Path):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    app = _autoname_app(tmp_path)
    app.harness.session.history = [ModelRequest(parts=[UserPromptPart(content="do work")])]
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, "/name")  # blank -> regenerate from conversation
        await pilot.pause()
        assert app.harness.session.session_name == "Auto Title"


@pytest.mark.anyio
async def test_autoname_posts_notice_after_first_turn(tmp_path: Path):
    app = _autoname_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.harness.run_turn("hello")
        await pilot.pause()
        assert app.harness.session.session_name == "Auto Title"
        assert "Auto Title" in _log_text(app)
        assert "Auto Title" in app.title  # name now lives in the terminal title


class _FakeSource:
    """Stand-in for ModelSource in app tests: builds TestModels, no network."""

    def __init__(self, entries=None, is_local=False):
        self._entries = entries or []
        self._is_local = is_local
        self.built = []

    def build(self, model_id):
        from pydantic_ai.models.test import TestModel

        self.built.append(model_id)
        return TestModel(call_tools=[])

    def label(self, model_id):
        return f"fake/{model_id}"

    @property
    def is_local(self):
        return self._is_local

    async def list_models(self):
        return self._entries


def _switch_app(tmp_path: Path, source) -> HarnessApp:
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.session import SessionManager
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    harness = Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="test",
        store=manager.create(), manager=manager, model_source=source,
        model_id="startup",
    )
    return HarnessApp(harness)


@pytest.mark.anyio
async def test_model_command_sets_model_directly(tmp_path: Path):
    app = _switch_app(tmp_path, _FakeSource())
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, "/model openai/gpt-5.2")
        await pilot.pause()
        assert app.harness.model_id == "openai/gpt-5.2"
        assert "fake/openai/gpt-5.2" in str(app.query_one("#status-bar").render())


@pytest.mark.anyio
async def test_model_command_opens_picker_from_input(tmp_path: Path):
    """Regression: `/model` (no arg) dispatches from the input handler, which is
    not a worker. The picker must open there without raising NoActiveWorker."""
    from marim_harness.interfaces.tui.model_picker import ModelPickerModal
    from marim_harness.workspace import ModelEntry

    source = _FakeSource(entries=[ModelEntry(id="openai/gpt-5.2", name="GPT-5.2")])
    app = _switch_app(tmp_path, source)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, "/model")  # the exact path that crashed the TUI
        await pilot.pause()
        # the picker is on screen, the app is alive
        assert isinstance(app.screen, ModelPickerModal)
        assert app.is_running is True
        await pilot.press("escape")  # dismiss; model unchanged
        await pilot.pause()
        assert app.harness.model_id == "startup"


@pytest.mark.anyio
async def test_model_picker_applies_choice(tmp_path: Path):
    from marim_harness.interfaces.tui.widgets import NoticeMessage
    from marim_harness.workspace import ModelEntry

    source = _FakeSource(entries=[ModelEntry(id="openai/gpt-5.2", name="GPT-5.2")])
    app = _switch_app(tmp_path, source)

    def fake_push(screen, callback=None):
        if callback is not None:
            callback("openai/gpt-5.2")

    app.push_screen = fake_push  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.open_model_picker()
        await pilot.pause()
        assert app.harness.model_id == "openai/gpt-5.2"
        notices = [str(n.render()) for n in app.query(NoticeMessage)]
        assert any("fake/openai/gpt-5.2" in n for n in notices)


@pytest.mark.anyio
async def test_model_picker_cancel_keeps_model(tmp_path: Path):
    app = _switch_app(tmp_path, _FakeSource())

    def fake_push(screen, callback=None):
        if callback is not None:
            callback(None)  # cancelled

    app.push_screen = fake_push  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.open_model_picker()
        await pilot.pause()
        assert app.harness.model_id == "startup"  # unchanged


@pytest.mark.anyio
async def test_picker_opens_without_blocking_on_catalog_fetch(tmp_path: Path):
    """The picker must appear immediately even when the catalog fetch is slow —
    the modal loads the catalog in its own worker, so a stalled provider never
    holds the UI hostage."""
    import anyio

    from marim_harness.interfaces.tui.model_picker import ModelPickerModal
    from marim_harness.workspace import ModelEntry

    gate = anyio.Event()

    class _SlowSource(_FakeSource):
        async def list_models(self):
            await gate.wait()
            return [ModelEntry(id="openai/gpt-5.2", name="GPT-5.2")]

    app = _switch_app(tmp_path, _SlowSource())
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, "/model")  # the input-handler path
        await pilot.pause()
        # picker is on screen even though list_models is still awaiting the gate
        assert isinstance(app.screen, ModelPickerModal)
        gate.set()  # let the fetch finish; nothing should have blocked
        await pilot.pause()
        await pilot.pause()


@pytest.mark.anyio
async def test_enter_keypress_submits_and_clears(tmp_path: Path):
    """Real key path: Enter routes through the prompt widget to the app, mounts
    the user message, clears the box, and starts a turn."""
    from marim_harness.interfaces.tui.widgets import PromptInput, UserMessage

    app = _app(tmp_path)
    started: list = []

    def fake_worker(coro, *a, **k):
        started.append(coro)
        coro.close()  # we never run it; close to avoid an un-awaited warning

    app.run_worker = fake_worker  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("h", "i")
        await pilot.press("enter")
        await pilot.pause()
        users = [str(w.render()) for w in app.query(UserMessage)]
        assert any("hi" in u for u in users)
        assert pi.text == ""  # box cleared after submit
        assert started  # a turn worker was started


@pytest.mark.anyio
async def test_shift_enter_keypress_does_not_submit(tmp_path: Path):
    """Real key path: Shift+Enter inserts a newline; no turn, no user message."""
    from marim_harness.interfaces.tui.widgets import PromptInput, UserMessage

    app = _app(tmp_path)
    started: list = []
    app.run_worker = lambda *a, **k: started.append(a)  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("a")
        await pilot.press("shift+enter")
        await pilot.press("b")
        await pilot.pause()
        assert pi.text == "a\nb"
        assert not started
        assert not list(app.query(UserMessage))


@pytest.mark.anyio
async def test_job_panel_hidden_until_jobs_then_live_updates(tmp_path: Path):
    import asyncio

    from marim_harness.interfaces.tui.widgets import JobPanel

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.query_one(JobPanel)
        assert panel.display is False  # nothing running yet

        # Registering a job fires on_change -> the panel appears live.
        async def slow() -> str:
            await asyncio.sleep(5)
            return "done"

        job_id = app.harness.deps.jobs.register("bash", "sleep 5", slow())
        await pilot.pause()
        assert panel.display is True
        assert "sleep 5" in str(panel.render())

        # Cancelling it repaints with the terminal status, panel stays visible.
        await app.harness.deps.jobs.cancel(job_id)
        await pilot.pause()
        assert "(cancelled)" in str(panel.render())


@pytest.mark.anyio
async def test_job_panel_reflects_jobs_on_mount(tmp_path: Path):
    import asyncio

    from marim_harness.interfaces.tui.widgets import JobPanel

    app = _app(tmp_path)

    async def slow() -> str:
        await asyncio.sleep(5)
        return "done"

    # A job launched before the app mounts (jobs are process-scoped).
    app.harness.deps.jobs.register("agent", "explore: look", slow())
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.query_one(JobPanel)
        assert panel.display is True
        assert "explore: look" in str(panel.render())
        await app.harness.deps.jobs.cancel_all()


@pytest.mark.anyio
async def test_background_spawn_renders_as_tool_widget(tmp_path: Path):
    """A background spawn_agent doesn't stream, so it gets a plain ToolCallWidget
    rather than a live SubAgentWidget."""
    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        ToolCallPart,
        ToolReturnPart,
    )

    from marim_harness.interfaces.tui.widgets import SubAgentWidget, ToolCallWidget

    call = FunctionToolCallEvent(
        part=ToolCallPart(
            tool_name="spawn_agent",
            args={"type": "explore", "task": "look", "background": True},
            tool_call_id="spawn-bg",
        )
    )
    result = FunctionToolResultEvent(
        part=ToolReturnPart(
            tool_name="spawn_agent",
            content="Started job-1 (agent) — explore: look",
            tool_call_id="spawn-bg",
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
        widget = app._tool_widgets.get("spawn-bg")
        assert isinstance(widget, ToolCallWidget)
        assert not isinstance(widget, SubAgentWidget)
        assert widget.status == "done"


@pytest.mark.anyio
async def test_jobs_cancelled_on_app_exit(tmp_path: Path):
    import asyncio

    app = _app(tmp_path)

    async def slow() -> str:
        await asyncio.sleep(30)
        return "done"

    async with app.run_test() as pilot:
        await pilot.pause()
        job_id = app.harness.deps.jobs.register("bash", "sleep 30", slow())
        await pilot.pause()
        assert app.harness.deps.jobs.get(job_id).status == "running"
    # Leaving the context unmounts the app, which cancels running jobs.
    assert app.harness.deps.jobs.get(job_id).status == "cancelled"


def _spawn_call(tool_call_id: str, task: str):
    from pydantic_ai.messages import FunctionToolCallEvent, ToolCallPart

    return FunctionToolCallEvent(
        part=ToolCallPart(
            tool_name="spawn_agent",
            args={"type": "explore", "task": task},
            tool_call_id=tool_call_id,
        )
    )


@pytest.mark.anyio
async def test_single_subagent_stays_expanded(tmp_path: Path):
    from marim_harness.interfaces.tui.widgets import SubAgentWidget

    async def gen():
        yield _spawn_call("s1", "only one")

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._on_events(None, gen())
        await pilot.pause()
        w = app._tool_widgets["s1"]
        assert isinstance(w, SubAgentWidget)
        assert w.collapsed is False


@pytest.mark.anyio
async def test_parallel_subagents_collapse(tmp_path: Path):
    """A fan-out (>1 sub-agent live at once) collapses every sibling so the log
    stays legible; the user expands the one they want."""
    from marim_harness.interfaces.tui.widgets import SubAgentWidget

    async def gen():
        yield _spawn_call("s1", "first")
        yield _spawn_call("s2", "second")
        yield _spawn_call("s3", "third")

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._on_events(None, gen())
        await pilot.pause()
        for sid in ("s1", "s2", "s3"):
            w = app._tool_widgets[sid]
            assert isinstance(w, SubAgentWidget)
            assert w.collapsed is True


@pytest.mark.anyio
async def test_subagent_event_updates_activity_title(tmp_path: Path):
    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        ToolCallPart,
    )

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Mount a parent sub-agent widget via a spawn call.
        async def spawn():
            yield _spawn_call("s1", "look")

        await app._on_events(None, spawn())
        await pilot.pause()
        # A nested tool call inside the sub-agent updates the parent's title.
        tool_call = FunctionToolCallEvent(
            part=ToolCallPart(
                tool_name="grep", args={"pattern": "x"}, tool_call_id="t1"
            )
        )
        await app._on_subagent_event("s1", tool_call)
        await pilot.pause()
        parent = app._tool_widgets["s1"]
        assert "grep" in str(parent.title)


@pytest.mark.anyio
async def test_subagent_event_updates_token_usage_in_title(tmp_path: Path):
    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        ToolCallPart,
    )
    from pydantic_ai.usage import RunUsage

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        async def spawn():
            yield _spawn_call("s1", "look")

        await app._on_events(None, spawn())
        await pilot.pause()
        tool_call = FunctionToolCallEvent(
            part=ToolCallPart(
                tool_name="grep", args={"pattern": "x"}, tool_call_id="t1"
            )
        )
        # The handler forwards the run's live usage; the title shows the total.
        await app._on_subagent_event(
            "s1", tool_call, RunUsage(input_tokens=1500, output_tokens=500)
        )
        await pilot.pause()
        parent = app._tool_widgets["s1"]
        assert "2k" in str(parent.title)
        assert "tok" in str(parent.title)


@pytest.mark.anyio
async def test_lone_nested_tool_call_is_not_wrapped_in_a_group(tmp_path: Path):
    """A single nested tool call under a sub-agent mounts bare in the body — no
    redundant group wrapper, which is what inflated the sub-agent's height."""
    from pydantic_ai.messages import FunctionToolCallEvent, ToolCallPart

    from marim_harness.interfaces.tui.widgets import (
        SubAgentWidget,
        ToolCallWidget,
        ToolGroupWidget,
    )

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        async def spawn():
            yield _spawn_call("s1", "look")

        await app._on_events(None, spawn())
        await pilot.pause()
        await app._on_subagent_event(
            "s1",
            FunctionToolCallEvent(
                part=ToolCallPart(tool_name="grep", args={}, tool_call_id="t1")
            ),
        )
        await pilot.pause()
        parent = app._tool_widgets["s1"]
        assert isinstance(parent, SubAgentWidget)
        assert len(parent.query(ToolGroupWidget)) == 0
        assert len(parent.query(ToolCallWidget)) == 1


@pytest.mark.anyio
async def test_nested_tool_burst_groups_under_a_subagent(tmp_path: Path):
    """Two-or-more consecutive nested calls fold into one group, reparenting the
    first (bare) call into it."""
    from pydantic_ai.messages import FunctionToolCallEvent, ToolCallPart

    from marim_harness.interfaces.tui.widgets import ToolCallWidget, ToolGroupWidget

    def nested(call_id: str):
        return FunctionToolCallEvent(
            part=ToolCallPart(tool_name="read_file", args={}, tool_call_id=call_id)
        )

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        async def spawn():
            yield _spawn_call("s1", "look")

        await app._on_events(None, spawn())
        await pilot.pause()
        await app._on_subagent_event("s1", nested("t1"))
        await app._on_subagent_event("s1", nested("t2"))
        await app._on_subagent_event("s1", nested("t3"))
        await pilot.pause()
        parent = app._tool_widgets["s1"]
        groups = parent.query(ToolGroupWidget)
        assert len(groups) == 1
        assert len(groups.first().query(ToolCallWidget)) == 3


@pytest.mark.anyio
async def test_streaming_text_is_debounced_until_flush(tmp_path: Path):
    from pydantic_ai.messages import (
        PartDeltaEvent,
        PartStartEvent,
        TextPart,
        TextPartDelta,
    )

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        async def stream():
            yield PartStartEvent(index=0, part=TextPart(content="# Hi"))
            yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=" there"))

        await app._on_events(None, stream())
        msg = app._current_assistant
        # The full text is buffered into the widget...
        assert msg.text == "# Hi there"
        # ...and a delta marks it dirty without rendering (the per-delta debounce).
        # Asserted synchronously so the shared interval timer can't interleave.
        msg.append("!")
        assert msg._pending is True
        # The shared flush renders it and clears the pending flag.
        app._flush_streams()
        assert msg._pending is False


@pytest.mark.anyio
async def test_flush_streams_renders_nested_subagent_text(tmp_path: Path):
    """One shared flush covers nested sub-agent streams too — they are the same
    AssistantMessage class found by a single query."""
    from pydantic_ai.messages import PartStartEvent, TextPart

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        async def spawn():
            yield _spawn_call("s1", "look")

        await app._on_events(None, spawn())
        await pilot.pause()
        await app._on_subagent_event(
            "s1", PartStartEvent(index=0, part=TextPart(content="nested"))
        )
        msg = app._sub_assistants["s1"]
        # Synchronous append → assert → flush so the interval timer can't interleave.
        msg.append("!")
        assert msg._pending is True
        app._flush_streams()
        assert msg._pending is False


@pytest.mark.anyio
async def test_log_is_anchored_to_bottom(tmp_path: Path):
    """The log uses Textual's scroll anchor so streaming content stays pinned to
    the bottom (and re-pins to the true bottom during layout)."""
    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        log = app.query_one("#log")
        assert log.is_anchored is True


@pytest.mark.anyio
async def test_stream_does_not_yank_when_scrolled_up(tmp_path: Path):
    """When the user has scrolled up to read, a streaming event must not snap the
    viewport back to the bottom — scrolling up releases the anchor."""
    from marim_harness.interfaces.tui.widgets import AssistantMessage

    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        log = app.query_one("#log")
        # Overflow the viewport.
        for _ in range(40):
            m = AssistantMessage()
            await log.mount(m)
            m.append("line of text")
        await pilot.pause()
        log.scroll_to(y=0, animate=False)  # releases the anchor
        await pilot.pause()
        assert log.scroll_offset.y == 0

        # A streaming text event arrives — we are scrolled up, so stay put.
        from pydantic_ai.messages import PartStartEvent, TextPart

        async def gen():
            yield PartStartEvent(index=0, part=TextPart(content="new streamed text"))

        await app._on_events(None, gen())
        await pilot.pause()
        assert log.scroll_offset.y == 0


@pytest.mark.anyio
async def test_app_starts_on_saved_marim_theme(tmp_path, monkeypatch):
    """The app registers the marim themes and starts on the persisted one."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from marim_harness import prefs

    prefs.save_theme("marim-violet")

    app = _app(tmp_path)
    async with app.run_test():
        assert app.theme == "marim-violet"
        assert "marim-teal" in app.available_themes


# ---------------------------------------------------------------------------
# Grouping consecutive tool calls (strategy A: a batch-container widget)
# ---------------------------------------------------------------------------


def _call(tool_name: str, call_id: str):
    from pydantic_ai.messages import FunctionToolCallEvent, ToolCallPart

    return FunctionToolCallEvent(
        part=ToolCallPart(tool_name=tool_name, args={}, tool_call_id=call_id)
    )


def _text(content: str):
    from pydantic_ai.messages import PartStartEvent, TextPart

    return PartStartEvent(index=0, part=TextPart(content=content))


@pytest.mark.anyio
async def test_consecutive_tool_calls_group_into_one_widget(tmp_path: Path):
    """A run of back-to-back tool calls collapses into a single ToolGroupWidget
    holding the individual ToolCallWidgets — not N siblings in the log."""
    from marim_harness.interfaces.tui.widgets import ToolCallWidget, ToolGroupWidget

    async def gen():
        yield _call("read_file", "c1")
        yield _call("read_file", "c2")
        yield _call("grep", "c3")

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._on_events(None, gen())
        await pilot.pause()
        groups = app.query(ToolGroupWidget)
        assert len(groups) == 1
        assert len(groups.first().query(ToolCallWidget)) == 3


@pytest.mark.anyio
async def test_lone_tool_call_is_not_wrapped_in_a_group(tmp_path: Path):
    """A single tool call in a run mounts as a bare ToolCallWidget — wrapping one
    tool in a group is pure overhead (a redundant header and an extra click)."""
    from marim_harness.interfaces.tui.widgets import ToolCallWidget, ToolGroupWidget

    async def gen():
        yield _call("read_file", "c1")

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._on_events(None, gen())
        await pilot.pause()
        assert len(app.query(ToolGroupWidget)) == 0
        assert len(app.query(ToolCallWidget)) == 1


@pytest.mark.anyio
async def test_text_between_tool_calls_breaks_the_group(tmp_path: Path):
    """Assistant text is a boundary: single tool calls on either side of it stay
    bare (no group), so the log reflects the model's actual cadence."""
    from marim_harness.interfaces.tui.widgets import ToolCallWidget, ToolGroupWidget

    async def gen():
        yield _call("read_file", "c1")
        yield _text("now let me search")
        yield _call("grep", "c2")

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._on_events(None, gen())
        await pilot.pause()
        assert len(app.query(ToolGroupWidget)) == 0
        assert len(app.query(ToolCallWidget)) == 2


@pytest.mark.anyio
async def test_tool_result_still_resolves_widget_inside_a_group(tmp_path: Path):
    """Grouping only changes the mount target; results must still finish the
    individual tool widget looked up by call id."""
    from pydantic_ai.messages import FunctionToolResultEvent, ToolReturnPart

    from marim_harness.interfaces.tui.widgets import ToolCallWidget

    async def gen():
        yield _call("read_file", "c1")
        yield _call("read_file", "c2")
        yield FunctionToolResultEvent(
            part=ToolReturnPart(
                tool_name="read_file", content="file body", tool_call_id="c1"
            )
        )

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._on_events(None, gen())
        await pilot.pause()
        w = app._tool_widgets["c1"]
        assert isinstance(w, ToolCallWidget)
        assert w.status == "done"
        assert w.result_text == "file body"
        assert "marim-green" in app.available_themes


def _done(value: str):
    """A coroutine that resolves immediately to ``value`` — a finished job body."""
    async def coro():
        return value
    return coro()


@pytest.mark.anyio
async def test_wake_fires_autonomous_turn_when_job_finishes_idle(tmp_path: Path):
    """A background job finishing while the turn worker is idle fires exactly one
    autonomous (empty-prompt) turn and arms the depth counter."""
    started: list = []

    def fake_worker(coro, *a, **k):
        started.append(coro)
        coro.close()  # don't actually run the turn
        return "worker"

    app = _app(tmp_path)
    app.run_worker = fake_worker  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.autonomous_wake is True  # seeded from harness default
        job_id = app.harness.deps.jobs.register("agent", "explore: x", _done("R"))
        await app.harness.deps.jobs.wait(job_id)  # completion fires on_change
        await pilot.pause()
        assert len(started) == 1  # one autonomous turn started
        assert app._auto_turn_depth == 1
        assert any("Resumed" in str(n.render()) for n in app.query(NoticeMessage))


@pytest.mark.anyio
async def test_wake_disabled_does_not_fire(tmp_path: Path):
    started: list = []
    app = _app(tmp_path)
    app.run_worker = lambda c, *a, **k: (started.append(c), c.close())  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        app.autonomous_wake = False
        job_id = app.harness.deps.jobs.register("agent", "explore: x", _done("R"))
        await app.harness.deps.jobs.wait(job_id)
        await pilot.pause()
        assert started == []
        # The digest is left pending for the next user turn.
        assert app.harness.deps.jobs.has_finished_pending() is True


@pytest.mark.anyio
async def test_wake_stops_at_depth_cap(tmp_path: Path):
    started: list = []
    app = _app(tmp_path)
    app.run_worker = lambda c, *a, **k: (started.append(c), c.close())  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        app._auto_turn_depth = app._wake_depth_cap  # already at the cap
        job_id = app.harness.deps.jobs.register("agent", "explore: x", _done("R"))
        await app.harness.deps.jobs.wait(job_id)
        await pilot.pause()
        assert started == []  # capped, no further autonomous turn


@pytest.mark.anyio
async def test_wake_does_not_fire_while_a_turn_is_running(tmp_path: Path):
    started: list = []
    app = _app(tmp_path)
    app.run_worker = lambda c, *a, **k: (started.append(c), c.close())  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        app._turn_worker = object()  # pretend a turn is in flight
        job_id = app.harness.deps.jobs.register("agent", "explore: x", _done("R"))
        await app.harness.deps.jobs.wait(job_id)
        await pilot.pause()
        assert started == []  # queued; drains on the next turn's completion
        app._turn_worker = None  # turn ends -> finally calls _maybe_wake
        app._maybe_wake()
        assert len(started) == 1


@pytest.mark.anyio
async def test_user_turn_resets_auto_depth(tmp_path: Path):
    app = _app(tmp_path)
    app.run_worker = lambda c, *a, **k: (c.close() if hasattr(c, "close") else None)  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        app._auto_turn_depth = 2
        await _submit(app, "do something")  # a user-initiated turn
        assert app._auto_turn_depth == 0


@pytest.mark.anyio
async def test_ask_user_callback_is_wired(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.harness.deps.ask_user == app._ask_user


@pytest.mark.anyio
async def test_ask_user_callback_shows_modal_and_returns_answer(tmp_path: Path):
    from marim_harness.ask_user import Choice, Question

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        qs = [Question("Pick one", "Pick", [Choice("Alpha"), Choice("Beta")])]
        worker = app.run_worker(app._ask_user(qs))
        await pilot.pause()
        await pilot.press("enter")  # selects highlighted "Alpha"
        await pilot.pause()
        assert worker.result == {"Pick": "Alpha"}


def test_format_duration_units():
    from marim_harness.interfaces.tui.app import _format_duration

    assert _format_duration(5) == "5s"
    assert _format_duration(5, precise=True) == "5.0s"
    assert _format_duration(65) == "1m"
    assert _format_duration(3725) == "1h 2m"


@pytest.mark.anyio
async def test_status_shows_session_duration(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = str(app.query_one("#status-bar").render())
        assert "session" in text


@pytest.mark.anyio
async def test_status_shows_live_turn_timer_when_busy(tmp_path: Path):
    import time

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._busy = True
        app._turn_start = time.monotonic() - 5  # 5s into a turn
        app._refresh_status()
        await pilot.pause()
        text = str(app.query_one("#status-bar").render())
        assert "working" in text and "5s" in text


@pytest.mark.anyio
async def test_successful_turn_stamps_duration(tmp_path: Path):
    from marim_harness.interfaces.tui.widgets import TurnMeta

    async def fake_run_turn(*a, **k):
        return "ok"

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.harness.run_turn = fake_run_turn
        await app._run_turn("hi")
        await pilot.pause()
        metas = list(app.query(TurnMeta))
        assert len(metas) == 1
        assert "s" in str(metas[0].render())


@pytest.mark.anyio
async def test_errored_turn_does_not_stamp_duration(tmp_path: Path):
    from marim_harness.interfaces.tui.widgets import ErrorMessage, TurnMeta

    async def boom(*a, **k):
        raise RuntimeError("nope")

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.harness.run_turn = boom
        await app._run_turn("hi")
        await pilot.pause()
        assert list(app.query(TurnMeta)) == []
        assert list(app.query(ErrorMessage))  # error surfaced instead


@pytest.mark.anyio
async def test_title_shows_idle_and_working_indicator(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        type(_app(tmp_path).harness.session),
        "session_name", property(lambda self: "my-session"),
    )
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.title == "○ my-session"
        app._busy = True
        app._spin = 0  # first spinner frame (⠋)
        app._refresh_title()
        assert app.title == "⠋ my-session"
        # workspace path stays in the sub_title
        assert str(tmp_path) in app.sub_title


@pytest.mark.anyio
async def test_unnamed_session_title_falls_back(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        type(_app(tmp_path).harness.session),
        "session_name", property(lambda self: None),
    )
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.title == "○ marim-harness"


@pytest.mark.anyio
async def test_session_name_in_title_not_status_bar(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        type(_app(tmp_path).harness.session),
        "session_name", property(lambda self: "secret-session"),
    )
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "secret-session" in app.title
        assert "secret-session" not in str(app.query_one("#status-bar").render())


def test_osc_title_sequence_format():
    from marim_harness.interfaces.tui.app import _osc_title

    # OSC 0 sets both the terminal tab and window title: ESC ] 0 ; <text> BEL
    assert _osc_title("● my-session") == "\033]0;● my-session\007"


@pytest.mark.anyio
async def test_refresh_title_writes_osc_to_terminal(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        type(_app(tmp_path).harness.session),
        "session_name", property(lambda self: "my-session"),
    )
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        calls: list[str] = []
        real_write = app._driver.write
        app._driver.write = lambda data: calls.append(data)
        try:
            app._busy = True
            app._spin = 0  # first spinner frame (⠋)
            app._refresh_title()
        finally:
            app._driver.write = real_write
        blob = "".join(calls)
        assert "\033]0;⠋ my-session\007" in blob


@pytest.mark.anyio
async def test_ctrl_o_toggles_reveal_all_outputs(tmp_path: Path):
    from textual.containers import VerticalScroll

    from marim_harness.interfaces.tui.widgets import ToolCallWidget

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        log = app.query_one("#log", VerticalScroll)
        w = ToolCallWidget(
            "edit_file",
            {"path": "a.py", "edits": [{"old_string": "x", "new_string": "y"}]},
        )
        await log.mount(w)
        await pilot.pause()
        assert w.reveal is False and app._show_all_output is False
        app.action_toggle_outputs()
        await pilot.pause()
        assert app._show_all_output is True and w.reveal is True
        app.action_toggle_outputs()
        await pilot.pause()
        assert app._show_all_output is False and w.reveal is False


@pytest.mark.anyio
async def test_busy_title_uses_spinner_frame(tmp_path: Path, monkeypatch):
    from marim_harness.interfaces.tui.app import _SPINNER

    monkeypatch.setattr(
        type(_app(tmp_path).harness.session),
        "session_name", property(lambda self: "my-session"),
    )
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._busy = True
        for frame_idx, glyph in enumerate(_SPINNER):
            app._spin = frame_idx
            app._refresh_title()
            assert app.title == f"{glyph} my-session"


@pytest.mark.anyio
async def test_tick_spinner_advances_only_when_busy(tmp_path: Path, monkeypatch):
    from marim_harness.interfaces.tui.app import _SPINNER

    monkeypatch.setattr(
        type(_app(tmp_path).harness.session),
        "session_name", property(lambda self: "my-session"),
    )
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Idle: the tick is a no-op and the title stays the static ○.
        app._busy = False
        app._spin = 0
        app._tick_spinner()
        assert app._spin == 0
        assert app.title == "○ my-session"
        # Busy: the tick advances the frame and re-renders the title.
        app._busy = True
        app._spin = 0
        app._tick_spinner()
        assert app._spin == 1
        assert app.title == f"{_SPINNER[1]} my-session"
