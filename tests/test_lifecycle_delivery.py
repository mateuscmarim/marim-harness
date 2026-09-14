"""Lifecycle notices use the session stream, keep their order and stay display-only."""

import io

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.claude.lifecycle import BackendTask
from marim_harness.config.lifecycle import (
    BackendNotice,
    deliver_notice,
    notice_from_part,
    notice_part,
)
from marim_harness.interfaces.cli.headless import run_headless
from marim_harness.interfaces.tui.session_view import order_response_parts
from marim_harness.interfaces.tui.stream_render import wire_from_event
from marim_harness.interfaces.tui.widgets import AssistantMessage, NoticeMessage
from marim_harness.server.bus import EventBus
from marim_harness.server.host import SessionHost
from marim_harness.server.wire_events import SessionNotice, TextDelta, parse_wire_event
from tests.test_headless import _harness
from tests.test_server_host import _drain_until, _make_deps, _make_harness, _spy
from tests.test_session_view_replay import _app


def test_notice_wire_is_additive_and_old_message_still_parses():
    notice = BackendNotice("Backend compacted", "claude-cli", "compaction", id="notice-1")
    wire = wire_from_event(notice)
    assert isinstance(wire, SessionNotice)
    assert wire.id == "notice-1"
    assert wire.message == notice.message
    old = parse_wire_event({"type": "session.notice", "message": "old notice"})
    assert isinstance(old, SessionNotice)
    assert old.id is None


@pytest.mark.anyio
async def test_host_notice_precedes_completion_without_mirror_compaction(tmp_path):
    notice = BackendNotice("Backend compacted", "claude-cli", "compaction", id="notice-1")

    async def stream(messages, info):
        yield "before"
        await host._on_cli_activity([notice])
        yield "after"

    model = FunctionModel(stream_function=stream)
    host = SessionHost(_make_harness(model, _make_deps(tmp_path)), EventBus())
    events = _spy(host.bus)
    try:
        host.submit("go")
        terminal = await _drain_until(events, "turn.finished")
        [event] = [e for e in events if e.type == "session.notice"]
        assert event.data == notice.to_payload()
        assert event.seq < terminal.seq
        assert not any(e.type == "compaction.finished" for e in events)
        assert terminal.data["output"] == "beforeafter"
    finally:
        await host.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("output_format", ["text", "json", "stream-json"])
async def test_headless_notice_uses_supplied_stderr_only(tmp_path, output_format):
    async def stream(messages, info):
        yield "answer"
        await deliver_notice(BackendNotice("notice only", "codex-cli", "warning"), None)

    out, err = io.StringIO(), io.StringIO()
    harness = _harness(tmp_path, model=FunctionModel(stream_function=stream))
    assert await run_headless(harness, "go", output_format, out=out, err=err) == 0
    assert err.getvalue() == "notice only\n"
    assert "notice only" not in out.getvalue()
    assert "answer" in out.getvalue()


@pytest.mark.anyio
async def test_tui_notice_splits_text_and_deduplicates_restored_identity(tmp_path):
    app = _app(tmp_path)
    first = BackendNotice("same message", "codex-cli", "warning", id="one")
    second = BackendNotice("same message", "codex-cli", "warning", id="two")
    async with app.run_test() as pilot:
        await pilot.pause()
        log = app.query_one("#log")
        await log.remove_children()
        await app.stream.on_wire(TextDelta(type="text.delta", text="before"))
        await app.stream.on_wire(wire_from_event(first))
        await app.stream.on_wire(TextDelta(type="text.delta", text="after"))
        app.stream.flush_streams()
        assert [type(w) for w in log.children] == [
            AssistantMessage,
            NoticeMessage,
            AssistantMessage,
        ]
        await app.session._replay_parts(
            notice_part(first.to_payload()), log, log.mount, {}, None, None
        )
        await app.stream.on_wire(wire_from_event(second))
        assert len(log.query(NoticeMessage)) == 2


@pytest.mark.anyio
async def test_history_marker_and_parallel_result_notice_restore_in_order(tmp_path):
    app = _app(tmp_path)
    note = BackendNotice("compacted", "claude-cli", "compaction", id="history")
    mounted = []

    async def mount(widget):
        mounted.append(widget)

    parts = [TextPart("before"), notice_part(note.to_payload()), ThinkingPart("after")]
    assert order_response_parts(parts) == parts
    async with app.run_test() as pilot:
        await pilot.pause()
        for part in parts:
            await app.session._replay_parts(part, None, mount, {}, None, None)
        assert isinstance(mounted[1], NoticeMessage)
        other = BackendNotice("after result", "claude-cli", "notification", id="after-tool")
        result = ToolReturnPart(
            "bash", "ok", "tool", metadata={"backend_notices_after": [other.to_payload()]}
        )
        await app.session._replay_parts(result, None, mount, {}, None, None)
        assert isinstance(mounted[-1], NoticeMessage)
        await app.session._replay_parts(result, None, mount, {}, None, None)
        assert len(mounted) == 4


@pytest.mark.anyio
async def test_background_shell_updates_one_card(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        for status, description in (
            ("running", "build"),
            ("running", "testing"),
            ("interrupted", "testing"),
        ):
            await app.stream.on_wire(wire_from_event(BackendTask("task", description, status)))
        assert len(app.stream.backend_tasks) == 1
        widget = app.stream.backend_tasks["task"]
        assert widget.status == "failed"
        assert widget.result_text == "testing (interrupted)"
        assert widget.args == {"description": "testing"}
        assert app.stream.subagents == []


def test_claude_child_lifecycle_uses_child_stream_and_history():
    from marim_harness.subagents.cli_demux import CliSubagentDemux

    demux = CliSubagentDemux()
    obj = {
        "type": "system",
        "subtype": "notification",
        "text": "child warning",
        "parent_tool_use_id": "child",
        "session_id": "session",
        "uuid": "event",
    }
    events, remainder = demux.route(obj)
    assert remainder is None
    [routed] = events
    assert routed.stream_id == "child"
    assert isinstance(routed.event, BackendNotice)
    [message] = demux.child_transcripts()["child"]
    assert isinstance(message, ModelResponse)
    assert message.parts[0].content == ""
    assert notice_from_part(message.parts[0])["id"] == routed.event.id
    assert demux.route(obj) == ([], None)


@pytest.mark.anyio
async def test_codex_child_lifecycle_stream_and_history_share_identity():
    from marim_harness.codex.collab import ChildSinks, ChildStreams, Routed
    from marim_harness.codex.translate import Notice

    received = []

    async def on_event(sid, event, usage):
        received.append((sid, event))

    streams = ChildStreams(ChildSinks(on_event=on_event))
    await streams.deliver(Routed("child", Notice("warning", id="same-id")))
    [(sid, notice)] = received
    assert sid == "child"
    assert isinstance(notice, BackendNotice)
    [message] = streams.transcripts["child"]
    assert notice_from_part(message.parts[0])["id"] == notice.id == "same-id"


@pytest.mark.anyio
async def test_backend_observation_preserves_parked_approval_identity(tmp_path):
    from marim_harness.config.lifecycle import BackendObservation
    from tests.test_server_host import _text_only_model

    host = SessionHost(_make_harness(_text_only_model(), _make_deps(tmp_path)), EventBus())
    events = _spy(host.bus)
    try:
        ask = host._park("approval", {"tool": "bash"})
        before = host.pending_asks()
        await host._on_cli_activity([BackendObservation(telemetry={"state": "idle"})])
        await host._on_cli_activity([BackendNotice("denied", "claude-cli", "permission_denied")])
        assert host.pending_asks() == before
        assert host.pending_asks()[0]["id"] == ask.id
        assert not ask.future.done()
        assert not any(e.type in ("ask.resolved", "turn.finished") for e in events)
    finally:
        await host.aclose()


@pytest.mark.anyio
async def test_child_notice_callback_failure_retains_history_without_payload_log(caplog):
    from marim_harness.codex.collab import ChildSinks, ChildStreams, Routed
    from marim_harness.codex.translate import Notice, TextDelta

    async def fail(sid, event, usage):
        if isinstance(event, BackendNotice):
            raise ValueError("private error")

    streams = ChildStreams(ChildSinks(on_event=fail))
    await streams.deliver(Routed("child", Notice("private notice", id="private-id")))
    await streams.deliver(Routed("child", TextDelta("message", "still working")))
    [message] = streams.transcripts["child"]
    assert notice_from_part(message.parts[0])["id"] == "private-id"
    assert message.parts[1].content == "still working"
    assert "codex-cli" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["claude", "codex"])
async def test_fake_backend_through_host_persists_same_notice_identity(
    tmp_path, monkeypatch, backend
):
    from tests.test_claude_cli_model import _model as claude_model
    from tests.test_codex_cli_model import _model as codex_model

    if backend == "claude":
        steps = [
            {"text": "before"},
            {"raw": {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 17}},
            {
                "raw": {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "uuid": "host-compact",
                    "session_id": "s",
                    "compact_metadata": {"post_tokens": 100},
                }
            },
            {"text": "after"},
        ]
        model = claude_model(tmp_path, monkeypatch, {"turns": [steps]})
    else:
        steps = [
            {"notify": "item/agentMessage/delta", "params": {"itemId": "m", "delta": "before"}},
            {"notify": "model/rerouted", "params": {"fromModel": "old", "toModel": "new"}},
            {"notify": "item/agentMessage/delta", "params": {"itemId": "m", "delta": "after"}},
        ]
        model = codex_model(tmp_path, {"turns": [steps]})
    harness = _harness(tmp_path, model=model)
    host = SessionHost(harness, EventBus())
    events = _spy(host.bus)
    try:
        host.submit("go")
        terminal = await _drain_until(events, "turn.finished")
        [notice] = [e for e in events if e.type == "session.notice"]
        assert notice.seq < terminal.seq
        assert notice.data["backend"] == f"{backend}-cli"
        assert notice.data["message"] not in terminal.data["output"]
        assert terminal.data["output"].replace("\n", "") == "beforeafter"
        assert not any(e.type == "compaction.finished" for e in events)
        messages = harness.session.store.load()[0]
        persisted = [notice_from_part(p) for m in messages for p in m.parts if notice_from_part(p)]
        assert persisted == [notice.data]
        if backend == "claude":
            snapshots = [e for e in events if e.type == "session.backend_state"]
            assert any(
                e.data["inventory"].get("tools") == ["Read", "Write", "Edit", "Bash"]
                for e in snapshots
            )
            assert any(
                e.data["telemetry"].get("thinking_tokens") == 17 and e.seq < terminal.seq
                for e in snapshots
            )
    finally:
        await host.aclose()
