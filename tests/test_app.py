from pathlib import Path

import pytest

from marim_harness.interfaces.tui.app import HarnessApp
from marim_harness.interfaces.tui.widgets import NoticeMessage
from marim_harness.runtime.permissions import Mode
from tests.conftest import _make_deps


def _app(tmp_path: Path) -> HarnessApp:
    from pydantic_ai.models.test import TestModel

    from marim_harness.runtime.harness import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = _make_deps(tmp_path)
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
async def test_after_turn_survives_drain_failure(tmp_path: Path):
    """_after_turn runs from _run_turn's finally; if starting the next queued
    turn raises, it must not propagate (which would kill the worker before it
    unwinds). The queue pauses and the error surfaces instead."""
    from marim_harness.interfaces.tui.widgets import ErrorMessage

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        async def boom() -> None:
            raise RuntimeError("drain exploded")

        app._drain_next = boom  # type: ignore[method-assign]
        app._enqueue("queued message")
        assert app._queue and not app._queue.paused

        # Must not raise out of _after_turn.
        await app._after_turn()
        await pilot.pause()

        assert app._queue.paused is True
        errors = [w for w in app.query(ErrorMessage)]
        assert any("failed to start next turn" in str(w.render()) for w in errors)


@pytest.mark.anyio
async def test_resumed_spawn_agent_renders_as_subagent_card(tmp_path: Path):
    """On resume, a foreground spawn_agent call in history rebuilds as a
    SubAgentWidget carrying its final report — not a generic tool widget."""
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        ToolCallPart,
        ToolReturnPart,
    )

    from marim_harness.interfaces.tui.subagents import SubAgentWidget
    from marim_harness.interfaces.tui.widgets import ToolCallWidget

    app = _app(tmp_path)
    app.harness.session.history = [
        ModelResponse(parts=[ToolCallPart(
            tool_name="spawn_agent",
            args={"type": "explore", "task": "review the core loop"},
            tool_call_id="s1",
        )]),
        ModelRequest(parts=[ToolReturnPart(
            tool_name="spawn_agent", content="REPORT-BODY", tool_call_id="s1",
        )]),
    ]
    async with app.run_test() as pilot:
        await pilot.pause()
        cards = list(app.query(SubAgentWidget))
        assert len(cards) == 1
        card = cards[0]
        assert card.agent_type == "explore"
        assert card.report == "REPORT-BODY"
        assert card.status == "done"
        # It is NOT also rendered as a generic spawn_agent tool widget.
        generic = [w for w in app.query(ToolCallWidget) if w.tool_name == "spawn_agent"]
        assert generic == []


@pytest.mark.anyio
async def test_resumed_spawn_card_label_prefers_description(tmp_path: Path):
    """When the spawn carried a short `description`, the resumed card uses it as
    the label instead of the full task."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    from marim_harness.interfaces.tui.subagents import SubAgentWidget

    app = _app(tmp_path)
    app.harness.session.history = [
        ModelResponse(parts=[ToolCallPart(
            tool_name="spawn_agent",
            args={"type": "explore", "task": "a long task body",
                  "description": "review core loop"},
            tool_call_id="s1",
        )]),
    ]
    async with app.run_test() as pilot:
        await pilot.pause()
        cards = list(app.query(SubAgentWidget))
        assert len(cards) == 1
        # The card *label* prefers the short description; agent_task stays the full
        # spawn prompt (the two were split so a short label never displaces the
        # disclosure's verbatim task — see SubAgentWidget).
        assert cards[0].display_title() == "review core loop"
        assert cards[0].agent_task == "a long task body"


@pytest.mark.anyio
async def test_status_bar_shows_token_split(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one("#status-bar")
        assert "0↑" in str(bar.render())  # starts at zero
        app.harness.session.usage.input_tokens = 12
        app.harness.session.usage.output_tokens = 8
        app.status.refresh_status()
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
        app.status.refresh_status()
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
        app.status.refresh_status()
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
        app.status.set_busy(True)
        app.stream.live_run_tokens = 50  # in-flight this turn
        app.status.refresh_status()
        await pilot.pause()
        text = str(app.query_one("#status-bar").render())
        assert "100↑" in text  # committed split
        assert "+50" in text   # live in-flight delta


@pytest.mark.anyio
async def test_bind_ui_wires_ttft_reports_to_the_renderer(tmp_path: Path):
    """TTFT is reported by the controller's TtftTrackingModel wrapper through
    bind_ui's on_ttft callback (measuring in on_events would always read ~0:
    pydantic-ai waits for the first chunk while opening the stream, before the
    handler is invoked). The app wires that callback to the renderer slot the
    status bar reads."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        on_ttft = app.harness.deps.ui.on_ttft
        assert on_ttft is not None  # bind_ui wired it; the controller wraps only then
        assert app.stream.last_ttft is None  # no request streamed yet
        on_ttft(1.23)  # what the wrapper does after each streamed request
        assert app.stream.last_ttft == 1.23


@pytest.mark.anyio
async def test_status_bar_shows_ttft_once_known(tmp_path: Path):
    """The bar surfaces the latest request's TTFT; before any stream the field
    is absent entirely (no 'ttft ?' placeholder noise)."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = str(app.query_one("#status-bar").render())
        assert "ttft" not in text  # nothing streamed yet
        app.stream.last_ttft = 0.83
        app.status.refresh_status()
        await pilot.pause()
        text = str(app.query_one("#status-bar").render())
        assert "ttft 0.8s" in text


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
        await app.stream.on_events(ctx, gen())
        assert app.stream.live_run_tokens == 1234


@pytest.mark.anyio
async def test_live_run_tokens_reset_when_turn_ends(tmp_path: Path):
    """At turn end the harness commits the run into session usage; the in-flight
    counter must reset so the committed tokens aren't shown twice."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.stream.live_run_tokens = 500
        app.status.set_busy(False)
        assert app.stream.live_run_tokens == 0


@pytest.mark.anyio
async def test_flush_refreshes_status_while_busy(tmp_path: Path):
    """The shared streaming flush tick repaints the status bar while busy, so the
    live token counter advances without waiting for the turn to finish."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.status.set_busy(True)  # paints the initial split
        app.harness.session.usage.input_tokens = 200
        app.stream.live_run_tokens = 99
        app.stream.flush_streams()  # the per-frame tick picks up the live delta
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

        def _stub(widget, name):
            # Model a real successful flush: render and clear _pending, so
            # flush_streams' re-arm (which retries a stream still _pending, e.g. one
            # holding off mid-append) doesn't keep it in the dirty set.
            def _flush() -> bool:
                flushed.append(name)
                widget._pending = False
                return True
            return _flush

        clean.flush = _stub(clean, "clean")  # type: ignore[assignment]
        dirty.flush = _stub(dirty, "dirty")  # type: ignore[assignment]

        app.stream.append_stream(dirty, "hello")  # buffers a delta and marks it dirty
        app.stream.flush_streams()

        assert flushed == ["dirty"]  # the clean, untouched message is skipped
        # The dirty set drains each tick, so a second flush with no new deltas is
        # a no-op (nothing re-flushed).
        flushed.clear()
        app.stream.flush_streams()
        assert flushed == []


@pytest.mark.anyio
async def test_thinking_stream_renders_inline_widget(tmp_path: Path):
    """A streamed ThinkingPart mounts an inline ThinkingWidget and its deltas
    accumulate in the widget's text — reasoning is shown as its own styled block,
    never as ordinary assistant text."""
    import types

    from pydantic_ai.messages import (
        PartDeltaEvent,
        PartStartEvent,
        ThinkingPart,
        ThinkingPartDelta,
    )

    from marim_harness.interfaces.tui.widgets import AssistantMessage, ThinkingWidget

    ctx = types.SimpleNamespace(usage=types.SimpleNamespace(total_tokens=0))

    async def gen():
        yield PartStartEvent(index=0, part=ThinkingPart(content="step one"))
        yield PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta=" step two"))

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_events(ctx, gen())
        await pilot.pause()

        thinking = list(app.query(ThinkingWidget))
        assert len(thinking) == 1
        # The streaming interface writes through ``body`` (which is the widget).
        assert thinking[0].body is thinking[0]
        assert thinking[0].text == "step one step two"

        # The reasoning is NOT rendered as ordinary assistant text.
        assert all("step one" not in w.text for w in app.query(AssistantMessage))


@pytest.mark.anyio
async def test_thinking_caps_after_stream_and_reveals_on_ctrl_o(tmp_path: Path):
    """A long thought streams in full, then caps to its last lines once the next
    part (assistant text) starts. Ctrl+O reveals the full reasoning in place and a
    second press restores the capped preview."""
    import types

    from pydantic_ai.messages import (
        PartStartEvent,
        TextPart,
        ThinkingPart,
    )

    from marim_harness.interfaces.tui.widgets import ThinkingWidget

    ctx = types.SimpleNamespace(usage=types.SimpleNamespace(total_tokens=0))
    long_thought = "\n".join(f"line {i}" for i in range(40))

    async def gen():
        yield PartStartEvent(index=0, part=ThinkingPart(content=long_thought))
        # The assistant reply that follows ends the thought, capping it.
        yield PartStartEvent(index=1, part=TextPart(content="the answer"))

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_events(ctx, gen())
        await pilot.pause()

        thinking = app.query_one(ThinkingWidget)
        assert thinking.text == long_thought  # full reasoning retained in state
        capped = str(thinking._render())
        assert "line 0" not in capped and "line 39" in capped  # tail kept
        assert "more lines (ctrl+o)" in capped

        await pilot.press("ctrl+o")  # reveal-all
        await pilot.pause()
        revealed = str(thinking._render())
        assert "line 0" in revealed and "more lines" not in revealed

        await pilot.press("ctrl+o")  # restore capped preview
        await pilot.pause()
        assert "line 0" not in str(thinking._render())


@pytest.mark.anyio
async def test_replay_renders_compaction_summary_as_widget(tmp_path: Path):
    """A restored summary message renders as a distinct SummaryWidget, while a
    normal prompt still renders as a UserMessage."""
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from marim_harness.compaction import SUMMARY_PREFIX
    from marim_harness.interfaces.tui.widgets import SummaryWidget, UserMessage

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        app.harness.session.history = [
            ModelRequest(parts=[UserPromptPart(
                content=f"{SUMMARY_PREFIX}\n\nthe condensed story")]),
            ModelRequest(parts=[UserPromptPart(content="a normal question")]),
        ]
        await app.session.render_session("resume")
        await pilot.pause()

        summaries = list(app.query(SummaryWidget))
        assert len(summaries) == 1
        assert "the condensed story" in str(summaries[0]._body.render())

        users = [str(u.render()) for u in app.query(UserMessage)]
        assert any("a normal question" in u for u in users)
        assert not any("Summary of earlier conversation" in u for u in users)


@pytest.mark.anyio
async def test_replay_caps_restored_thinking(tmp_path: Path):
    """A restored ThinkingPart is an already-complete thought, so the resumed view
    caps it to its preview (matching the live resting state) rather than dumping
    the full reasoning."""
    from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart

    from marim_harness.interfaces.tui.widgets import ThinkingWidget

    long_thought = "\n".join(f"line {i}" for i in range(40))
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        app.harness.session.history = [
            ModelResponse(parts=[
                ThinkingPart(content=long_thought),
                TextPart(content="the answer"),
            ]),
        ]
        await app.session.render_session("resume")
        await pilot.pause()

        thinking = app.query_one(ThinkingWidget)
        rendered = str(thinking._render())
        assert "line 0" not in rendered and "line 39" in rendered
        assert "more lines (ctrl+o)" in rendered


@pytest.mark.anyio
async def test_live_compaction_mounts_summary_widget(tmp_path: Path):
    """When compaction happens during a session, _on_compact mounts the just-made
    summary as a SummaryWidget right after the notice."""
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from marim_harness.compaction import SUMMARY_PREFIX
    from marim_harness.interfaces.tui.widgets import SummaryWidget

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        app.harness.session.history = [
            ModelRequest(parts=[UserPromptPart(content="original task")]),
            ModelRequest(parts=[UserPromptPart(
                content=f"{SUMMARY_PREFIX}\n\nlive-made summary body")]),
        ]
        app._on_compact(80, 22)
        await pilot.pause()
        widgets = list(app.query(SummaryWidget))
        assert len(widgets) == 1
        assert "live-made summary body" in str(widgets[0]._body.render())


def test_human_tokens_formatting():
    from marim_harness.interfaces.tui.widgets import human_tokens as _human_tokens

    assert _human_tokens(0) == "0"
    assert _human_tokens(950) == "950"
    assert _human_tokens(1500) == "1.5k"
    assert _human_tokens(100_000) == "100k"
    assert _human_tokens(1_000_000) == "1M"
    assert _human_tokens(1_500_000) == "1.5M"
    assert _human_tokens(12_820_900) == "12.8M"


@pytest.mark.anyio
async def test_status_bar_shows_context_usage(tmp_path: Path):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one("#status-bar")
        assert "ctx" in str(bar.render()).lower()  # shown even when empty
        # ~500 tokens of content against a 1000-token window -> 50%. The gauge
        # denominates against compact_threshold, which defers to a wired
        # ContextLimits when present (build_collaborators always wires one);
        # drop it here to exercise the legacy fixed-budget fallback directly.
        app.harness.session.limits = None
        app.harness.session.max_context_tokens = 1000
        app.harness.session.history = [
            ModelRequest(parts=[UserPromptPart(content="x" * 2000)])
        ]
        app.status.refresh_status()
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
        app.status.refresh_title()
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

    from marim_harness.interfaces.history import PromptHistory
    from marim_harness.runtime.harness import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = _make_deps(tmp_path)
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
        await _submit(app, cmd)  # /exit quits immediately
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

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, f"/mode {arg}")
        await pilot.pause()
        assert app.harness.deps.workspace.mode is Mode(expected)


@pytest.mark.anyio
async def test_slash_mode_no_arg_cycles(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        start = app.harness.deps.workspace.mode
        await _submit(app, "/mode")
        await pilot.pause()
        assert app.harness.deps.workspace.mode is start.cycle()


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
        assert app.status.busy is True

        app.action_cancel_turn()
        for _ in range(50):
            await pilot.pause()
            if not app.status.busy:
                break

        assert app.status.busy is False
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
        app.status.set_busy(True)
        await pilot.pause()
        assert "working" in str(bar.render()).lower()
        app.status.set_busy(False)
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
        app.status.set_busy(False)
        app.status.refresh_status()
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
        text = str(app.query_one("#task-body").render())
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
        assert "resumed item" in str(app.query_one("#task-body").render())


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

    from marim_harness.interfaces.tui.widgets import UserMessage
    from marim_harness.runtime.harness import wrap_turn_context

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

    from marim_harness.interfaces.tui.widgets import ToolCallWidget
    from marim_harness.runtime.harness import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    state = {"n": 0}

    async def stream_fn(messages, info):
        state["n"] += 1
        if state["n"] == 1:
            yield {0: DeltaToolCall(
                name="bash", json_args='{"command": "echo hi"}', tool_call_id="b1")}
        else:
            yield "done"

    deps = _make_deps(tmp_path)
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
        start = app.harness.deps.workspace.mode
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.harness.deps.workspace.mode is not start


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
        await app.stream.on_events(None, gen())
        await pilot.pause()

        widget = app.stream.tool_widgets.get("call-1")
        assert isinstance(widget, ToolCallWidget)
        log = app.query_one("#log")
        assert widget in log.walk_children()
        assert widget.status == "done"
        assert "1\tfoo" in widget.result_text


def _fake_jobs(job):
    class _Reg:
        def get(self, _id):
            return job
    return _Reg()


def test_wait_subagent_label_for_agent_job_any_status():
    from types import SimpleNamespace

    from marim_harness.interfaces.tui.stream_render import _wait_subagent_label

    # A label is returned for an agent job regardless of status — we're naming the
    # wait, not carding it, so the running case is included (unlike the old card).
    running = SimpleNamespace(kind="agent", status="running", label="explore: map the loop")
    assert _wait_subagent_label({"id": "j1"}, _fake_jobs(running)) == "explore: map the loop"
    done = SimpleNamespace(kind="agent", status="done", label="explore: x")
    assert _wait_subagent_label({"id": "j1"}, _fake_jobs(done)) == "explore: x"


def test_wait_subagent_label_skips_bash_and_missing():
    from types import SimpleNamespace

    from marim_harness.interfaces.tui.stream_render import _wait_subagent_label

    bash = SimpleNamespace(kind="bash", status="running", label="echo: hi")
    assert _wait_subagent_label({"id": "j1"}, _fake_jobs(bash)) is None
    assert _wait_subagent_label({"id": "j1"}, _fake_jobs(None)) is None


@pytest.mark.anyio
async def test_wait_for_job_row_names_the_subagent(tmp_path: Path):
    """Waiting on a (still-running) detached sub-agent is a thin tool row — not a
    card (the spawn owns the card) — but the row names the sub-agent it's blocking
    on instead of a bare job id."""
    import asyncio

    from pydantic_ai.messages import FunctionToolCallEvent, ToolCallPart

    from marim_harness.interfaces.tui.subagents import SubAgentWidget
    from marim_harness.interfaces.tui.widgets import ToolCallWidget
    from marim_harness.interfaces.tui.widgets.tool_summary import summarize

    app = _app(tmp_path)
    reg = app.harness.deps.jobs
    gate = asyncio.Event()

    async def _work():
        await gate.wait()
        return "r"

    jid = reg.register("agent", "explore: review TUI subsystem", _work())  # running

    call = FunctionToolCallEvent(part=ToolCallPart(
        tool_name="wait_for_job", args={"id": jid}, tool_call_id="w1"))

    async def gen():
        yield call

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_events(None, gen())
        await pilot.pause()
        widget = app.stream.tool_widgets.get("w1")
        assert isinstance(widget, ToolCallWidget)        # a row, not a card
        assert not isinstance(widget, SubAgentWidget)
        # The row's preview names the sub-agent, not just the job id.
        assert "review TUI subsystem" in summarize("wait_for_job", widget.args).target
        gate.set()


def test_subagent_failed_detects_runner_error_text():
    from marim_harness.interfaces.tui.stream_render import subagent_failed

    assert subagent_failed("Sub-agent 'explore' failed: ValueError: boom") is True
    assert subagent_failed("No sub-agent type 'ghost'. Available: explore") is True
    assert subagent_failed("Couldn't create an isolated worktree: locked") is True
    # A normal report (even one that mentions a sub-agent) is not a failure.
    assert subagent_failed("Here is my report. The sub-agent system looks fine.") is False


def test_subagent_failed_detects_after_rejections():
    from marim_harness.interfaces.tui.stream_render import subagent_failed

    assert subagent_failed("Cannot spawn with after=['job-9']: no such job(s).") is True
    assert subagent_failed("after= requires a detached spawn. Pass background=True…") is True


def test_after_ids_normalizes_str_and_list():
    from marim_harness.interfaces.tui.stream_render import _after_ids

    assert _after_ids({"after": "job-1"}) == ["job-1"]
    assert _after_ids({"after": ["job-1", " job-2 ", ""]}) == ["job-1", "job-2"]
    assert _after_ids({"after": None}) == []
    assert _after_ids({}) == []


def test_deps_pending_only_while_a_prerequisite_runs():
    from types import SimpleNamespace

    from marim_harness.interfaces.tui.stream_render import _deps_pending

    jobs = SimpleNamespace(get=lambda jid: {
        "job-1": SimpleNamespace(status="running"),
        "job-2": SimpleNamespace(status="done"),
    }.get(jid))
    assert _deps_pending(["job-1", "job-2"], jobs) is True
    assert _deps_pending(["job-2"], jobs) is False
    # A pruned/unknown id counts as settled — never blocks a card forever.
    assert _deps_pending(["job-gone"], jobs) is False


def test_blocked_by_id_parses_prerequisite_failures():
    from marim_harness.interfaces.tui.stream_render import blocked_by_id

    assert blocked_by_id("prerequisite job-3 failed — boom") == "job-3"
    assert blocked_by_id("PrerequisiteFailed: prerequisite job-7 cancelled") == "job-7"
    assert blocked_by_id("Sub-agent 'merge' failed: ValueError: boom") is None
    assert blocked_by_id("prerequisite job-2 no longer exists") == "job-2"
    assert blocked_by_id("prerequisite check failed — flaky infra") is None


def test_detached_job_id_round_trips_with_the_handoff():
    from marim_harness.interfaces.tui.stream_render import _detached_job_id
    from marim_harness.tools.spawn_tools import _detach_handoff

    assert _detached_job_id(_detach_handoff("job-7")) == "job-7"
    # A normal report is not a handoff.
    assert _detached_job_id("Here is my report on the parser.") is None
    assert _detached_job_id("") is None


def test_detached_job_id_parses_explicit_background_return():
    """An explicit background=True agent spawn returns
    "Started <id> (agent) — <label>" (provider.py spawn_agent) — the parser must
    recover its job id too, so the card fills for that path as well."""
    from marim_harness.interfaces.tui.stream_render import _detached_job_id

    assert _detached_job_id("Started job-9 (agent) — explore: map the loop") == "job-9"
    # A bash background job is not an agent spawn → no card.
    assert _detached_job_id("Started job-3 (bash) — echo hi") is None
    # A normal report that merely starts with "Started" is not a handoff.
    assert _detached_job_id("Started writing the report; here goes.") is None


@pytest.mark.anyio
async def test_detached_card_stays_pending_then_fills_on_settle(tmp_path: Path):
    """An auto-detached spawn's card holds at pending on the handoff note, then
    fills with the real report when the background job finishes."""
    import asyncio

    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        ToolCallPart,
        ToolReturnPart,
    )

    from marim_harness.interfaces.tui.subagents import SubAgentWidget
    from marim_harness.tools.spawn_tools import _detach_handoff

    app = _app(tmp_path)
    reg = app.harness.deps.jobs
    gate = asyncio.Event()

    async def _work():
        await gate.wait()
        return "THE REAL REPORT"

    jid = reg.register("agent", "explore: map the core loop", _work())  # running

    call = FunctionToolCallEvent(part=ToolCallPart(
        tool_name="spawn_agent",
        args={"type": "explore", "task": "map the core loop"},
        tool_call_id="s1"))
    result = FunctionToolResultEvent(part=ToolReturnPart(
        tool_name="spawn_agent", content=_detach_handoff(jid), tool_call_id="s1"))

    async def gen():
        yield call
        yield result

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_events(None, gen())
        await pilot.pause()
        card = app.stream.tool_widgets.get("s1")
        assert isinstance(card, SubAgentWidget)
        assert card.status == "pending"          # not finished on the handoff
        assert card.report != _detach_handoff(jid)

        gate.set()                               # let the job finish
        for _ in range(400):
            if reg.get(jid).status != "running":
                break
            await asyncio.sleep(0)
        app.stream.fill_finished_detached_cards(reg)
        await pilot.pause()
        assert card.status == "done"
        assert card.report == "THE REAL REPORT"


@pytest.mark.anyio
async def test_detached_card_fills_failed_when_job_fails(tmp_path: Path):
    import asyncio

    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        ToolCallPart,
        ToolReturnPart,
    )

    from marim_harness.interfaces.tui.subagents import SubAgentWidget
    from marim_harness.tools.spawn_tools import _detach_handoff

    app = _app(tmp_path)
    reg = app.harness.deps.jobs

    async def _boom():
        raise ValueError("upstream 500")

    jid = reg.register("agent", "explore: x", _boom())
    for _ in range(400):  # let it settle to failed
        if reg.get(jid).status != "running":
            break
        await asyncio.sleep(0)

    call = FunctionToolCallEvent(part=ToolCallPart(
        tool_name="spawn_agent", args={"type": "explore", "task": "x"},
        tool_call_id="s1"))
    result = FunctionToolResultEvent(part=ToolReturnPart(
        tool_name="spawn_agent", content=_detach_handoff(jid), tool_call_id="s1"))

    async def gen():
        yield call
        yield result

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_events(None, gen())   # job already terminal → immediate fill
        await pilot.pause()
        card = app.stream.tool_widgets.get("s1")
        assert isinstance(card, SubAgentWidget)
        assert card.status == "failed"
        assert "upstream 500" in card.report  # the failure text lands on the card


@pytest.mark.anyio
async def test_explicit_background_spawn_renders_card_and_fills(tmp_path: Path):
    """A spawn the model explicitly marks background=True (return
    "Started <id> (agent) — …") now also renders a SubAgentWidget that holds
    pending and fills on settle — not a generic ✓ tool row."""
    import asyncio

    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        ToolCallPart,
        ToolReturnPart,
    )

    from marim_harness.interfaces.tui.subagents import SubAgentWidget

    app = _app(tmp_path)
    reg = app.harness.deps.jobs
    gate = asyncio.Event()

    async def _work():
        await gate.wait()
        return "EXPLICIT BG REPORT"

    jid = reg.register("agent", "explore: review TUI", _work())  # running

    call = FunctionToolCallEvent(part=ToolCallPart(
        tool_name="spawn_agent",
        args={"type": "explore", "task": "review TUI", "background": True},
        tool_call_id="s1"))
    result = FunctionToolResultEvent(part=ToolReturnPart(
        tool_name="spawn_agent",
        content=f"Started {jid} (agent) — explore: review TUI", tool_call_id="s1"))

    async def gen():
        yield call
        yield result

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_events(None, gen())
        await pilot.pause()
        card = app.stream.tool_widgets.get("s1")
        assert isinstance(card, SubAgentWidget)   # a card, not a generic tool row
        assert card.status == "pending"           # not a misleading ✓
        gate.set()
        for _ in range(400):
            if reg.get(jid).status != "running":
                break
            await asyncio.sleep(0)
        app.stream.fill_finished_detached_cards(reg)
        await pilot.pause()
        assert card.status == "done"
        assert card.report == "EXPLICIT BG REPORT"


@pytest.mark.anyio
async def test_failed_spawn_renders_card_as_failed(tmp_path: Path):
    """A spawn that fails returns its error as a (successful) tool result; the card
    must still render failed (✕), not a misleading ✓."""
    from pydantic_ai.messages import FunctionToolResultEvent, ToolReturnPart

    from marim_harness.interfaces.tui.subagents import SubAgentWidget

    async def gen():
        yield _spawn_call("s1", "look around")
        yield FunctionToolResultEvent(
            part=ToolReturnPart(
                tool_name="spawn_agent",
                content="Sub-agent 'explore' failed: ValueError: boom",
                tool_call_id="s1",
            )
        )

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_events(None, gen())
        await pilot.pause()
        w = app.stream.tool_widgets["s1"]
        assert isinstance(w, SubAgentWidget)
        assert w.status == "failed"
        assert "✕" in str(w._header.visual)
        # The reason shows on the ↳ line (prefix stripped) and the full report is
        # appended to the transcript body for the viewer.
        assert "ValueError: boom" in str(w._activity.visual)
        body_text = " ".join(str(c.visual) for c in w.pane.query(".subagent-error"))
        assert "ValueError: boom" in body_text


@pytest.mark.anyio
async def test_successful_spawn_renders_card_done(tmp_path: Path):
    """A spawn whose report happens to start like prose still renders done (✓)."""
    from pydantic_ai.messages import FunctionToolResultEvent, ToolReturnPart

    from marim_harness.interfaces.tui.subagents import SubAgentWidget

    async def gen():
        yield _spawn_call("s1", "look around")
        yield FunctionToolResultEvent(
            part=ToolReturnPart(
                tool_name="spawn_agent",
                content="Here is the report: the codebase is well structured.",
                tool_call_id="s1",
            )
        )

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_events(None, gen())
        await pilot.pause()
        w = app.stream.tool_widgets["s1"]
        assert isinstance(w, SubAgentWidget)
        assert w.status == "done"
        assert "✓" in str(w._header.visual)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "outcome, expected",
    [("denied", "denied"), ("failed", "failed"), ("success", "done")],
)
async def test_on_events_reflects_tool_outcome_in_status(
    tmp_path: Path, outcome: str, expected: str
):
    """A denied/failed tool result must render its real status, not a green ✓.
    Regression: finish() defaulted status='done' regardless of the part outcome."""
    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        ToolCallPart,
        ToolReturnPart,
    )

    from marim_harness.interfaces.tui.widgets import ToolCallWidget

    call = FunctionToolCallEvent(
        part=ToolCallPart(
            tool_name="write_file",
            args={"path": "a.txt", "content": "x"},
            tool_call_id="call-1",
        )
    )
    result = FunctionToolResultEvent(
        part=ToolReturnPart(
            tool_name="write_file",
            content="denied by user",
            tool_call_id="call-1",
            outcome=outcome,  # pyright: ignore[reportArgumentType]
        )
    )

    async def gen():
        yield call
        yield result

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_events(None, gen())
        await pilot.pause()
        widget = app.stream.tool_widgets.get("call-1")
        assert isinstance(widget, ToolCallWidget)
        assert widget.status == expected


@pytest.mark.anyio
async def test_resume_reflects_denied_tool_outcome(tmp_path: Path):
    """The replay-from-history path must also render a denied tool as denied."""
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    from marim_harness.interfaces.tui.widgets import ToolCallWidget

    app = _app(tmp_path)
    app.harness.session.history = [
        ModelRequest(parts=[UserPromptPart(content="write a.txt")]),
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="write_file",
                    args={"path": "a.txt", "content": "x"},
                    tool_call_id="t1",
                ),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="write_file",
                    content="denied by user",
                    tool_call_id="t1",
                    outcome="denied",
                )
            ]
        ),
    ]
    async with app.run_test() as pilot:
        await pilot.pause()
        tools = list(app.query(ToolCallWidget))
        assert len(tools) == 1
        assert tools[0].status == "denied"


@pytest.mark.anyio
async def test_ask_user_is_not_folded_into_tool_group(tmp_path: Path):
    """ask_user is a user-facing Q&A: it must mount standalone, never buried in a
    collapsed '≡ N tools' group, even when sandwiched in a run of tool calls."""
    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        ToolCallPart,
        ToolReturnPart,
    )

    from marim_harness.interfaces.tui.widgets import ToolCallWidget, ToolGroupWidget

    def _call(name, args, cid):
        return FunctionToolCallEvent(
            part=ToolCallPart(tool_name=name, args=args, tool_call_id=cid)
        )

    def _ret(name, content, cid):
        return FunctionToolResultEvent(
            part=ToolReturnPart(tool_name=name, content=content, tool_call_id=cid)
        )

    async def gen():
        # Two reads fold into a group, then ask_user, then another read.
        yield _call("read_file", {"path": "a.py"}, "r1")
        yield _ret("read_file", "x", "r1")
        yield _call("read_file", {"path": "b.py"}, "r2")
        yield _ret("read_file", "y", "r2")
        yield _call("ask_user", {"questions": [{"question": "Q?", "options": [
            {"label": "A"}]}]}, "q1")
        yield _ret("ask_user", "{}", "q1")
        yield _call("read_file", {"path": "c.py"}, "r3")
        yield _ret("read_file", "z", "r3")

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_events(None, gen())
        await pilot.pause()

        ask = app.stream.tool_widgets.get("q1")
        assert isinstance(ask, ToolCallWidget)
        assert ask.tool_name == "ask_user"
        # The two reads before it grouped…
        groups = list(app.query(ToolGroupWidget))
        assert groups, "consecutive reads should have folded into a group"
        # …but ask_user is in none of them.
        for g in groups:
            assert ask not in g.walk_children()


@pytest.mark.anyio
async def test_resume_ask_user_not_folded_into_tool_group(tmp_path: Path):
    """On a resumed session, ask_user must mount standalone (mirroring the live path),
    not buried in a collapsed '≡ N tools' group when adjacent to other tool calls."""
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    from marim_harness.interfaces.tui.widgets import ToolCallWidget, ToolGroupWidget

    app = _app(tmp_path)
    app.harness.session.history = [
        ModelRequest(parts=[UserPromptPart(content="do something")]),
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="read_file", args={"path": "a.py"}, tool_call_id="r1"
                ),
                ToolCallPart(
                    tool_name="read_file", args={"path": "b.py"}, tool_call_id="r2"
                ),
                ToolCallPart(
                    tool_name="ask_user",
                    args={"questions": [{"question": "Proceed?", "options": [{"label": "Yes"}]}]},
                    tool_call_id="q1",
                ),
                ToolCallPart(
                    tool_name="read_file", args={"path": "c.py"}, tool_call_id="r3"
                ),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(tool_name="read_file", content="a", tool_call_id="r1"),
                ToolReturnPart(tool_name="read_file", content="b", tool_call_id="r2"),
                ToolReturnPart(tool_name="ask_user", content="{}", tool_call_id="q1"),
                ToolReturnPart(tool_name="read_file", content="c", tool_call_id="r3"),
            ]
        ),
    ]
    async with app.run_test() as pilot:
        await pilot.pause()

        # The ask_user widget must be present and finished.
        ask_widgets = [w for w in app.query(ToolCallWidget) if w.tool_name == "ask_user"]
        assert len(ask_widgets) == 1, "ask_user ToolCallWidget should be mounted"
        ask = ask_widgets[0]

        # Adjacent reads may group — that's fine — but ask_user must not be in any group.
        groups = list(app.query(ToolGroupWidget))
        for g in groups:
            assert ask not in g.walk_children(), (
                "ask_user must mount standalone on replay, not folded into a ToolGroupWidget"
            )


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

    from marim_harness.interfaces.tui.subagents import SubAgentWidget

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
        await app.stream.on_events(None, gen())
        await pilot.pause()

        widget = app.stream.tool_widgets.get("spawn-1")
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

    from marim_harness.interfaces.tui.subagents import SubAgentWidget
    from marim_harness.interfaces.tui.widgets import (
        AssistantMessage,
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
        await app.stream.on_events(None, spawn_gen())
        await pilot.pause()

        parent = app.stream.tool_widgets["s1"]
        assert isinstance(parent, SubAgentWidget)

        # The sub-agent emits text, then a nested read_file call + result.
        await app.stream.on_subagent_event(
            "s1", PartStartEvent(index=0, part=TextPart(content="checking files"))
        )
        await app.stream.on_subagent_event(
            "s1",
            FunctionToolCallEvent(
                part=ToolCallPart(
                    tool_name="read_file",
                    args={"path": "x.py"},
                    tool_call_id="nested-1",
                )
            ),
        )
        await app.stream.on_subagent_event(
            "s1",
            FunctionToolResultEvent(
                part=ToolReturnPart(
                    tool_name="read_file", content="1\tcode", tool_call_id="nested-1"
                )
            ),
        )
        await pilot.pause()

        # Both nested widgets live inside the SubAgentWidget's pane.
        body_children = list(parent.pane.walk_children())
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
        await app.stream.on_subagent_event(
            "ghost", PartStartEvent(index=0, part=TextPart(content="orphan"))
        )
        await pilot.pause()
        assert app.is_running is True


@pytest.mark.anyio
async def test_subagent_event_usage_populates_total_and_body_split(tmp_path: Path):
    """A sub-agent event carrying a RunUsage drives the widget: the running total is
    tracked (for the viewer footer), and the body shows the full split."""
    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        PartStartEvent,
        TextPart,
        ToolCallPart,
    )
    from pydantic_ai.usage import RunUsage
    from textual.widgets import Static

    from marim_harness.interfaces.tui.subagents import SubAgentWidget

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
        await app.stream.on_events(None, spawn_gen())
        await pilot.pause()
        parent = app.stream.tool_widgets["s1"]
        assert isinstance(parent, SubAgentWidget)

        usage = RunUsage(
            input_tokens=56000, output_tokens=2000,
            cache_read_tokens=50000, cache_write_tokens=5000,
        )
        await app.stream.on_subagent_event(
            "s1",
            PartStartEvent(index=0, part=TextPart(content="checking")),
            usage,
        )
        # Usage pricing is coalesced onto the flush tick (not priced per delta).
        app.stream.flush_streams()
        await pilot.pause()

        # 56k in + 2k out = 58k total, tracked for the screen's list row.
        assert parent.tokens == 58000
        # The full split lands in the pane's usage line (cost may be absent if unpriced).
        usage_line = parent.pane.query_one(".subagent-usage", Static)
        assert "1k↑ 55k⚡ 2k↓" in str(usage_line.visual)


def _app_with_manager(tmp_path: Path) -> HarnessApp:
    from pydantic_ai.models.test import TestModel

    from marim_harness.runtime.harness import Harness
    from marim_harness.session import SessionManager
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = _make_deps(tmp_path)
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

    from marim_harness.runtime.harness import Harness
    from marim_harness.session import SessionManager
    from marim_harness.tools.provider import BuiltinToolProvider

    async def titler(messages):
        return "Auto Title"

    deps = _make_deps(tmp_path)
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
        await app.harness.session.wait_autoname()  # the rename runs in the background
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

    from marim_harness.runtime.harness import Harness
    from marim_harness.session import SessionManager
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = _make_deps(tmp_path)
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
        assert "sleep 5" in str(app.query_one("#job-body").render())

        # Cancelling it repaints with the terminal status, panel stays visible.
        await app.harness.deps.jobs.cancel(job_id)
        await pilot.pause()
        assert "(cancelled)" in str(app.query_one("#job-body").render())


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
        body = str(app.query_one("#job-body").render())
        assert "explore" in body and "look" in body  # type column + concise title
        await app.harness.deps.jobs.cancel_all()


@pytest.mark.anyio
async def test_job_panel_collapsed_by_default(tmp_path: Path):
    import asyncio

    from marim_harness.interfaces.tui.widgets import JobPanel, TaskPanel

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.query_one(JobPanel)

        async def slow() -> str:
            await asyncio.sleep(5)
            return "done"

        app.harness.deps.jobs.register("bash", "sleep 5", slow())
        await pilot.pause()
        # The panel appears, but starts collapsed: body hidden, collapsed glyph,
        # and only the title row is shown.
        assert panel.display is True
        assert panel._collapsed is True
        assert app.query_one("#job-body").display is False
        assert "▸" in str(app.query_one("#job-header").render())
        # The task panel is unaffected — still expanded by default.
        assert app.query_one(TaskPanel)._collapsed is False
        # Clicking the header expands the jobs body.
        panel.on_panel_header_clicked(None)
        await pilot.pause()
        assert panel._collapsed is False
        assert app.query_one("#job-body").display is True
        await app.harness.deps.jobs.cancel_all()


@pytest.mark.anyio
async def test_input_snaps_back_to_focus_on_main_screen(tmp_path: Path):
    """Focus landing on a non-input main-screen widget (here a panel header)
    bounces straight back to the prompt, so the user can always just type."""
    import asyncio

    from marim_harness.interfaces.tui.widgets import PromptInput

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one(PromptInput)
        assert prompt.has_focus  # on_mount lands focus here

        # A visible, focusable non-input widget to steal focus to.
        async def slow() -> str:
            await asyncio.sleep(5)
            return "done"

        app.harness.deps.jobs.register("bash", "sleep 5", slow())
        await pilot.pause()
        header = app.query_one("#job-header")
        header.focus()
        await pilot.pause()
        assert prompt.has_focus  # snapped back
        assert not header.has_focus
        await app.harness.deps.jobs.cancel_all()


@pytest.mark.anyio
async def test_input_does_not_steal_focus_while_subagents_screen_open(tmp_path: Path):
    """The snap-back must not fight the sub-agents screen, which owns its own
    list/pane focus while open."""
    import asyncio

    from marim_harness.interfaces.tui.widgets import PromptInput

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(PromptInput)

        async def slow() -> str:
            await asyncio.sleep(5)
            return "done"

        app.harness.deps.jobs.register("bash", "sleep 5", slow())
        await pilot.pause()
        app.subagents.open = True  # pretend the ctrl+x screen is up
        header = app.query_one("#job-header")
        header.focus()
        await pilot.pause()
        assert header.has_focus  # not bounced while the sub-agents screen owns focus
        app.subagents.open = False
        await app.harness.deps.jobs.cancel_all()


@pytest.mark.anyio
async def test_background_spawn_renders_a_card_held_pending(tmp_path: Path):
    """A background spawn_agent now renders a SubAgentWidget (not a plain tool
    row). With no live job behind it, the card holds pending rather than showing a
    misleading ✓ on the dispatch handoff."""
    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        ToolCallPart,
        ToolReturnPart,
    )

    from marim_harness.interfaces.tui.subagents import SubAgentWidget

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
        await app.stream.on_events(None, gen())
        await pilot.pause()
        widget = app.stream.tool_widgets.get("spawn-bg")
        assert isinstance(widget, SubAgentWidget)
        assert widget.status == "pending"


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
async def test_single_subagent_registers_card(tmp_path: Path):
    """A lone spawn mounts a compact card whose transcript lives in a detail-host
    pane (not inline) and is registered in the screen's navigation list."""
    from marim_harness.interfaces.tui.subagents import SubAgentWidget

    async def gen():
        yield _spawn_call("s1", "only one")

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_events(None, gen())
        await pilot.pause()
        w = app.stream.tool_widgets["s1"]
        assert isinstance(w, SubAgentWidget)
        # The transcript is no longer inline on the card; it lives in a pane in the
        # detail host, which is hidden until the screen is opened (ctrl+x).
        assert w.pane is not None
        assert w.stream_id == "s1"
        assert app.stream.subagents == [w]


@pytest.mark.anyio
async def test_parallel_subagents_register_in_spawn_order(tmp_path: Path):
    """A fan-out registers every card in the screen's ordered navigation list, each
    rendered as a compact card whose transcript lives in its own detail-host pane."""
    from marim_harness.interfaces.tui.subagents import SubAgentWidget

    async def gen():
        yield _spawn_call("s1", "first")
        yield _spawn_call("s2", "second")
        yield _spawn_call("s3", "third")

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_events(None, gen())
        await pilot.pause()
        cards = [app.stream.tool_widgets[sid] for sid in ("s1", "s2", "s3")]
        for w in cards:
            assert isinstance(w, SubAgentWidget)
            assert w.pane is not None
        assert app.stream.subagents == cards


@pytest.mark.anyio
async def test_subagent_viewer_opens_navigates_and_closes(tmp_path: Path):
    """ctrl+x opens the full-bleed screen on the most recent spawn; moving the list
    cursor selects another agent's transcript (via the detail host); closing hides
    the screen and restores the log."""
    from marim_harness.interfaces.tui.subagents import SubAgentsView

    async def gen():
        yield _spawn_call("s1", "first")
        yield _spawn_call("s2", "second")
        yield _spawn_call("s3", "third")

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_events(None, gen())
        await pilot.pause()

        # Open: lands on the most recent (index 2), screen shown, log hidden, the
        # host showing s3's pane.
        app.action_toggle_subagents()
        await pilot.pause()
        view = app.query_one(SubAgentsView)
        assert app.subagents.open is True
        assert app.subagents.index == 2
        assert view.display is True
        assert app.query_one("#log").display is False
        assert view.host.current_sid() == "s3"

        # Moving the list cursor up selects s2; the shown transcript follows.
        view.list.move_cursor(row=1)
        await pilot.pause()
        assert app.subagents.index == 1
        assert view.host.current_sid() == "s2"

        # Driving the opener directly jumps to a specific card.
        app.subagents.open_at("s1")
        await pilot.pause()
        assert app.subagents.index == 0
        assert view.host.current_sid() == "s1"

        # Close: screen hidden, log restored.
        app.action_toggle_subagents()
        await pilot.pause()
        assert app.subagents.open is False
        assert view.display is False
        assert app.query_one("#log").display is True


@pytest.mark.anyio
async def test_subagent_viewer_noop_without_subagents(tmp_path: Path):
    """ctrl+x with nothing spawned posts a notice and stays closed."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_toggle_subagents()
        await pilot.pause()
        assert app.subagents.open is False
        assert "no sub-agents" in _log_text(app).lower()


@pytest.mark.anyio
async def test_subagent_event_shows_current_tool(tmp_path: Path):
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

        await app.stream.on_events(None, spawn())
        await pilot.pause()
        # A nested tool call shows on the card's ↳ line as the current tool, humanized
        # with its arg preview, and bumps the tally.
        tool_call = FunctionToolCallEvent(
            part=ToolCallPart(
                tool_name="grep", args={"pattern": "needle"}, tool_call_id="t1"
            )
        )
        await app.stream.on_subagent_event("s1", tool_call)
        await pilot.pause()
        parent = app.stream.tool_widgets["s1"]
        assert parent.tool_count == 1
        assert "Grep · needle" in parent._activity.render().plain


@pytest.mark.anyio
async def test_subagent_event_updates_token_usage(tmp_path: Path):
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

        await app.stream.on_events(None, spawn())
        await pilot.pause()
        tool_call = FunctionToolCallEvent(
            part=ToolCallPart(
                tool_name="grep", args={"pattern": "x"}, tool_call_id="t1"
            )
        )
        # The handler forwards the run's live usage; the widget tracks the total.
        await app.stream.on_subagent_event(
            "s1", tool_call, RunUsage(input_tokens=1500, output_tokens=500)
        )
        # Usage pricing is coalesced onto the flush tick (not priced per delta).
        app.stream.flush_streams()
        await pilot.pause()
        parent = app.stream.tool_widgets["s1"]
        assert parent.tokens == 2000


@pytest.mark.anyio
async def test_lone_nested_tool_call_is_not_wrapped_in_a_group(tmp_path: Path):
    """A single nested tool call under a sub-agent mounts bare in the body — no
    redundant group wrapper, which is what inflated the sub-agent's height."""
    from pydantic_ai.messages import FunctionToolCallEvent, ToolCallPart

    from marim_harness.interfaces.tui.subagents import SubAgentWidget
    from marim_harness.interfaces.tui.widgets import (
        ToolCallWidget,
        ToolGroupWidget,
    )

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        async def spawn():
            yield _spawn_call("s1", "look")

        await app.stream.on_events(None, spawn())
        await pilot.pause()
        await app.stream.on_subagent_event(
            "s1",
            FunctionToolCallEvent(
                part=ToolCallPart(tool_name="grep", args={}, tool_call_id="t1")
            ),
        )
        await pilot.pause()
        parent = app.stream.tool_widgets["s1"]
        assert isinstance(parent, SubAgentWidget)
        assert len(parent.pane.query(ToolGroupWidget)) == 0
        assert len(parent.pane.query(ToolCallWidget)) == 1


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

        await app.stream.on_events(None, spawn())
        await pilot.pause()
        await app.stream.on_subagent_event("s1", nested("t1"))
        await app.stream.on_subagent_event("s1", nested("t2"))
        await app.stream.on_subagent_event("s1", nested("t3"))
        await pilot.pause()
        parent = app.stream.tool_widgets["s1"]
        groups = parent.pane.query(ToolGroupWidget)
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

        await app.stream.on_events(None, stream())
        msg = app.stream.current_assistant
        # The full text is buffered into the widget...
        assert msg.text == "# Hi there"
        # ...and a delta marks it dirty without rendering (the per-delta debounce).
        # Asserted synchronously so the shared interval timer can't interleave.
        msg.append("!")
        assert msg._pending is True
        # The shared flush renders it and clears the pending flag.
        app.stream.flush_streams()
        assert msg._pending is False


@pytest.mark.anyio
async def test_flush_streams_renders_viewed_subagent_text(tmp_path: Path):
    """The shared flush renders a nested sub-agent stream when its pane is the one
    the detail host is currently showing."""
    from pydantic_ai.messages import PartStartEvent, TextPart

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        async def spawn():
            yield _spawn_call("s1", "look")

        await app.stream.on_events(None, spawn())
        await pilot.pause()
        await app.stream.on_subagent_event(
            "s1", PartStartEvent(index=0, part=TextPart(content="nested"))
        )
        msg = app.stream._sub_streams["s1"].assistant
        # Show this card's pane in the detail host so its transcript flushes.
        app.stream.detail_host.show("s1")
        # Synchronous append → assert → flush so the interval timer can't interleave.
        msg.append("!")
        assert msg._pending is True
        app.stream.flush_streams()
        assert msg._pending is False


@pytest.mark.anyio
async def test_fresh_log_top_aligned_then_anchors_on_overflow(tmp_path: Path):
    """A fresh session starts top-aligned — the intro header pinned at the top, not
    bottom-anchored — and only anchors once content overflows the viewport, so the
    header then scrolls away with the messages."""
    from marim_harness.interfaces.tui.widgets import UserMessage

    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        log = app.query_one("#log")
        assert log.is_anchored is False  # header pinned at the top
        assert log.scroll_offset.y == 0
        # Overflow the viewport; the flush tick anchors on overflow.
        for i in range(40):
            await log.mount(UserMessage(f"line {i}"))
        app.stream.flush_streams()
        await pilot.pause()
        assert log.is_anchored is True  # now tail-follows the newest content


@pytest.mark.anyio
async def test_flush_does_not_anchor_during_rebuild(tmp_path: Path):
    """A flush tick firing while a session is being rebuilt must not anchor off the
    stale max_scroll_y — that left a cleared session bottom-aligned instead of
    pinning the header at the top."""
    from marim_harness.interfaces.tui.widgets import UserMessage

    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        log = app.query_one("#log")
        for i in range(40):  # overflow ⇒ max_scroll_y > 0
            await log.mount(UserMessage(f"line {i}"))
        await pilot.pause()
        # Mirror the real rebuild entry (render_session): reset() drops the overflow
        # latch and the inherited anchor is cleared before the rebuild proceeds.
        app.stream.reset()
        log.anchor(False)
        app.stream.rebuilding = True
        app.stream.flush_streams()  # the mid-rebuild interval tick
        assert log.is_anchored is False  # guard suppressed the stale anchor
        app.stream.rebuilding = False
        app.stream.flush_streams()
        assert log.is_anchored is True  # anchors normally once the rebuild is done


@pytest.mark.anyio
async def test_flush_skips_streams_for_unviewed_subagents(tmp_path: Path):
    """A sub-agent transcript whose pane isn't the one shown in the detail host must
    not be re-rendered on every flush tick — re-parsing the full markdown of N cards
    each tick blocks the event loop and freezes the UI. It stays pending and renders
    only once its pane is shown."""
    from marim_harness.interfaces.tui.widgets import AssistantMessage

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        async def spawn():
            yield _spawn_call("s1", "look")
            yield _spawn_call("s2", "other")

        await app.stream.on_events(None, spawn())
        await pilot.pause()
        # Show s2's pane so s1's transcript is the unviewed one.
        app.stream.detail_host.show("s2")
        sa = app.stream.tool_widgets["s1"]
        msg = AssistantMessage()
        await sa.pane.add(msg)
        await pilot.pause()

        app.stream.append_stream(msg, "hello world")
        app.stream.flush_streams()
        assert msg._pending is True  # s1's pane not shown → skipped
        assert msg in app.stream.dirty_streams  # kept pending for later

        app.stream.detail_host.show("s1")  # show this card's pane
        app.stream.flush_streams()
        assert msg._pending is False


async def _pump_until(pilot, predicate, tries: int = 80) -> bool:
    """Pump the event loop until ``predicate()`` holds (or ``tries`` is reached),
    returning whether it held. A single ``pilot.pause()`` is one message-loop
    cycle, which under full-suite load isn't always enough for Textual to finish a
    reflow/scroll — polling makes layout-dependent assertions deterministic."""
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause()
    return predicate()


@pytest.mark.anyio
async def test_stream_does_not_yank_when_scrolled_up(tmp_path: Path):
    """When the user has scrolled up to read, a streaming event must not snap the
    viewport back to the bottom — scrolling up releases the anchor."""
    from marim_harness.interfaces.tui.widgets import AssistantMessage

    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        log = app.query_one("#log")
        # Overflow the viewport, then let the first-overflow flush engage the anchor
        # so the log is now tail-following — the state a user scrolls up *from*.
        for _ in range(40):
            m = AssistantMessage()
            await log.mount(m)
            m.append("line of text")
        # Wait for the reflow to actually overflow before flushing — under load a
        # single pause isn't enough for 40 fresh widgets to lay out.
        assert await _pump_until(pilot, lambda: log.max_scroll_y > 0)
        app.stream.flush_streams()  # engages the on-overflow anchor (latched once)
        assert await _pump_until(pilot, lambda: log.is_anchored)
        # The user scrolls up to read. A real scroll-up releases Textual's anchor;
        # a programmatic ``scroll_to`` alone doesn't in the test harness, so drop the
        # anchor explicitly to model the interaction (anchor released, viewport up).
        log.anchor(False)
        log.scroll_to(y=0, animate=False)
        assert await _pump_until(pilot, lambda: log.scroll_offset.y == 0)

        # A streaming text event arrives — we are scrolled up, so stay put.
        from pydantic_ai.messages import PartStartEvent, TextPart

        async def gen():
            yield PartStartEvent(index=0, part=TextPart(content="new streamed text"))

        await app.stream.on_events(None, gen())
        # Drive the render+anchor decision synchronously rather than waiting on the
        # interval flush tick — its timing under load is what made this test flaky.
        # flush_streams() is exactly what the tick calls, so this stays faithful.
        app.stream.flush_streams()
        # Confirm we stayed put. Under the bug, the latch-less re-anchor (here and on
        # every interval tick) snaps the viewport to the bottom and never lets it sit
        # up the log — so a yank moves off 0 and stays off.
        await _pump_until(pilot, lambda: log.scroll_offset.y != 0, tries=5)
        assert log.scroll_offset.y == 0  # did not yank back to the bottom
        assert log.is_anchored is False  # and did not silently re-engage the anchor


@pytest.mark.anyio
async def test_app_starts_on_saved_marim_theme(tmp_path, monkeypatch):
    """The app registers the marim themes and starts on the persisted one."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from marim_harness.interfaces import prefs

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
        await app.stream.on_events(None, gen())
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
        await app.stream.on_events(None, gen())
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
        await app.stream.on_events(None, gen())
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
        await app.stream.on_events(None, gen())
        await pilot.pause()
        w = app.stream.tool_widgets["c1"]
        assert isinstance(w, ToolCallWidget)
        assert w.status == "done"
        assert w.result_text == "file body"
        assert "marim-green" in app.available_themes


@pytest.mark.anyio
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
        assert g.collapsed is True  # all done → fold
        assert "·" in g.title.plain  # duration appended


@pytest.mark.anyio
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


@pytest.mark.anyio
async def test_tool_group_stays_open_when_bash_exits_nonzero(tmp_path: Path):
    """A bash call that exits non-zero self-flips its status to 'failed' inside
    finish(); the group must still detect the failure and stay open."""
    from pydantic_ai.messages import FunctionToolResultEvent, ToolReturnPart

    from marim_harness.interfaces.tui.widgets import ToolGroupWidget

    async def gen():
        yield _call("bash", "c1")
        yield _call("bash", "c2")
        yield FunctionToolResultEvent(
            part=ToolReturnPart(
                tool_name="bash", content="exit 1\ncommand not found", tool_call_id="c1"
            )
        )
        yield FunctionToolResultEvent(
            part=ToolReturnPart(
                tool_name="bash", content="exit 1\nsomething failed", tool_call_id="c2"
            )
        )

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_events(None, gen())
        await pilot.pause()
        groups = list(app.query(ToolGroupWidget))
        assert len(groups) == 1
        # The group must stay open because children failed.
        assert groups[0].collapsed is False


@pytest.mark.anyio
async def test_tool_group_stays_open_when_non_bash_tool_fails(tmp_path: Path):
    """A non-bash tool (e.g. read_file) whose ToolReturnPart carries
    outcome='failed' must cause the group to stay open — exercises the
    status_from_part → widget.finish() → group.note_child_finished(failed=True)
    path for tools that don't use the exit-code heuristic."""
    from pydantic_ai.messages import FunctionToolResultEvent, ToolReturnPart

    from marim_harness.interfaces.tui.widgets import ToolGroupWidget

    async def gen():
        yield _call("read_file", "r1")
        yield _call("read_file", "r2")
        yield FunctionToolResultEvent(
            part=ToolReturnPart(
                tool_name="read_file",
                content="file not found",
                tool_call_id="r1",
                outcome="failed",
            )
        )
        yield FunctionToolResultEvent(
            part=ToolReturnPart(
                tool_name="read_file",
                content="ok",
                tool_call_id="r2",
            )
        )

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_events(None, gen())
        await pilot.pause()
        groups = list(app.query(ToolGroupWidget))
        assert len(groups) == 1
        # The group must stay open because one child failed.
        assert groups[0].collapsed is False


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
        assert app._wake.depth == 1
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
        # The digest is left for the next user turn, but wake-consumed.
        assert app.harness.deps.jobs.has_finished_pending() is False
        assert "job-1 (agent) done" in app.harness.deps.jobs.take_finished_digest()


@pytest.mark.anyio
async def test_wake_stops_at_depth_cap(tmp_path: Path):
    started: list = []
    app = _app(tmp_path)
    app.run_worker = lambda c, *a, **k: (started.append(c), c.close())  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(app._wake.depth_cap):  # drive the chain up to the cap
            app._wake.record_auto_turn()
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
        assert started == []  # queued; but wake-consumed by wait()
        app._turn_worker = None  # turn ends -> finally calls _maybe_wake
        app._maybe_wake()
        assert len(started) == 0  # no redundant wake — result already consumed


@pytest.mark.anyio
async def test_user_turn_resets_auto_depth(tmp_path: Path):
    app = _app(tmp_path)
    app.run_worker = lambda c, *a, **k: (c.close() if hasattr(c, "close") else None)  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        app._wake.record_auto_turn()
        app._wake.record_auto_turn()
        await _submit(app, "do something")  # a user-initiated turn
        assert app._wake.depth == 0


@pytest.mark.anyio
async def test_ask_user_callback_is_wired(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.harness.deps.ui.ask_user == app._ask_user


@pytest.mark.anyio
async def test_ask_user_callback_shows_panel_and_returns_answer(tmp_path: Path):
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


@pytest.mark.anyio
async def test_ask_user_escape_cancels_only_the_question(tmp_path: Path):
    """Esc with panel focus must cancel the question only — the panel's own
    ``escape`` binding (priority within the panel) wins over the app's
    non-priority ``escape -> cancel_turn`` binding. This only exercises the
    real HarnessApp binding table; the stub-harness tests in
    test_ask_user_panel.py don't have the app's escape binding at all."""
    from marim_harness.ask_user import Choice, Question
    from marim_harness.interfaces.tui.ask_user import AskUserPanel

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        qs = [Question("Pick one", "Pick", [Choice("Alpha"), Choice("Beta")])]
        # _ask_user runs as its own worker here (not the turn worker), so
        # asserting its result and the panel's teardown is enough to prove
        # the escape landed on the panel rather than falling through to
        # cancel_turn.
        worker = app.run_worker(app._ask_user(qs))
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert worker.result is None
        assert not app.query(AskUserPanel)
        assert app.is_running


@pytest.mark.anyio
async def test_ask_user_panel_closes_open_subagents_viewer(tmp_path: Path):
    """A panel mounted while the ctrl+x sub-agents screen is open would render
    underneath it (invisible, its own layer) yet still take focus, and the
    viewer's Esc ("back") would land on it instead of the panel — silently
    cancelling the question. run_panel closes the viewer first."""
    from marim_harness.ask_user import Choice, Question
    from marim_harness.interfaces.tui.subagents import SubAgentsView

    async def gen():
        yield _spawn_call("s1", "first")

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_events(None, gen())
        await pilot.pause()
        app.action_toggle_subagents()
        await pilot.pause()
        assert app.subagents.open is True

        qs = [Question("Pick one", "Pick", [Choice("Alpha"), Choice("Beta")])]
        worker = app.run_worker(app._ask_user(qs))
        await pilot.pause()

        assert app.subagents.open is False
        view = app.query_one(SubAgentsView)
        assert view.display is False
        assert app.query_one("#log").display is True

        await pilot.press("enter")  # selects highlighted "Alpha"
        await pilot.pause()
        assert worker.result == {"Pick": "Alpha"}


def test_format_duration_units():
    from marim_harness.interfaces.tui.status import format_duration as _format_duration

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
        app.status.busy = True
        app.status.turn_start = time.monotonic() - 5  # 5s into a turn
        app.status.refresh_status()
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
        assert app.title == "● my-session"
        app.status.busy = True
        app.status.spin = 0  # first spinner frame (⠋)
        app.status.refresh_title()
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
        assert app.title == "● marim-harness"


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
    from marim_harness.interfaces.tui.status import osc_title as _osc_title

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
            app.status.busy = True
            app.status.spin = 0  # first spinner frame (⠋)
            app.status.refresh_title()
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
        assert w.reveal is False and app.stream.show_all_output is False
        app.action_toggle_outputs()
        await pilot.pause()
        assert app.stream.show_all_output is True and w.reveal is True
        app.action_toggle_outputs()
        await pilot.pause()
        assert app.stream.show_all_output is False and w.reveal is False


@pytest.mark.anyio
async def test_busy_title_uses_spinner_frame(tmp_path: Path, monkeypatch):
    from marim_harness.interfaces.tui.status import _SPINNER

    monkeypatch.setattr(
        type(_app(tmp_path).harness.session),
        "session_name", property(lambda self: "my-session"),
    )
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.status.busy = True
        for frame_idx, glyph in enumerate(_SPINNER):
            app.status.spin = frame_idx
            app.status.refresh_title()
            assert app.title == f"{glyph} my-session"


@pytest.mark.anyio
async def test_tick_spinner_advances_only_when_busy(tmp_path: Path, monkeypatch):
    from marim_harness.interfaces.tui.status import _SPINNER

    monkeypatch.setattr(
        type(_app(tmp_path).harness.session),
        "session_name", property(lambda self: "my-session"),
    )
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Idle: the tick is a no-op and the title stays the static ●.
        app.status.busy = False
        app.status.spin = 0
        app.status.tick_spinner()
        assert app.status.spin == 0
        assert app.title == "● my-session"
        # Busy: the tick advances the frame and re-renders the title.
        app.status.busy = True
        app.status.spin = 0
        app.status.tick_spinner()
        assert app.status.spin == 1
        assert app.title == f"{_SPINNER[1]} my-session"


@pytest.mark.anyio
async def test_finished_job_notifies_even_when_wake_disabled(tmp_path: Path):
    """A completed background job pings the desktop notifier once, even with
    autonomous wake off — notification is decoupled from the wake path."""
    sent = []

    class _Notifier:
        # _notify dispatches OFF the event loop via send_async (the blocking send
        # would freeze the UI), so the stub records through that path.
        async def send_async(self, title, body, event_type):
            sent.append((title, body, event_type))

    async def _done():
        return "result"

    app = _app(tmp_path)
    app.harness.deps.ui.notifier = _Notifier()
    app.autonomous_wake = False  # wake off — notification must still fire
    async with app.run_test() as pilot:
        await pilot.pause()
        app.harness.deps.jobs.register("agent", "x", _done())
        fired = await _pump_until(pilot, lambda: any(e[2] == "job_done" for e in sent))
        assert fired
        # Exactly one ping for the single completion.
        assert sum(1 for e in sent if e[2] == "job_done") == 1


@pytest.mark.anyio
async def test_rewind_command_truncates_and_rerenders(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        # Seed two checkpoints by hand against the live manager.
        mgr = app.harness.checkpoints
        mgr.snapshot("turn one")                       # index 0, history_len 0
        app.harness.session.set_history(["u1", "a1"])
        mgr.snapshot("turn two")                       # index 1, history_len 2
        app.harness.session.set_history(["u1", "a1", "u2", "a2"])

        await app.rewind_to_checkpoint(0)
        assert app.harness.session.history == []
        assert [c.index for c in mgr.list()] == [0]


class _RewindSnap:
    """A snapshotter whose restore success is configurable, for app-level tests."""

    def __init__(self, *, restore_ok: bool) -> None:
        self.restore_ok = restore_ok
        self.restored: list[str] = []

    def capture(self, ref: str, message: str) -> str:
        return f"commit:{ref}"

    def restore(self, commit: str) -> bool:
        self.restored.append(commit)
        return self.restore_ok

    def delete(self, ref: str) -> None:
        pass


@pytest.mark.anyio
async def test_rewind_note_reports_restore_failure(tmp_path: Path):
    """A failed file restore must be surfaced, not silently reported as a clean
    rewind (the old behavior always claimed success)."""
    from marim_harness.interfaces.tui.widgets import AssistantMessage

    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        mgr = app.harness.checkpoints
        mgr.snapshotter = _RewindSnap(restore_ok=False)
        mgr.snapshot("t1")  # checkpoint gets a commit, so restore is attempted
        app.harness.session.set_history(["u1", "a1"])
        await app.rewind_to_checkpoint(0)
        notes = " ".join(w.text for w in app.query(AssistantMessage)).lower()
        assert "fail" in notes
        assert "files restored" not in notes


@pytest.mark.anyio
async def test_undo_rewind_restores_pre_rewind_files(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        mgr = app.harness.checkpoints
        snap = _RewindSnap(restore_ok=True)
        mgr.snapshotter = snap
        mgr.snapshot("t1")
        app.harness.session.set_history(["u1", "a1"])
        await app.rewind_to_checkpoint(0)
        snap.restored.clear()
        await app.undo_rewind()
        assert any("_pre_restore" in c for c in snap.restored)


@pytest.mark.anyio
async def test_undo_rewind_without_prior_rewind_notes_nothing_to_undo(tmp_path: Path):
    from marim_harness.interfaces.tui.widgets import AssistantMessage

    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await app.undo_rewind()
        notes = " ".join(w.text for w in app.query(AssistantMessage)).lower()
        assert "nothing to undo" in notes


@pytest.mark.anyio
async def test_rewind_command_refuses_while_busy(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.harness.checkpoints.snapshot("t1")
        app.harness.session.set_history(["u1", "a1"])
        app.status.set_busy(True)
        await app.rewind_to_checkpoint(0)
        # Busy → refused, history untouched.
        assert app.harness.session.history == ["u1", "a1"]
        app.status.set_busy(False)


@pytest.mark.anyio
async def test_start_system_turn_refused_while_busy(tmp_path: Path):
    # /remember and /skill spawn an exclusive system turn. Doing so mid-turn would
    # silently cancel the in-flight worker (Textual exclusivity) and race its
    # bookkeeping — so it must refuse while a turn is running.
    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        sentinel = object()
        app._turn_worker = sentinel  # pretend a turn is running
        started = app.start_system_turn("save this fact")
        assert started is False
        assert app._turn_worker is sentinel  # the running turn was not clobbered


@pytest.mark.anyio
async def test_start_system_turn_runs_when_idle(tmp_path: Path, monkeypatch):
    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        spawned: list[str] = []

        async def fake_run_turn(text, attachments=None):
            spawned.append(text)

        monkeypatch.setattr(app, "_run_turn", fake_run_turn)
        assert app._turn_worker is None
        started = app.start_system_turn("save this fact")
        assert started is True
        assert app._turn_worker is not None
        await app.workers.wait_for_complete()  # let the spawned worker finish cleanly
        assert spawned == ["save this fact"]  # the prompt was routed to a turn


@pytest.mark.anyio
async def test_clear_refused_while_busy(tmp_path: Path, monkeypatch):
    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        called = False

        async def spy() -> None:
            nonlocal called
            called = True

        monkeypatch.setattr(app.session, "reset_conversation", spy)
        app._turn_worker = object()  # a turn is running
        await app.reset_conversation()
        assert called is False  # refused: did not tear down the live conversation


@pytest.mark.anyio
async def test_new_session_refused_while_busy(tmp_path: Path, monkeypatch):
    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        called = False

        async def spy(name=None) -> None:
            nonlocal called
            called = True

        monkeypatch.setattr(app.session, "start_new_session", spy)
        app._turn_worker = object()
        await app.start_new_session("feature")
        assert called is False


@pytest.mark.anyio
async def test_switch_session_refused_while_busy(tmp_path: Path, monkeypatch):
    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        called = False

        async def spy(session_id) -> None:
            nonlocal called
            called = True

        monkeypatch.setattr(app.session, "switch_to_session_id", spy)
        app._turn_worker = object()
        await app.switch_to_session_id("alpha")
        assert called is False


@pytest.mark.anyio
async def test_on_compact_noop_clears_indicator_without_message(tmp_path: Path):
    # A forced compaction that doesn't shrink calls _on_compact(before, before).
    # The "compacting…" notice must be cleared, and no misleading "compacted
    # history: N → N" line should be added.
    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app._on_compact_start()
        assert app._compacting_notice is not None
        app._on_compact(3, 3)  # no-shrink signal
        await pilot.pause()
        assert app._compacting_notice is None  # indicator cleared
        texts = [str(w.render()) for w in app.query(NoticeMessage)]
        assert not any("compacted history" in t for t in texts)


@pytest.mark.anyio
async def test_on_compact_shrink_shows_message(tmp_path: Path):
    # A real compaction (after < before) still posts the summary line.
    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app._on_compact_start()
        app._on_compact(10, 4)
        await pilot.pause()
        assert app._compacting_notice is None
        texts = [str(w.render()) for w in app.query(NoticeMessage)]
        assert any("compacted history: 10 → 4" in t for t in texts)


@pytest.mark.anyio
async def test_detached_card_fills_automatically_when_job_settles(tmp_path: Path):
    """Settling the job fires on_jobs_changed, which fills the card with no manual
    call — the live end-to-end path."""
    import asyncio

    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        ToolCallPart,
        ToolReturnPart,
    )

    from marim_harness.tools.spawn_tools import _detach_handoff

    app = _app(tmp_path)
    reg = app.harness.deps.jobs
    gate = asyncio.Event()

    async def _work():
        await gate.wait()
        return "AUTO REPORT"

    jid = reg.register("agent", "explore: x", _work())

    call = FunctionToolCallEvent(part=ToolCallPart(
        tool_name="spawn_agent", args={"type": "explore", "task": "x"},
        tool_call_id="s1"))
    result = FunctionToolResultEvent(part=ToolReturnPart(
        tool_name="spawn_agent", content=_detach_handoff(jid), tool_call_id="s1"))

    async def gen():
        yield call
        yield result

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_events(None, gen())
        await pilot.pause()
        card = app.stream.tool_widgets.get("s1")
        assert card.status == "pending"

        gate.set()                          # job finishes → on_change → fill (no manual call)
        for _ in range(400):
            if reg.get(jid).status != "running":
                break
            await asyncio.sleep(0)
        await pilot.pause()
        assert card.status == "done"
        assert card.report == "AUTO REPORT"


@pytest.mark.anyio
async def test_sub_streams_pruned_after_subagent_finish(tmp_path: Path):
    """_sub_streams entries are removed at prune_completed so finished sub-agents
    don't accumulate stream state for the session lifetime."""
    from pydantic_ai.messages import (
        FunctionToolResultEvent,
        PartStartEvent,
        TextPart,
        ToolReturnPart,
    )

    async def gen():
        yield _spawn_call("s1", "explore the repo")
        yield FunctionToolResultEvent(
            part=ToolReturnPart(
                tool_name="spawn_agent",
                content="Exploration complete.",
                tool_call_id="s1",
            )
        )

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_events(None, gen())
        await pilot.pause()
        # Simulate the sub-agent streaming a text event so _sub_streams gets populated.
        await app.stream.on_subagent_event(
            "s1", PartStartEvent(index=0, part=TextPart(content="working…"))
        )
        assert "s1" in app.stream._sub_streams  # populated by sub-agent stream

        # After the spawn result, prune_completed must drop the finished entry.
        app.stream.prune_completed()
        assert "s1" not in app.stream._sub_streams


@pytest.mark.anyio
async def test_bang_submission_runs_command_not_a_turn(tmp_path: Path):
    """A `!` submission executes locally: the result lands in the pending
    shell-results queue and no agent turn starts (history stays empty)."""
    from marim_harness.interfaces.tui.widgets.prompt import PromptInput

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.on_prompt_input_submitted(
            PromptInput.Submitted("!echo bang-marker")
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        pending = app.harness.turn_controller._pending_shell_results
        assert len(pending) == 1
        assert pending[0][0] == "echo bang-marker"
        assert "bang-marker" in pending[0][1]
        assert list(app.harness.session.history) == []  # no turn ran


@pytest.mark.anyio
async def test_bang_render_failure_surfaces_error_not_crash(tmp_path: Path):
    """An unexpected exception inside the passthrough (here: the transcript
    render) must surface as an ErrorMessage, not exit the app — and the result
    must already be queued for the model."""
    from marim_harness.interfaces.tui.widgets import ErrorMessage
    from marim_harness.interfaces.tui.widgets.prompt import PromptInput

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        async def boom(markdown: str) -> None:
            raise RuntimeError("render exploded")

        app.post_system = boom  # type: ignore[method-assign]
        await app.on_prompt_input_submitted(PromptInput.Submitted("!echo queued-anyway"))
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.is_running  # the app survived
        pending = app.harness.turn_controller._pending_shell_results
        assert len(pending) == 1 and "queued-anyway" in pending[0][1]
        errors = [str(w.render()) for w in app.query(ErrorMessage)]
        assert any("render exploded" in e for e in errors)


@pytest.mark.anyio
async def test_bang_survives_a_turn_starting_mid_run(tmp_path: Path):
    """A turn starting while a ! command runs must not cancel it: the
    passthrough worker lives in its own worker group, outside the default
    group that the exclusive turn worker sweeps."""
    from marim_harness.interfaces.tui.widgets.prompt import PromptInput

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.on_prompt_input_submitted(
            PromptInput.Submitted("!sleep 0.3 && echo survived")
        )
        await app._start_turn("hello")  # exclusive turn worker joins now
        await app.workers.wait_for_complete()
        await pilot.pause()
        pending = app.harness.turn_controller._pending_shell_results
        assert len(pending) == 1
        assert "survived" in pending[0][1]


@pytest.mark.anyio
async def test_bare_bang_shows_usage_hint(tmp_path: Path):
    from marim_harness.interfaces.tui.widgets.prompt import PromptInput

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        shown: list[str] = []
        real_post_system = app.post_system

        async def spy(markdown: str) -> None:
            shown.append(markdown)
            await real_post_system(markdown)

        app.post_system = spy  # type: ignore[method-assign]
        await app.on_prompt_input_submitted(PromptInput.Submitted("!"))
        await pilot.pause()
        assert any("Usage" in s for s in shown)
        assert app.harness.turn_controller._pending_shell_results == []
        assert list(app.harness.session.history) == []  # no turn started


@pytest.mark.anyio
async def test_bang_refused_while_turn_busy(tmp_path: Path):
    """A `!` command mid-turn is refused with a notice — running it would
    interleave its output with the streaming response."""
    from marim_harness.interfaces.tui.widgets.prompt import PromptInput

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._turn_starting = True  # the turn_busy property's spawn-gap term
        await app.on_prompt_input_submitted(PromptInput.Submitted("!echo hi"))
        await pilot.pause()
        assert app.harness.turn_controller._pending_shell_results == []
        notices = [str(w.render()) for w in app.query(NoticeMessage)]
        assert any("shell command" in n for n in notices)


@pytest.mark.anyio
async def test_bang_sudo_prompts_for_password_and_cancel_skips_run(
    tmp_path: Path,
):
    """A leading-sudo command opens the password modal; cancelling it (None)
    skips the run entirely."""
    from marim_harness.interfaces.tui.shell_passthrough import SudoPasswordModal
    from marim_harness.interfaces.tui.widgets.prompt import PromptInput

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        seen: list = []

        async def fake_wait(screen):
            seen.append(screen)
            return None  # user cancelled

        app.push_screen_wait = fake_wait  # type: ignore[method-assign]
        await app.on_prompt_input_submitted(
            PromptInput.Submitted("!sudo whoami")
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(seen) == 1
        assert isinstance(seen[0], SudoPasswordModal)
        assert app.harness.turn_controller._pending_shell_results == []


@pytest.mark.anyio
async def test_idle_steer_routes_like_a_submission(tmp_path: Path):
    """A steer fired while no turn runs is just a submission: it must go
    through the same slash routing (and history recall) as Enter, not bypass
    straight into a turn — which would send '/mode auto' to the model as
    prose instead of executing it."""
    from marim_harness.interfaces.tui.widgets.prompt import PromptInput
    from marim_harness.runtime.permissions import Mode

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.harness.mode is not Mode.plan
        await app.on_prompt_input_steer(PromptInput.Steer("/mode plan", []))
        await pilot.pause()
        assert app.harness.mode is Mode.plan, "idle steer bypassed slash routing"
        assert not app.turn_busy  # a command, not a model turn


@pytest.mark.anyio
async def test_quit_warning_rearms_after_confirm_window(tmp_path: Path, monkeypatch):
    """The confirm-to-quit guard must re-warn once the confirm window has
    elapsed — otherwise, once a user has been warned a single time, any later
    accidental Ctrl+C (however much later) silently quits."""
    import marim_harness.interfaces.tui.app as app_module

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._enqueue("first")
        clock = [1000.0]
        monkeypatch.setattr(app_module.time, "monotonic", lambda: clock[0])
        assert app._maybe_warn_pending_quit() is True  # warns
        assert app._maybe_warn_pending_quit() is False  # confirmed quit proceeds
        clock[0] += app_module._QUIT_CONFIRM_WINDOW + 1
        assert app._maybe_warn_pending_quit() is True, (
            "a quit attempt after the confirm window elapses must warn again"
        )


@pytest.mark.anyio
async def test_model_command_refused_mid_turn(tmp_path: Path):
    """/model applies immediately (rebuilds the per-turn model and full-persists
    session metadata); mid-turn that races the running turn exactly like /clear
    and /new — it must be refused with the same guidance."""
    from marim_harness.interfaces.tui.commands import dispatch

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        calls: list = []
        app.harness.set_model = lambda mid: calls.append(mid)
        app._turn_worker = object()  # simulate a running turn
        await dispatch(app, "/model some/other-model")
        await pilot.pause()
        app._turn_worker = None
        assert calls == [], "/model applied mid-turn"
