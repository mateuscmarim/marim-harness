"""backend: codex-cli sub-agents against the scripted fake app-server."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.codex.server import close_shared_server
from marim_harness.runtime.permissions import Mode
from marim_harness.session import SessionStore, TranscriptStore
from tests.conftest import _make_deps, _make_harness
from tests.fakes import fake_codex_bin, read_request_log

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
async def _shared_server_cleanup():
    yield
    await close_shared_server()


def _login(monkeypatch, tmp_path: Path, scenario: dict) -> None:
    """Point codex-cli at the fake binary AND a logged-in CODEX_HOME (the
    conftest isolation points CODEX_HOME at nothing, so `codex_available()`
    would otherwise be False)."""
    monkeypatch.setenv("MARIM_CODEX_CLI_BIN", fake_codex_bin(tmp_path, scenario))
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    (home / "auth.json").write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(home))


def _report_turn(text: str = "Done: report body") -> list[dict]:
    return [
        {"notify": "item/agentMessage/delta", "params": {"itemId": "m1", "delta": text}},
        {
            "notify": "thread/tokenUsage/updated",
            "params": {"tokenUsage": {"total": {"inputTokens": 9, "outputTokens": 5}}},
        },
    ]


def _write_codex_agent(tmp_path: Path, tools: str = "read_file", extra: str = "") -> None:
    d = tmp_path / ".marim" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "codex-worker.md").write_text(
        f"---\ndescription: Codex worker\nbackend: codex-cli\ntools: {tools}\n{extra}---\n"
        "You are a Codex worker.\n",
        encoding="utf-8",
    )


def _session_store(tmp_path: Path) -> SessionStore:
    """A real session store: read_meta needs one to persist into — _make_deps
    alone leaves the runner storeless (SpawnTranscripts.has_store is False),
    mirroring the claude-cli sidecar tests' setup."""
    return SessionStore(
        path=tmp_path / "sessions" / "test.json",
        workspace_root=tmp_path,
        session_id="test-session",
        name="test",
    )


def _dummy_model() -> FunctionModel:
    async def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="unused")])

    return FunctionModel(fn)


async def test_codex_backend_spawn_returns_report(tmp_path: Path, monkeypatch):
    _login(monkeypatch, tmp_path, {"turns": [_report_turn()]})
    _write_codex_agent(tmp_path)
    store = _session_store(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path), store=store).subagents
    out = await runner.run("codex-worker", "do the thing", stream_id="s1")
    assert "Done: report body" in out
    assert runner.session.usage.output_tokens == 5
    log = read_request_log(tmp_path)
    start = next(r for r in log if r["method"] == "thread/start")
    assert start["params"]["developerInstructions"].startswith("You are a Codex worker.")
    assert start["params"]["sandbox"] == "read-only"  # read_file only -> no writes
    turn = next(r for r in log if r["method"] == "turn/start")
    assert turn["params"]["input"][0]["text"] == "do the thing"
    # The sidecar records the backend and the thread for a later resume.
    meta = TranscriptStore(store.path, store.session_id).read_meta("s1")
    assert meta["backend"] == "codex-cli" and meta["codex_thread_id"] == "thread-1"
    assert meta["status"] == "finished"


async def test_mutating_tools_get_workspace_write_in_auto_mode(tmp_path: Path, monkeypatch):
    _login(monkeypatch, tmp_path, {"turns": [_report_turn()]})
    _write_codex_agent(tmp_path, tools="read_file, edit_file, bash")
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path, Mode.auto)).subagents
    await runner.run("codex-worker", "edit", stream_id="s1")
    start = next(r for r in read_request_log(tmp_path) if r["method"] == "thread/start")
    assert start["params"]["sandbox"] == "workspace-write"
    assert start["params"]["approvalPolicy"] == "on-request"


async def test_plan_mode_forces_read_only_even_with_mutating_tools(tmp_path: Path, monkeypatch):
    _login(monkeypatch, tmp_path, {"turns": [_report_turn()]})
    _write_codex_agent(tmp_path, tools="read_file, edit_file, bash")
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path, Mode.plan)).subagents
    await runner.run("codex-worker", "edit", stream_id="s1")
    start = next(r for r in read_request_log(tmp_path) if r["method"] == "thread/start")
    assert start["params"]["sandbox"] == "read-only"
    assert start["params"]["approvalPolicy"] == "never"


async def test_output_schema_and_effort_are_forwarded(tmp_path: Path, monkeypatch):
    _login(monkeypatch, tmp_path, {"turns": [_report_turn('{"ok": true}')]})
    _write_codex_agent(tmp_path, extra="thinking: high\n")
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    schema = {"type": "array", "items": {"type": "string"}}  # non-object root: still native
    out = await runner.run("codex-worker", "go", stream_id="s1", output_schema=schema)
    assert out.strip().endswith('{"ok": true}')
    turn = next(r for r in read_request_log(tmp_path) if r["method"] == "turn/start")
    assert turn["params"]["outputSchema"] == schema
    assert turn["params"]["effort"] == "high"


async def test_model_precedence_and_model_env(tmp_path: Path, monkeypatch):
    _login(monkeypatch, tmp_path, {"turns": [_report_turn()]})
    monkeypatch.setenv("MARIM_CODEX_CLI_MODEL", "gpt-5.4-mini")
    _write_codex_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    seen: list[tuple[str, str]] = []

    async def on_model(stream_id, model):
        seen.append((stream_id, model))

    runner.deps.ui.on_subagent_model = on_model
    await runner.run("codex-worker", "go", stream_id="s1")
    start = next(r for r in read_request_log(tmp_path) if r["method"] == "thread/start")
    assert start["params"]["model"] == "gpt-5.4-mini"
    assert seen == [("s1", "codex-cli:gpt-5.4-mini")]


async def test_tool_activity_streams_as_subagent_events(tmp_path: Path, monkeypatch):
    turn = [
        {
            "notify": "item/started",
            "params": {
                "item": {"id": "c1", "type": "commandExecution", "command": "ls", "cwd": "/w"}
            },
        },
        {
            "notify": "item/completed",
            "params": {
                "item": {
                    "id": "c1",
                    "type": "commandExecution",
                    "command": "ls",
                    "cwd": "/w",
                    "status": "completed",
                    "exitCode": 0,
                    "aggregatedOutput": "a.py",
                }
            },
        },
        *_report_turn("saw a.py"),
    ]
    _login(monkeypatch, tmp_path, {"turns": [turn]})
    _write_codex_agent(tmp_path)
    store = _session_store(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path), store=store).subagents
    events: list = []

    async def on_event(stream_id, event, usage):
        events.append(type(event).__name__)

    runner.deps.ui.on_subagent_event = on_event
    await runner.run("codex-worker", "go", stream_id="s1")
    assert "FunctionToolCallEvent" in events and "FunctionToolResultEvent" in events
    assert "PartDeltaEvent" in events
    meta = TranscriptStore(store.path, store.session_id).read_meta("s1")
    assert meta["tool_count"] == 1


async def test_failed_turn_is_contained(tmp_path: Path, monkeypatch):
    _login(monkeypatch, tmp_path, {"turns": [[{"fail": "rate limited"}]]})
    _write_codex_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    out = await runner.run("codex-worker", "go", stream_id="s1")
    assert "failed" in out.lower() and "rate limited" in out


async def test_missing_binary_is_contained(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MARIM_CODEX_CLI_BIN", "no-such-codex-binary")
    _write_codex_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    out = await runner.run("codex-worker", "go", stream_id="s1")
    assert "failed" in out.lower() and "codex" in out.lower()


async def test_mcp_grants_are_noted_not_forwarded(tmp_path: Path, monkeypatch):
    _login(monkeypatch, tmp_path, {"turns": [_report_turn()]})
    _write_codex_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    out = await runner.run("codex-worker", "go", stream_id="s1", mcp_names=["gitea"])
    assert "not forwarded to codex-cli" in out


async def test_run_codex_resumes_a_persisted_thread(tmp_path: Path, monkeypatch):
    _login(monkeypatch, tmp_path, {"resumable": ["thread-7"], "turns": [_report_turn()]})
    _write_codex_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    defn = runner._resolve_agent("codex-worker")
    assert defn is not None
    run = await runner._codex.run_codex(
        defn, "continue", None, None, "s1", resume_thread_id="thread-7"
    )
    assert run.thread_id == "thread-7" and "Done: report body" in run.output
    log = read_request_log(tmp_path)
    assert any(r["method"] == "thread/resume" for r in log)
    assert not any(r["method"] == "thread/start" for r in log)


async def test_resume_refuses_without_a_thread_id(tmp_path: Path, monkeypatch):
    _write_codex_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    job_id, msg = await runner._codex.resume(
        "s1", {"type": "codex-worker", "task": "t", "backend": "codex-cli", "status": "interrupted"}
    )
    assert job_id is None and "thread id was never recorded" in msg
