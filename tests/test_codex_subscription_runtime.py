"""Native Harness behavior driven by the real subscription model's HTTP streams."""

import asyncio
import json

import httpx2
import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.toolsets import FunctionToolset

from marim_harness.config.model import MultiModelSource
from marim_harness.runtime.builder import HarnessBuilder
from marim_harness.runtime.permissions import Mode
from marim_harness.session import SessionManager
from tests._codex_subscription import MODEL, harness, source, sse
from tests._codex_subscription import wire as wire  # noqa: F401


def builder(tmp_path, **config):
    src = source()
    return HarnessBuilder(workspace=tmp_path, model=src.build(MODEL)).with_config_overrides(
        model_source=MultiModelSource({"openai-codex": src}, "openai-codex"),
        model_id=f"openai-codex:{MODEL}",
        titler=None,
        compaction_strategy=None,
        **config,
    )


def returns(request):
    return [part for part in request["json"]["input"] if part.get("type") == "function_call_output"]


@pytest.mark.anyio
async def test_native_tool_round(wire, tmp_path, monkeypatch):
    from marim_harness.tools.impl import fs

    (tmp_path / "input.txt").write_text("native filesystem sentinel")
    original = fs.read_file
    reads = []

    def observe(*args, **kwargs):
        reads.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(fs, "read_file", observe)
    wire.replies.extend([sse(tool=("read_file", {"path": "input.txt"})), sse()])
    h = builder(tmp_path).build()
    assert (await h.run_turn("read input")).result == "done"
    assert len(reads) == 1
    assert len(wire.requests) == 2
    assert "native filesystem sentinel" in returns(wire.requests[1])[0]["output"]
    await h.aclose()


@pytest.mark.anyio
async def test_ask_before_write(wire, tmp_path):
    target = tmp_path / "new.txt"
    asked = []

    async def approve(call):
        assert not target.exists()
        asked.append(call)
        return True

    h = builder(tmp_path).with_mode(Mode.ask).build()
    h.bind_ui(request_approval=approve)
    wire.replies.extend(
        [sse(tool=("write_file", {"path": "new.txt", "content": "approved"})), sse()]
    )
    assert (await h.run_turn("write")).result == "done"
    assert len(asked) == 1
    assert target.read_text() == "approved"
    assert len(returns(wire.requests[1])) == 1
    await h.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", [Mode.ask, Mode.plan])
async def test_denied_write(wire, tmp_path, mode):
    target = tmp_path / "new.txt"
    called = []

    async def deny(call):
        called.append(call)
        return False

    h = builder(tmp_path).with_mode(mode).build()
    h.bind_ui(request_approval=deny)
    wire.replies.extend(
        [sse(tool=("write_file", {"path": "new.txt", "content": "forbidden"})), sse()]
    )
    await h.run_turn("write")
    assert not target.exists()
    assert len(called) == (1 if mode == Mode.ask else 0)
    expected = "denied" if mode == Mode.ask else "read-only plan mode"
    assert expected in returns(wire.requests[1])[0]["output"].lower()
    await h.aclose()


@pytest.mark.anyio
async def test_mcp_tool(wire, tmp_path):
    server = FunctionToolset(id="trusted")
    calls = []

    from pydantic_ai import RunContext

    @server.tool
    def echo(ctx: RunContext, value: str) -> str:
        calls.append(value)
        return "mcp-result:" + value

    h = builder(tmp_path, mcp_trust_project=True).with_mcp_server(server).build()
    await h.connect()
    wire.replies.extend([sse(tool=("trusted_echo", {"value": "sentinel"})), sse()])
    await h.run_turn("call trusted tool")
    assert calls == ["sentinel"]
    assert "mcp-result:sentinel" in returns(wire.requests[1])[0]["output"]
    assert "trusted_echo" in {tool["name"] for tool in wire.requests[0]["json"]["tools"]}
    await h.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("explicit", [False, True])
async def test_native_subagent(wire, tmp_path, explicit):
    from marim_harness.workspace.agents import AgentDef

    h = (
        builder(tmp_path, subagent_concurrency=1)
        .with_subagent(
            AgentDef(
                name="reader",
                description="Read files",
                prompt="Read only.",
                source="test",
                tools=frozenset({"read_file"}),
            )
        )
        .build()
    )
    release, started = asyncio.Event(), asyncio.Event()
    active = 0
    peak = 0
    limiter = h.subagents._limiter
    assert limiter is not None

    async def wait_for_admission():
        # Observe upstream's queue before releasing the active request so
        # slow child startup cannot masquerade as an enforced concurrency cap.
        while limiter.waiting_count != 1:
            await asyncio.sleep(0)

    async def reply(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        started.set()
        await release.wait()
        active -= 1
        return httpx2.Response(
            200, headers={"Content-Type": "text/event-stream"}, content=sse(text="child done")
        )

    wire.handler = reply
    override = f"openai-codex:{MODEL}" if explicit else None
    tasks = [
        asyncio.create_task(h.subagents.run("reader", "inspect", model=override, stream_id=str(i)))
        for i in range(2)
    ]
    try:
        await asyncio.wait_for(asyncio.gather(wait_for_admission(), started.wait()), 10)
        assert limiter.running_count == 1
        assert limiter.waiting_count == 1
        assert active == 1
        release.set()
        results = await asyncio.gather(*tasks)
        assert all("child done" in result for result in results)
        assert peak == 1
        assert limiter.running_count == 0
        assert limiter.waiting_count == 0
        for request in wire.requests:
            names = {tool["name"] for tool in request["json"]["tools"]}
            assert "read_file" in names
            assert "write_file" not in names and "bash" not in names
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await h.aclose()


@pytest.mark.anyio
async def test_native_auxiliary(wire, tmp_path):
    from pydantic_ai_harness.compaction import SummarizingCompaction, compact_now

    from marim_harness.compaction import make_titler
    from marim_harness.session.ctrl import aux_model_for

    src = source()
    native = src.build(MODEL)
    aux = aux_model_for(native, cwd=str(tmp_path))
    assert aux is native
    history = [
        ModelRequest(parts=[UserPromptPart("Remember the project")]),
        ModelResponse(parts=[TextPart("Details")]),
    ]
    wire.replies.append(sse(text="Project work"))
    assert await make_titler(aux)(history) == "Project work"
    wire.replies.append(sse(text="The project details were retained."))
    summary = await compact_now(
        SummarizingCompaction(model=aux, max_tokens=1, keep_messages=0), history, model=native
    )
    assert "project details" in str(summary)
    h = builder(tmp_path, advisor_model=f"openai-codex:{MODEL}").build()
    wire.replies.extend(
        [
            sse(tool=("advisor", {"prompt": "What is next?"})),
            sse(text="Inspect files"),
            sse(text="Consulted"),
        ]
    )
    assert (await h.run_turn("consult advisor")).result == "Consulted"
    assert "Inspect files" in json.dumps(wire.requests[-1]["json"])
    assert len(wire.requests) == 5
    assert all(request["json"]["store"] is False for request in wire.requests)
    await h.aclose()


@pytest.mark.anyio
async def test_interrupted_resume(wire, tmp_path):
    h = (
        builder(tmp_path)
        .with_sessions(tmp_path / "sessions", stats=False)
        .with_mode(Mode.ask)
        .build()
    )
    waiting = asyncio.Event()

    async def approve(call):
        waiting.set()
        await asyncio.Event().wait()

    h.bind_ui(request_approval=approve)
    wire.replies.append(sse(tool=("write_file", {"path": "pending.txt", "content": "no"})))
    turn = asyncio.create_task(h.run_turn("interrupted write"))
    await asyncio.wait_for(waiting.wait(), 10)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    assert not (tmp_path / "pending.txt").exists()
    saved = h.session.store.load()[0]
    calls = {
        part.tool_call_id for msg in saved for part in msg.parts if isinstance(part, ToolCallPart)
    }
    results = {
        part.tool_call_id for msg in saved for part in msg.parts if isinstance(part, ToolReturnPart)
    }
    assert calls <= results
    wire.replies.append(sse(text="resumed"))
    assert (await h.run_turn("continue safely")).result == "resumed"
    await h.aclose()


@pytest.mark.anyio
async def test_image_and_thinking(wire, tmp_path, monkeypatch):
    from marim_harness.images import store_image

    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "images"))
    image = store_image("fixture", b"\x89PNG\r\n\x1a\nfixture", "image/png")
    h = builder(tmp_path, thinking_level="high").build()
    wire.replies.append(sse())
    await h.run_turn("inspect image", attachments=[(image.path.read_bytes(), image.media_type)])
    request = wire.requests[0]["json"]
    assert request["reasoning"]["effort"] == "high"
    assert "data:image/png;base64," in json.dumps(request["input"])
    await h.aclose()


async def three_requests(wire, tmp_path, **config):
    (tmp_path / "input.txt").write_text("small")
    wire.replies.extend(
        [
            sse(tool=("read_file", {"path": "input.txt"}), input_tokens=40000),
            sse(tool=("read_file", {"path": "input.txt"}), input_tokens=41000),
            sse(input_tokens=42000),
        ]
    )
    h = builder(tmp_path, **config).build()
    out = await h.run_turn("read twice")
    return h, out


@pytest.mark.anyio
async def test_request_usage(wire, tmp_path):
    h, out = await three_requests(wire, tmp_path)
    assert h.session.last_input_tokens == 42000
    assert out.usage.input_tokens == 123000
    assert h.session.usage.input_tokens == 123000
    await h.aclose()


@pytest.mark.anyio
async def test_no_false_compaction(wire, tmp_path, monkeypatch):
    from marim_harness.session import ctrl

    reductions = []
    original = ctrl.reduce_history

    async def observe(*args, **kwargs):
        reductions.append(True)
        return await original(*args, **kwargs)

    monkeypatch.setattr(ctrl, "reduce_history", observe)
    h, out = await three_requests(wire, tmp_path, max_context_tokens=80000)
    assert out.usage.input_tokens == 123000
    assert h.session.compact_threshold == 80000
    assert not await h.session.maybe_compact()
    assert reductions == []
    assert h.session.last_input_tokens == 42000
    await h.aclose()


@pytest.mark.anyio
async def test_compacted_input(wire, tmp_path):
    h = (
        builder(tmp_path, max_context_tokens=1000, keep_last_messages=2)
        .with_sessions(tmp_path / "sessions", stats=False)
        .build()
    )
    wire.replies.extend(
        [sse(text="ARCHIVED:" + "x" * 10000), sse(text="recent answer"), sse(text="continued")]
    )
    await h.run_turn("old user message")
    await h.run_turn("recent question")
    assert await h.manual_compact()
    assert "ARCHIVED:" not in str(h.session.history)
    assert "ARCHIVED:" in str(h.session.transcript)
    await h.run_turn("continue")
    assert "ARCHIVED:" not in json.dumps(wire.requests[-1]["json"]["input"])
    assert "recent" in json.dumps(wire.requests[-1]["json"]["input"])
    await h.aclose()


@pytest.mark.anyio
async def test_session_resume(wire, tmp_path):
    h = builder(tmp_path).with_sessions(tmp_path / "sessions", stats=False).build()
    wire.replies.append(sse(text="first reply"))
    await h.run_turn("remember LOCAL-SENTINEL")
    store = h.session.store
    manager = h.session.manager
    await h.aclose()
    other = harness(tmp_path, store=manager.store(store.session_id), manager=manager)
    other.resume()
    wire.replies.append(sse(text="second reply"))
    await other.run_turn("recall")
    request = wire.requests[-1]["json"]
    assert "LOCAL-SENTINEL" in json.dumps(request["input"])
    assert "first reply" in json.dumps(request["input"])
    assert request["store"] is False
    assert "previous_response_id" not in request
    assert other.session.store.cli_thread_id is None
    await other.aclose()


@pytest.mark.anyio
async def test_separate_cli_session(wire, tmp_path):
    manager = SessionManager(tmp_path, base_dir=tmp_path / "sessions")
    cli = manager.create()
    cli.model = "codex-cli:gpt-6-astra"
    cli.cli_thread_id = "codex-cli:existing-thread"
    cli.save([], __import__("pydantic_ai.usage", fromlist=["RunUsage"]).RunUsage())
    before = cli.path.read_bytes()
    h = harness(tmp_path, store=manager.create(), manager=manager)
    wire.replies.append(sse())
    await h.run_turn("new native session")
    assert cli.path.read_bytes() == before
    assert manager.store(cli.session_id).cli_thread_id == "codex-cli:existing-thread"
    assert h.session.store.cli_thread_id is None
    await h.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [429, "network", 404])
async def test_bounded_failures(wire, tmp_path, failure):
    attempts = 0

    async def reply(request):
        nonlocal attempts
        attempts += 1
        assert attempts <= 3, "native request retries exceeded the SDK bound"
        if failure == "network":
            raise httpx2.ConnectError("offline fixture", request=request)
        return httpx2.Response(failure, json={"error": {"message": "fixture failure"}})

    wire.handler = reply
    h = builder(tmp_path).build()
    with pytest.raises(Exception, match="fixture failure|Connection error"):
        await asyncio.wait_for(h.run_turn("fail safely"), 10)
    assert attempts == (1 if failure == 404 else 3)
    assert all(req["url"].startswith("https://chatgpt.com/") for req in wire.requests)
    await h.aclose()
