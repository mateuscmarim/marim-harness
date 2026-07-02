from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPartDelta,
)

from marim_harness.subagents.cli_demux import SPAWN_TOOL_NAMES, CliSubagentDemux


def _spawn_obj(tid="t1", name="Agent", stype="Explore", desc="find it", prompt="do it",
               extra_blocks=(), parent=None):
    obj = {"type": "assistant", "message": {"model": "claude-haiku-4-5", "id": "msg_p1",
           "content": [
               {"type": "tool_use", "id": tid, "name": name,
                "input": {"description": desc, "subagent_type": stype, "prompt": prompt}},
               *extra_blocks,
           ]}}
    if parent:
        obj["parent_tool_use_id"] = parent
    return obj


def _child_text(parent="t1", text="4", mid="msg_c1", usage=None, model="claude-haiku-4-5"):
    msg = {"model": model, "id": mid,
           "content": [{"type": "text", "text": text}]}
    if usage is not None:
        msg["usage"] = usage
    return {"type": "assistant", "parent_tool_use_id": parent, "message": msg}


def test_unrelated_objects_pass_through_unchanged():
    d = CliSubagentDemux()
    obj = {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}
    events, passthrough = d.route(obj)
    assert events == [] and passthrough is obj
    events, passthrough = d.route({"type": "system", "subtype": "init"})
    assert events == [] and passthrough == {"type": "system", "subtype": "init"}
    events, passthrough = d.route({"type": "result", "result": "done"})
    assert events == [] and passthrough == {"type": "result", "result": "done"}


def test_spawn_tool_use_becomes_spawn_agent_call():
    d = CliSubagentDemux()
    events, passthrough = d.route(_spawn_obj())
    assert passthrough is None  # the spawn was the only block
    (r,) = events
    assert r.stream_id is None  # routed to the containing (main) stream
    assert isinstance(r.event, FunctionToolCallEvent)
    part = r.event.part
    assert part.tool_name == "spawn_agent" and part.tool_call_id == "t1"
    assert part.args_as_dict() == {
        "type": "Explore", "task": "do it", "description": "find it",
    }


def test_legacy_task_tool_name_also_claimed():
    assert "Task" in SPAWN_TOOL_NAMES
    d = CliSubagentDemux()
    events, passthrough = d.route(_spawn_obj(name="Task"))
    assert passthrough is None and events[0].event.part.tool_name == "spawn_agent"


def test_mixed_assistant_message_keeps_other_blocks():
    d = CliSubagentDemux()
    obj = _spawn_obj(extra_blocks=({"type": "text", "text": "spawning now"},))
    events, passthrough = d.route(obj)
    assert len(events) == 1
    assert passthrough is not None and passthrough is not obj  # a stripped copy
    kept = passthrough["message"]["content"]
    assert kept == [{"type": "text", "text": "spawning now"}]
    # original object was not mutated
    assert len(obj["message"]["content"]) == 2


def test_child_messages_route_to_child_stream():
    d = CliSubagentDemux()
    d.route(_spawn_obj())
    events, passthrough = d.route(_child_text())
    assert passthrough is None
    assert [r.stream_id for r in events] == ["t1", "t1"]
    assert isinstance(events[0].event, PartStartEvent)
    assert isinstance(events[1].event, PartDeltaEvent)
    assert isinstance(events[1].event.delta, TextPartDelta)
    assert events[1].event.delta.content_delta == "4"


def test_child_usage_accumulates_once_per_message_id():
    usage = {"input_tokens": 10, "output_tokens": 5,
             "cache_creation_input_tokens": 10066}
    d = CliSubagentDemux()
    d.route(_spawn_obj())
    # stream-json repeats the same message.usage on both events of one message
    events, _ = d.route(_child_text(mid="msg_c1", usage=usage))
    assert events[0].usage.output_tokens == 5
    assert events[0].usage.input_tokens == 10 + 10066  # cache-inclusive fold
    events, _ = d.route(_child_text(mid="msg_c1", usage=usage))
    assert events[0].usage.output_tokens == 5  # same message id — not re-counted
    events, _ = d.route(_child_text(mid="msg_c2", usage=usage))
    assert events[0].usage.output_tokens == 10  # a new message id accumulates


def test_child_model_surfaced_once():
    d = CliSubagentDemux()
    d.route(_spawn_obj())
    events, _ = d.route(_child_text(mid="m1"))
    assert events[0].model == "claude-haiku-4-5"
    assert events[1].model is None  # only the first event of the stream
    events, _ = d.route(_child_text(mid="m2"))
    assert events[0].model is None  # already sent


def test_async_launch_tool_result_is_suppressed():
    d = CliSubagentDemux()
    d.route(_spawn_obj())
    events, passthrough = d.route(
        {"type": "system", "subtype": "task_started", "tool_use_id": "t1"})
    assert events == [] and passthrough is None
    events, passthrough = d.route({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": "Async agent launched successfully..."},
    ]}})
    assert events == [] and passthrough is None  # launch metadata: fully swallowed


def test_task_notification_finishes_spawn():
    d = CliSubagentDemux()
    d.route(_spawn_obj())
    d.route({"type": "system", "subtype": "task_started", "tool_use_id": "t1"})
    events, passthrough = d.route(
        {"type": "system", "subtype": "task_notification", "tool_use_id": "t1",
         "status": "completed", "summary": "4",
         "usage": {"total_tokens": 10086, "tool_uses": 0, "duration_ms": 2073}})
    assert passthrough is None
    (r,) = events
    assert r.stream_id is None and isinstance(r.event, FunctionToolResultEvent)
    assert r.event.part.tool_name == "spawn_agent"
    assert r.event.part.tool_call_id == "t1"
    assert r.event.part.content == "4" and r.event.part.outcome == "success"
    # a duplicate notification does not double-finish
    events, _ = d.route(
        {"type": "system", "subtype": "task_notification", "tool_use_id": "t1",
         "status": "completed", "summary": "4"})
    assert events == []


def test_task_notification_failed_status_marks_failed():
    d = CliSubagentDemux()
    d.route(_spawn_obj())
    events, _ = d.route(
        {"type": "system", "subtype": "task_notification", "tool_use_id": "t1",
         "status": "failed", "summary": ""})
    assert events[0].event.part.outcome == "failed"
    assert "failed" in events[0].event.part.content


def test_notification_for_unknown_spawn_is_dropped():
    d = CliSubagentDemux()
    events, passthrough = d.route(
        {"type": "system", "subtype": "task_notification", "tool_use_id": "nope",
         "status": "completed", "summary": "x"})
    assert events == [] and passthrough is None


def test_sync_tool_result_finishes_spawn():
    # Legacy CLIs: no task_started; the spawn's tool_result IS the report.
    d = CliSubagentDemux()
    d.route(_spawn_obj())
    events, passthrough = d.route({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "the report"},
    ]}})
    assert passthrough is None
    (r,) = events
    assert isinstance(r.event, FunctionToolResultEvent)
    assert r.event.part.content == "the report"
    assert r.event.part.outcome == "success"


def test_unrelated_tool_result_passes_through():
    d = CliSubagentDemux()
    obj = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "other", "content": "x"},
    ]}}
    events, passthrough = d.route(obj)
    assert events == [] and passthrough is obj


def test_nested_spawn_routes_to_child_container():
    d = CliSubagentDemux()
    d.route(_spawn_obj(tid="t1"))
    # the child t1 itself spawns g1
    events, passthrough = d.route(_spawn_obj(tid="g1", parent="t1"))
    assert passthrough is None
    (r,) = events
    assert r.stream_id == "t1"  # the spawn call renders inside t1's pane
    assert r.event.part.tool_call_id == "g1"
    # the grandchild's own messages route to g1
    events, _ = d.route(_child_text(parent="g1", mid="msg_g"))
    assert events[0].stream_id == "g1"
    # t1's sidecar transcript carries the nested spawn call
    calls = [p for m in d.child_transcripts()["t1"] for p in m.parts]
    assert any(getattr(p, "tool_name", "") == "spawn_agent" for p in calls)


def test_child_transcripts_capture_messages():
    d = CliSubagentDemux()
    d.route(_spawn_obj())
    d.route(_child_text())
    transcripts = d.child_transcripts()
    assert "t1" in transcripts and len(transcripts["t1"]) == 1
