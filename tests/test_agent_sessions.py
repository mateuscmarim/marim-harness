import asyncio
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.runtime.deps import Deps, UIHooks, WorkspaceConfig
from marim_harness.runtime.permissions import Mode
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import _edit_then_done_model, _make_deps, _make_harness, _text_model


def _raising_model() -> FunctionModel:
    """A model that fails mid-turn (simulates an API outage, or — the reported
    case — a render error raised by the TUI's event_stream_handler)."""

    def fn(messages, info):
        raise RuntimeError("turn boom")

    return FunctionModel(fn)


def _fail_once_then_echo_model(exc: BaseException) -> FunctionModel:
    """Turn 1 raises ``exc``; every later turn echoes back the latest user
    prompt text it received, so a test can assert what was prepended."""
    state = {"n": 0}

    def fn(messages, info):
        state["n"] += 1
        if state["n"] == 1:
            raise exc
        latest = ""
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "UserPromptPart":
                    latest = str(p.content)
        return ModelResponse(parts=[TextPart(content=latest)])

    return FunctionModel(fn)


def _unanswered_tool_calls(messages) -> set:
    """The tool names whose ToolCallPart has no matching ToolReturnPart — a
    history with any is unresumable (every provider rejects an unanswered
    tool_use). Computed here independently of the production helper."""
    calls: dict = {}
    returns: set = set()
    for m in messages:
        for p in getattr(m, "parts", []):
            if type(p).__name__ == "ToolCallPart":
                calls[p.tool_call_id] = p.tool_name
            elif type(p).__name__ == "ToolReturnPart":
                returns.add(p.tool_call_id)
    return {name for cid, name in calls.items() if cid not in returns}


def _capture_prompt_model(captured: dict) -> FunctionModel:
    """Records the first user-prompt text it sees, then answers 'ok'."""
    def fn(messages, info):
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "UserPromptPart":
                    captured.setdefault("prompt", str(p.content))
        return ModelResponse(parts=[TextPart(content="ok")])

    return FunctionModel(fn)


def _autoname_harness(tmp_path, titler, *, name=None):
    from marim_harness.runtime.harness import Harness, HarnessConfig
    from marim_harness.session import SessionManager

    deps = _make_deps(tmp_path)
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    store = manager.create(name)
    return Harness(
        model=_text_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="x",
        config=HarnessConfig(store=store, manager=manager, titler=titler),
    )


async def _fake_titler(messages) -> str:
    return "Generated Title"


@pytest.mark.anyio
async def test_reset_clears_job_history(tmp_path: Path):
    """/clear (harness.reset) drops finished-job history and any re-stashed jobs
    digest so the wiped conversation starts with a clean jobs slate."""
    deps = _make_deps(tmp_path)
    harness = _make_harness(_text_model(), deps)

    async def _quick():
        return "R"

    jid = harness.deps.jobs.register("agent", "a", _quick())
    for _ in range(400):  # let the background job settle
        job = harness.deps.jobs.get(jid)
        if job is not None and job.status != "running":
            break
        await asyncio.sleep(0.005)
    assert harness.deps.jobs.has_finished_pending() is True
    harness.turn_controller._pending_jobs_digest = "stale digest"

    harness.reset()

    assert harness.deps.jobs.get(jid) is None
    assert harness.deps.jobs.has_finished_pending() is False
    assert harness.turn_controller._pending_jobs_digest is None


@pytest.mark.anyio
async def test_new_and_switch_clear_job_history(tmp_path: Path):
    """/new and /switch change the active conversation, so they drop finished-job
    history and any re-stashed digest too — same as /clear."""
    from marim_harness.runtime.harness import Harness, HarnessConfig
    from marim_harness.session import SessionManager

    deps = _make_deps(tmp_path)
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    store = manager.create("first")
    first_id = store.session_id
    harness = Harness(
        model=_text_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="x", config=HarnessConfig(store=store, manager=manager),
    )

    async def _quick():
        return "R"

    async def _seed_finished_job() -> str:
        jid = harness.deps.jobs.register("agent", "a", _quick())
        for _ in range(400):
            job = harness.deps.jobs.get(jid)
            if job is not None and job.status != "running":
                break
            await asyncio.sleep(0.005)
        assert harness.deps.jobs.has_finished_pending() is True
        harness.turn_controller._pending_jobs_digest = "stale"
        return jid

    # /new wipes the finished-job history.
    jid = await _seed_finished_job()
    harness.new_session("second")
    assert harness.deps.jobs.get(jid) is None
    assert harness.deps.jobs.has_finished_pending() is False
    assert harness.turn_controller._pending_jobs_digest is None

    # /switch (back to the first session) wipes it too.
    jid = await _seed_finished_job()
    harness.switch_session(first_id)
    assert harness.deps.jobs.get(jid) is None
    assert harness.deps.jobs.has_finished_pending() is False
    assert harness.turn_controller._pending_jobs_digest is None


@pytest.mark.anyio
async def test_auto_mode_applies_edit(tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo")
    deps = _make_deps(tmp_path)
    harness = _make_harness(_edit_then_done_model(), deps)
    output = await harness.run_turn("change foo to bar")
    assert output == "done"
    assert (tmp_path / "a.txt").read_text() == "bar"


@pytest.mark.anyio
async def test_plan_mode_denies_edit(tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo")
    deps = _make_deps(tmp_path, mode=Mode.plan)
    harness = _make_harness(_edit_then_done_model(), deps)
    output = await harness.run_turn("change foo to bar")
    assert output == "done"
    assert (tmp_path / "a.txt").read_text() == "foo"  # unchanged


@pytest.mark.anyio
async def test_run_turn_accumulates_token_usage(tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo")
    deps = _make_deps(tmp_path)
    harness = _make_harness(_edit_then_done_model(), deps)
    assert harness.session.total_tokens == 0
    await harness.run_turn("change foo to bar")
    after_first = harness.session.total_tokens
    assert after_first > 0
    await harness.run_turn("anything else")
    assert harness.session.total_tokens > after_first  # accumulates across turns


@pytest.mark.anyio
async def test_run_turn_persists_to_store(tmp_path: Path):
    from marim_harness.runtime.harness import Harness
    from marim_harness.session import SessionManager

    (tmp_path / "a.txt").write_text("foo")
    deps = _make_deps(tmp_path)
    store = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data").create()
    harness = Harness(
        model=_edit_then_done_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="x", store=store,
    )
    await harness.run_turn("change foo to bar")
    messages, usage, _, _, _ = store.load()
    assert len(messages) > 0
    assert usage.total_tokens == harness.session.total_tokens


@pytest.mark.anyio
async def test_resume_restores_history_and_tokens(tmp_path: Path):
    from marim_harness.runtime.harness import Harness
    from marim_harness.session import SessionManager

    (tmp_path / "a.txt").write_text("foo")
    deps = _make_deps(tmp_path)
    store = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data").create()
    first = Harness(
        model=_edit_then_done_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="x", store=store,
    )
    await first.run_turn("change foo to bar")
    saved_count = len(first.session.history)
    saved_tokens = first.session.total_tokens

    # A brand-new harness on the same store resumes the prior conversation.
    second = Harness(
        model=_edit_then_done_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="x", store=store,
    )
    assert second.session.history == []  # nothing until we resume
    restored = second.resume()
    assert restored == saved_count
    assert len(second.session.history) == saved_count
    assert second.session.total_tokens == saved_tokens


@pytest.mark.anyio
async def test_session_switch_preserves_each_conversation(tmp_path: Path):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from marim_harness.runtime.harness import Harness
    from marim_harness.session import SessionManager

    deps = _make_deps(tmp_path)
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    harness = Harness(
        model=_edit_then_done_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="x", store=manager.create("alpha"), manager=manager,
    )
    harness.session.history = [ModelRequest(parts=[UserPromptPart(content="in alpha")])]
    harness.session.persist()
    alpha_id = harness.session.store.session_id

    # A fresh session starts empty without disturbing alpha.
    harness.new_session("beta")
    assert harness.session.session_name == "beta"
    assert harness.session.history == []
    harness.session.persist()

    names = {info.name for info in harness.session.sessions()}
    assert {"alpha", "beta"} <= names

    # Switching back restores alpha's conversation.
    restored = harness.switch_session(alpha_id)
    assert restored == 1
    assert harness.session.session_name == "alpha"
    assert harness.session.history[0].parts[0].content == "in alpha"


@pytest.mark.anyio
async def test_tasks_persist_and_restore_across_sessions(tmp_path: Path):
    from marim_harness.runtime.harness import Harness
    from marim_harness.session import SessionManager

    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])

    deps = _make_deps(tmp_path)
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    harness = Harness(
        model=FunctionModel(fn), provider=BuiltinToolProvider(), deps=deps,
        instructions="x", store=manager.create("alpha"), manager=manager,
    )
    deps.tasks.replace([{"text": "ship it", "status": "in_progress"}])
    await harness.run_turn("go")  # persists tasks alongside history
    alpha_id = harness.session.store.session_id

    # A new session clears the checklist...
    harness.new_session("beta")
    assert harness.deps.tasks.items == []

    # ...and switching back restores alpha's checklist.
    harness.switch_session(alpha_id)
    assert [t.text for t in harness.deps.tasks.items] == ["ship it"]
    assert harness.deps.tasks.items[0].status == "in_progress"


@pytest.mark.anyio
async def test_run_turn_compacts_when_over_budget(tmp_path: Path):
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        UserPromptPart,
    )

    from marim_harness.runtime.harness import Harness

    (tmp_path / "a.txt").write_text("foo")
    deps = _make_deps(tmp_path)
    # Tiny budget forces compaction; keep_last small so a tail survives.
    harness = Harness(
        model=_edit_then_done_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="x", max_context_tokens=1, keep_last_messages=4,
    )
    notices = []
    harness.session.on_compact = lambda before, after: notices.append((before, after))

    # Seed a long prior history of clean user turns.
    for i in range(30):
        harness.session.history.append(
            ModelRequest(parts=[UserPromptPart(content=f"old prompt {i}")])
        )
        harness.session.history.append(ModelResponse(parts=[TextPart(content=f"old answer {i}")]))
    before = len(harness.session.history)

    await harness.run_turn("change foo to bar")

    assert notices, "on_compact should fire when history is over budget"
    before_n, after_n = notices[0]
    assert before_n == before
    assert after_n < before_n


@pytest.mark.anyio
async def test_run_turn_summarizes_when_over_budget(tmp_path: Path):
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        UserPromptPart,
    )
    from pydantic_ai.models.test import TestModel

    from marim_harness.runtime.harness import Harness

    (tmp_path / "a.txt").write_text("foo")
    deps = _make_deps(tmp_path)

    async def summarizer(messages):
        return "CONDENSED RECAP"

    harness = Harness(
        model=TestModel(call_tools=[]), provider=BuiltinToolProvider(), deps=deps,
        instructions="x", max_context_tokens=1, keep_last_messages=4,
        summarizer=summarizer,
    )
    for i in range(30):
        harness.session.history.append(
            ModelRequest(parts=[UserPromptPart(content=f"old prompt {i}")])
        )
        harness.session.history.append(ModelResponse(parts=[TextPart(content=f"old answer {i}")]))

    await harness.run_turn("now do this")

    texts = [
        getattr(p, "content", "")
        for m in harness.session.history
        for p in m.parts
        if isinstance(getattr(p, "content", ""), str)
    ]
    assert any("CONDENSED RECAP" in t for t in texts)


@pytest.mark.anyio
async def test_make_summarizer_produces_text():
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from pydantic_ai.models.test import TestModel

    from marim_harness.runtime.harness import make_summarizer

    summarize = make_summarizer(TestModel(custom_output_text="A SUMMARY"))
    out = await summarize([ModelRequest(parts=[UserPromptPart(content="hello")])])
    assert "A SUMMARY" in out


@pytest.mark.anyio
async def test_run_turn_does_not_compact_under_budget(tmp_path: Path):
    from marim_harness.runtime.harness import Harness

    (tmp_path / "a.txt").write_text("foo")
    deps = _make_deps(tmp_path)
    harness = Harness(
        model=_edit_then_done_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="x", max_context_tokens=1_000_000,
    )
    notices = []
    harness.session.on_compact = lambda before, after: notices.append((before, after))
    await harness.run_turn("change foo to bar")
    assert notices == []


@pytest.mark.anyio
async def test_cancel_during_approval_keeps_session_resumable(tmp_path: Path):
    """Cancelling the approval modal mid-turn must not leave the session ending
    in an unanswered tool call — a dangling tool_use the provider would reject,
    breaking every later turn until a manual clear."""
    from marim_harness.runtime.harness import Harness
    from marim_harness.session import SessionManager

    (tmp_path / "a.txt").write_text("foo")

    async def cancel_at_approval(call):
        raise asyncio.CancelledError()

    deps = Deps(workspace=WorkspaceConfig(root=tmp_path, mode=Mode.ask),
                ui=UIHooks(request_approval=cancel_at_approval))
    store = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data").create()
    harness = Harness(
        model=_edit_then_done_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="x", store=store,
    )
    with pytest.raises(asyncio.CancelledError):
        await harness.run_turn("change foo to bar")

    # On disk: resumable (no dangling tool calls)...
    messages, _, _, _, _ = store.load()
    assert _unanswered_tool_calls(messages) == set()
    # ...and in memory too, so the next turn can proceed.
    assert _unanswered_tool_calls(harness.session.history) == set()


@pytest.mark.anyio
async def test_ask_mode_calls_back(tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo")
    asked = []

    async def approve(call):
        asked.append(call.tool_name)
        return True

    deps = _make_deps(tmp_path, mode=Mode.ask, request_approval=approve)
    harness = _make_harness(_edit_then_done_model(), deps)
    await harness.run_turn("change foo to bar")
    assert asked == ["edit_file"]
    assert (tmp_path / "a.txt").read_text() == "bar"


@pytest.mark.anyio
async def test_memory_policy_flips_with_toggle(tmp_path: Path):
    from marim_harness.runtime.harness import Harness

    captured: dict = {}

    def fn(messages, info):
        captured["instructions"] = _last_instructions_fn(messages)
        return ModelResponse(parts=[TextPart(content="ok")])

    deps = _make_deps(tmp_path)

    # Off (default): a restraint block that forbids proactive saves.
    off = Harness(model=FunctionModel(fn), provider=BuiltinToolProvider(),
                  deps=deps, instructions="BASE")
    await off.run_turn("hi")
    off_instr = captured["instructions"].lower()
    assert "proactive memory is on" not in off_instr
    assert "only when the user explicitly asks" in off_instr
    assert "BASE" in captured["instructions"]

    # On: the encouragement block, present even with no memories saved yet.
    on = Harness(model=FunctionModel(fn), provider=BuiltinToolProvider(),
                 deps=deps, instructions="BASE", proactive_memory=True)
    await on.run_turn("hi")
    on_instr = captured["instructions"].lower()
    assert "proactive memory is on" in on_instr
    assert "secret" in on_instr  # mentions not saving secrets
    assert "only when the user explicitly asks" not in on_instr


def _last_instructions_fn(messages) -> str:
    """The instructions attached to the current (most recent) request."""
    result = ""
    for message in messages:
        instr = getattr(message, "instructions", None)
        if instr:
            result = instr
    return result


@pytest.mark.anyio
async def test_run_turn_prepends_finished_job_digest(tmp_path: Path):
    deps = _make_deps(tmp_path)
    captured: dict = {}
    h = _make_harness(_capture_prompt_model(captured), deps)

    async def quick() -> str:
        return "r"

    job_id = deps.jobs.register("agent", "explore: look", quick())
    await deps.jobs.wait(job_id)

    await h.run_turn("what next?")
    assert "background jobs finished" in captured["prompt"]
    assert "job-1 (agent) done" in captured["prompt"]
    assert "what next?" in captured["prompt"]


@pytest.mark.anyio
async def test_run_turn_no_digest_when_nothing_finished(tmp_path: Path):
    deps = _make_deps(tmp_path)
    captured: dict = {}
    h = _make_harness(_capture_prompt_model(captured), deps)
    await h.run_turn("hello")
    assert captured["prompt"] == "hello"


@pytest.mark.anyio
async def test_finished_digest_consumed_once(tmp_path: Path):
    deps = _make_deps(tmp_path)

    async def quick() -> str:
        return "r"

    job_id = deps.jobs.register("agent", "a", quick())
    await deps.jobs.wait(job_id)

    first: dict = {}
    await _make_harness(_capture_prompt_model(first), deps).run_turn("one")
    assert "background jobs finished" in first["prompt"]

    second: dict = {}
    await _make_harness(_capture_prompt_model(second), deps).run_turn("two")
    assert second["prompt"] == "two"  # digest already drained


def test_lsp_manager_built_by_default(tmp_path: Path):
    from marim_harness.lsp.manager import LspManager

    deps = _make_deps(tmp_path)
    harness = _make_harness(_edit_then_done_model(), deps)
    assert isinstance(harness.lsp, LspManager)
    assert deps.services.lsp is harness.lsp


def test_lsp_disabled_builds_no_manager(tmp_path: Path):
    from marim_harness.runtime.harness import Harness, HarnessConfig

    deps = _make_deps(tmp_path)
    harness = Harness(
        model=_edit_then_done_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="You are a coding agent.",
        config=HarnessConfig(lsp_enabled=False),
    )
    assert harness.lsp is None
    assert deps.services.lsp is None


@pytest.mark.anyio
async def test_cancel_does_not_block_on_slow_persist(tmp_path: Path):
    """Ctrl-C must propagate quickly. If the persist is slow (or hangs), the
    handler must time out and re-raise the CancelledError without waiting for
    the disk write to finish — the session is best-effort by design."""
    import time

    from marim_harness.runtime.harness import Harness
    from marim_harness.session import SessionManager

    deps = _make_deps(tmp_path)
    store = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data").create()

    harness = Harness(
        model=_edit_then_done_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="x", store=store,
    )

    # Replace persist() with a sleeper to expose the absence of a deadline.
    def slow_persist():
        time.sleep(5.0)
    harness.session.persist = slow_persist

    # Force the cancel path by raising CancelledError out of agent.run.
    async def cancelling_agent_run(*args, **kwargs):
        raise asyncio.CancelledError()

    harness.agent.run = cancelling_agent_run

    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await harness.run_turn("hi")
    elapsed = time.monotonic() - started
    # Deadline is ~250ms; allow generous headroom but reject the 5s sleep.
    assert elapsed < 2.0, f"cancel took {elapsed:.2f}s — deadline not enforced"
