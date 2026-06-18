import asyncio
import stat
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.agent import Harness
from marim_harness.deps import Deps
from marim_harness.hooks import events as hook_events
from marim_harness.hooks.runner import HookRunner
from marim_harness.lsp.manager import LspManager
from marim_harness.permissions import Mode
from marim_harness.tools.provider import BuiltinToolProvider


def _edit_then_done_model() -> FunctionModel:
    """First model turn: call edit_file. After the tool result: reply 'done'.
    Supports both non-streamed and streamed requests so tests using
    event_stream_handler (e.g. the Pre/PostToolUse hook test) work correctly."""
    import json as _json

    from pydantic_ai.models.function import DeltaToolCall

    state = {"n": 0}
    stream_state = {"n": 0}

    def fn(messages, info):
        state["n"] += 1
        if state["n"] == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="edit_file",
                        args={
                            "path": "a.txt",
                            "edits": [{"old_string": "foo", "new_string": "bar"}],
                        },
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="done")])

    async def stream_fn(messages, info):
        stream_state["n"] += 1
        if stream_state["n"] == 1:
            yield {
                0: DeltaToolCall(
                    name="edit_file",
                    json_args=_json.dumps(
                        {
                            "path": "a.txt",
                            "edits": [{"old_string": "foo", "new_string": "bar"}],
                        }
                    ),
                    tool_call_id="tc-edit-1",
                )
            }
        else:
            yield "done"

    return FunctionModel(fn, stream_function=stream_fn)


def _make_harness(model, deps) -> Harness:
    return Harness(model=model, provider=BuiltinToolProvider(), deps=deps,
                   instructions="You are a coding agent.")


def test_lsp_manager_built_by_default(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_edit_then_done_model(), deps)
    assert isinstance(harness.lsp, LspManager)
    assert deps.lsp is harness.lsp


def test_lsp_disabled_builds_no_manager(tmp_path: Path):
    from marim_harness.agent import HarnessConfig

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(
        model=_edit_then_done_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="You are a coding agent.",
        config=HarnessConfig(lsp_enabled=False),
    )
    assert harness.lsp is None
    assert deps.lsp is None


@pytest.mark.anyio
async def test_auto_mode_applies_edit(tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo")
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_edit_then_done_model(), deps)
    output = await harness.run_turn("change foo to bar")
    assert output == "done"
    assert (tmp_path / "a.txt").read_text() == "bar"


@pytest.mark.anyio
async def test_plan_mode_denies_edit(tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo")
    deps = Deps(workspace_root=tmp_path, mode=Mode.plan)
    harness = _make_harness(_edit_then_done_model(), deps)
    output = await harness.run_turn("change foo to bar")
    assert output == "done"
    assert (tmp_path / "a.txt").read_text() == "foo"  # unchanged


@pytest.mark.anyio
async def test_run_turn_accumulates_token_usage(tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo")
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_edit_then_done_model(), deps)
    assert harness.session.total_tokens == 0
    await harness.run_turn("change foo to bar")
    after_first = harness.session.total_tokens
    assert after_first > 0
    await harness.run_turn("anything else")
    assert harness.session.total_tokens > after_first  # accumulates across turns


@pytest.mark.anyio
async def test_run_turn_persists_to_store(tmp_path: Path):
    from marim_harness.session import SessionManager

    (tmp_path / "a.txt").write_text("foo")
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    store = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data").create()
    harness = Harness(
        model=_edit_then_done_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="x", store=store,
    )
    await harness.run_turn("change foo to bar")
    messages, usage, _ = store.load()
    assert len(messages) > 0
    assert usage.total_tokens == harness.session.total_tokens


@pytest.mark.anyio
async def test_subagent_output_cap_spills_full_and_returns_pointer(tmp_path: Path):
    """When the spawner sets max_output_chars, an over-budget sub-agent report is
    written to a file and the main agent receives a within-budget head + pointer,
    not the raw dump — so a per-call cap actually bounds the inflow."""
    long = "CONCLUSION first. " + "filler. " * 500

    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content=long)])

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(FunctionModel(fn), deps)
    result = await harness.subagents.run("explore", "go", "tc-1", None, 200)

    assert len(result) <= 200
    assert result.startswith("CONCLUSION first.")
    spill = tmp_path / ".marim" / "subagent-output" / "tc-1.md"
    assert spill.read_text() == long
    assert ".marim/subagent-output/tc-1.md" in result


@pytest.mark.anyio
async def test_subagent_no_cap_returns_full_output(tmp_path: Path):
    """Without a cap, the sub-agent's full report passes through unchanged and no
    spill file is created."""
    long = "x" * 5000

    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content=long)])

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(FunctionModel(fn), deps)
    result = await harness.subagents.run("explore", "go", "tc-2", None, None)

    assert result == long
    assert not (tmp_path / ".marim" / "subagent-output").exists()


def _raising_model() -> FunctionModel:
    """A model that fails mid-turn (simulates an API outage, or — the reported
    case — a render error raised by the TUI's event_stream_handler)."""

    def fn(messages, info):
        raise RuntimeError("turn boom")

    return FunctionModel(fn)


@pytest.mark.anyio
async def test_failed_turn_preserves_user_prompt_in_history(tmp_path: Path):
    """When a turn raises, the user's prompt must survive in history so the
    session can continue instead of forgetting the request entirely."""
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_raising_model(), deps)
    with pytest.raises(RuntimeError):
        await harness.run_turn("please remember this request")
    user_texts = [
        p.content
        for m in harness.session.history
        for p in getattr(m, "parts", [])
        if type(p).__name__ == "UserPromptPart"
    ]
    assert any("please remember this request" in str(t) for t in user_texts)


@pytest.mark.anyio
async def test_failed_turn_persists_so_a_new_harness_can_resume(tmp_path: Path):
    """A turn that fails must still be persisted to the store, so a resumed
    session sees the lost prompt rather than starting blank."""
    from marim_harness.session import SessionManager

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    store = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data").create()
    harness = Harness(
        model=_raising_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="x", store=store,
    )
    with pytest.raises(RuntimeError):
        await harness.run_turn("a request that crashed the turn")

    resumed = Harness(
        model=_edit_then_done_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="x", store=store,
    )
    resumed.resume()
    user_texts = [
        p.content
        for m in resumed.session.history
        for p in getattr(m, "parts", [])
        if type(p).__name__ == "UserPromptPart"
    ]
    assert any("a request that crashed the turn" in str(t) for t in user_texts)


def test_actionable_error_note_surfaces_only_model_fixable_failures():
    """Only failures the model itself can act on get a next-turn note. Harness
    or render bugs, cancellations, and transient infra (rate limits, 5xx) get
    None — re-prompting the model wouldn't help and would only add noise."""
    from pydantic_ai.exceptions import (
        ModelHTTPError,
        UnexpectedModelBehavior,
        UsageLimitExceeded,
    )
    from textual.markup import MarkupError

    from marim_harness.agent import _actionable_error_note

    # Not the model's to fix.
    assert _actionable_error_note(MarkupError("bad markup")) is None
    assert _actionable_error_note(RuntimeError("a render bug")) is None
    assert _actionable_error_note(asyncio.CancelledError()) is None
    assert _actionable_error_note(
        ModelHTTPError(status_code=429, model_name="m")
    ) is None  # rate limit — transient
    assert _actionable_error_note(
        ModelHTTPError(status_code=503, model_name="m")
    ) is None  # server error — transient

    # The model can adjust and continue from these.
    assert _actionable_error_note(
        ModelHTTPError(status_code=400, model_name="m", body="too long")
    ) is not None
    assert _actionable_error_note(
        UnexpectedModelBehavior("Exceeded maximum retries")
    ) is not None
    assert _actionable_error_note(UsageLimitExceeded("limit reached")) is not None


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


@pytest.mark.anyio
async def test_actionable_failure_is_surfaced_to_model_next_turn(tmp_path: Path):
    """After an actionable failure, the next turn's prompt carries a short note
    so the model knows the prior turn did not complete and can adjust."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(
        _fail_once_then_echo_model(UnexpectedModelBehavior("Exceeded max retries")),
        deps,
    )
    with pytest.raises(UnexpectedModelBehavior):
        await harness.run_turn("first request")
    echoed = await harness.run_turn("second request")
    assert "did not complete" in echoed  # the note rode along
    assert "second request" in echoed  # ...prepended to the real prompt
    # And it is one-shot: a third, clean turn carries no stale note.
    again = await harness.run_turn("third request")
    assert "did not complete" not in again


@pytest.mark.anyio
async def test_non_actionable_failure_leaves_no_note(tmp_path: Path):
    """A plain harness/render failure must not pollute the next prompt — the
    model can't fix it, so surfacing it would only mislead."""
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_fail_once_then_echo_model(RuntimeError("render boom")), deps)
    with pytest.raises(RuntimeError):
        await harness.run_turn("first request")
    echoed = await harness.run_turn("second request")
    assert "did not complete" not in echoed
    assert echoed == "second request"


@pytest.mark.anyio
async def test_resume_restores_history_and_tokens(tmp_path: Path):
    from marim_harness.session import SessionManager

    (tmp_path / "a.txt").write_text("foo")
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
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


def test_clean_title_strips_noise():
    from marim_harness.compaction import clean_title

    assert clean_title('"Fix the bug"') == "Fix the bug"
    assert clean_title("Title: Add a feature") == "Add a feature"
    assert clean_title("Refactor parser\n\nignored") == "Refactor parser"
    assert clean_title("Do the thing.") == "Do the thing"
    assert clean_title("   ") == "Untitled session"


def test_clean_title_clamps_length():
    from marim_harness.compaction import clean_title

    out = clean_title("word " * 30)
    assert len(out) <= 51
    assert out.endswith("…")


@pytest.mark.anyio
async def test_make_titler_returns_clean_title():
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import make_titler

    run = await Agent(TestModel(), instructions="x").run("do a thing")
    history = run.all_messages()
    titler = make_titler(TestModel(custom_output_text='"Generated Title"'))
    assert await titler(history) == "Generated Title"


async def _fake_titler(messages) -> str:
    return "Generated Title"


def _text_model() -> FunctionModel:
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])

    return FunctionModel(fn)


def _autoname_harness(tmp_path, titler, *, name=None):
    from marim_harness.agent import HarnessConfig
    from marim_harness.session import SessionManager

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    store = manager.create(name)
    return Harness(
        model=_text_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="x",
        config=HarnessConfig(store=store, manager=manager, titler=titler),
    )


@pytest.mark.anyio
async def test_autoname_after_first_turn(tmp_path: Path):
    renames = []
    h = _autoname_harness(tmp_path, _fake_titler)
    h.session.on_rename = lambda old, new: renames.append((old, new))

    await h.run_turn("hello there")
    assert h.session.session_name == "Generated Title"
    assert h.session.store.auto_named is False
    assert renames and renames[-1][1] == "Generated Title"
    # persisted
    assert h.session.manager.store(h.session.store.session_id).name == "Generated Title"


@pytest.mark.anyio
async def test_explicitly_named_session_not_autorenamed(tmp_path: Path):
    h = _autoname_harness(tmp_path, _fake_titler, name="my project")
    await h.run_turn("hello")
    assert h.session.session_name == "my project"


@pytest.mark.anyio
async def test_autoname_happens_only_once(tmp_path: Path):
    calls = {"n": 0}

    async def counting_titler(messages):
        calls["n"] += 1
        return f"Title {calls['n']}"

    h = _autoname_harness(tmp_path, counting_titler)
    await h.run_turn("first")
    await h.run_turn("second")
    assert calls["n"] == 1
    assert h.session.session_name == "Title 1"


@pytest.mark.anyio
async def test_rename_session_explicit_and_generated(tmp_path: Path):
    h = _autoname_harness(tmp_path, _fake_titler, name="start")
    # Explicit rename sets the name verbatim.
    assert await h.rename_session("Manual Name") == "Manual Name"
    assert h.session.session_name == "Manual Name"
    # Blank rename regenerates from the conversation via the titler.
    await h.run_turn("do work")
    assert await h.rename_session() == "Generated Title"
    assert h.session.session_name == "Generated Title"


def _last_instructions(messages) -> str:
    """The instructions attached to the current (most recent) request."""
    result = ""
    for message in messages:
        instr = getattr(message, "instructions", None)
        if instr:
            result = instr
    return result


@pytest.mark.anyio
async def test_project_instructions_injected_and_dynamic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    captured: dict = {}

    def fn(messages, info):
        captured["instructions"] = _last_instructions(messages)
        return ModelResponse(parts=[TextPart(content="ok")])

    # Isolate the global config dir so a real ~/.config/marim/AGENTS.md on the
    # host can't leak its instructions into this project-scoped assertion.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(
        model=FunctionModel(fn), provider=BuiltinToolProvider(), deps=deps,
        instructions="BASE PROMPT",
    )

    # No AGENTS.md yet -> only the base prompt reaches the model.
    await harness.run_turn("hi")
    assert "BASE PROMPT" in captured["instructions"]
    assert "AGENTS.md" not in captured["instructions"]

    # Adding the file changes what the very next turn sees (dynamic reload).
    (tmp_path / "AGENTS.md").write_text("Always write docstrings.")
    await harness.run_turn("hi again")
    assert "Always write docstrings." in captured["instructions"]
    # Base prompt still present and comes before the project instructions.
    instr = captured["instructions"]
    assert "BASE PROMPT" in instr
    assert instr.index("BASE PROMPT") < instr.index("Always write docstrings.")


@pytest.mark.anyio
async def test_global_instructions_injected_and_dynamic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    captured: dict = {}

    def fn(messages, info):
        captured["instructions"] = _last_instructions(messages)
        return ModelResponse(parts=[TextPart(content="ok")])

    # Isolate the global config dir to a temp location (no global AGENTS.md yet).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "cfg" / "marim").mkdir(parents=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    deps = Deps(workspace_root=workspace, mode=Mode.auto)
    harness = Harness(
        model=FunctionModel(fn), provider=BuiltinToolProvider(), deps=deps,
        instructions="BASE PROMPT",
    )

    # No global file yet -> the global block is absent.
    await harness.run_turn("hi")
    assert "Global instructions" not in captured["instructions"]

    # Creating it changes what the very next turn sees (dynamic reload).
    (tmp_path / "cfg" / "marim" / "AGENTS.md").write_text("Never force-push.")
    await harness.run_turn("hi again")
    assert "Never force-push." in captured["instructions"]

    # Ordering: base prompt, then global, then project instructions.
    (workspace / "AGENTS.md").write_text("Project rule: use ruff.")
    await harness.run_turn("third")
    instr = captured["instructions"]
    assert (
        instr.index("BASE PROMPT")
        < instr.index("Never force-push.")
        < instr.index("Project rule: use ruff.")
    )


@pytest.mark.anyio
async def test_memory_indexes_injected_and_dynamic(tmp_path: Path):
    from marim_harness.workspace import memory

    captured: dict = {}

    def fn(messages, info):
        captured["instructions"] = _last_instructions(messages)
        return ModelResponse(parts=[TextPart(content="ok")])

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(
        model=FunctionModel(fn), provider=BuiltinToolProvider(), deps=deps,
        instructions="BASE PROMPT",
    )

    # No memories yet -> no memory section in the prompt.
    await harness.run_turn("hi")
    assert "Project memory" not in captured["instructions"]

    # Saving a project memory makes the very next turn see the index.
    memory.save_memory(
        memory.project_scope(tmp_path), name="Build tool",
        description="uses uv", mem_type="project", body="b", title="Build tool",
    )
    await harness.run_turn("hi again")
    instr = captured["instructions"]
    assert "Project memory" in instr
    assert "build-tool.md" in instr
    assert "BASE PROMPT" in instr


@pytest.mark.anyio
async def test_skill_index_injected_and_dynamic(tmp_path: Path, monkeypatch):
    # Isolate the global skill roots so the real user's skills don't leak in.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    captured: dict = {}

    def fn(messages, info):
        captured["instructions"] = _last_instructions(messages)
        return ModelResponse(parts=[TextPart(content="ok")])

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(
        model=FunctionModel(fn), provider=BuiltinToolProvider(), deps=deps,
        instructions="BASE PROMPT",
    )

    # No skills yet -> no skills section in the prompt.
    await harness.run_turn("hi")
    assert "Available skills" not in captured["instructions"]

    # Dropping a skill in makes the very next turn see the index (dynamic reload).
    skill_dir = tmp_path / ".marim" / "skills" / "code-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: code-review\ndescription: Reviews diffs for bugs.\n---\n\nDo it.\n",
        encoding="utf-8",
    )
    await harness.run_turn("hi again")
    instr = captured["instructions"]
    assert "Available skills" in instr
    assert "code-review" in instr
    assert "Reviews diffs for bugs." in instr
    assert "BASE PROMPT" in instr


@pytest.mark.anyio
async def test_task_state_injected_and_dynamic(tmp_path: Path):
    captured: dict = {}

    def fn(messages, info):
        captured["instructions"] = _last_instructions(messages)
        return ModelResponse(parts=[TextPart(content="ok")])

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(
        model=FunctionModel(fn), provider=BuiltinToolProvider(), deps=deps,
        instructions="BASE PROMPT",
    )

    # No tasks yet -> no checklist section in the prompt.
    await harness.run_turn("hi")
    assert "checklist" not in captured["instructions"].lower()

    # Setting tasks makes the very next turn see them (dynamic).
    deps.tasks.replace([
        {"text": "read the code", "status": "done"},
        {"text": "write the test", "status": "in_progress"},
    ])
    await harness.run_turn("hi again")
    instr = captured["instructions"]
    assert "read the code" in instr
    assert "write the test" in instr
    assert "BASE PROMPT" in instr


@pytest.mark.anyio
async def test_tasks_persist_and_restore_across_sessions(tmp_path: Path):
    from marim_harness.session import SessionManager

    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
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
async def test_memory_policy_flips_with_toggle(tmp_path: Path):
    captured: dict = {}

    def fn(messages, info):
        captured["instructions"] = _last_instructions(messages)
        return ModelResponse(parts=[TextPart(content="ok")])

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)

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


@pytest.mark.anyio
async def test_session_switch_preserves_each_conversation(tmp_path: Path):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from marim_harness.session import SessionManager

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
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
async def test_run_turn_compacts_when_over_budget(tmp_path: Path):
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        UserPromptPart,
    )

    (tmp_path / "a.txt").write_text("foo")
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
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

    (tmp_path / "a.txt").write_text("foo")
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)

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

    from marim_harness.agent import make_summarizer

    summarize = make_summarizer(TestModel(custom_output_text="A SUMMARY"))
    out = await summarize([ModelRequest(parts=[UserPromptPart(content="hello")])])
    assert "A SUMMARY" in out


@pytest.mark.anyio
async def test_run_turn_does_not_compact_under_budget(tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo")
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(
        model=_edit_then_done_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="x", max_context_tokens=1_000_000,
    )
    notices = []
    harness.session.on_compact = lambda before, after: notices.append((before, after))
    await harness.run_turn("change foo to bar")
    assert notices == []


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


@pytest.mark.anyio
async def test_cancel_during_approval_keeps_session_resumable(tmp_path: Path):
    """Cancelling the approval modal mid-turn must not leave the session ending
    in an unanswered tool call — a dangling tool_use the provider would reject,
    breaking every later turn until a manual clear."""
    from marim_harness.session import SessionManager

    (tmp_path / "a.txt").write_text("foo")

    async def cancel_at_approval(call):
        raise asyncio.CancelledError()

    deps = Deps(workspace_root=tmp_path, mode=Mode.ask,
                request_approval=cancel_at_approval)
    store = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data").create()
    harness = Harness(
        model=_edit_then_done_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="x", store=store,
    )
    with pytest.raises(asyncio.CancelledError):
        await harness.run_turn("change foo to bar")

    # On disk: resumable (no dangling tool calls)...
    messages, _, _ = store.load()
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

    deps = Deps(workspace_root=tmp_path, mode=Mode.ask, request_approval=approve)
    harness = _make_harness(_edit_then_done_model(), deps)
    await harness.run_turn("change foo to bar")
    assert asked == ["edit_file"]
    assert (tmp_path / "a.txt").read_text() == "bar"


def _named_model(model_id: str) -> FunctionModel:
    """A model whose every reply names the id it was built for, so a test can
    tell which model actually ran a turn."""
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content=f"from {model_id}")])

    return FunctionModel(fn)


class _FakeSource:
    """Stand-in for config.ModelSource: builds id-tagged models, no network."""

    def __init__(self) -> None:
        self.built: list[str] = []

    def build(self, model_id: str) -> FunctionModel:
        self.built.append(model_id)
        return _named_model(model_id)

    def label(self, model_id: str) -> str:
        return f"fake/{model_id}"

    @property
    def is_local(self) -> bool:
        return False

    async def list_models(self):
        return []


def _switch_harness(tmp_path, *, source=None, summarizer=None, titler=None):
    from marim_harness.agent import HarnessConfig
    from marim_harness.session import SessionManager

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    return Harness(
        model=_named_model("startup"), provider=BuiltinToolProvider(), deps=deps,
        instructions="x",
        config=HarnessConfig(
            store=manager.create(), manager=manager,
            model_source=source, model_id="startup",
            summarizer=summarizer, titler=titler,
        ),
    )


@pytest.mark.anyio
async def test_set_model_switches_model_and_label(tmp_path: Path):
    src = _FakeSource()
    h = _switch_harness(tmp_path, source=src)
    h.set_model("openai/gpt-5.2")
    assert h.model_id == "openai/gpt-5.2"
    assert h.model_label == "fake/openai/gpt-5.2"
    assert src.built == ["openai/gpt-5.2"]
    out = await h.run_turn("hello")
    assert out == "from openai/gpt-5.2"  # the new model actually ran the turn


@pytest.mark.anyio
async def test_set_model_rebuilds_configured_aux_agents(tmp_path: Path):
    async def summarizer(messages):
        return "s"

    h = _switch_harness(tmp_path, source=_FakeSource(),
                        summarizer=summarizer, titler=_fake_titler)
    old_summarizer, old_titler = h.session.summarizer, h.session.titler
    h.set_model("openai/gpt-5.2")
    assert h.session.summarizer is not old_summarizer  # repointed at the new model
    assert h.session.titler is not old_titler


@pytest.mark.anyio
async def test_set_model_leaves_unconfigured_aux_alone(tmp_path: Path):
    h = _switch_harness(tmp_path, source=_FakeSource())  # no summarizer/titler
    h.set_model("openai/gpt-5.2")
    assert h.session.summarizer is None  # not fabricated
    assert h.session.titler is None


def test_set_model_without_source_is_noop(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = Harness(model=_named_model("startup"), provider=BuiltinToolProvider(),
                deps=deps, instructions="x", model_id="startup")
    h.set_model("openai/gpt-5.2")  # no source -> nothing changes
    assert h.model_id == "startup"


def test_set_model_persists_to_session(tmp_path: Path):
    h = _switch_harness(tmp_path, source=_FakeSource())
    h.set_model("openai/gpt-5.2")
    assert h.session.store.model == "openai/gpt-5.2"
    assert h.session.manager.store(h.session.store.session_id).model == "openai/gpt-5.2"


@pytest.mark.anyio
async def test_switch_session_restores_its_model(tmp_path: Path):
    h = _switch_harness(tmp_path, source=_FakeSource())
    h.set_model("openai/gpt-5.2")
    alpha_id = h.session.store.session_id

    # A fresh session reverts to the startup model...
    h.new_session("beta")
    h.set_model("anthropic/claude-sonnet-4-6")
    assert h.model_id == "anthropic/claude-sonnet-4-6"

    # ...and switching back restores alpha's saved model.
    h.switch_session(alpha_id)
    assert h.model_id == "openai/gpt-5.2"
    assert h.model_label == "fake/openai/gpt-5.2"


@pytest.mark.anyio
async def test_run_subagent_returns_output(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(call_tools=[], custom_output_text="FINDINGS"), deps)
    out = await h.subagents.run("explore", "find the parser", "sid")
    assert out == "FINDINGS"


@pytest.mark.anyio
async def test_run_subagent_counts_usage_in_session_total(tmp_path: Path):
    """A foreground spawn's own token spend lands in the session total, not just
    its returned report — counted immediately as the run completes."""
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(call_tools=[], custom_output_text="FINDINGS"), deps)
    assert h.session.total_tokens == 0
    await h.subagents.run("explore", "find the parser", "sid")
    assert h.session.total_tokens > 0


@pytest.mark.anyio
async def test_run_subagent_restricts_tools_by_mode(tmp_path: Path):
    from marim_harness.tools.provider import NET_TOOLS, READ_TOOLS, SUBAGENT_TOOLS

    captured: dict = {}

    def fn(messages, info):
        captured["tools"] = {t.name for t in info.function_tools}
        return ModelResponse(parts=[TextPart(content="report")])

    deps = Deps(workspace_root=tmp_path, mode=Mode.ask)
    h = _make_harness(FunctionModel(fn), deps)

    # ask mode: general drops its gated tools, keeping local reads + net tools.
    out = await h.subagents.run("general", "do it", "sid")
    assert out == "report"
    assert captured["tools"] == set(READ_TOOLS | NET_TOOLS)

    # auto mode: the full set, including write/edit/bash.
    deps.mode = Mode.auto
    await h.subagents.run("general", "do it", "sid")
    assert captured["tools"] == set(SUBAGENT_TOOLS)


@pytest.mark.anyio
async def test_subagent_handler_forwards_run_usage(tmp_path: Path):
    """The sub-agent event handler tags each forwarded event with the run's live
    usage (the whole RunUsage), so the UI can show the token total, cache split,
    and cost in the widget — not just a bare count."""
    from types import SimpleNamespace

    recorded: list = []

    async def cb(stream_id, event, usage):
        recorded.append((stream_id, event, usage))

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto, on_subagent_event=cb)
    h = _make_harness(_text_model(), deps)
    handler = h.subagents.handler("sid")

    async def events():
        yield "evt-a"
        yield "evt-b"

    usage = SimpleNamespace(total_tokens=4096)
    ctx = SimpleNamespace(usage=usage)
    await handler(ctx, events())

    # The full usage object is forwarded verbatim, tagged with the stream id.
    assert recorded == [
        ("sid", "evt-a", usage),
        ("sid", "evt-b", usage),
    ]


@pytest.mark.anyio
async def test_subagent_handler_none_without_listener(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)  # no on_subagent_event
    h = _make_harness(_text_model(), deps)
    assert h.subagents.handler("sid") is None


@pytest.mark.anyio
async def test_run_subagent_unknown_type(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(_text_model(), deps)
    out = await h.subagents.run("ghost", "do it", "sid")
    assert "No sub-agent type 'ghost'" in out
    assert "explore" in out and "general" in out  # lists what's available


@pytest.mark.anyio
async def test_agent_index_injected(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    captured: dict = {}

    def fn(messages, info):
        captured["instructions"] = _last_instructions(messages)
        return ModelResponse(parts=[TextPart(content="ok")])

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = Harness(model=FunctionModel(fn), provider=BuiltinToolProvider(), deps=deps,
                instructions="BASE PROMPT")
    await h.run_turn("hi")
    instr = captured["instructions"]
    assert "spawn_agent" in instr
    assert "explore" in instr
    assert "general" in instr


@pytest.mark.anyio
async def test_run_background_subagent_returns_output(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(call_tools=[], custom_output_text="BG REPORT"), deps)
    out = await h.subagents.run_background("explore", "scan the repo")
    assert out == "BG REPORT"


@pytest.mark.anyio
async def test_run_background_subagent_counts_and_persists_usage(tmp_path: Path):
    """A background spawn finishes off-turn, so its spend is folded into the
    session total AND persisted right away — not left for the next turn."""
    from pydantic_ai.models.test import TestModel

    from marim_harness.session import SessionManager

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    store = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data").create()
    h = Harness(
        model=TestModel(call_tools=[], custom_output_text="BG"),
        provider=BuiltinToolProvider(), deps=deps, instructions="x", store=store,
    )
    assert h.session.total_tokens == 0
    await h.subagents.run_background("explore", "scan the repo")
    assert h.session.total_tokens > 0
    # The spend reached disk immediately, without waiting for a run_turn.
    _, usage, _ = store.load()
    assert usage.total_tokens == h.session.total_tokens


@pytest.mark.anyio
async def test_run_background_subagent_unknown_type(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(_text_model(), deps)
    out = await h.subagents.run_background("ghost", "do it")
    assert "No sub-agent type 'ghost'" in out


@pytest.mark.anyio
async def test_run_background_subagent_respects_mode(tmp_path: Path):
    from marim_harness.tools.provider import NET_TOOLS, READ_TOOLS, SUBAGENT_TOOLS

    captured: dict = {}

    def fn(messages, info):
        captured["tools"] = {t.name for t in info.function_tools}
        return ModelResponse(parts=[TextPart(content="r")])

    deps = Deps(workspace_root=tmp_path, mode=Mode.ask)
    h = _make_harness(FunctionModel(fn), deps)
    await h.subagents.run_background("general", "x")
    assert captured["tools"] == set(READ_TOOLS | NET_TOOLS)
    deps.mode = Mode.auto
    await h.subagents.run_background("general", "x")
    assert captured["tools"] == set(SUBAGENT_TOOLS)


def test_background_agent_runner_wired(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(_text_model(), deps)
    assert deps.run_background_agent == h.subagents.run_background


def _spawn_then_done_model() -> FunctionModel:
    """Main agent: spawn an explore sub-agent, then echo its report. The same
    model backs the sub-agent, so it's told apart by its instructions."""
    def fn(messages, info):
        instr = _last_instructions(messages)
        if "sub-agent" in instr:
            return ModelResponse(parts=[TextPart(content="SUBREPORT")])
        ret = None
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "ToolReturnPart" and \
                        getattr(p, "tool_name", "") == "spawn_agent":
                    ret = str(p.content)
        if ret is not None:
            return ModelResponse(parts=[TextPart(content=f"done: {ret}")])
        return ModelResponse(parts=[ToolCallPart(
            tool_name="spawn_agent", args={"type": "explore", "task": "find X"}
        )])

    return FunctionModel(fn)


@pytest.mark.anyio
async def test_spawn_agent_tool_runs_subagent_end_to_end(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(_spawn_then_done_model(), deps)
    out = await h.run_turn("investigate")
    assert out == "done: SUBREPORT"


def _capture_prompt_model(captured: dict) -> FunctionModel:
    """Records the first user-prompt text it sees, then answers 'ok'."""
    def fn(messages, info):
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "UserPromptPart":
                    captured.setdefault("prompt", str(p.content))
        return ModelResponse(parts=[TextPart(content="ok")])

    return FunctionModel(fn)


@pytest.mark.anyio
async def test_run_turn_prepends_finished_job_digest(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
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
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    captured: dict = {}
    h = _make_harness(_capture_prompt_model(captured), deps)
    await h.run_turn("hello")
    assert captured["prompt"] == "hello"


@pytest.mark.anyio
async def test_finished_digest_consumed_once(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)

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


class _FakeServer:
    """A stand-in MCP server: an async context manager that can be made to fail
    on enter, so connect()'s per-server degradation can be exercised."""

    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.id = name
        self.fail = fail
        self.entered = False

    async def __aenter__(self):
        if self.fail:
            raise RuntimeError("boom")
        self.entered = True
        return self

    async def __aexit__(self, *exc) -> bool:
        self.entered = False
        return False


@pytest.mark.anyio
async def test_connect_degrades_past_failing_server(tmp_path: Path):
    bad = _FakeServer("bad", fail=True)
    good = _FakeServer("good")
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = Harness(model=_text_model(), provider=BuiltinToolProvider(), deps=deps,
                instructions="x", mcp_servers=[bad, good])

    status = await h.connect()
    # The good server is live; the bad one is reported, not fatal.
    assert good in h.mcp._live_servers
    assert bad not in h.mcp._live_servers
    assert good.entered is True
    assert status["connected"] == ["good"]
    assert status["failed"] and status["failed"][0][0] == "bad"

    await h.aclose()
    assert good.entered is False  # connection closed on shutdown
    assert h.mcp._live_servers == []


@pytest.mark.anyio
async def test_connect_noop_without_servers(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(_text_model(), deps)  # no mcp_servers
    status = await h.connect()
    assert status == {"connected": [], "failed": []}
    await h.aclose()  # safe with nothing open


@pytest.mark.anyio
async def test_run_turn_forwards_live_toolsets(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.usage import RunUsage

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(_text_model(), deps)
    sentinel = object()
    h.mcp._live_servers = [sentinel]

    captured: dict = {}

    async def fake_run(user_prompt, **kwargs):
        captured["toolsets"] = kwargs.get("toolsets")
        return SimpleNamespace(
            all_messages=lambda: [], usage=RunUsage(), output="ok"
        )

    h.agent.run = fake_run
    out = await h.run_turn("hi")
    assert out == "ok"
    assert captured["toolsets"] == [sentinel]  # live servers reach agent.run


@pytest.mark.anyio
async def test_connect_skips_disabled_servers(tmp_path: Path):
    off = _FakeServer("off")
    on = _FakeServer("on")
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = Harness(model=_text_model(), provider=BuiltinToolProvider(), deps=deps,
                instructions="x", mcp_servers=[off, on], mcp_disabled=["off"])

    status = await h.connect()
    assert status["connected"] == ["on"]
    assert off.entered is False  # config-disabled: never launched
    assert on in h.mcp._live_servers
    assert off not in h.mcp._live_servers
    await h.aclose()


@pytest.mark.anyio
async def test_run_turn_omits_disabled_from_toolsets(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.usage import RunUsage

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(_text_model(), deps)
    live_on, live_off = _FakeServer("on"), _FakeServer("off")
    h.mcp._live_servers = [live_on, live_off]
    h.mcp.disabled = {"off"}

    captured: dict = {}

    async def fake_run(user_prompt, **kwargs):
        captured["toolsets"] = kwargs.get("toolsets")
        return SimpleNamespace(all_messages=lambda: [], usage=RunUsage(), output="ok")

    h.agent.run = fake_run
    await h.run_turn("hi")
    assert captured["toolsets"] == [live_on]  # the disabled one is muted


@pytest.mark.anyio
async def test_disable_server_keeps_connection_but_mutes(tmp_path: Path):
    srv = _FakeServer("demo")
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = Harness(model=_text_model(), provider=BuiltinToolProvider(), deps=deps,
                instructions="x", mcp_servers=[srv])
    await h.connect()
    assert srv.entered is True

    await h.disable_server("demo")
    assert "demo" in h.mcp.disabled
    assert srv.entered is True  # still connected, just not offered
    await h.aclose()


@pytest.mark.anyio
async def test_enable_server_connects_on_demand(tmp_path: Path):
    srv = _FakeServer("demo")
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = Harness(model=_text_model(), provider=BuiltinToolProvider(), deps=deps,
                instructions="x", mcp_servers=[srv], mcp_disabled=["demo"])
    await h.connect()
    assert srv.entered is False  # started disabled, so not launched

    err = await h.enable_server("demo")
    assert err is None
    assert "demo" not in h.mcp.disabled
    assert srv.entered is True  # connected on demand
    assert srv in h.mcp._live_servers
    assert "demo" in h.mcp.mcp_status["connected"]
    await h.aclose()


@pytest.mark.anyio
async def test_enable_after_close_does_not_double_list_connected(tmp_path: Path):
    """Re-enabling a server whose name is still in mcp_status['connected'] (e.g.
    after an aclose that cleared the live list but not the status) must not add a
    duplicate entry."""
    srv = _FakeServer("demo")
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = Harness(model=_text_model(), provider=BuiltinToolProvider(), deps=deps,
                instructions="x", mcp_servers=[srv])
    await h.connect()
    assert h.mcp.mcp_status["connected"] == ["demo"]
    await h.aclose()

    await h.enable_server("demo")
    assert h.mcp.mcp_status["connected"].count("demo") == 1
    await h.aclose()


@pytest.mark.anyio
async def test_toggle_persists_to_config_across_the_session(tmp_path: Path):
    import json

    ppath = tmp_path / ".marim" / "mcp.json"
    ppath.parent.mkdir(parents=True)
    ppath.write_text(
        json.dumps({"mcpServers": {"demo": {"command": "x"}}}), encoding="utf-8"
    )
    srv = _FakeServer("demo")
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = Harness(model=_text_model(), provider=BuiltinToolProvider(), deps=deps,
                instructions="x", mcp_servers=[srv])
    await h.connect()

    await h.disable_server("demo")
    assert json.loads(ppath.read_text())["mcpServers"]["demo"]["enabled"] is False

    await h.enable_server("demo")
    assert json.loads(ppath.read_text())["mcpServers"]["demo"]["enabled"] is True
    await h.aclose()


@pytest.mark.anyio
async def test_enable_server_reports_connection_failure(tmp_path: Path):
    srv = _FakeServer("demo", fail=True)
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = Harness(model=_text_model(), provider=BuiltinToolProvider(), deps=deps,
                instructions="x", mcp_servers=[srv], mcp_disabled=["demo"])
    await h.connect()

    err = await h.enable_server("demo")
    assert err and "boom" in err  # surfaced, not fatal
    assert srv not in h.mcp._live_servers
    await h.aclose()


def test_resume_restores_saved_model(tmp_path: Path):
    from marim_harness.session import SessionManager

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    store = manager.create()
    first = Harness(model=_named_model("startup"), provider=BuiltinToolProvider(),
                    deps=deps, instructions="x", store=store, manager=manager,
                    model_source=_FakeSource(), model_id="startup")
    first.set_model("openai/gpt-5.2")

    second = Harness(model=_named_model("startup"), provider=BuiltinToolProvider(),
                     deps=deps, instructions="x", store=manager.store(store.session_id),
                     manager=manager, model_source=_FakeSource(), model_id="startup")
    second.resume()
    assert second.model_id == "openai/gpt-5.2"


def test_granted_servers_resolves_named(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    a = SimpleNamespace(tool_prefix="mddocs")
    b = SimpleNamespace(tool_prefix="sentry")
    h.mcp._live_servers = [a, b]

    granted, unknown = h.mcp.granted_servers(["mddocs"])
    assert granted == [a]
    assert unknown == []


def test_granted_servers_none_grants_nothing(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = [SimpleNamespace(tool_prefix="mddocs")]

    assert h.mcp.granted_servers(None) == ([], [])
    assert h.mcp.granted_servers([]) == ([], [])


def test_granted_servers_reports_unknown(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = [SimpleNamespace(tool_prefix="mddocs")]

    granted, unknown = h.mcp.granted_servers(["mddocs", "nope"])
    assert granted == [h.mcp._live_servers[0]]
    assert unknown == ["nope"]


def test_granted_servers_excludes_disabled(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = [SimpleNamespace(tool_prefix="mddocs")]
    h.mcp.disabled = {"mddocs"}

    granted, unknown = h.mcp.granted_servers(["mddocs"])
    assert granted == []
    assert unknown == ["mddocs"]


def test_granted_servers_dedupes(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    a = SimpleNamespace(tool_prefix="mddocs")
    h.mcp._live_servers = [a]

    granted, unknown = h.mcp.granted_servers(["mddocs", "mddocs"])
    assert granted == [a]
    assert unknown == []


def test_granted_servers_dedupes_unknown(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = []

    granted, unknown = h.mcp.granted_servers(["nope", "nope"])
    assert granted == []
    assert unknown == ["nope"]


def test_mcp_grant_note_lists_unknown_and_enabled(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = [
        SimpleNamespace(tool_prefix="mddocs"),
        SimpleNamespace(tool_prefix="sentry"),
    ]

    note = h.mcp.grant_note(["nope"])
    assert "nope" in note
    assert "mddocs" in note and "sentry" in note
    assert note.endswith("\n\n")


def test_mcp_grant_note_empty_when_nothing_unknown(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    assert h.mcp.grant_note([]) == ""


def test_mcp_grant_note_handles_no_enabled_servers(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = []  # nothing enabled

    note = h.mcp.grant_note(["nope"])
    assert "nope" in note
    assert "none" in note.lower()


# ---------------------------------------------------------------------------
# Task 5: SessionStart / UserPromptSubmit hook context injection
# ---------------------------------------------------------------------------

def _hook_script(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(p)


def _prompt_capturing_model(sink: list) -> FunctionModel:
    """Records the LAST user-prompt text it sees per call (the current turn's
    new prompt, not history), then replies 'ok'. pydantic-ai's FunctionModel
    receives the full conversation history each call, so we capture only the
    latest UserPromptPart to isolate the current turn's new prompt.
    Supports both non-streamed and streamed requests (streaming is required when
    an event_stream_handler is set, e.g. when hooks are configured)."""
    def fn(messages, info):
        latest = None
        for msg in messages:
            for part in getattr(msg, "parts", []):
                content = getattr(part, "content", None)
                if isinstance(content, str) and part.__class__.__name__ == "UserPromptPart":
                    latest = content
        if latest is not None:
            sink.append(latest)
        return ModelResponse(parts=[TextPart(content="ok")])

    async def stream_fn(messages, info):
        latest = None
        for msg in messages:
            for part in getattr(msg, "parts", []):
                content = getattr(part, "content", None)
                if isinstance(content, str) and part.__class__.__name__ == "UserPromptPart":
                    latest = content
        if latest is not None:
            sink.append(latest)
        yield "ok"

    return FunctionModel(fn, stream_function=stream_fn)


@pytest.mark.anyio
async def test_session_start_context_is_prepended_once(tmp_path):
    cmd = _hook_script(tmp_path, "ss.sh", "echo SESSION_CTX\n")
    deps = Deps(
        workspace_root=tmp_path, mode=Mode.auto,
        hooks=HookRunner(
            {hook_events.SESSION_START: [{"hooks": [{"type": "command", "command": cmd}]}]}
        ),
    )
    sink: list = []
    harness = _make_harness(_prompt_capturing_model(sink), deps)
    await harness.session_start("startup")
    await harness.run_turn("first")
    assert "SESSION_CTX" in sink[0]
    await harness.run_turn("second")
    assert "SESSION_CTX" not in sink[1]  # consumed; not repeated


@pytest.mark.anyio
async def test_user_prompt_submit_context_is_prepended(tmp_path):
    cmd = _hook_script(tmp_path, "ups.sh", "echo PROMPT_CTX\n")
    deps = Deps(
        workspace_root=tmp_path, mode=Mode.auto,
        hooks=HookRunner(
            {hook_events.USER_PROMPT_SUBMIT: [{"hooks": [{"type": "command", "command": cmd}]}]}
        ),
    )
    sink: list = []
    harness = _make_harness(_prompt_capturing_model(sink), deps)
    await harness.run_turn("do the thing")
    assert "PROMPT_CTX" in sink[0]
    assert "do the thing" in sink[0]


@pytest.mark.anyio
async def test_user_prompt_submit_fires_on_every_turn(tmp_path):
    """UserPromptSubmit hook fires on every turn, not just the first. The hook
    context is prepended to each turn's prompt, proving repeated activation."""
    cmd = _hook_script(tmp_path, "ups_every.sh", "echo PROMPT_CTX\n")
    deps = Deps(
        workspace_root=tmp_path, mode=Mode.auto,
        hooks=HookRunner(
            {hook_events.USER_PROMPT_SUBMIT: [{"hooks": [{"type": "command", "command": cmd}]}]}
        ),
    )
    sink: list = []
    harness = _make_harness(_prompt_capturing_model(sink), deps)
    await harness.run_turn("first")
    await harness.run_turn("second")
    assert "PROMPT_CTX" in sink[0]
    assert "PROMPT_CTX" in sink[1]  # fires again on the second turn, not one-shot


@pytest.mark.anyio
async def test_no_hooks_runs_turn_normally(tmp_path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)  # hooks=None
    sink: list = []
    harness = _make_harness(_prompt_capturing_model(sink), deps)
    out = await harness.run_turn("hello")
    assert out == "ok"
    assert sink[0] == "hello"  # untouched


def test_strip_turn_context_recovers_typed_text():
    from marim_harness.agent import strip_turn_context, wrap_turn_context

    wrapped = wrap_turn_context("<agentmemory-context>stuff</agentmemory-context>",
                                "implement a fetch tool")
    assert strip_turn_context(wrapped) == "implement a fetch tool"
    # Multi-line typed text survives intact.
    wrapped2 = wrap_turn_context("ctx", "line one\n\nline two")
    assert strip_turn_context(wrapped2) == "line one\n\nline two"


def test_strip_turn_context_passes_through_plain_prompt():
    from marim_harness.agent import strip_turn_context

    # No envelope -> returned unchanged, even if it mentions the tag in prose.
    assert strip_turn_context("just a normal prompt") == "just a normal prompt"
    assert strip_turn_context("talk about <turn-context> as a topic") == (
        "talk about <turn-context> as a topic"
    )


@pytest.mark.anyio
async def test_injected_context_is_wrapped_so_replay_can_recover_typed_text(tmp_path):
    """A SessionStart hook injects context that gets prepended to the prompt.
    The persisted UserPromptPart must wrap it in a turn-context envelope so a
    resumed session can recover just what the user typed, while the model still
    sees the injected context."""
    from marim_harness.agent import strip_turn_context

    cmd = _hook_script(tmp_path, "ss.sh", "echo SESSION_CTX\n")
    hooks = HookRunner(
        {hook_events.SESSION_START: [{"hooks": [{"type": "command", "command": cmd}]}]}
    )
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto, hooks=hooks)
    sink: list = []
    harness = _make_harness(_prompt_capturing_model(sink), deps)
    await harness.session_start("startup")
    await harness.run_turn("implement a fetch tool")

    # The model still sees the injected context this turn.
    assert "SESSION_CTX" in sink[0]
    # The persisted prompt wraps it so replay can strip back to the typed text.
    persisted = [
        p.content
        for m in harness.session.history
        for p in getattr(m, "parts", [])
        if type(p).__name__ == "UserPromptPart"
    ]
    assert persisted, "expected a persisted user prompt"
    assert "SESSION_CTX" in persisted[0]  # context is in the stored prompt
    assert strip_turn_context(persisted[0]) == "implement a fetch tool"


@pytest.mark.anyio
async def test_plain_turn_is_not_wrapped(tmp_path):
    """With nothing injected, the persisted prompt is the typed text verbatim —
    no envelope — so existing sessions and output are unaffected."""
    from marim_harness.agent import strip_turn_context

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)  # no hooks
    sink: list = []
    harness = _make_harness(_prompt_capturing_model(sink), deps)
    await harness.run_turn("hello")
    persisted = [
        p.content
        for m in harness.session.history
        for p in getattr(m, "parts", [])
        if type(p).__name__ == "UserPromptPart"
    ]
    assert persisted[0] == "hello"
    assert strip_turn_context(persisted[0]) == "hello"


def test_parallel_tool_calls_enabled_on_main_agent(tmp_path):
    """The main agent forces parallel tool calls on, so providers that support
    it (Anthropic, OpenAI, Groq, xAI, …) run same-turn tool calls concurrently
    rather than relying on a provider default that may be off."""
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_edit_then_done_model(), deps)
    assert harness.agent.model_settings is not None
    assert harness.agent.model_settings.get("parallel_tool_calls") is True


def test_parallel_tool_calls_enabled_on_subagent(tmp_path):
    """Spawned sub-agents inherit the same setting — fan-out work should be as
    parallel as the main agent's."""
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_edit_then_done_model(), deps)
    sub, err = harness.subagents.build("explore")
    assert err is None
    assert sub.model_settings is not None
    assert sub.model_settings.get("parallel_tool_calls") is True


def _capture_subagent(h, report="report"):
    """Replace _build_subagent so the spawned agent's run() records the toolsets
    it was given and returns a canned report. Returns the capture dict."""
    from types import SimpleNamespace

    from pydantic_ai.usage import RunUsage

    cap: dict = {}

    class _StubAgent:
        async def run(self, task, **kwargs):
            cap["task"] = task
            cap["toolsets"] = kwargs.get("toolsets")
            return SimpleNamespace(output=report, usage=RunUsage())

    h.subagents.build = lambda type, max_output_chars=None: (_StubAgent(), None)
    return cap


@pytest.mark.anyio
async def test_run_subagent_grants_named_server(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    server = SimpleNamespace(tool_prefix="mddocs")
    h.mcp._live_servers = [server]
    cap = _capture_subagent(h)

    out = await h.subagents.run("explore", "read docs", "sid", ["mddocs"])
    assert out == "report"
    # Identity, not just equality: gating relies on the SAME hooked server
    # object reaching run() — a copy would silently drop the approval hook.
    assert cap["toolsets"][0] is server


@pytest.mark.anyio
async def test_run_subagent_default_grants_no_servers(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = [SimpleNamespace(tool_prefix="mddocs")]
    cap = _capture_subagent(h)

    await h.subagents.run("explore", "investigate", "sid")
    assert cap["toolsets"] == []


@pytest.mark.anyio
async def test_run_subagent_prepends_unknown_note(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = []
    _capture_subagent(h, report="FINDINGS")

    out = await h.subagents.run("explore", "investigate", "sid", ["nope"])
    assert "nope" in out
    assert out.rstrip().endswith("FINDINGS")


@pytest.mark.anyio
async def test_run_background_subagent_grants_named_server(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    server = SimpleNamespace(tool_prefix="mddocs")
    h.mcp._live_servers = [server]
    cap = _capture_subagent(h)

    out = await h.subagents.run_background("general", "do it", ["mddocs"])
    assert out == "report"
    # Identity, not just equality: the background path must also forward the
    # SAME hooked server object so its approval gating is preserved.
    assert cap["toolsets"][0] is server


@pytest.mark.anyio
async def test_run_background_subagent_prepends_unknown_note(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = []
    _capture_subagent(h, report="DONE")

    out = await h.subagents.run_background("general", "do it", ["nope"])
    assert "nope" in out
    assert out.rstrip().endswith("DONE")


@pytest.mark.anyio
async def test_run_background_subagent_default_grants_no_servers(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = [SimpleNamespace(tool_prefix="mddocs")]
    cap = _capture_subagent(h)

    await h.subagents.run_background("general", "do it")
    assert cap["toolsets"] == []


def test_mcp_index_text_lists_enabled(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = [
        SimpleNamespace(tool_prefix="mddocs"),
        SimpleNamespace(tool_prefix="sentry"),
    ]
    text = h.mcp.mcp_index_text()
    assert "mddocs" in text and "sentry" in text
    assert "spawn_agent" in text  # tells the model how to use them


def test_mcp_index_text_silent_when_none(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = []
    assert h.mcp.mcp_index_text() == ""


def test_mcp_index_text_excludes_disabled(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = [
        SimpleNamespace(tool_prefix="mddocs"),
        SimpleNamespace(tool_prefix="sentry"),
    ]
    h.mcp.disabled = {"sentry"}
    text = h.mcp.mcp_index_text()
    assert "mddocs" in text
    assert "sentry" not in text


# ---------------------------------------------------------------------------
# Task 6: PreToolUse / PostToolUse hooks via composed event handler
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_pre_and_post_tool_use_fire(tmp_path):
    (tmp_path / "a.txt").write_text("foo")
    log = tmp_path / "toolhooks.log"
    # Write a Python helper script that the hook shell script will invoke; this
    # avoids bash single-quote escaping issues when embedding the log path.
    helper = tmp_path / "loghook.py"
    helper.write_text(
        f"import sys, json\n"
        f"d = json.load(sys.stdin)\n"
        f"open({str(log)!r}, 'a').write("
        f"d['hook_event_name'] + ' ' + d.get('tool_name', '') + '\\n')\n",
        encoding="utf-8",
    )
    cmd = _hook_script(
        tmp_path, "tool.sh",
        f"python3 {str(helper)}\n",
    )
    runner = HookRunner({
        hook_events.PRE_TOOL_USE: [
            {"matcher": "*", "hooks": [{"type": "command", "command": cmd}]}
        ],
        hook_events.POST_TOOL_USE: [
            {"matcher": "*", "hooks": [{"type": "command", "command": cmd}]}
        ],
    })
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto, hooks=runner)
    harness = _make_harness(_edit_then_done_model(), deps)
    await harness.run_turn("change foo to bar")
    lines = log.read_text().splitlines()
    assert "PreToolUse edit_file" in lines
    assert any(line.startswith("PostToolUse edit_file") for line in lines)


@pytest.mark.anyio
async def test_post_tool_use_includes_tool_input(tmp_path):
    """PostToolUse payload must carry tool_input (the args) correlated from the
    matching PreToolUse call so that CC plugin scripts can read the call's
    input to correlate it with its result (CC-contract fidelity)."""
    (tmp_path / "a.txt").write_text("foo")
    log = tmp_path / "toolinput.log"
    # Python helper logs the full JSON payload for PostToolUse events only.
    helper = tmp_path / "loginput.py"
    helper.write_text(
        f"import sys, json\n"
        f"d = json.load(sys.stdin)\n"
        f"if d.get('hook_event_name') == 'PostToolUse':\n"
        f"    open({str(log)!r}, 'a').write(json.dumps(d) + '\\n')\n",
        encoding="utf-8",
    )
    cmd = _hook_script(
        tmp_path, "toolinput.sh",
        f"python3 {str(helper)}\n",
    )
    runner = HookRunner({
        hook_events.PRE_TOOL_USE: [
            {"matcher": "*", "hooks": [{"type": "command", "command": cmd}]}
        ],
        hook_events.POST_TOOL_USE: [
            {"matcher": "*", "hooks": [{"type": "command", "command": cmd}]}
        ],
    })
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto, hooks=runner)
    harness = _make_harness(_edit_then_done_model(), deps)
    await harness.run_turn("change foo to bar")

    # The log file must exist and contain at least one PostToolUse line.
    assert log.exists(), "No PostToolUse hook fired"
    import json as _json
    post_payloads = [_json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert post_payloads, "No PostToolUse payloads logged"

    # Every PostToolUse payload must carry tool_input with the actual args.
    for payload in post_payloads:
        assert "tool_input" in payload, (
            f"PostToolUse payload missing tool_input: {payload}"
        )
    # Specifically: the edit_file call's args (path + edits) must be present.
    edit_payloads = [p for p in post_payloads if p.get("tool_name") == "edit_file"]
    assert edit_payloads, "No PostToolUse for edit_file found"
    assert edit_payloads[0]["tool_input"].get("path") == "a.txt", (
        f"Expected tool_input.path == 'a.txt', got: {edit_payloads[0]['tool_input']}"
    )


# ---------------------------------------------------------------------------
# Task 8: SubagentStart / SubagentStop hooks
# ---------------------------------------------------------------------------

def _make_subagent_def(ws: Path, name: str = "helper") -> None:
    d = ws / ".marim" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: A helper.\ntools: [read_file]\n---\n\nHelp out.\n",
        encoding="utf-8",
    )


@pytest.mark.anyio
async def test_subagent_start_and_stop_fire(tmp_path):
    _make_subagent_def(tmp_path)
    log = tmp_path / "sub.log"
    # Use a Python helper file to avoid bash single-quote escaping issues when
    # embedding the log path (same pattern as test_pre_and_post_tool_use_fire).
    helper = tmp_path / "subhook.py"
    helper.write_text(
        f"import sys, json\n"
        f"d = json.load(sys.stdin)\n"
        f"open({str(log)!r}, 'a').write(d['hook_event_name'] + '\\n')\n",
        encoding="utf-8",
    )
    cmd = _hook_script(
        tmp_path, "sub.sh",
        f"python3 {str(helper)}\n",
    )
    runner = HookRunner({
        hook_events.SUBAGENT_START: [{"hooks": [{"type": "command", "command": cmd}]}],
        hook_events.SUBAGENT_STOP: [{"hooks": [{"type": "command", "command": cmd}]}],
    })
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto, hooks=runner)
    # A model the sub-agent will run: just reply 'sub-done'.
    harness = _make_harness(
        FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="sub-done")])), deps
    )
    out = await harness.subagents.run("helper", "do a thing", "stream-1")
    assert "sub-done" in out
    lines = log.read_text().splitlines()
    assert "SubagentStart" in lines
    assert "SubagentStop" in lines


# ---------------------------------------------------------------------------
# Task 9: Stop hook at turn end; SessionStart/SessionEnd wired into entry points
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_stop_fires_at_turn_end(tmp_path):
    log = tmp_path / "stop.log"
    cmd = _hook_script(tmp_path, "stop.sh", f"cat >> {log}\n")
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto,
                hooks=HookRunner(
                    {hook_events.STOP: [{"hooks": [{"type": "command", "command": cmd}]}]}
                ))
    # Use a streaming-capable model: hooks configure a hooked_handler that forces
    # streaming mode (same discipline as test_pre_and_post_tool_use_fire).
    sink: list = []
    harness = _make_harness(_prompt_capturing_model(sink), deps)
    out = await harness.run_turn("anything")
    assert out == "ok"
    assert '"hook_event_name": "Stop"' in log.read_text()


@pytest.mark.anyio
async def test_session_end_fires(tmp_path):
    log = tmp_path / "end.log"
    cmd = _hook_script(tmp_path, "end.sh", f"cat >> {log}\n")
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto,
                hooks=HookRunner(
                    {hook_events.SESSION_END: [{"hooks": [{"type": "command", "command": cmd}]}]}
                ))
    harness = _make_harness(
        FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="x")])), deps
    )
    await harness.session_end("exit")
    assert '"hook_event_name": "SessionEnd"' in log.read_text()
    assert '"reason": "exit"' in log.read_text()


@pytest.mark.anyio
async def test_background_subagent_start_and_stop_fire(tmp_path):
    _make_subagent_def(tmp_path)
    log = tmp_path / "bg_sub.log"
    # Use a Python helper file to avoid bash single-quote escaping issues when
    # embedding the log path (same pattern as test_pre_and_post_tool_use_fire).
    helper = tmp_path / "bghook.py"
    helper.write_text(
        f"import sys, json\n"
        f"d = json.load(sys.stdin)\n"
        f"open({str(log)!r}, 'a').write(d['hook_event_name'] + '\\n')\n",
        encoding="utf-8",
    )
    cmd = _hook_script(
        tmp_path, "bgsub.sh",
        f"python3 {str(helper)}\n",
    )
    runner = HookRunner({
        hook_events.SUBAGENT_START: [{"hooks": [{"type": "command", "command": cmd}]}],
        hook_events.SUBAGENT_STOP: [{"hooks": [{"type": "command", "command": cmd}]}],
    })
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto, hooks=runner)
    # A model the sub-agent will run: just reply 'bg-done'.
    harness = _make_harness(
        FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="bg-done")])), deps
    )
    out = await harness.subagents.run_background("helper", "do a thing")
    assert "bg-done" in out
    lines = log.read_text().splitlines()
    assert "SubagentStart" in lines
    assert "SubagentStop" in lines


# ---------------------------------------------------------------------------
# Task 7: LspManager lifecycle wiring
# ---------------------------------------------------------------------------


def _minimal_harness(tmp_path: Path):
    """Build a Harness with the simplest valid wiring for lifecycle tests."""
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.deps import Deps
    from marim_harness.tools.provider import BuiltinToolProvider

    return Harness(
        TestModel(),
        BuiltinToolProvider(),
        Deps(workspace_root=tmp_path),
        instructions="test",
    )


def test_harness_wires_lsp_manager(tmp_path):
    h = _minimal_harness(tmp_path)
    assert isinstance(h.lsp, LspManager)
    assert h.deps.lsp is h.lsp


@pytest.mark.anyio
async def test_harness_aclose_shuts_down_lsp(tmp_path):
    h = _minimal_harness(tmp_path)
    closed = {"n": 0}

    async def fake_aclose():
        closed["n"] += 1

    h.lsp.aclose = fake_aclose  # type: ignore[method-assign]
    await h.aclose()
    assert closed["n"] == 1
