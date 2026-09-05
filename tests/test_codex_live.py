"""Live smoke against a real `codex app-server` — OFF unless MARIM_LIVE_CODEX=1.

Uses the developer's own Codex login (CODEX_HOME defaults back to ~/.codex
here; the conftest isolation points it at nothing for every other test).
Five probes: a main-loop reply with effort + thread reuse, an ask-mode file
change that must be declined, a codex-cli sub-agent, and (no turn at all)
the app-server's own report that none of the user's MCP servers loaded.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.settings import ModelSettings

from marim_harness.codex.env import resolve_codex_binary
from marim_harness.codex.server import (
    close_shared_server,
    configured_mcp_servers,
    shared_server,
)
from marim_harness.config.codex_cli_model import CodexCliModel
from marim_harness.runtime.permissions import Mode
from marim_harness.session import SessionStore, TranscriptStore
from tests.conftest import _make_deps, _make_harness

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(os.environ.get("MARIM_LIVE_CODEX") != "1", reason="set MARIM_LIVE_CODEX=1"),
]

PARAMS = ModelRequestParameters(function_tools=[], allow_text_output=True, output_tools=[])


@pytest.fixture(autouse=True)
async def _real_codex_home(monkeypatch):
    home = os.environ.get("MARIM_LIVE_CODEX_HOME") or str(Path.home() / ".codex")
    monkeypatch.setenv("CODEX_HOME", home)
    monkeypatch.delenv("MARIM_CODEX_CLI_BIN", raising=False)
    yield
    await close_shared_server()


def _msgs(text: str) -> list:
    return [
        ModelRequest(
            parts=[
                SystemPromptPart(content="Answer in one short line. No tools unless asked."),
                UserPromptPart(content=text),
            ]
        )
    ]


async def test_main_loop_turn_and_thread_reuse(tmp_path: Path):
    m = CodexCliModel(None)  # the CLI's default model
    m.cwd = str(tmp_path)
    m.mode_getter = lambda: Mode.plan.value
    resp = await m.request(_msgs("Reply with exactly the word: pong"), None, PARAMS)
    assert "pong" in resp.parts[0].content.lower()
    assert resp.usage.output_tokens > 0
    first_thread = m.thread
    resp2 = await m.request(
        _msgs("What word did you just reply with? One word."),
        ModelSettings(thinking="low"),  # type: ignore[typeddict-item]
        PARAMS,
    )
    assert "pong" in resp2.parts[0].content.lower()  # same thread: it remembers
    assert m.thread is first_thread


async def test_ask_mode_file_change_is_brokered_and_declined(tmp_path: Path):
    m = CodexCliModel(None)
    m.cwd = str(tmp_path)
    m.mode_getter = lambda: Mode.ask.value
    asked: list[str] = []

    async def decline(call):
        asked.append(call.tool_name)
        return False

    m.request_approval = decline
    await m.request(
        _msgs(
            "Create a file named probe.txt in the current directory containing 'hi'. "
            "If you are not allowed, say so."
        ),
        None,
        PARAMS,
    )
    assert asked, "Codex never asked before writing — check approvalPolicy=untrusted"
    assert not (tmp_path / "probe.txt").exists()


async def test_user_mcp_servers_do_not_load():
    """Isolation (spec §Isolation): the user's configured MCP servers are
    disabled by launch override and the built-in apps connector is off, so
    the live app-server reports no server connected — after a settle window,
    since MCP connections are established asynchronously after startup.
    Deterministic — no model turn — and meaningful only when the developer's
    own config declares at least one server, which is asserted so a bare
    CODEX_HOME cannot pass this vacuously."""
    binary = resolve_codex_binary()
    assert binary is not None
    configured = await configured_mcp_servers(binary, None)
    assert configured, "the live config declares no MCP servers; nothing to isolate from"
    server = shared_server()
    await server.start()
    await asyncio.sleep(3)
    assert await server.connected_mcp_servers() == []


async def test_codex_cli_subagent_reports(tmp_path: Path):
    d = tmp_path / ".marim" / "agents"
    d.mkdir(parents=True)
    (d / "codex-worker.md").write_text(
        "---\ndescription: w\nbackend: codex-cli\ntools: read_file\n---\n"
        "Answer in one short line.\n"
    )

    async def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="unused")])

    # A real session store: the sidecar meta (and the codex thread id in it)
    # is persisted only when there is a session to persist into —
    # SpawnTranscripts.save is a no-op without one, so a storeless harness
    # would read None here no matter what the spawn did.
    store = SessionStore(
        path=tmp_path / "sessions" / "live.json",
        workspace_root=tmp_path,
        session_id="live-session",
        name="live",
    )
    runner = _make_harness(FunctionModel(fn), _make_deps(tmp_path), store=store).subagents
    out = await runner.run("codex-worker", "Reply with exactly the word: pong", stream_id="live1")
    assert "pong" in out.lower()
    meta = TranscriptStore(store.path, store.session_id).read_meta("live1")
    assert meta is not None and meta["codex_thread_id"]
    assert meta["status"] == "finished" and meta["backend"] == "codex-cli"
