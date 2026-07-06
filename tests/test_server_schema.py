from marim_harness.server.schema import STREAM_EVENT_TYPES, AskAnswerIn, Event, sse_format


def test_event_as_dict_and_sse_format():
    event = Event(seq=7, ts="2026-07-06T00:00:00+00:00", type="turn.started", data={"a": 1})
    assert event.as_dict() == {
        "seq": 7,
        "ts": "2026-07-06T00:00:00+00:00",
        "type": "turn.started",
        "data": {"a": 1},
    }
    assert sse_format(event) == 'id: 7\nevent: turn.started\ndata: {"a": 1}\n\n'


def test_stream_event_types_cover_shared_mapping():
    assert STREAM_EVENT_TYPES == {
        "text": "text.delta",
        "thinking": "thinking.delta",
        "tool_call": "tool.call",
        "tool_result": "tool.result",
    }


def test_ask_answer_shapes():
    assert AskAnswerIn(approve=True).as_answer() == {"approve": True, "reason": None}
    assert AskAnswerIn(approve=False, reason="nope").as_answer() == {
        "approve": False,
        "reason": "nope",
    }
    assert AskAnswerIn(answers={"Color": "red"}).as_answer() == {"answers": {"Color": "red"}}
    assert AskAnswerIn(cancel=True).as_answer() == {"cancel": True}
