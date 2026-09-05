"""backend: codex-cli sub-agents against the scripted fake app-server."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.codex.env import CodexUnavailable
from marim_harness.codex.server import CodexServer, close_shared_server
from marim_harness.config.external_cli import CliModelError
from marim_harness.runtime.permissions import Mode
from marim_harness.session import SessionStore, TranscriptStore
from marim_harness.subagents.backend import CONTINUATION_PROMPT
from marim_harness.subagents.codex_spawn import CodexSpawnRequest
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


def _flip_agent_to_native(tmp_path: Path) -> None:
    """Same agent name as `_write_codex_agent`, but no longer codex-cli-backed —
    for the "backend changed out from under the sidecar" resume refusal."""
    d = tmp_path / ".marim" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "codex-worker.md").write_text(
        "---\ndescription: Codex worker\ntools: read_file\n---\nWork.\n", encoding="utf-8"
    )


def _codex_meta(sid: str, **overrides) -> dict:
    meta = {
        "stream_id": sid,
        "type": "codex-worker",
        "task": "original task",
        "model": None,
        "mcp": None,
        "depth": 1,
        "max_output_chars": None,
        "isolation": None,
        "status": "running",
        "backend": "codex-cli",
        "codex_thread_id": "thread-7",
    }
    meta.update(overrides)
    return meta


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
        CodexSpawnRequest(
            defn=defn,
            task="continue",
            work_root=None,
            model=None,
            stream_id="s1",
            resume_thread_id="thread-7",
        )
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


async def test_run_codex_folds_multi_chunk_text_thinking_and_notice(tmp_path: Path, monkeypatch):
    """Two text chunks (same item) continue the SAME ModelResponse rather than
    opening a new one; two reasoning chunks (same item) grow one ThinkingPart;
    a `contextCompaction` item becomes a silent Notice, not a transcript entry."""
    turn = [
        {"notify": "item/started", "params": {"item": {"id": "cc1", "type": "contextCompaction"}}},
        {"notify": "item/reasoning/textDelta", "params": {"itemId": "r1", "delta": "Thinking a"}},
        {"notify": "item/reasoning/textDelta", "params": {"itemId": "r1", "delta": " more"}},
        {"notify": "item/agentMessage/delta", "params": {"itemId": "m1", "delta": "Hello "}},
        {"notify": "item/agentMessage/delta", "params": {"itemId": "m1", "delta": "world"}},
        {
            "notify": "thread/tokenUsage/updated",
            "params": {"tokenUsage": {"total": {"inputTokens": 1, "outputTokens": 1}}},
        },
    ]
    _login(monkeypatch, tmp_path, {"turns": [turn]})
    _write_codex_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    defn = runner._resolve_agent("codex-worker")
    assert defn is not None
    run = await runner._codex.run_codex(
        CodexSpawnRequest(defn=defn, task="go", work_root=None, model=None, stream_id="s1")
    )
    assert run.output == "Hello world"
    assert len(run.transcript) == 1  # thinking + text landed on the SAME response
    resp = run.transcript[0]
    assert isinstance(resp, ModelResponse)
    assert [type(p).__name__ for p in resp.parts] == ["ThinkingPart", "TextPart"]
    assert resp.parts[0].content == "Thinking a more"
    assert resp.parts[1].content == "Hello world"


async def test_run_codex_output_is_empty_without_a_final_text(tmp_path: Path, monkeypatch):
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
                    "aggregatedOutput": "ok",
                }
            },
        },
        {
            "notify": "thread/tokenUsage/updated",
            "params": {"tokenUsage": {"total": {"inputTokens": 1, "outputTokens": 0}}},
        },
    ]
    _login(monkeypatch, tmp_path, {"turns": [turn]})
    _write_codex_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    defn = runner._resolve_agent("codex-worker")
    assert defn is not None
    run = await runner._codex.run_codex(
        CodexSpawnRequest(defn=defn, task="go", work_root=None, model=None, stream_id="s1")
    )
    assert run.output == ""


async def test_execute_without_a_stream_id_skips_the_sidecar_save(tmp_path: Path, monkeypatch):
    """`run_background` (and any other caller) may omit `stream_id` (untracked,
    headless run) — `execute` must skip the sidecar-meta save entirely rather
    than persist under an empty key."""
    _login(monkeypatch, tmp_path, {"turns": [_report_turn()]})
    _write_codex_agent(tmp_path)
    store = _session_store(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path), store=store).subagents
    defn = runner._resolve_agent("codex-worker")
    assert defn is not None
    out = await runner._codex.execute(
        defn, "go", None, None, None, None, None, "", background=False
    )
    assert "Done: report body" in out
    assert TranscriptStore(store.path, store.session_id).read_meta("") is None


async def test_server_start_failure_is_reported_as_a_user_visible_error(
    tmp_path: Path, monkeypatch
):
    """`CodexUnavailable` from `server.start()` (app-server crashed on launch,
    handshake failed, ...) must surface as a CliModelError the lifecycle wrapper
    turns into a contained failure report — not propagate raw."""
    import marim_harness.subagents.codex_spawn as spawn_mod

    class _FailingServer(CodexServer):
        async def start(self) -> None:
            raise CodexUnavailable("boom")

    monkeypatch.setattr(spawn_mod, "codex_available", lambda: True)
    _write_codex_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    runner._codex._server = _FailingServer()
    out = await runner.run("codex-worker", "go", stream_id="s1")
    assert "failed" in out.lower() and "boom" in out


async def test_resuming_a_forgotten_thread_raises_cli_model_error(tmp_path: Path, monkeypatch):
    # No "resumable" list in the scenario -> the fake app-server treats ANY
    # thread id as gone, exactly like a real Codex restart losing the thread.
    _login(monkeypatch, tmp_path, {"turns": [_report_turn()]})
    _write_codex_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    defn = runner._resolve_agent("codex-worker")
    assert defn is not None
    with pytest.raises(CliModelError, match="thread-ghost is gone"):
        await runner._codex.run_codex(
            CodexSpawnRequest(
                defn=defn,
                task="continue",
                work_root=None,
                model=None,
                stream_id="s1",
                resume_thread_id="thread-ghost",
            )
        )


async def test_model_list_failure_degrades_effort_but_does_not_fail_the_turn(
    tmp_path: Path, monkeypatch
):
    """`server.list_models()` is best-effort (spec: catalog degrades `xhigh` to
    `high`) — a failure there must not abort the turn."""

    class _NoModelListServer(CodexServer):
        async def list_models(self):
            raise RuntimeError("model list boom")

    _login(monkeypatch, tmp_path, {"turns": [_report_turn()]})  # sets up codex_available()
    _write_codex_agent(tmp_path, extra="thinking: xhigh\n")
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    runner._codex._server = _NoModelListServer(
        binary=fake_codex_bin(tmp_path, {"turns": [_report_turn()]})
    )
    out = await runner.run("codex-worker", "go", stream_id="s1")
    assert "Done: report body" in out
    turn = next(r for r in read_request_log(tmp_path) if r["method"] == "turn/start")
    assert turn["params"]["effort"] == "high"  # unknown catalog -> conservative fallback


async def test_usage_callback_is_invoked_when_bound(tmp_path: Path, monkeypatch):
    _login(monkeypatch, tmp_path, {"turns": [_report_turn()]})
    _write_codex_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    seen: list[tuple[str, int]] = []

    async def on_usage(stream_id, usage):
        seen.append((stream_id, usage.output_tokens))

    runner.deps.ui.on_subagent_usage = on_usage
    await runner.run("codex-worker", "go", stream_id="s1")
    assert seen == [("s1", 5)]


async def test_resume_refuses_unknown_agent_type(tmp_path: Path):
    store = _session_store(tmp_path)
    ts = TranscriptStore(store.path, store.session_id)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path), store=store).subagents
    prior = [ModelRequest(parts=[UserPromptPart(content="original task")])]
    ts.write("sg-x", prior, 2000, meta=_codex_meta("sg-x", type="no-such-agent"))
    job_id, msg = await runner.resume_spawn("sg-x")
    assert job_id is None and "Unknown agent type" in msg


async def test_resume_refuses_when_agent_backend_changed(tmp_path: Path):
    store = _session_store(tmp_path)
    ts = TranscriptStore(store.path, store.session_id)
    _flip_agent_to_native(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path), store=store).subagents
    prior = [ModelRequest(parts=[UserPromptPart(content="original task")])]
    ts.write("sg-flip", prior, 2000, meta=_codex_meta("sg-flip"))
    job_id, msg = await runner.resume_spawn("sg-flip")
    assert job_id is None and "no longer a codex-cli agent" in msg


async def test_resume_refuses_when_isolation_branch_is_gone(tmp_path: Path):
    store = _session_store(tmp_path)
    ts = TranscriptStore(store.path, store.session_id)
    _write_codex_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path), store=store).subagents
    prior = [ModelRequest(parts=[UserPromptPart(content="original task")])]
    ts.write("sg-iso", prior, 2000, meta=_codex_meta("sg-iso", isolation="subagent/gone"))
    job_id, msg = await runner.resume_spawn("sg-iso")
    assert job_id is None and "subagent/gone" in msg


async def test_resume_without_isolation_reaches_the_job_queue(tmp_path: Path, monkeypatch):
    """A non-isolated spawn (no `isolation` branch recorded — the common case)
    must skip the worktree-reopen step entirely and still queue the resume job,
    rather than that step being implicitly required."""
    _login(
        monkeypatch, tmp_path, {"resumable": ["thread-7"], "turns": [_report_turn("resumed body")]}
    )
    _write_codex_agent(tmp_path)
    store = _session_store(tmp_path)
    ts = TranscriptStore(store.path, store.session_id)
    harness = _make_harness(_dummy_model(), _make_deps(tmp_path), store=store)
    prior = [ModelRequest(parts=[UserPromptPart(content="original task")])]
    ts.write("sg-plain", prior, 2000, meta=_codex_meta("sg-plain"))  # isolation=None

    job_id, message = await harness.subagents.resume_spawn("sg-plain")
    assert job_id is not None, message
    report = await harness.deps.jobs.wait(job_id)
    assert "resumed body" in report


async def test_resume_reopens_the_persisted_thread_and_continues(tmp_path: Path, monkeypatch):
    """The full resume happy path, including a REAL isolation branch that
    successfully reopens (`SpawnWorktree.reopen` returns a worktree, not an
    error) — the counterpart to `test_resume_refuses_when_isolation_branch_is_gone`,
    which only exercises the failure arm."""
    import subprocess

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "Test")
    (tmp_path / "README.md").write_text("hi\n")
    git("add", ".")
    git("commit", "-qm", "init")
    git("branch", "subagent/sg-resume")

    _login(
        monkeypatch, tmp_path, {"resumable": ["thread-7"], "turns": [_report_turn("resumed body")]}
    )
    _write_codex_agent(tmp_path)
    store = _session_store(tmp_path)
    ts = TranscriptStore(store.path, store.session_id)
    harness = _make_harness(_dummy_model(), _make_deps(tmp_path), store=store)
    prior = [ModelRequest(parts=[UserPromptPart(content="original task")])]
    ts.write(
        "sg-resume", prior, 2000, meta=_codex_meta("sg-resume", isolation="subagent/sg-resume")
    )

    job_id, message = await harness.subagents.resume_spawn("sg-resume")
    assert job_id is not None, message
    report = await harness.deps.jobs.wait(job_id)
    assert "resumed body" in report

    log = read_request_log(tmp_path)
    assert any(r["method"] == "thread/resume" for r in log)
    resumed_turn = next(r for r in log if r["method"] == "turn/start")
    assert resumed_turn["params"]["input"][0]["text"] == CONTINUATION_PROMPT

    meta_after = ts.read_meta("sg-resume")
    assert meta_after["status"] == "finished"
    assert meta_after["task"] == "original task"  # the continuation prompt never leaks into meta
    assert meta_after["codex_thread_id"] == "thread-7"
    # the pre-interrupt segment survives alongside the continuation's own messages
    msgs = ts.read("sg-resume")
    assert any(
        isinstance(m, ModelRequest)
        and any(isinstance(p, UserPromptPart) and p.content == "original task" for p in m.parts)
        for m in msgs
    )
