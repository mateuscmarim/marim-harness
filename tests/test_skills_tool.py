from pathlib import Path

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from marim_harness.runtime.deps import Deps
from marim_harness.runtime.permissions import Mode
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import _make_deps


@pytest.fixture(autouse=True)
def _trust_project(monkeypatch):
    """Every test here exercises project-local ``.marim/skills``, which now load
    only in a TRUSTED workspace (see workspace.skills._project_trusted — a cloned
    untrusted repo's skills are gated). Run them as trusted."""
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "1")


def _agent() -> Agent:
    agent = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider().register(agent)
    return agent


def _make_skill(root: Path, name: str, *, description="A skill.", body="Do it.",
                files: dict[str, str] | None = None) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    for rel, content in (files or {}).items():
        f = d / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")


def _call_tool(tool_name: str, args: dict):
    """A FunctionModel that calls ``tool_name`` once, then echoes its return so a
    test can assert on the tool output."""
    state: dict = {}
    captured: dict = {}

    def model(messages, info):
        if not state:
            state["called"] = True
            return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args)])
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "ToolReturnPart":
                    captured["ret"] = str(p.content)
        return ModelResponse(parts=[TextPart(content=captured.get("ret", ""))])

    return FunctionModel(model), captured


def test_activate_skill_returns_body_and_dir(tmp_path: Path):
    _make_skill(tmp_path / ".marim" / "skills", "greet", body="Say hi warmly.")
    agent = _agent()
    model, captured = _call_tool("activate_skill", {"name": "greet"})
    with agent.override(model=model):
        agent.run_sync("go", deps=_make_deps(tmp_path, mode=Mode.ask))
    assert "Say hi warmly." in captured["ret"]
    assert "Skill directory:" in captured["ret"]
    assert str((tmp_path / ".marim" / "skills" / "greet").resolve()) in captured["ret"]


def test_activate_skill_unknown_name(tmp_path: Path):
    agent = _agent()
    model, captured = _call_tool("activate_skill", {"name": "missing"})
    with agent.override(model=model):
        agent.run_sync("go", deps=_make_deps(tmp_path, mode=Mode.ask))
    assert "No skill named" in captured["ret"]


def test_read_skill_file_returns_bundled_content(tmp_path: Path):
    _make_skill(
        tmp_path / ".marim" / "skills", "withref",
        files={"references/REFERENCE.md": "the deep detail"},
    )
    agent = _agent()
    model, captured = _call_tool(
        "read_skill_file", {"name": "withref", "path": "references/REFERENCE.md"}
    )
    with agent.override(model=model):
        agent.run_sync("go", deps=_make_deps(tmp_path, mode=Mode.ask))
    assert "the deep detail" in captured["ret"]


def test_read_skill_file_reaches_global_skill(tmp_path: Path, monkeypatch):
    """Global skills live outside the workspace; read_skill_file must still reach
    their bundled files (the read_file sandbox can't)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    _make_skill(
        tmp_path / "cfg" / "marim" / "skills", "glob-skill",
        files={"references/N.md": "global bundled note"},
    )
    agent = _agent()
    model, captured = _call_tool(
        "read_skill_file", {"name": "glob-skill", "path": "references/N.md"}
    )
    with agent.override(model=model):
        agent.run_sync("go", deps=_make_deps(tmp_path, mode=Mode.ask))
    assert "global bundled note" in captured["ret"]


def test_activate_skill_points_at_read_skill_file(tmp_path: Path):
    """activate_skill's output must name read_skill_file as the way to load
    bundled files, so the agent reaches for the right tool instead of blindly
    read_file-ing a relative link against the absolute skill dir."""
    _make_skill(tmp_path / ".marim" / "skills", "greet", body="Say hi warmly.")
    agent = _agent()
    model, captured = _call_tool("activate_skill", {"name": "greet"})
    with agent.override(model=model):
        agent.run_sync("go", deps=_make_deps(tmp_path, mode=Mode.ask))
    assert "read_skill_file" in captured["ret"]


def test_read_file_reaches_global_skill_bundled_file(tmp_path: Path, monkeypatch):
    """read_file with the absolute path of a global skill's bundled file works:
    even though the file is outside the workspace, the skill roots are whitelisted
    for reading (so an agent that reaches for read_file by path still succeeds)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    _make_skill(
        tmp_path / "cfg" / "marim" / "skills", "sdd",
        files={"implementer-prompt.md": "implementer instructions here"},
    )
    skill_dir = (tmp_path / "cfg" / "marim" / "skills" / "sdd").resolve()
    agent = _agent()
    model, captured = _call_tool(
        "read_file", {"path": str(skill_dir / "implementer-prompt.md")}
    )
    with agent.override(model=model):
        agent.run_sync("go", deps=_make_deps(ws, mode=Mode.ask))
    assert "implementer instructions here" in captured["ret"]


def test_skill_tools_are_not_approval_gated(tmp_path: Path):
    """activate_skill / read_skill_file only read marim/claude skill dirs, so they
    must run without an approval round (the run completes and echoes the body)."""
    _make_skill(tmp_path / ".marim" / "skills", "noapprove", body="ungated body")
    agent = _agent()
    model, captured = _call_tool("activate_skill", {"name": "noapprove"})
    with agent.override(model=model):
        agent.run_sync("go", deps=_make_deps(tmp_path, mode=Mode.ask))
    assert "ungated body" in captured["ret"]
