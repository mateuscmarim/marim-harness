from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.agent import Harness
from marim_harness.deps import Deps
from marim_harness.permissions import Mode
from marim_harness.tools.provider import BuiltinToolProvider


def _edit_then_done_model() -> FunctionModel:
    """First model turn: call edit_file. After the tool result: reply 'done'."""
    state = {"n": 0}

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

    return FunctionModel(fn)


def _make_harness(model, deps) -> Harness:
    return Harness(model=model, provider=BuiltinToolProvider(), deps=deps,
                   instructions="You are a coding agent.")


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
    assert harness.total_tokens == 0
    await harness.run_turn("change foo to bar")
    after_first = harness.total_tokens
    assert after_first > 0
    await harness.run_turn("anything else")
    assert harness.total_tokens > after_first  # accumulates across turns


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
    assert usage.total_tokens == harness.total_tokens


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
    saved_count = len(first.history)
    saved_tokens = first.total_tokens

    # A brand-new harness on the same store resumes the prior conversation.
    second = Harness(
        model=_edit_then_done_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="x", store=store,
    )
    assert second.history == []  # nothing until we resume
    restored = second.resume()
    assert restored == saved_count
    assert len(second.history) == saved_count
    assert second.total_tokens == saved_tokens


def test_clean_title_strips_noise():
    from marim_harness.agent import clean_title

    assert clean_title('"Fix the bug"') == "Fix the bug"
    assert clean_title("Title: Add a feature") == "Add a feature"
    assert clean_title("Refactor parser\n\nignored") == "Refactor parser"
    assert clean_title("Do the thing.") == "Do the thing"
    assert clean_title("   ") == "Untitled session"


def test_clean_title_clamps_length():
    from marim_harness.agent import clean_title

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
    from marim_harness.session import SessionManager

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    store = manager.create(name)
    return Harness(
        model=_text_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="x", store=store, manager=manager, titler=titler,
    )


@pytest.mark.anyio
async def test_autoname_after_first_turn(tmp_path: Path):
    renames = []
    h = _autoname_harness(tmp_path, _fake_titler)
    h.on_rename = lambda old, new: renames.append((old, new))

    await h.run_turn("hello there")
    assert h.session_name == "Generated Title"
    assert h.store.auto_named is False
    assert renames and renames[-1][1] == "Generated Title"
    # persisted
    assert h.manager.store(h.store.session_id).name == "Generated Title"


@pytest.mark.anyio
async def test_explicitly_named_session_not_autorenamed(tmp_path: Path):
    h = _autoname_harness(tmp_path, _fake_titler, name="my project")
    await h.run_turn("hello")
    assert h.session_name == "my project"


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
    assert h.session_name == "Title 1"


@pytest.mark.anyio
async def test_rename_session_explicit_and_generated(tmp_path: Path):
    h = _autoname_harness(tmp_path, _fake_titler, name="start")
    # Explicit rename sets the name verbatim.
    assert await h.rename_session("Manual Name") == "Manual Name"
    assert h.session_name == "Manual Name"
    # Blank rename regenerates from the conversation via the titler.
    await h.run_turn("do work")
    assert await h.rename_session() == "Generated Title"
    assert h.session_name == "Generated Title"


def _last_instructions(messages) -> str:
    """The instructions attached to the current (most recent) request."""
    result = ""
    for message in messages:
        instr = getattr(message, "instructions", None)
        if instr:
            result = instr
    return result


@pytest.mark.anyio
async def test_project_instructions_injected_and_dynamic(tmp_path: Path):
    captured: dict = {}

    def fn(messages, info):
        captured["instructions"] = _last_instructions(messages)
        return ModelResponse(parts=[TextPart(content="ok")])

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
async def test_memory_indexes_injected_and_dynamic(tmp_path: Path):
    from marim_harness import memory

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
    alpha_id = harness.store.session_id

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
    harness.history = [ModelRequest(parts=[UserPromptPart(content="in alpha")])]
    harness._persist()
    alpha_id = harness.store.session_id

    # A fresh session starts empty without disturbing alpha.
    harness.new_session("beta")
    assert harness.session_name == "beta"
    assert harness.history == []
    harness._persist()

    names = {info.name for info in harness.sessions()}
    assert {"alpha", "beta"} <= names

    # Switching back restores alpha's conversation.
    restored = harness.switch_session(alpha_id)
    assert restored == 1
    assert harness.session_name == "alpha"
    assert harness.history[0].parts[0].content == "in alpha"


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
    harness.on_compact = lambda before, after: notices.append((before, after))

    # Seed a long prior history of clean user turns.
    for i in range(30):
        harness.history.append(
            ModelRequest(parts=[UserPromptPart(content=f"old prompt {i}")])
        )
        harness.history.append(ModelResponse(parts=[TextPart(content=f"old answer {i}")]))
    before = len(harness.history)

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
        harness.history.append(
            ModelRequest(parts=[UserPromptPart(content=f"old prompt {i}")])
        )
        harness.history.append(ModelResponse(parts=[TextPart(content=f"old answer {i}")]))

    await harness.run_turn("now do this")

    texts = [
        getattr(p, "content", "")
        for m in harness.history
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
    harness.on_compact = lambda before, after: notices.append((before, after))
    await harness.run_turn("change foo to bar")
    assert notices == []


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
    from marim_harness.session import SessionManager

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    return Harness(
        model=_named_model("startup"), provider=BuiltinToolProvider(), deps=deps,
        instructions="x", store=manager.create(), manager=manager,
        model_source=source, model_id="startup", summarizer=summarizer, titler=titler,
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
    old_summarizer, old_titler = h.summarizer, h.titler
    h.set_model("openai/gpt-5.2")
    assert h.summarizer is not old_summarizer  # repointed at the new model
    assert h.titler is not old_titler


@pytest.mark.anyio
async def test_set_model_leaves_unconfigured_aux_alone(tmp_path: Path):
    h = _switch_harness(tmp_path, source=_FakeSource())  # no summarizer/titler
    h.set_model("openai/gpt-5.2")
    assert h.summarizer is None  # not fabricated
    assert h.titler is None


def test_set_model_without_source_is_noop(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = Harness(model=_named_model("startup"), provider=BuiltinToolProvider(),
                deps=deps, instructions="x", model_id="startup")
    h.set_model("openai/gpt-5.2")  # no source -> nothing changes
    assert h.model_id == "startup"


def test_set_model_persists_to_session(tmp_path: Path):
    h = _switch_harness(tmp_path, source=_FakeSource())
    h.set_model("openai/gpt-5.2")
    assert h.store.model == "openai/gpt-5.2"
    assert h.manager.store(h.store.session_id).model == "openai/gpt-5.2"


@pytest.mark.anyio
async def test_switch_session_restores_its_model(tmp_path: Path):
    h = _switch_harness(tmp_path, source=_FakeSource())
    h.set_model("openai/gpt-5.2")
    alpha_id = h.store.session_id

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
    out = await h._run_subagent("explore", "find the parser", "sid")
    assert out == "FINDINGS"


@pytest.mark.anyio
async def test_run_subagent_counts_usage_in_session_total(tmp_path: Path):
    """A foreground spawn's own token spend lands in the session total, not just
    its returned report — counted immediately as the run completes."""
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(call_tools=[], custom_output_text="FINDINGS"), deps)
    assert h.total_tokens == 0
    await h._run_subagent("explore", "find the parser", "sid")
    assert h.total_tokens > 0


@pytest.mark.anyio
async def test_run_subagent_restricts_tools_by_mode(tmp_path: Path):
    from marim_harness.tools.provider import READ_TOOLS, SUBAGENT_TOOLS

    captured: dict = {}

    def fn(messages, info):
        captured["tools"] = {t.name for t in info.function_tools}
        return ModelResponse(parts=[TextPart(content="report")])

    deps = Deps(workspace_root=tmp_path, mode=Mode.ask)
    h = _make_harness(FunctionModel(fn), deps)

    # ask mode: general drops its gated tools, leaving the read-only set.
    out = await h._run_subagent("general", "do it", "sid")
    assert out == "report"
    assert captured["tools"] == set(READ_TOOLS)

    # auto mode: the full set, including write/edit/bash.
    deps.mode = Mode.auto
    await h._run_subagent("general", "do it", "sid")
    assert captured["tools"] == set(SUBAGENT_TOOLS)


@pytest.mark.anyio
async def test_subagent_handler_forwards_token_usage(tmp_path: Path):
    """The sub-agent event handler tags each forwarded event with the run's live
    total token count, so the UI can show it in the (collapsed) widget title."""
    from types import SimpleNamespace

    recorded: list = []

    async def cb(stream_id, event, tokens):
        recorded.append((stream_id, event, tokens))

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto, on_subagent_event=cb)
    h = _make_harness(_text_model(), deps)
    handler = h._subagent_handler("sid")

    async def events():
        yield "evt-a"
        yield "evt-b"

    ctx = SimpleNamespace(usage=SimpleNamespace(total_tokens=4096))
    await handler(ctx, events())

    assert recorded == [
        ("sid", "evt-a", 4096),
        ("sid", "evt-b", 4096),
    ]


@pytest.mark.anyio
async def test_subagent_handler_none_without_listener(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)  # no on_subagent_event
    h = _make_harness(_text_model(), deps)
    assert h._subagent_handler("sid") is None


@pytest.mark.anyio
async def test_run_subagent_unknown_type(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(_text_model(), deps)
    out = await h._run_subagent("ghost", "do it", "sid")
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
    out = await h._run_background_subagent("explore", "scan the repo")
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
    assert h.total_tokens == 0
    await h._run_background_subagent("explore", "scan the repo")
    assert h.total_tokens > 0
    # The spend reached disk immediately, without waiting for a run_turn.
    _, usage, _ = store.load()
    assert usage.total_tokens == h.total_tokens


@pytest.mark.anyio
async def test_run_background_subagent_unknown_type(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(_text_model(), deps)
    out = await h._run_background_subagent("ghost", "do it")
    assert "No sub-agent type 'ghost'" in out


@pytest.mark.anyio
async def test_run_background_subagent_respects_mode(tmp_path: Path):
    from marim_harness.tools.provider import READ_TOOLS, SUBAGENT_TOOLS

    captured: dict = {}

    def fn(messages, info):
        captured["tools"] = {t.name for t in info.function_tools}
        return ModelResponse(parts=[TextPart(content="r")])

    deps = Deps(workspace_root=tmp_path, mode=Mode.ask)
    h = _make_harness(FunctionModel(fn), deps)
    await h._run_background_subagent("general", "x")
    assert captured["tools"] == set(READ_TOOLS)
    deps.mode = Mode.auto
    await h._run_background_subagent("general", "x")
    assert captured["tools"] == set(SUBAGENT_TOOLS)


def test_background_agent_runner_wired(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(_text_model(), deps)
    assert deps.run_background_agent == h._run_background_subagent


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
    assert good in h._live_servers
    assert bad not in h._live_servers
    assert good.entered is True
    assert status["connected"] == ["good"]
    assert status["failed"] and status["failed"][0][0] == "bad"

    await h.aclose()
    assert good.entered is False  # connection closed on shutdown
    assert h._live_servers == []


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
    h._live_servers = [sentinel]

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
    assert on in h._live_servers
    assert off not in h._live_servers
    await h.aclose()


@pytest.mark.anyio
async def test_run_turn_omits_disabled_from_toolsets(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.usage import RunUsage

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(_text_model(), deps)
    live_on, live_off = _FakeServer("on"), _FakeServer("off")
    h._live_servers = [live_on, live_off]
    h.disabled = {"off"}

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
    assert "demo" in h.disabled
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
    assert "demo" not in h.disabled
    assert srv.entered is True  # connected on demand
    assert srv in h._live_servers
    assert "demo" in h.mcp_status["connected"]
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
    assert srv not in h._live_servers
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
    h._live_servers = [a, b]

    granted, unknown = h._granted_servers(["mddocs"])
    assert granted == [a]
    assert unknown == []


def test_granted_servers_none_grants_nothing(tmp_path: Path):
    from types import SimpleNamespace
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h._live_servers = [SimpleNamespace(tool_prefix="mddocs")]

    assert h._granted_servers(None) == ([], [])
    assert h._granted_servers([]) == ([], [])


def test_granted_servers_reports_unknown(tmp_path: Path):
    from types import SimpleNamespace
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h._live_servers = [SimpleNamespace(tool_prefix="mddocs")]

    granted, unknown = h._granted_servers(["mddocs", "nope"])
    assert granted == [h._live_servers[0]]
    assert unknown == ["nope"]


def test_granted_servers_excludes_disabled(tmp_path: Path):
    from types import SimpleNamespace
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h._live_servers = [SimpleNamespace(tool_prefix="mddocs")]
    h.disabled = {"mddocs"}

    granted, unknown = h._granted_servers(["mddocs"])
    assert granted == []
    assert unknown == ["mddocs"]


def test_granted_servers_dedupes(tmp_path: Path):
    from types import SimpleNamespace
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    a = SimpleNamespace(tool_prefix="mddocs")
    h._live_servers = [a]

    granted, unknown = h._granted_servers(["mddocs", "mddocs"])
    assert granted == [a]
    assert unknown == []
