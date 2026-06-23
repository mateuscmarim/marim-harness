from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.agent import Harness
from marim_harness.deps import Deps
from marim_harness.permissions import Mode
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import _last_instructions


def _last_user_prompt(messages) -> str:
    """The text of the most recent user-prompt part across the request list."""
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    text = ""
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    text = part.content
    return text


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
async def test_task_checklist_rides_in_turn_context_not_instructions(tmp_path: Path):
    captured: dict = {}

    def fn(messages, info):
        captured["instructions"] = _last_instructions(messages)
        captured["prompt"] = _last_user_prompt(messages)
        return ModelResponse(parts=[TextPart(content="ok")])

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(
        model=FunctionModel(fn), provider=BuiltinToolProvider(), deps=deps,
        instructions="BASE PROMPT",
    )

    # No tasks yet -> no checklist anywhere.
    await harness.run_turn("hi")
    assert "checklist" not in captured["instructions"].lower()
    assert "checklist" not in captured["prompt"].lower()

    # Setting tasks makes the next turn surface them in the user prompt
    # (turn-context), and keeps them OUT of the cached system instructions.
    deps.tasks.replace([
        {"text": "read the code", "status": "done"},
        {"text": "write the test", "status": "in_progress"},
    ])
    await harness.run_turn("hi again")
    assert "write the test" in captured["prompt"]
    assert "read the code" in captured["prompt"]
    assert "write the test" not in captured["instructions"]
    # Base system prompt is unaffected.
    assert "BASE PROMPT" in captured["instructions"]
