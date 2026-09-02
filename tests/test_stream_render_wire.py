"""The stream renderer's wire-event entry points.

Phase 3a moves the TUI off pydantic-ai stream events onto the session-event wire
vocabulary. The wire FLATTENS ``PartStartEvent`` + ``PartDeltaEvent`` into one
``text.delta`` type, so the renderer has to reconstruct "is a message open"
itself — these tests pin that state machine, plus the tool lifecycle, the
stale-block finalize parity, sub-agent routing and the claude-cli side channel.

Wire dicts are parsed through ``parse_wire_event`` exactly the way the app's
pump does, so the tests script the same values the bus carries."""

from pathlib import Path

import pytest

from marim_harness.interfaces.tui.app import HarnessApp
from marim_harness.interfaces.tui.subagents import SubAgentWidget
from marim_harness.interfaces.tui.widgets import (
    AssistantMessage,
    ThinkingWidget,
    ToolCallWidget,
    ToolGroupWidget,
)
from marim_harness.server.wire_events import parse_wire_event
from tests.conftest import _make_deps


def _app(tmp_path: Path) -> HarnessApp:
    from pydantic_ai.models.test import TestModel

    from marim_harness.runtime.harness import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = _make_deps(tmp_path)
    harness = Harness(TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="test")
    return HarnessApp(harness)


def _wire(data: dict):
    """Parse one wire dict the way the app's pump does (unknown → None)."""
    wire = parse_wire_event(data)
    assert wire is not None, data
    return wire


def _replies(app: HarnessApp) -> list[AssistantMessage]:
    """The assistant messages the RENDERER mounted, in transcript order.

    Scoped to the log's own children on purpose: the intro/welcome header is
    itself an ``AssistantMessage`` (nested in ``#intro-header``), so an app-wide
    ``query(AssistantMessage)`` counts app chrome as streamed output."""
    return [w for w in app.query_one("#log").children if isinstance(w, AssistantMessage)]


def _text(text: str):
    return _wire({"type": "text.delta", "text": text})


def _thinking(text: str):
    return _wire({"type": "thinking.delta", "text": text})


def _call(call_id: str, name: str = "bash", args: dict | None = None):
    return _wire(
        {"type": "tool.call", "id": call_id, "name": name, "args": args or {"command": "ls"}}
    )


def _result(call_id: str, content: str = "ok", status: str | None = None):
    data = {"type": "tool.result", "id": call_id, "content": content}
    if status is not None:
        data["status"] = status
    return _wire(data)


@pytest.mark.anyio
async def test_consecutive_text_deltas_render_one_message(tmp_path: Path):
    """Two ``text.delta`` in a row are ONE assistant message: the wire has no
    part-start marker, so the renderer must append to the open message rather
    than opening a second one per delta."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_wire(_text("Hello"))
        await app.stream.on_wire(_text(" world"))
        await pilot.pause()

        messages = _replies(app)
        assert len(messages) == 1
        assert messages[0].text == "Hello world"


@pytest.mark.anyio
async def test_text_delta_after_tool_result_opens_a_new_message(tmp_path: Path):
    """A ``text.delta`` arriving after a tool round-trip opens a NEW message
    below the tool row — the start-vs-delta reconstruction. The pointer to the
    previous message survives (callers read it as the turn's resting reply), so
    "is a message open" cannot be `assistant is not None` alone."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_wire(_text("before"))
        await app.stream.on_wire(_call("c1"))
        await app.stream.on_wire(_result("c1", "ran"))
        await app.stream.on_wire(_text("after"))
        await pilot.pause()

        assert [m.text for m in _replies(app)] == ["before", "after"]

        # ...and the new message sits below the tool row, not above it.
        log = app.query_one("#log")
        kinds = [type(w).__name__ for w in log.children]
        assert kinds.count("AssistantMessage") == 2
        assert kinds.index("ToolCallWidget") > kinds.index("AssistantMessage")
        assert kinds[-1] == "AssistantMessage"


@pytest.mark.anyio
async def test_thinking_deltas_stream_then_finalize_when_text_starts(tmp_path: Path):
    """Reasoning streams into one collapsed block; the first ``text.delta``
    closes it (capped to its preview) and opens the assistant message."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_wire(_thinking("let me "))
        await app.stream.on_wire(_thinking("think"))
        await pilot.pause()

        thoughts = list(app.query(ThinkingWidget))
        assert len(thoughts) == 1
        assert thoughts[0].text == "let me think"
        assert thoughts[0]._done is False  # still streaming

        await app.stream.on_wire(_text("the answer"))
        await pilot.pause()

        assert thoughts[0]._done is True  # finalized/collapsed
        assert app.stream.current_thinking is None
        assert [m.text for m in _replies(app)] == ["the answer"]


@pytest.mark.anyio
async def test_tool_call_then_result_lifecycle(tmp_path: Path):
    """``tool.call`` mounts a pending widget carrying the name/args; the matching
    ``tool.result`` finishes it with the content."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_wire(_call("c1", "read_file", {"path": "a.py"}))
        await pilot.pause()

        widget = app.stream.tool_widgets["c1"]
        assert isinstance(widget, ToolCallWidget)
        assert widget.tool_name == "read_file"
        assert widget.args == {"path": "a.py"}
        assert widget.status == "pending"

        await app.stream.on_wire(_result("c1", "1: import pytest"))
        await pilot.pause()

        assert widget.status == "done"
        assert widget.result_text == "1: import pytest"


@pytest.mark.anyio
async def test_tool_result_status_rides_the_wire(tmp_path: Path):
    """A denied/failed result must render as ✕, not a green ✓ — so the outcome
    travels on the wire's ``status`` field (the wire has no ToolReturnPart to
    read an ``outcome`` off)."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_wire(_call("c1", "write_file", {"path": "a.py"}))
        await app.stream.on_wire(_result("c1", "denied by the user", status="denied"))
        await pilot.pause()

        assert app.stream.tool_widgets["c1"].status == "denied"


@pytest.mark.anyio
async def test_tool_call_finalizes_stale_assistant_text(tmp_path: Path):
    """Parity with ``_finalize_stale_blocks``: any non-text event closes the open
    assistant block (one clean reparse) before it is routed."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_wire(_text("# heading\n\nbody"))
        await pilot.pause()
        app.stream.flush_streams()
        message = app.stream.current_assistant
        assert message is not None
        assert message._finalized is False

        await app.stream.on_wire(_call("c1"))
        await pilot.pause()

        assert message._finalized is True


@pytest.mark.anyio
async def test_whitespace_opener_closes_across_a_tool_round_trip(tmp_path: Path):
    """The unmounted-block "keep open" exception in ``_finalize_stale_blocks`` is
    for a reasoning delta interrupting a still-buffering whitespace opener
    (``test_blank_text_part_does_not_claim_the_slot_above_thinking``). It must NOT
    also hold the block open across an intervening TOOL round-trip: that used to
    let the second tool call rejoin the first tool's group, mounting c2 ABOVE the
    reply that came between them."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_wire(_text(" "))  # whitespace-only opener, buffered
        await app.stream.on_wire(_call("c1"))
        await app.stream.on_wire(_result("c1"))
        await app.stream.on_wire(_text("the answer"))
        await app.stream.on_wire(_call("c2"))
        await pilot.pause()

        log = app.query_one("#log")
        c1 = app.stream.tool_widgets["c1"]
        c2 = app.stream.tool_widgets["c2"]
        assert not list(app.query(ToolGroupWidget))  # c2 did NOT rejoin c1's group
        assert [m.text for m in _replies(app)] == ["the answer"]  # whitespace discarded

        reply = _replies(app)[0]
        assert log.children.index(c1) < log.children.index(reply)
        assert log.children.index(reply) < log.children.index(c2)


@pytest.mark.anyio
async def test_subagent_wire_routes_into_the_owning_pane(tmp_path: Path):
    """``on_subagent_wire`` mounts into the spawn card's transcript pane (never
    the main log) and folds the wire usage dump onto the card."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        renderer = app.stream
        card = renderer.mount_spawn_widget({"type": "explore", "task": "map the loop"})
        card.stream_id = "s1"
        renderer.tool_widgets["s1"] = card
        pane = renderer.ensure_pane(card)
        await pilot.pause()

        await renderer.on_subagent_wire(
            "s1",
            _call("s1-c1", "read_file", {"path": "a.py"}),
            usage={"input_tokens": 80, "output_tokens": 20, "details": {}},
        )
        await pilot.pause()
        renderer.flush_streams()

        assert pane is not None
        tools = [w for w in pane.walk_children(ToolCallWidget)]
        assert [w.tool_name for w in tools] == ["read_file"]
        assert not list(app.query_one("#log").query(ToolCallWidget))
        # The usage dump is priced onto the card exactly like a live RunUsage.
        assert card.tokens == 100


@pytest.mark.anyio
async def test_subagent_wire_text_deltas_share_the_stream_state(tmp_path: Path):
    """Per-stream state: two sub-agent ``text.delta`` build one transcript
    message in that agent's pane."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        renderer = app.stream
        card = renderer.mount_spawn_widget({"type": "explore", "task": "map"})
        card.stream_id = "s1"
        renderer.tool_widgets["s1"] = card
        pane = renderer.ensure_pane(card)
        await pilot.pause()

        await renderer.on_subagent_wire("s1", _text("part one"))
        await renderer.on_subagent_wire("s1", _text(" and two"))
        await pilot.pause()

        assert pane is not None
        messages = [w for w in pane.walk_children(AssistantMessage)]
        assert [m.text for m in messages] == ["part one and two"]


@pytest.mark.anyio
async def test_subagent_wire_ignores_unknown_stream(tmp_path: Path):
    """An event for a stream with no card is dropped, not crashed on."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_subagent_wire("nope", _text("orphan"))
        await pilot.pause()
        assert _replies(app) == []


@pytest.mark.anyio
async def test_cli_activity_wire_renders_main_transcript_cards(tmp_path: Path):
    """The claude-cli side channel renders its tool activity as native cards in
    the MAIN transcript, sharing the top-level sink's run state."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_cli_activity_wire(
            [
                _call("t1", "read_file", {"path": "config.py"}),
                _result("t1", "PORT = 8080"),
            ]
        )
        await pilot.pause()

        widget = app.stream.tool_widgets["t1"]
        assert isinstance(widget, ToolCallWidget)
        assert widget.status == "done"
        assert widget.result_text == "PORT = 8080"
        assert widget in app.query_one("#log").walk_children(ToolCallWidget)


@pytest.mark.anyio
async def test_spawn_agent_tool_call_wire_claims_a_subagent_card(tmp_path: Path):
    """A ``spawn_agent`` call over the wire still gets intercepted into a live
    SubAgentWidget (the sink's claim path), not a generic tool row."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_wire(
            _call("s1", "spawn_agent", {"type": "explore", "task": "map the loop"})
        )
        await pilot.pause()

        card = app.stream.tool_widgets["s1"]
        assert isinstance(card, SubAgentWidget)
        assert card.stream_id == "s1"
        assert card in app.stream.subagents
