"""Live smoke against a real `codex app-server` — OFF unless MARIM_LIVE_CODEX=1.

Uses the developer's own Codex login (CODEX_HOME defaults back to ~/.codex
here; the conftest isolation points it at nothing for every other test).
Four probes, each one short turn: a main-loop reply, effort + thread reuse,
an ask-mode file change that must be declined, and a codex-cli sub-agent.
"""

from __future__ import annotations

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

from marim_harness.codex.server import close_shared_server
from marim_harness.config.codex_cli_model import CodexCliModel
from marim_harness.runtime.permissions import Mode
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


async def test_codex_cli_subagent_reports(tmp_path: Path):
    d = tmp_path / ".marim" / "agents"
    d.mkdir(parents=True)
    (d / "codex-worker.md").write_text(
        "---\ndescription: w\nbackend: codex-cli\ntools: read_file\n---\n"
        "Answer in one short line.\n"
    )

    async def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="unused")])

    runner = _make_harness(FunctionModel(fn), _make_deps(tmp_path)).subagents
    out = await runner.run("codex-worker", "Reply with exactly the word: pong", stream_id="live1")
    assert "pong" in out.lower()
    assert runner._transcripts.read_meta("live1")["codex_thread_id"]
