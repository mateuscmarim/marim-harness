"""Subscription wire regressions for deferred MCP discovery; all HTTP is mocked."""

import json

import httpx2
import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.openai_codex import OpenAICodexModel
from pydantic_ai.toolsets import FunctionToolset

from marim_harness.runtime.builder import HarnessBuilder
from tests._codex_subscription import source, sse
from tests._codex_subscription import wire as wire  # noqa: F401


class ListedToolset(FunctionToolset):
    """In-process MCP stand-in whose catalog reflects its actual registered tools."""

    async def list_tools(self):
        return list(self.tools.values())


def make_harness(tmp_path, count, model_name="gpt-6-astra"):
    server = ListedToolset(id="trusted")
    calls = []

    def echo(ctx: RunContext, value: str) -> str:
        """Echo the supplied value."""
        calls.append(value)
        return "mcp-result:" + value

    for i in range(count):
        server.add_function(echo, name=f"echo_{i}")
    h = (
        HarnessBuilder(workspace=tmp_path, model=source().build(model_name))
        .with_config_overrides(titler=None, compaction_strategy=None, mcp_trust_project=True)
        .with_mcp_server(server)
        .build()
    )
    return h, calls


def discovery_stream(tool):
    """Real server-executed discovery events preceding a function call."""
    events = [
        json.loads(frame.removeprefix("data: "))
        for frame in sse(tool=("trusted_echo_0", {"value": "sentinel"})).split("\n\n")
        if frame
    ]
    call = dict(
        type="tool_search_call",
        id="ts_1",
        call_id="search_1",
        execution="server",
        status="completed",
        arguments={"queries": ["echo"]},
    )
    result = dict(
        type="tool_search_output",
        id="tso_1",
        call_id="search_1",
        execution="server",
        status="completed",
        tools=[tool],
    )
    search_events = [
        dict(type=f"response.output_item.{phase}", output_index=i, item=item)
        for i, item in enumerate((call, result))
        for phase in ("added", "done")
    ]
    events[1]["output_index"] = 2
    events[1:1] = search_events
    return "".join(
        f"data: {json.dumps({**event, 'sequence_number': i})}\n\n" for i, event in enumerate(events)
    )


@pytest.mark.anyio
async def test_deferred_discovery_roundtrip(wire, tmp_path):
    h, calls = make_harness(tmp_path, 16)

    async def respond(request):
        tools = json.loads(await request.aread())["tools"]
        native = any(t["type"] == "tool_search" for t in tools)
        deferred = [t for t in tools if t.get("defer_loading")]
        if native and not deferred:
            return httpx2.Response(
                400,
                json={
                    "error": {
                        "message": "tools.tool_search requires at least one deferred tool",
                        "type": "invalid_request_error",
                    }
                },
            )
        assert native
        assert {t["name"] for t in deferred} == {f"trusted_echo_{i}" for i in range(16)}
        assert all(t["type"] == "function" for t in deferred)
        content = sse()
        if len(wire.requests) == 1:
            content = discovery_stream(next(t for t in deferred if t["name"] == "trusted_echo_0"))
        return httpx2.Response(200, headers={"Content-Type": "text/event-stream"}, content=content)

    wire.handler = respond
    try:
        await h.connect()
        assert (await h.run_turn("Discover echo and call it with sentinel")).result == "done"
        assert calls == ["sentinel"]
        assert len(wire.requests) == 2
        replay = wire.requests[1]["json"]["input"]
        assert any(p.get("type") == "tool_search_call" and p["id"] == "ts_1" for p in replay)
        assert any(p.get("type") == "tool_search_output" and p["id"] == "tso_1" for p in replay)
        assert any(
            p.get("type") == "function_call_output" and "mcp-result:sentinel" in p["output"]
            for p in replay
        )
        assert wire.refreshes == []
    finally:
        await h.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("count", [0, 1, 15])
async def test_inline_tools_omit_search(wire, tmp_path, count):
    h, calls = make_harness(tmp_path, count)
    wire.replies.extend(
        [sse(tool=("trusted_echo_0", {"value": "sentinel"})), sse()] if count else [sse()]
    )
    try:
        await h.connect()
        assert (await h.run_turn("echo sentinel")).result == "done"
        tools = wire.requests[0]["json"]["tools"]
        assert all(t["type"] != "tool_search" and not t.get("defer_loading") for t in tools)
        assert {t["name"] for t in tools if t["name"].startswith("trusted_")} == {
            f"trusted_echo_{i}" for i in range(count)
        }
        assert calls == (["sentinel"] if count else [])
    finally:
        await h.aclose()


@pytest.mark.parametrize("model_name", ["gpt-6-astra", "gpt-5.5", "gpt-5.3-codex"])
def test_subscription_profile(wire, model_name):
    model = source().build(model_name)
    upstream = OpenAICodexModel(model_name, provider=model.provider)
    if model_name == "gpt-5.3-codex":
        assert model.profile == upstream.profile
        assert model.tool_deferral_mode is None
    else:
        assert model.profile == {**upstream.profile, "tool_deferral_mode": "with_tool_search"}
        assert model.tool_deferral_mode == "with_tool_search"
    assert model.profile["openai_responses_requires_streaming"] is True
    assert model.profile["openai_responses_requires_store_false"] is True
    assert model.profile["openai_supports_input_token_counting"] is False


@pytest.mark.anyio
async def test_local_discovery_roundtrip(wire, tmp_path):
    h, calls = make_harness(tmp_path, 16, model_name="gpt-5.3-codex")
    wire.replies.extend(
        [
            sse(tool=("search_tools", {"queries": ["trusted_echo_0"]})),
            sse(tool=("trusted_echo_0", {"value": "sentinel"})),
            sse(),
        ]
    )
    try:
        await h.connect()
        assert (await h.run_turn("Discover echo and call it with sentinel")).result == "done"
        first = wire.requests[0]["json"]["tools"]
        assert any(t["name"] == "search_tools" for t in first)
        assert all(t["type"] != "tool_search" and not t.get("defer_loading") for t in first)
        assert not any(t["name"].startswith("trusted_") for t in first)
        assert any(t["name"] == "trusted_echo_0" for t in wire.requests[1]["json"]["tools"])
        assert calls == ["sentinel"]
        assert len(wire.requests) == 3
        assert any(
            p.get("type") == "function_call_output" and "mcp-result:sentinel" in p["output"]
            for p in wire.requests[2]["json"]["input"]
        )
    finally:
        await h.aclose()
