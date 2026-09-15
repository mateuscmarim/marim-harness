"""Observable contracts for the upstream output policy, using deterministic models."""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import BinaryContent, ToolCallPart, ToolReturn
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RunUsage

from marim_harness.runtime.output_limits import MediaSafeOutputLimits


def _harness(tmp_path, model=None):
    from marim_harness import HarnessBuilder
    from marim_harness.workspace.agents import AgentDef

    h = HarnessBuilder(workspace=tmp_path, model=model or TestModel(call_tools=["sample"]))
    h = h.with_subagent(
        AgentDef(
            name="reader",
            description="read",
            prompt="read",
            tools=frozenset({"read_file"}),
            source="programmatic",
        )
    ).build()
    return h


def _returns(result):
    from pydantic_ai.messages import ToolReturnPart

    return [p for m in result.all_messages() for p in m.parts if isinstance(p, ToolReturnPart)]


async def _read(cap, handle, **kwargs):
    ts = cap.get_toolset()
    ctx = _ctx()
    tools = await ts.get_tools(ctx)
    return await ts.call_tool(
        "read_tool_result",
        {
            "handle": handle,
            "offset": 0,
            "limit": 200,
            "from_end": False,
            "pattern": None,
            **kwargs,
        },
        ctx,
        tools["read_tool_result"],
    )


@pytest.mark.anyio
@pytest.mark.parametrize("child", [False, True])
async def test_native_agents_reduce(tmp_path, child):
    from marim_harness.runtime.output_limits import OutputStorage

    h = _harness(tmp_path)
    agent = h.agent
    if child:
        agent, error = h.subagents.build("reader")
        assert error is None
    payload = "complete payload\n" * 2000

    @agent.tool_plain
    def sample():
        return payload

    result = await agent.run("read", deps=h.deps)
    (part,) = _returns(result)
    assert "read_tool_result" in part.content
    handle = part.metadata["overflow_handle"]
    store = OutputStorage.capture(h.deps).capability().store
    assert await store.read(handle) == payload.encode()
    assert result.usage.requests == 2  # no summarization model request


@pytest.mark.anyio
async def test_structured_mcp_return(tmp_path):
    from pydantic_ai import FunctionToolset

    from marim_harness.mcp.config import make_approval_hook
    from marim_harness.runtime.output_limits import OutputStorage

    h = _harness(tmp_path)
    payload = {"rows": [{"id": n, "text": "x" * 100} for n in range(200)]}
    called = []

    async def server_call(name, args):
        called.append(name)
        return payload

    ts = FunctionToolset()
    hook = make_approval_hook("sample", trusted=True)

    @ts.tool
    async def sample(ctx: RunContext):
        return await hook(ctx, server_call, "sample", {})

    result = await h.agent.run("read", deps=h.deps, toolsets=[ts])
    (part,) = _returns(result)
    store = OutputStorage.capture(h.deps).capability().store
    raw = await store.read(part.metadata["overflow_handle"])
    assert json.loads(raw) == payload
    assert raw.count(b"\n") > 200
    assert called == ["sample"]


@pytest.mark.anyio
async def test_call_identity(tmp_path):
    from pydantic_ai_harness.tool_output_limits import LocalFileStore

    store = LocalFileStore(tmp_path)
    cap = MediaSafeOutputLimits(store=store)
    cases = [("run-a", "a", "A"), ("run-a", "b", "B"), ("run-b", "a", "C")]
    results = await asyncio.gather(
        *(_reduce(cap, char * 20000, run_id=run, call_id=call) for run, call, char in cases)
    )
    handles = [r.metadata["overflow_handle"] for r in results]
    assert len(set(handles)) == 3
    for handle, (_, _, char) in zip(handles, cases, strict=True):
        assert await store.read(handle) == (char * 20000).encode()


@pytest.mark.anyio
async def test_retrieval(tmp_path):
    from pydantic_ai.exceptions import ModelRetry
    from pydantic_ai_harness.tool_output_limits import LocalFileStore

    cap = MediaSafeOutputLimits(store=LocalFileStore(tmp_path / "store"))
    payload = "\n".join(
        f"row {i}: " + ("[literal]" if i % 2 == 0 else "letters") for i in range(1000)
    )
    reduced = await _reduce(cap, payload)
    handle = reduced.metadata["overflow_handle"]
    page = await _read(cap, handle, offset=10, limit=2)
    assert "row 10:" in page and "row 11:" in page and "row 12:" not in page
    tail = await _read(cap, handle, from_end=True, limit=1)
    assert "row 999:" in tail and "row 998:" not in tail
    assert "row 0:" in await _read(cap, handle, pattern="[literal]", limit=1)
    # Regex `[literal]` matches "letters" too; an ignored pattern would also
    # return row 1. These assertions distinguish both from literal filtering.
    filtered = await _read(cap, handle, pattern="[literal]", limit=2)
    assert "row 0:" in filtered and "row 2:" in filtered
    assert "row 1:" not in filtered
    filtered_offset = await _read(cap, handle, pattern="[literal]", offset=1, limit=1)
    assert "row 2:" in filtered_offset and "row 0:" not in filtered_offset
    with pytest.raises(ModelRetry):
        await _read(cap, handle, offset=-1)
    outside = tmp_path / "secret"
    outside.write_text("SECRET-DATA")
    (tmp_path / "store" / "escape").symlink_to(outside)
    denied = await _read(cap, "escape")
    assert "SECRET-DATA" not in denied
    assert "No stored tool result" in denied
    (tmp_path / "store" / handle).unlink()
    missing = await _read(cap, handle)
    assert "No stored tool result" in missing
    assert not (tmp_path / "store" / handle).exists()


@pytest.mark.anyio
async def test_session_capture(tmp_path):
    from marim_harness.runtime.output_limits import OutputStorage
    from marim_harness.subagents.backend import SpawnRun

    h = _harness(tmp_path)
    active = ["A"]
    h.deps.services.get_session_id = lambda: active[0]
    h.deps.services.get_scratchpad = lambda: tmp_path / active[0]
    started = asyncio.Event()
    report_started = asyncio.Event()
    finish = asyncio.Event()
    a = OutputStorage.capture(h.deps)
    child, error = h.subagents.build("reader")
    assert error is None

    async def sample():
        started.set()
        await finish.wait()
        return "from A\n" * 3000

    h.agent.tool_plain(sample)
    child.tool_plain(sample)

    async def report():
        report_started.set()
        await finish.wait()
        return SpawnRun(output="report\n" * 3000, transcript=[], usage=RunUsage())

    main = asyncio.create_task(h.agent.run("read", deps=h.deps))
    bg = asyncio.create_task(
        h.subagents._run_spawn_lifecycle(
            report,
            iso=None,
            resumed=False,
            background=True,
            name="reader",
            stop_task="report",
            note="",
            max_output_chars=1000,
            stream_id="job",
        )
    )
    await started.wait()
    await report_started.wait()
    active[0] = "B"
    finish.set()
    result = await main
    child_result = await child.run("read", deps=h.deps)
    report_result = await bg
    for part in _returns(result) + _returns(child_result):
        assert await a.capability().store.read(part.metadata["overflow_handle"])
    assert str(tmp_path / "A" / "subagent-output") in report_result
    assert not (tmp_path / "B").exists()
    # Disabled scratchpad uses a session-specific workspace path. Sessionless
    # instances keep their key across turns and share it with replaced child deps.
    h.deps.services.get_scratchpad = None
    assert OutputStorage.capture(h.deps).root == tmp_path / ".marim/output/upstream/B"
    h.deps.services.get_session_id = None
    first = OutputStorage.capture(h.deps)
    assert first == OutputStorage.capture(h.deps.replace())
    assert first != OutputStorage.capture(_harness(tmp_path).deps)


@pytest.mark.anyio
async def test_resume_after_compaction(tmp_path):
    from pydantic_ai.messages import (
        ModelMessagesTypeAdapter,
        ModelRequest,
        ModelResponse,
        TextPart,
        UserPromptPart,
    )
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai_harness.compaction import SummarizingCompaction

    from marim_harness.session.compaction import ReductionOptions, reduce_history

    h = _harness(tmp_path)
    h.deps.services.get_session_id = lambda: "persisted-session"

    @h.agent.tool_plain
    def sample():
        return "restored payload\n" * 3000

    original = await h.agent.run("produce", deps=h.deps)
    handle = _returns(original)[0].metadata["overflow_handle"]
    history = []
    for _ in range(10):
        history.extend(
            [
                ModelRequest(parts=[UserPromptPart("old " * 1000)]),
                ModelResponse(parts=[TextPart("old reply")]),
            ]
        )
    history.extend(original.all_messages())
    reduction = await reduce_history(
        history,
        ReductionOptions(
            summary=SummarizingCompaction(max_tokens=1, keep_messages=4),
            target_tokens=5000,
            keep_messages=4,
            keep_pairs=2,
            clear=True,
            force=True,
            focus=None,
        ),
        model=TestModel(custom_output_text="Retained task summary"),
        usage=RunUsage(),
    )
    compacted = reduction.messages
    assert reduction.restructured and "summary" in reduction.stages
    assert len(compacted) < len(history)
    saved = tmp_path / "history.json"
    saved.write_bytes(ModelMessagesTypeAdapter.dump_json(compacted))
    restored = ModelMessagesTypeAdapter.validate_json(saved.read_bytes())
    assert any(
        getattr(p, "metadata", None) == {**_returns(original)[0].metadata}
        for m in restored
        for p in m.parts
    )
    seen = []

    def model(messages, info):
        if not seen:
            seen.append(True)
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "read_tool_result",
                        {
                            "handle": handle,
                            "limit": 2,
                        },
                        tool_call_id="retrieve",
                    )
                ]
            )
        seen.append(messages[-1].parts[0].content)
        return ModelResponse(parts=[TextPart("done")])

    fresh = _harness(tmp_path, FunctionModel(model))
    fresh.deps.services.get_session_id = lambda: "persisted-session"
    result = await fresh.agent.run("retrieve", deps=fresh.deps, message_history=restored)
    assert "restored payload" in seen[-1]
    assert result.output == "done"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mode,approved,executed",
    [
        ("auto", False, True),
        ("ask", True, True),
        ("ask", False, False),
        ("plan", True, False),
    ],
)
async def test_approval_modes(tmp_path, mode, approved, executed):
    from marim_harness import HarnessBuilder
    from marim_harness.runtime.permissions import Mode

    calls = []

    def sample(ctx: RunContext):
        calls.append("executed")
        return "authorized output\n" * 2000

    h = (
        HarnessBuilder(workspace=tmp_path, model=TestModel(call_tools=["sample"]))
        .with_tool(sample, requires_approval=True)
        .with_mode(Mode(mode))
        .build()
    )

    async def approval(call):
        return approved

    h.deps.ui.request_approval = approval
    result = await h.run_turn("run")
    assert result.subtype == "success"
    assert bool(calls) is executed
    files = list((tmp_path / ".marim/output/upstream").rglob("*"))
    assert any(p.is_file() for p in files) is executed


@pytest.mark.anyio
@pytest.mark.parametrize("child,deferred", [(False, False), (True, False), (False, True)])
async def test_retrieval_registration(tmp_path, child, deferred):
    from pydantic_ai import FunctionToolset
    from pydantic_ai.exceptions import UserError
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.toolsets import DeferredLoadingToolset

    from marim_harness.runtime.output_limits import OutputStorage

    names = []

    def model(messages, info):
        names.extend(t.name for t in info.function_tools)
        if len(names) == len(info.function_tools):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "read_tool_result",
                        {
                            "handle": handle,
                            "limit": 1,
                        },
                        tool_call_id="retrieve",
                    )
                ]
            )
        assert "registered payload" in messages[-1].parts[0].content
        return ModelResponse(parts=[TextPart("done")])

    h = _harness(tmp_path, FunctionModel(model))
    cap = OutputStorage.capture(h.deps).capability()
    handle = (await _reduce(cap, "registered payload\n" * 1000)).metadata["overflow_handle"]
    agent = h.agent
    if child:
        agent, error = h.subagents.build("reader")
        assert error is None
    toolsets = []
    if deferred:
        ts = FunctionToolset()

        @ts.tool_plain
        def remote_thing():
            return "remote"

        toolsets = [DeferredLoadingToolset(ts)]
    result = await agent.run("read", deps=h.deps, toolsets=toolsets)
    assert result.output == "done"
    assert names.count("read_tool_result") == 2  # once per request

    @agent.tool_plain
    def read_tool_result(handle: str):
        return "shadow"

    with pytest.raises(UserError, match="read_tool_result"):
        await agent.run("read", deps=h.deps)


@pytest.mark.anyio
async def test_history_and_stream(tmp_path):
    from pydantic_ai.messages import (
        FunctionToolResultEvent,
        ModelMessagesTypeAdapter,
        ToolReturnPart,
    )

    from marim_harness.stream_events import event_to_dict

    h = _harness(tmp_path)

    @h.agent.tool_plain
    def sample():
        return ToolReturn(
            return_value="value\n" * 3000, content="context\n" * 3000, metadata={"source": "test"}
        )

    events = []

    async def handler(ctx, stream):
        async for event in stream:
            if isinstance(event, FunctionToolResultEvent):
                events.append(event)

    result = await h.agent.run("read", deps=h.deps, event_stream_handler=handler)
    restored = ModelMessagesTypeAdapter.validate_json(result.all_messages_json())
    calls = {p.tool_call_id for m in restored for p in m.parts if isinstance(p, ToolCallPart)}
    (part,) = _returns(result)
    (restored_part,) = [p for m in restored for p in m.parts if isinstance(p, ToolReturnPart)]
    assert restored_part.metadata == part.metadata
    assert restored_part.tool_call_id == part.tool_call_id
    assert calls == {part.tool_call_id}
    assert {"overflow_handle", "overflow_content_handle", "source"} <= part.metadata.keys()
    assert part.metadata["source"] == "test"
    assert len(events) == 1
    rendered = json.dumps(event_to_dict(events[0]))
    assert "read_tool_result" in rendered
    assert len(rendered) < 5000
    assert "ToolReturn(" not in rendered


@pytest.mark.anyio
async def test_producer_bounds(tmp_path, monkeypatch):
    from marim_harness.tools.impl import fetch
    from marim_harness.tools.impl.shell import _BoundedOutput
    from tests.test_fetch import _mock_response, _patch_client

    buf = _BoundedOutput(1000)
    for _ in range(100):
        buf.add(b"x" * 1000)
    head, tail = buf.parts(b"")
    assert len(head) + len(tail) == 1000
    assert buf.dropped == 99000
    monkeypatch.setattr(fetch, "_MAX_BYTES", 1000)
    monkeypatch.setattr(fetch, "_validate_target", lambda url: (url, None))
    with _patch_client(_mock_response(text="x" * 20000, content_type="text/plain")):
        result = await fetch.fetch_url("https://example.com")
    assert "Content capped at 1,000 bytes" in result
    assert "x" * 1001 not in result


def test_report_budgets(tmp_path):
    from marim_harness.workflows.engine import MAX_RESULT_CHARS
    from marim_harness.workflows.schema import shape_result

    h = _harness(tmp_path)
    report = "report\n" * 10000
    capped = h.subagents._cap_output(report, 1000, "report")
    assert len(capped) <= 1000
    assert (tmp_path / ".marim/subagent-output/report.md").read_text() == report
    value = {"report": report}
    text, spill = shape_result(value, MAX_RESULT_CHARS, str(tmp_path / "workflow.json"))
    assert len(text) <= MAX_RESULT_CHARS
    assert json.loads(spill) == value
    assert "workflow.json" in text


def test_legacy_writers_removed():
    import marim_harness

    root = Path(marim_harness.__file__).parent
    for relative in [
        "tools/impl/fs.py",
        "tools/impl/shell.py",
        "tools/impl/fetch.py",
        "tools/skill_tools.py",
        "mcp/config.py",
    ]:
        source = (root / relative).read_text()
        for retired in [
            "offload_if_large",
            "write_preview_file",
            "_bound_tool_result",
            "def _offload(",
        ]:
            assert retired not in source, (relative, retired)


@pytest.mark.anyio
@pytest.mark.parametrize("timeout", [False, True])
async def test_command_status(tmp_path, timeout):
    from pydantic_ai_harness.tool_output_limits import LocalFileStore

    from marim_harness.tools.impl.shell import run_bash

    command = "head -c 20000 /dev/zero | tr '\\0' x; "
    command += "sleep 10" if timeout else "exit 7"
    raw = await run_bash(tmp_path, command, timeout=0.2 if timeout else 5)
    assert "timed out" in raw.lower() if timeout else "7" in raw
    store = LocalFileStore(tmp_path / "results")
    reduced = await _reduce(MediaSafeOutputLimits(store=store), raw)
    assert await store.read(reduced.metadata["overflow_handle"]) == raw.encode()


def _ctx(run_id="run"):
    return RunContext(deps=None, model=TestModel(), usage=RunUsage(), run_id=run_id)


async def _reduce(cap, value, *, call_id="call", run_id="run"):
    return await cap.after_tool_execute(
        _ctx(run_id),
        call=ToolCallPart("sample", {}, tool_call_id=call_id),
        tool_def=ToolDefinition(name="sample"),
        args={},
        result=value,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("size", [0, 9999, 10000, 20000])
async def test_text_boundaries(tmp_path, size):
    from pydantic_ai_harness.tool_output_limits import LocalFileStore

    store = LocalFileStore(tmp_path)
    cap = MediaSafeOutputLimits(store=store)
    text = "x" * size
    result = await _reduce(cap, text)
    if size < 10000:
        assert result == text
    else:
        assert isinstance(result, ToolReturn)
        assert len(result.return_value) < size
        assert await store.read(result.metadata["overflow_handle"]) == text.encode()
    agent = Agent(TestModel(call_tools=["sample"]), capabilities=[cap])

    @agent.tool_plain
    def sample():
        return text

    run = await agent.run("read")
    (model_return,) = _returns(run)
    if size < 10000:
        assert model_return.content == text
    else:
        assert "read_tool_result" in model_return.content
        assert await store.read(model_return.metadata["overflow_handle"]) == text.encode()


@pytest.mark.anyio
async def test_storage_failure(tmp_path, monkeypatch):
    from pydantic_ai_harness.tool_output_limits import LocalFileStore

    async def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(LocalFileStore, "write", fail)
    cap = MediaSafeOutputLimits(store=LocalFileStore(tmp_path))
    result = await _reduce(cap, "x" * 20000)
    assert isinstance(result, str)
    assert len(result) <= 4000


@pytest.mark.anyio
@pytest.mark.parametrize("shape", ["direct", "mixed", "wrapped"])
async def test_media_preserved(tmp_path, shape):
    from pydantic_ai_harness.tool_output_limits import LocalFileStore

    binary = BinaryContent(data=b"a" * 20000, media_type="image/png")
    value = binary
    if shape == "mixed":
        value = ["x" * 20000, binary]
    elif shape == "wrapped":
        value = ToolReturn(return_value="x" * 20000, content=[binary])
    cap = MediaSafeOutputLimits(store=LocalFileStore(tmp_path))
    assert await _reduce(cap, value) is value
    agent = Agent(TestModel(call_tools=["sample"]), capabilities=[cap])

    @agent.tool_plain
    def sample():
        return value

    result = await agent.run("read")
    # BinaryContent may be transferred into a UserPromptPart by the runtime.
    encoded = result.all_messages_json()
    from pydantic_ai.messages import ModelMessagesTypeAdapter

    restored = ModelMessagesTypeAdapter.validate_json(encoded)
    binaries = []
    for message in restored:
        for part in message.parts:
            content = getattr(part, "content", None)
            if isinstance(content, BinaryContent):
                binaries.append(content)
            elif isinstance(content, list):
                binaries.extend(x for x in content if isinstance(x, BinaryContent))
    assert any(item.data == binary.data for item in binaries)
    assert not list(tmp_path.rglob("*.txt"))
