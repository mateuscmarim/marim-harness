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
    messages, usage = store.load()
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
