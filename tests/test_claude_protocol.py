"""StreamJsonClient: newline-JSON framing and control_request correlation
over an in-memory pipe (no subprocess)."""

from __future__ import annotations

import asyncio
import json

import pytest

from marim_harness.claude.protocol import (
    CLOSED,
    ControlError,
    ProcessClosed,
    StreamJsonClient,
)

pytestmark = pytest.mark.anyio


class _Writer:
    """Stands in for the subprocess stdin StreamWriter; records parsed objects."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def write(self, data: bytes) -> None:
        for line in data.decode().splitlines():
            if line.strip():
                self.sent.append(json.loads(line))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    def is_closing(self) -> bool:
        return False


class _Pipe:
    def __init__(self) -> None:
        self.reader = asyncio.StreamReader()
        self.writer = _Writer()

    @property
    def sent(self) -> list[dict]:
        return self.writer.sent

    def feed(self, obj: dict) -> None:
        self.reader.feed_data((json.dumps(obj) + "\n").encode())

    def eof(self) -> None:
        self.reader.feed_eof()


def _client(pipe: _Pipe, **kw) -> tuple[StreamJsonClient, list[dict]]:
    events: list[dict] = []
    client = StreamJsonClient(
        pipe.reader,
        pipe.writer,  # type: ignore[arg-type]
        on_event=events.append,
        **kw,
    )
    return client, events


def _ok(rid: str, body: dict) -> dict:
    return {
        "type": "control_response",
        "response": {"subtype": "success", "request_id": rid, "response": body},
    }


async def test_control_correlates_by_request_id_and_returns_body():
    pipe = _Pipe()
    client, _ = _client(pipe)
    reader = asyncio.create_task(client.run())
    task = asyncio.create_task(client.control("initialize", hooks={}))
    await asyncio.sleep(0)
    sent = pipe.sent[-1]
    assert sent["type"] == "control_request"
    assert sent["request"] == {"subtype": "initialize", "hooks": {}}
    rid = sent["request_id"]
    # An unrelated response id first: must be ignored, not mis-correlated.
    pipe.feed(_ok("other", {}))
    pipe.feed(_ok(rid, {"pid": 7}))
    assert await asyncio.wait_for(task, 1) == {"pid": 7}
    pipe.eof()
    await reader


async def test_control_error_subtype_raises_control_error():
    pipe = _Pipe()
    client, _ = _client(pipe)
    reader = asyncio.create_task(client.run())
    task = asyncio.create_task(client.control("set_model", model="x"))
    await asyncio.sleep(0)
    rid = pipe.sent[-1]["request_id"]
    pipe.feed(
        {
            "type": "control_response",
            "response": {"subtype": "error", "request_id": rid, "error": "nope"},
        }
    )
    with pytest.raises(ControlError, match="nope"):
        await asyncio.wait_for(task, 1)
    pipe.eof()
    await reader


async def test_eof_fails_pending_control_and_publishes_closed():
    pipe = _Pipe()
    client, events = _client(pipe)
    reader = asyncio.create_task(client.run())
    task = asyncio.create_task(client.control("interrupt"))
    await asyncio.sleep(0)
    pipe.eof()
    with pytest.raises(ProcessClosed):
        await asyncio.wait_for(task, 1)
    await reader
    assert client.closed.is_set()
    assert events[-1]["type"] == CLOSED


async def test_control_after_close_raises_immediately():
    pipe = _Pipe()
    client, _ = _client(pipe)
    pipe.eof()
    await client.run()
    with pytest.raises(ProcessClosed):
        await client.control("interrupt")


async def test_events_are_routed_to_on_event_in_order():
    pipe = _Pipe()
    client, events = _client(pipe)
    reader = asyncio.create_task(client.run())
    pipe.feed({"type": "system", "subtype": "init", "session_id": "s1"})
    pipe.reader.feed_data(b"not json at all\n")  # noise on stdout is skipped
    pipe.feed({"type": "result", "subtype": "success"})
    pipe.eof()
    await reader
    assert [e["type"] for e in events] == ["system", "result", CLOSED]


async def test_request_handler_runs_concurrently_and_answer_is_written():
    pipe = _Pipe()
    gate = asyncio.Event()

    async def handler(rid: str, request: dict) -> dict:
        assert request["subtype"] == "can_use_tool"
        await gate.wait()
        return {"behavior": "allow", "updatedInput": request["input"]}

    client, events = _client(pipe, on_request=handler)
    reader = asyncio.create_task(client.run())
    pipe.feed(
        {
            "type": "control_request",
            "request_id": "r1",
            "request": {"subtype": "can_use_tool", "tool_name": "Write", "input": {"a": 1}},
        }
    )
    await asyncio.sleep(0.01)
    assert client.prompts_open == 1
    # The reader is NOT blocked behind the handler: a plain event still lands.
    pipe.feed({"type": "assistant", "message": {"content": []}})
    await asyncio.sleep(0.01)
    assert events[-1]["type"] == "assistant"
    gate.set()
    await asyncio.sleep(0.01)
    assert client.prompts_open == 0
    assert pipe.sent[-1] == _ok("r1", {"behavior": "allow", "updatedInput": {"a": 1}})
    pipe.eof()
    await reader


async def test_cancel_request_cancels_handler_and_calls_on_cancel():
    pipe = _Pipe()
    cancelled: list[str] = []
    started = asyncio.Event()

    async def handler(rid: str, request: dict) -> dict:
        started.set()
        await asyncio.sleep(10)
        return {"behavior": "allow", "updatedInput": {}}

    client, _ = _client(pipe, on_request=handler, on_cancel=cancelled.append)
    reader = asyncio.create_task(client.run())
    pipe.feed(
        {
            "type": "control_request",
            "request_id": "r9",
            "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {}},
        }
    )
    await asyncio.wait_for(started.wait(), 1)
    before = len(pipe.sent)
    pipe.feed({"type": "control_cancel_request", "request_id": "r9"})
    await asyncio.sleep(0.01)
    assert cancelled == ["r9"]
    assert client.prompts_open == 0
    assert len(pipe.sent) == before  # a cancelled prompt writes no answer
    pipe.eof()
    await reader


async def test_handler_exception_answers_with_error_response():
    pipe = _Pipe()

    async def handler(rid: str, request: dict) -> dict:
        raise RuntimeError("boom")

    client, _ = _client(pipe, on_request=handler)
    reader = asyncio.create_task(client.run())
    pipe.feed(
        {"type": "control_request", "request_id": "r2", "request": {"subtype": "can_use_tool"}}
    )
    await asyncio.sleep(0.01)
    assert pipe.sent[-1]["response"] == {"subtype": "error", "request_id": "r2", "error": "boom"}
    pipe.eof()
    await reader


async def test_no_handler_bound_answers_with_error_response():
    pipe = _Pipe()
    client, _ = _client(pipe)
    reader = asyncio.create_task(client.run())
    pipe.feed(
        {"type": "control_request", "request_id": "r3", "request": {"subtype": "can_use_tool"}}
    )
    await asyncio.sleep(0.01)
    assert pipe.sent[-1]["response"]["subtype"] == "error"
    assert pipe.sent[-1]["response"]["request_id"] == "r3"
    pipe.eof()
    await reader


async def test_user_writes_text_block_message():
    pipe = _Pipe()
    client, _ = _client(pipe)
    await client.user("hi there")
    assert pipe.sent[-1] == {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": "hi there"}]},
    }
