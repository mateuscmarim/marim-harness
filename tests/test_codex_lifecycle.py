"""Phase 3 public app-server fixtures (schema-derived; no paid CLI turns)."""

import pytest
from pydantic_ai.messages import TextPart

from marim_harness.codex.server import CodexServer, ThreadHandle
from marim_harness.codex.translate import ItemTranslator
from marim_harness.codex.turn import TurnState, _outputs
from marim_harness.config.context_report import ContextReport
from marim_harness.config.lifecycle import BackendNotice, notice_from_part
from marim_harness.runtime.cli_activity import expand_cli_activity
from tests.test_codex_cli_model import PARAMS, _model, _msgs


def test_completed_compactions_deduplicate_formats_and_keep_distinct_occurrences():
    t = ItemTranslator()

    def event(method, ident):
        return t.translate(
            method, {"turnId": "t", "item": {"id": ident, "type": "contextCompaction"}}
        )

    assert event("item/started", "c1") == []
    first = event("item/completed", "c1")
    assert first[0].message == "Codex compacted its context"
    assert first[0].kind == "compaction"
    assert t.translate("thread/compacted", {"turnId": "t"}) == []
    assert event("item/completed", "c1") == []
    second = event("item/completed", "c2")
    assert len(second) == 1 and second[0].id != first[0].id
    assert t.translate("thread/compacted", {"turnId": "t"}) == []
    other = ItemTranslator()
    assert len(other.translate("thread/compacted", {"turnId": "t"})) == 1
    assert (
        other.translate(
            "item/completed", {"turnId": "t", "item": {"id": "c", "type": "contextCompaction"}}
        )
        == []
    )


def test_warning_shapes_and_model_reroute_are_optional():
    t = ItemTranslator()
    for method, params in [
        ("warning", {"message": "warning"}),
        ("guardianWarning", {"message": "warning"}),
        ("configWarning", {"summary": "warning"}),
        ("deprecationNotice", {"summary": "warning"}),
    ]:
        assert t.translate(method, params)[0].message == "warning"
        assert t.translate(method, {"message": {}, "summary": None}) == []
    note = t.translate("model/rerouted", {"fromModel": "a", "toModel": "b", "reason": "future"})[0]
    assert note.data == {"fromModel": "a", "toModel": "b", "reason": "future"}
    assert note.message == "Codex model rerouted: a → b"
    assert t.translate("model/rerouted", {"toModel": "b"}) == []


def test_retry_and_compaction_do_not_finish_turn_and_stale_event_is_ignored():
    seen = []
    state = TurnState(context=ContextReport(900, 1000), on_context=seen.append)
    assert _outputs("thread/compacted", {"turnId": "old"}, state, "current") == []
    assert state.context == ContextReport(900, 1000)
    notes = _outputs("error", {"willRetry": True, "error": {"message": "busy"}}, state, "current")
    assert notes[0].message == "Codex is retrying: busy"
    assert state.done is None
    _outputs("thread/compacted", {"turnId": "current"}, state, "current")
    assert seen == [None] and state.context is None and state.done is None
    _outputs("turn/completed", {"turn": {"id": "current", "status": "completed"}}, state, "current")
    assert state.done.status == "completed"


@pytest.mark.anyio
async def test_global_warning_queues_once_per_connection_session():
    async def decline(*args):
        return {}

    server, unrelated = CodexServer(), CodexServer()
    parent = server._register({"id": "p"}, decline)
    second = server._register({"id": "q"}, decline)
    server._threads["c"] = ThreadHandle("c", parent.events, decline, parent_id="p")
    remote = unrelated._register({"id": "other"}, decline)
    await server._on_notification("configWarning", {"summary": "bad config"})
    for handle in (parent, second):
        assert handle.events.get_nowait() == ("configWarning", {"summary": "bad config"})
        assert handle.events.empty()
    assert remote.events.empty()


@pytest.mark.anyio
async def test_fake_stream_notice_between_text_and_history_has_same_identity(tmp_path):
    turn = [
        {"notify": "item/agentMessage/delta", "params": {"itemId": "m", "delta": "before"}},
        {
            "notify": "model/rerouted",
            "params": {"fromModel": "a", "toModel": "b", "reason": "future"},
        },
        {"notify": "item/agentMessage/delta", "params": {"itemId": "m", "delta": "after"}},
    ]
    model = _model(tmp_path, {"turns": [turn]})
    seen = []

    async def activity(events):
        seen.extend(events)

    model.on_activity = activity
    try:
        async with model.request_stream(_msgs(), None, PARAMS) as stream:
            async for _ in stream:
                pass
            response = stream.get()
    finally:
        await model.aclose()
    assert isinstance(seen[0], BackendNotice)
    assert seen[0].message == "Codex model rerouted: a → b"
    history = expand_cli_activity([response])
    parts = [part for msg in history for part in msg.parts]
    assert [part.content for part in parts if isinstance(part, TextPart)] == ["before", "", "after"]
    assert notice_from_part(parts[1])["id"] == seen[0].id
