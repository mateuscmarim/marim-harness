"""Sidecar checkpointing and interrupted-spawn resume.

A spawn used to write its transcript sidecar only at completion, so a process
death mid-run lost the transcript entirely. The runner now flushes a v2 envelope
(meta + messages) before every model request via a ProcessHistory capability and
finalizes it with a terminal status, so a crashed spawn leaves a resumable trail.
"""

from pathlib import Path

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel

from marim_harness.session import SessionStore, TranscriptStore
from marim_harness.subagents.backend import CONTINUATION_PROMPT
from tests.conftest import _make_deps, _make_harness
from tests.fakes import fake_claude_bin, read_claude_argv, read_claude_log


def _session_store(tmp_path: Path) -> SessionStore:
    return SessionStore(
        path=tmp_path / "sessions" / "test.json",
        workspace_root=tmp_path,
        session_id="test-session",
        name="test",
    )


def _tool_then_text_model() -> FunctionModel:
    """First request: call list_files. Second: final report."""

    def fn(messages, info):
        if len(messages) == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="list_files", args={"path": "."}, tool_call_id="t1")]
            )
        return ModelResponse(parts=[TextPart(content="report")])

    return FunctionModel(fn)


def _spy_saves(runner):
    """Record every transcript-save meta status, preserving behavior."""
    seen: list[str | None] = []
    orig = runner._transcripts.save

    def spy(stream_id, messages, meta=None, **kw):
        seen.append(None if meta is None else meta.get("status"))
        orig(stream_id, messages, meta=meta, **kw)

    runner._transcripts.save = spy
    return seen


@pytest.mark.anyio
async def test_spawn_checkpoints_running_then_finalizes(tmp_path):
    store = _session_store(tmp_path)
    harness = _make_harness(_tool_then_text_model(), _make_deps(tmp_path), store=store)
    seen = _spy_saves(harness.subagents)
    out = await harness.subagents.run("general", "look around", stream_id="sg-ck")
    assert out == "report"
    # At least one mid-run checkpoint (per model request) plus the final write.
    assert "running" in seen and seen[-1] == "finished"
    meta = TranscriptStore(store.path, store.session_id).read_meta("sg-ck")
    assert meta["status"] == "finished"
    assert meta["type"] == "general" and meta["task"] == "look around"
    assert meta["depth"] == 1 and meta["usage"]["output"] >= 0


@pytest.mark.anyio
async def test_failed_spawn_leaves_sidecar_marked_running(tmp_path):
    """A spawn that dies mid-run gets no final write — its sidecar stays
    status=running, which is exactly what the resume scan treats as interrupted."""

    def fn(messages, info):
        if len(messages) == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="list_files", args={"path": "."}, tool_call_id="t1")]
            )
        raise RuntimeError("boom")  # permanent → no retry, spawn fails

    store = _session_store(tmp_path)
    harness = _make_harness(FunctionModel(fn), _make_deps(tmp_path), store=store)
    out = await harness.subagents.run("general", "task", stream_id="sg-dead")
    assert "failed" in out  # foreground contains the crash as an error string
    meta = TranscriptStore(store.path, store.session_id).read_meta("sg-dead")
    assert meta is not None and meta["status"] == "running"


def _interrupted_meta(sid: str) -> dict:
    return {
        "stream_id": sid,
        "type": "general",
        "task": "original task",
        "model": None,
        "mcp": None,
        "depth": 1,
        "max_output_chars": None,
        "isolation": None,
        "status": "running",
    }


def _dangling_history() -> list:
    """A transcript that died mid-tool-call — the resume must repair it."""
    return [
        ModelRequest(parts=[UserPromptPart(content="original task")]),
        ModelResponse(
            parts=[ToolCallPart(tool_name="read_file", args={"path": "x"}, tool_call_id="dangling")]
        ),
    ]


def _resume_model() -> FunctionModel:
    """Asserts the incoming history was repaired (the dangling call has a
    synthesized return), then finishes."""

    def fn(messages, info):
        returns = [
            p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)
        ]
        assert any(p.tool_call_id == "dangling" for p in returns), (
            "resume must synthesize a return for the dangling tool call"
        )
        return ModelResponse(parts=[TextPart(content="resumed-ok")])

    return FunctionModel(fn)


@pytest.mark.anyio
async def test_resume_spawn_repairs_history_and_finishes(tmp_path):
    store = _session_store(tmp_path)
    harness = _make_harness(_resume_model(), _make_deps(tmp_path), store=store)
    ts = TranscriptStore(store.path, store.session_id)
    ts.write("sg-int", _dangling_history(), 2000, meta=_interrupted_meta("sg-int"))
    job_id, message = await harness.subagents.resume_spawn("sg-int")
    assert job_id is not None, message
    report = await harness.deps.jobs.wait(job_id)
    assert report == "resumed-ok"
    job = harness.deps.jobs.get(job_id)
    assert job is not None and job.stream_id == "sg-int"
    assert ts.read_meta("sg-int")["status"] == "finished"


@pytest.mark.anyio
async def test_resume_refuses_v1_finished_and_double_resume(tmp_path):
    store = _session_store(tmp_path)
    harness = _make_harness(_resume_model(), _make_deps(tmp_path), store=store)
    ts = TranscriptStore(store.path, store.session_id)
    # v1 sidecar (no meta) → refuse
    ts.write("sg-v1", _dangling_history(), 2000)
    job_id, msg = await harness.subagents.resume_spawn("sg-v1")
    assert job_id is None and "resumable" in msg.lower()
    # finished spawn → refuse
    ts.write(
        "sg-done",
        _dangling_history(),
        2000,
        meta={**_interrupted_meta("sg-done"), "status": "finished"},
    )
    job_id, msg = await harness.subagents.resume_spawn("sg-done")
    assert job_id is None
    # already resuming → refuse the second call
    ts.write("sg-int", _dangling_history(), 2000, meta=_interrupted_meta("sg-int"))
    first, _ = await harness.subagents.resume_spawn("sg-int")
    assert first is not None
    second, msg = await harness.subagents.resume_spawn("sg-int")
    assert second is None and first in msg
    await harness.deps.jobs.wait(first)


@pytest.mark.anyio
async def test_resume_refuses_with_no_session_store(tmp_path):
    # Characterization test (pinning current behavior before extracting
    # _resume_preconditions): a harness with no session store at all (the
    # `store=None` default) can't have a sidecar to resume from — this must
    # be refused before any sidecar lookup is attempted.
    harness = _make_harness(_resume_model(), _make_deps(tmp_path))
    job_id, msg = await harness.subagents.resume_spawn("sg-nostore")
    assert job_id is None and "no session store" in msg.lower()


@pytest.mark.anyio
async def test_resume_double_press_registers_exactly_one_job(tmp_path):
    """Two rapid `r` presses race: both clear the jobs-scan guard (neither has
    registered yet) and both await _prepare_spawn, double-spawning. The synchronous
    in-flight guard (self._resuming, added before the first await) must let exactly
    one through and refuse the other."""
    import asyncio

    store = _session_store(tmp_path)
    harness = _make_harness(_resume_model(), _make_deps(tmp_path), store=store)
    ts = TranscriptStore(store.path, store.session_id)
    ts.write("sg-race", _dangling_history(), 2000, meta=_interrupted_meta("sg-race"))

    gate = asyncio.Event()
    orig_prepare = harness.subagents._prepare_spawn

    async def gated_prepare(*a, **k):
        await gate.wait()  # park the first caller mid-resume, before it registers
        return await orig_prepare(*a, **k)

    harness.subagents._prepare_spawn = gated_prepare

    t1 = asyncio.create_task(harness.subagents.resume_spawn("sg-race"))
    t2 = asyncio.create_task(harness.subagents.resume_spawn("sg-race"))
    await asyncio.sleep(0.05)  # one parks at the gate; the other must refuse now
    gate.set()
    (id1, _msg1), (id2, _msg2) = await t1, await t2

    registered = [j for j in harness.deps.jobs.list() if j.stream_id == "sg-race"]
    assert len(registered) == 1, "the race must not double-spawn"
    ids = [id1, id2]
    assert ids.count(None) == 1 and len([i for i in ids if i]) == 1
    winner = id1 or id2
    assert winner is not None
    await harness.deps.jobs.wait(winner)


@pytest.mark.anyio
async def test_checkpoint_clips_oversized_reasoning(tmp_path):
    """A mid-run checkpoint (before every model request) must bound its payload —
    oversized ThinkingPart/TextPart contents are clipped, not just tool results, so
    a long reasoning stream doesn't make each checkpoint re-serialize unboundedly.
    The spawn fails after the checkpoint so no final write overwrites it on disk."""
    from pydantic_ai.messages import ThinkingPart

    def fn(messages, info):
        if len(messages) == 1:
            return ModelResponse(
                parts=[
                    ThinkingPart(
                        content="T" * 6000, signature="sig-123", provider_name="anthropic"
                    ),
                    ToolCallPart(tool_name="list_files", args={"path": "."}, tool_call_id="t1"),
                ]
            )
        raise RuntimeError("stop")  # permanent → no final write; checkpoint rests

    store = _session_store(tmp_path)
    harness = _make_harness(FunctionModel(fn), _make_deps(tmp_path), store=store)
    out = await harness.subagents.run("general", "task", stream_id="sg-think")
    assert "failed" in out  # foreground contains the crash

    msgs = TranscriptStore(store.path, store.session_id).read("sg-think")
    thoughts = [p for m in msgs for p in getattr(m, "parts", []) if isinstance(p, ThinkingPart)]
    assert thoughts, "the checkpoint must have captured the thinking part"
    assert all(len(str(p.content)) < 6000 for p in thoughts)
    assert any("truncated, 6000 chars" in str(p.content) for p in thoughts)
    # A clipped thinking block's signature no longer matches its (now-shorter)
    # content — Anthropic validates the signature against the FULL content, so a
    # stale signature on truncated content 400s on resume. Must be nulled.
    assert all(p.signature is None for p in thoughts)


@pytest.mark.anyio
async def test_resume_stop_hook_gets_original_task_not_continuation_prompt(tmp_path):
    """The subagent_stop hook must see the SAME task the start hook got — the
    original task — not the internal CONTINUATION_PROMPT the resumed run is fed."""
    store = _session_store(tmp_path)
    harness = _make_harness(_resume_model(), _make_deps(tmp_path), store=store)
    ts = TranscriptStore(store.path, store.session_id)
    ts.write("sg-hook", _dangling_history(), 2000, meta=_interrupted_meta("sg-hook"))

    seen: dict[str, str] = {}
    orig_stop = harness.subagents.hooks.subagent_stop

    async def spy_stop(subagent_type, task, result):
        seen["task"] = task
        return await orig_stop(subagent_type, task, result)

    harness.subagents.hooks.subagent_stop = spy_stop

    job_id, message = await harness.subagents.resume_spawn("sg-hook")
    assert job_id is not None, message
    await harness.deps.jobs.wait(job_id)
    assert seen["task"] == "original task"
    assert seen["task"] != CONTINUATION_PROMPT


@pytest.mark.anyio
async def test_resume_refuses_when_isolation_branch_is_gone(tmp_path):
    store = _session_store(tmp_path)
    harness = _make_harness(_resume_model(), _make_deps(tmp_path), store=store)
    ts = TranscriptStore(store.path, store.session_id)
    meta = {**_interrupted_meta("sg-iso"), "isolation": "subagent/gone"}
    ts.write("sg-iso", _dangling_history(), 2000, meta=meta)
    job_id, msg = await harness.subagents.resume_spawn("sg-iso")
    assert job_id is None and "subagent/gone" in msg


@pytest.mark.anyio
async def test_resume_build_failure_keeps_isolation_branch(tmp_path, monkeypatch):
    """A resumed isolated spawn whose build fails must NOT destroy its branch — the
    branch holds the interrupted run's committed work. _prepare_spawn owns the
    failure teardown and, on a resume, must keep the branch (drop only the checkout).
    The old code called iso.discard() unconditionally, deleting the branch, so the
    resume call site's keep-the-branch teardown ran only after the branch was gone."""
    import subprocess

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "Test")
    (tmp_path / "README.md").write_text("hi\n")
    git("add", ".")
    git("commit", "-qm", "init")
    git("branch", "subagent/sg-keep")  # the prior run's deliverable branch

    store = _session_store(tmp_path)
    harness = _make_harness(_resume_model(), _make_deps(tmp_path), store=store)
    ts = TranscriptStore(store.path, store.session_id)
    meta = {**_interrupted_meta("sg-keep"), "isolation": "subagent/sg-keep"}
    ts.write("sg-keep", _dangling_history(), 2000, meta=meta)

    # Force the sub-agent build to fail during resume (e.g. the agent definition
    # was deleted since, or a model override no longer resolves).
    monkeypatch.setattr(harness.subagents, "build", lambda *a, **k: (None, "build failed"))

    job_id, msg = await harness.subagents.resume_spawn("sg-keep")
    assert job_id is None and "build failed" in msg
    branches = subprocess.run(
        ["git", "branch", "--list", "subagent/sg-keep"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    ).stdout
    assert branches.strip() != "", (
        "the failed resume destroyed the prior-work branch it was meant to keep"
    )
    assert not (tmp_path / ".worktrees" / "subagent" / "sg-keep").exists(), (
        "the checkout must still be torn down on failure"
    )


def _cli_meta(sid: str, session: str | None = "sess-abc") -> dict:
    return {
        "stream_id": sid,
        "type": "cli-worker",
        "task": "original cli task",
        "model": None,
        "mcp": None,
        "depth": 1,
        "max_output_chars": None,
        "isolation": None,
        "status": "running",
        "backend": "claude-cli",
        "cli_session_id": session,
    }


def _cli_agent(tmp_path):
    d = tmp_path / ".marim" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "cli-worker.md").write_text(
        "---\ndescription: w\nbackend: claude-cli\ntools: read_file\n---\nWork.\n"
    )


def _resume_fake_cli(tmp_path) -> str:
    # A DIFFERENT session id than the one seeded in meta ("sess-abc"): a fork
    # on --resume mints a new session, and the finished sidecar must record the
    # NEWEST id (result.session_id wins over the meta's), so a later re-resume
    # keys off the fork, not the exhausted original.
    #
    # The "resuming" text step matters too: TranscriptStore.write no-ops on an
    # empty message list (see transcripts.py), which would leave the sidecar's
    # status stuck at "running".
    return fake_claude_bin(
        tmp_path,
        {
            "session_id": "sess-def",
            "known_sessions": ["sess-abc"],
            "turns": [
                [
                    {"text": "resuming"},
                    {
                        "result": {
                            "result": "resumed-cli-ok",
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                        }
                    },
                ]
            ],
        },
    )


def _sent_prompt(tmp_path) -> str:
    """The first user message marim pushed over the CLI's stdin. The prompt no
    longer rides in argv (`-p`): a bidirectional run sends it as a stream-json
    user message."""
    for msg in read_claude_log(tmp_path):
        if msg.get("type") != "user":
            continue
        content = (msg.get("message") or {}).get("content")
        if isinstance(content, str):
            return content
        return "".join(c.get("text", "") for c in content or [] if isinstance(c, dict))
    return ""


@pytest.mark.anyio
async def test_resume_cli_spawn_relaunches_with_resume_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _resume_fake_cli(tmp_path))
    _cli_agent(tmp_path)
    store = _session_store(tmp_path)
    harness = _make_harness(_resume_model(), _make_deps(tmp_path), store=store)
    ts = TranscriptStore(store.path, store.session_id)
    ts.write("sg-cli", _dangling_history(), 2000, meta=_cli_meta("sg-cli"))
    job_id, message = await harness.subagents.resume_spawn("sg-cli")
    assert job_id is not None, message
    report = await harness.deps.jobs.wait(job_id)
    assert report == "resumed-cli-ok"
    argv = read_claude_argv(tmp_path)
    assert "--resume" in argv and argv[argv.index("--resume") + 1] == "sess-abc"
    assert "--append-system-prompt" not in argv
    assert _sent_prompt(tmp_path).startswith("You were interrupted")
    meta = ts.read_meta("sg-cli")
    assert meta["status"] == "finished"
    assert meta["task"] == "original cli task"  # continuation prompt never leaks in
    # Newest-session-id-wins: the fork reported "sess-def" on --resume; the finished
    # meta must key future resumes off it, not the exhausted seeded "sess-abc".
    assert meta["cli_session_id"] == "sess-def"


@pytest.mark.anyio
async def test_resume_cli_preserves_prior_transcript(tmp_path, monkeypatch):
    """The resumed CLI run's translator starts empty and `claude -p --resume` does
    not re-emit prior history, so without prepending the persisted transcript the
    resume's checkpoints (and final write) would overwrite the sidecar with
    tail-only content, destroying the pre-interrupt segment the pane replays."""
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _resume_fake_cli(tmp_path))
    _cli_agent(tmp_path)
    store = _session_store(tmp_path)
    harness = _make_harness(_resume_model(), _make_deps(tmp_path), store=store)
    ts = TranscriptStore(store.path, store.session_id)
    ts.write("sg-keep", _dangling_history(), 2000, meta=_cli_meta("sg-keep"))
    job_id, message = await harness.subagents.resume_spawn("sg-keep")
    assert job_id is not None, message
    await harness.deps.jobs.wait(job_id)
    msgs = ts.read("sg-keep")
    calls = [p for m in msgs for p in getattr(m, "parts", []) if isinstance(p, ToolCallPart)]
    texts = [p for m in msgs for p in getattr(m, "parts", []) if isinstance(p, TextPart)]
    assert any(p.tool_call_id == "dangling" for p in calls), (
        "the pre-interrupt segment must survive the resume's checkpoints"
    )
    assert any("resuming" in str(p.content) for p in texts), (
        "the continuation's content must be present too"
    )
    assert ts.read_meta("sg-keep")["status"] == "finished"


@pytest.mark.anyio
async def test_cli_spawn_records_caller_depth(tmp_path, monkeypatch):
    """A CLI spawn made by a nested native sub-agent records the real depth
    (caller_depth + 1), not a hardcoded 1."""
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _resume_fake_cli(tmp_path))
    _cli_agent(tmp_path)
    store = _session_store(tmp_path)
    harness = _make_harness(_resume_model(), _make_deps(tmp_path), store=store)
    out = await harness.subagents.run("cli-worker", "task", "sg-depth", caller_depth=1)
    assert out == "resumed-cli-ok"
    meta = TranscriptStore(store.path, store.session_id).read_meta("sg-depth")
    assert meta["depth"] == 2


@pytest.mark.anyio
async def test_resume_cli_refusals(tmp_path, monkeypatch):
    _cli_agent(tmp_path)
    store = _session_store(tmp_path)
    harness = _make_harness(_resume_model(), _make_deps(tmp_path), store=store)
    ts = TranscriptStore(store.path, store.session_id)
    # No session id recorded (killed before init) → refuse, don't run the CLI.
    ts.write("sg-nosid", _dangling_history(), 2000, meta=_cli_meta("sg-nosid", session=None))
    job_id, msg = await harness.subagents.resume_spawn("sg-nosid")
    assert job_id is None and "never recorded" in msg
    # Agent type vanished → refuse.
    ts.write(
        "sg-gone", _dangling_history(), 2000, meta={**_cli_meta("sg-gone"), "type": "no-such-agent"}
    )
    job_id, msg = await harness.subagents.resume_spawn("sg-gone")
    assert job_id is None and "no-such-agent" in msg


@pytest.mark.anyio
async def test_resume_cli_spawn_survives_backend_drift(tmp_path, monkeypatch):
    """A sidecar recorded ``backend: claude-cli`` (a tier-routed native spawn —
    see AD-007 — or a spec later edited away from claude-cli) resumes through
    Claude CLI on its persisted session id: the persisted meta is authoritative
    over the resolved agent definition's CURRENT declared backend, so this must
    NOT refuse or engine-swap to the native path underneath the CLI session."""
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _resume_fake_cli(tmp_path))
    d = tmp_path / ".marim" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "flipped.md").write_text("---\ndescription: w\ntools: read_file\n---\nWork.\n")
    store = _session_store(tmp_path)
    harness = _make_harness(_resume_model(), _make_deps(tmp_path), store=store)
    ts = TranscriptStore(store.path, store.session_id)
    ts.write("sg-flip", _dangling_history(), 2000, meta={**_cli_meta("sg-flip"), "type": "flipped"})
    job_id, message = await harness.subagents.resume_spawn("sg-flip")
    assert job_id is not None, message
    report = await harness.deps.jobs.wait(job_id)
    assert report == "resumed-cli-ok"
    argv = read_claude_argv(tmp_path)
    assert "--resume" in argv and argv[argv.index("--resume") + 1] == "sess-abc"


@pytest.mark.anyio
async def test_final_meta_records_tool_count_and_duration(tmp_path):
    """The terminal sidecar meta carries the run's tool tally and wall-clock
    duration alongside usage, so a resumed session can rehydrate the sub-agents
    screen's stats columns instead of showing 0 / 0s."""
    store = _session_store(tmp_path)
    harness = _make_harness(_tool_then_text_model(), _make_deps(tmp_path), store=store)
    await harness.subagents.run("general", "look around", stream_id="sg-stats")
    meta = TranscriptStore(store.path, store.session_id).read_meta("sg-stats")
    assert meta["status"] == "finished"
    assert meta["tool_count"] == 1  # the single list_files call
    assert meta["duration"] > 0


# --- Cross-model resume: another model's reasoning is not replayed ---
#
# Seen live: a spawn transcript written by zen-go mimo-v2.5 resumed on glm-5.2
# failed 400 "Extra inputs are not permitted, field: messages[n].reasoning" —
# pydantic-ai replays a persisted ThinkingPart under the field it arrived in
# whenever the part's provider matches, and the two models name it differently.
# The runner stamps the model it actually ran on into the sidecar (model_ref)
# and strips the thinking parts when the resumed build lands on another one.


def _thinking_history() -> list:
    """A transcript with reasoning in both shapes that matter: a thinking-only
    response (must be dropped whole, never sent as content:null) and a thinking
    + tool-call response (keeps its call, so the repaired return still pairs)."""
    return [
        ModelRequest(parts=[UserPromptPart(content="original task")]),
        ModelResponse(
            parts=[ThinkingPart(content="let me think", id="reasoning", provider_name="openai")]
        ),
        ModelResponse(
            parts=[
                ThinkingPart(content="I'll read x", id="reasoning", provider_name="openai"),
                ToolCallPart(tool_name="read_file", args={"path": "x"}, tool_call_id="dangling"),
            ]
        ),
    ]


def _thinking_parts(messages) -> list:
    return [p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ThinkingPart)]


def test_strip_thinking_drops_reasoning_and_thinking_only_responses():
    from marim_harness.subagents.run_driver import strip_thinking

    original = _thinking_history()
    out = strip_thinking(original)
    assert _thinking_parts(out) == []
    # The thinking-only response is gone entirely; the tool call survived.
    assert len(out) == 2
    assert isinstance(out[1], ModelResponse)
    assert [type(p) for p in out[1].parts] == [ToolCallPart]
    # A copy: the persisted transcript's objects are left as they were.
    assert len(original) == 3 and len(original[2].parts) == 2


def test_strip_thinking_returns_the_same_list_when_there_is_nothing_to_strip():
    from marim_harness.subagents.run_driver import strip_thinking

    history = _dangling_history()
    assert strip_thinking(history) is history


def test_model_ref_names_system_and_model_or_falls_back_to_str():
    from marim_harness.subagents.run_driver import model_ref

    fm = FunctionModel(lambda messages, info: ModelResponse(parts=[]), model_name="glm-5.2")
    assert model_ref(fm) == "function:glm-5.2"
    assert model_ref("zen-go:glm-5.2") == "zen-go:glm-5.2"
    assert model_ref(None) == "None"


@pytest.mark.anyio
async def test_spawn_stamps_the_model_it_ran_on_into_the_sidecar(tmp_path):
    from marim_harness.subagents.run_driver import model_ref

    store = _session_store(tmp_path)
    model = _tool_then_text_model()
    harness = _make_harness(model, _make_deps(tmp_path), store=store)
    assert await harness.subagents.run("general", "look around", stream_id="sg-ref") == "report"
    meta = TranscriptStore(store.path, store.session_id).read_meta("sg-ref")
    assert meta["model_ref"] == model_ref(model)


def _model_expecting_thinking(present: bool) -> FunctionModel:
    """Finishes after asserting whether the incoming history still carries
    reasoning, and that the dangling call was repaired either way."""

    def fn(messages, info):
        returns = [
            p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)
        ]
        assert any(p.tool_call_id == "dangling" for p in returns)
        assert bool(_thinking_parts(messages)) is present
        return ModelResponse(parts=[TextPart(content="resumed-ok")])

    return FunctionModel(fn)


@pytest.mark.parametrize("recorded", ["openai:mimo-v2.5", None], ids=["other-model", "pre-ref"])
@pytest.mark.anyio
async def test_resume_on_another_model_drops_the_persisted_thinking(tmp_path, recorded):
    """A sidecar written by a different model — or one that predates the
    model_ref stamp, where the writer is unknown — resumes without its
    reasoning; the tool call and its repaired return are kept."""
    store = _session_store(tmp_path)
    harness = _make_harness(_model_expecting_thinking(False), _make_deps(tmp_path), store=store)
    ts = TranscriptStore(store.path, store.session_id)
    meta = _interrupted_meta("sg-x")
    if recorded is not None:
        meta["model_ref"] = recorded
    ts.write("sg-x", _thinking_history(), 2000, meta=meta)
    job_id, message = await harness.subagents.resume_spawn("sg-x")
    assert job_id is not None, message
    assert await harness.deps.jobs.wait(job_id) == "resumed-ok"


@pytest.mark.anyio
async def test_resume_on_the_same_model_keeps_the_persisted_thinking(tmp_path):
    from marim_harness.subagents.run_driver import model_ref

    store = _session_store(tmp_path)
    model = _model_expecting_thinking(True)
    harness = _make_harness(model, _make_deps(tmp_path), store=store)
    ts = TranscriptStore(store.path, store.session_id)
    meta = _interrupted_meta("sg-same")
    meta["model_ref"] = model_ref(model)
    ts.write("sg-same", _thinking_history(), 2000, meta=meta)
    job_id, message = await harness.subagents.resume_spawn("sg-same")
    assert job_id is not None, message
    assert await harness.deps.jobs.wait(job_id) == "resumed-ok"
