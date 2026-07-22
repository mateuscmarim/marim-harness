"""Sub-agent thinking: frontmatter parsing (thinking:/effort:) and the runner's
per-spawn resolution (override → spec → inherited session level), asserted on
the built Agent's model_settings."""

from unittest.mock import MagicMock

from pydantic_ai.models.test import TestModel
from pydantic_ai.settings import ModelSettings

from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.runtime.permissions import Mode
from marim_harness.subagents.runner import SubagentRunner
from marim_harness.tools.provider import BuiltinToolProvider
from marim_harness.workspace.agents import AgentDef, _parse_agent


def test_frontmatter_thinking_field_parses(tmp_path):
    p = tmp_path / "coder.md"
    p.write_text(
        "---\ndescription: careful coder\nthinking: high\ntools: read_file\n"
        "---\nBe careful.\n",
        encoding="utf-8",
    )
    defn = _parse_agent("project", p)
    assert defn is not None
    assert defn.thinking == "high"


def test_frontmatter_effort_alias_parses_and_normalizes(tmp_path):
    p = tmp_path / "coder.md"
    p.write_text(
        "---\ndescription: careful coder\neffort: MEDIUM\ntools: read_file\n---\nGo.\n",
        encoding="utf-8",
    )
    defn = _parse_agent("project", p)
    assert defn is not None
    assert defn.thinking == "medium"


def test_frontmatter_unknown_thinking_is_dropped(tmp_path):
    p = tmp_path / "coder.md"
    p.write_text(
        "---\ndescription: careful coder\nthinking: ultra\ntools: read_file\n---\nGo.\n",
        encoding="utf-8",
    )
    defn = _parse_agent("project", p)
    assert defn is not None
    assert defn.thinking is None


def _runner(tmp_path, **kwargs) -> SubagentRunner:
    deps = Deps(workspace=WorkspaceConfig(root=tmp_path, mode=Mode.auto))
    return SubagentRunner(
        BuiltinToolProvider(), MagicMock(), deps, MagicMock(), MagicMock(),
        get_model=lambda: TestModel(call_tools=[]),
        model_settings=ModelSettings(parallel_tool_calls=True),
        **kwargs,
    )


def _spec(thinking: str | None) -> AgentDef:
    return AgentDef(
        name="coder", description="", prompt="Go.",
        tools=frozenset({"read_file"}), source="built-in", thinking=thinking,
    )


def test_spawn_override_beats_spec_and_inherited(tmp_path):
    runner = _runner(tmp_path, thinking_default=lambda: "low")
    sub, err = runner.build("coder", defn=_spec("medium"), thinking="high")
    assert err is None
    assert sub.model_settings["thinking"] == "high"


def test_spec_beats_inherited_when_no_override(tmp_path):
    runner = _runner(tmp_path, thinking_default=lambda: "low")
    sub, err = runner.build("coder", defn=_spec("medium"))
    assert sub.model_settings["thinking"] == "medium"


def test_inherited_session_level_when_no_override_or_spec(tmp_path):
    runner = _runner(tmp_path, thinking_default=lambda: "low")
    sub, err = runner.build("coder", defn=_spec(None))
    assert sub.model_settings["thinking"] == "low"


def test_off_resolution_leaves_base_settings_unchanged(tmp_path):
    runner = _runner(tmp_path, thinking_default=lambda: "high")
    sub, err = runner.build("coder", defn=_spec(None), thinking="off")
    assert "thinking" not in sub.model_settings


def test_no_thinking_anywhere_leaves_base_settings_unchanged(tmp_path):
    runner = _runner(tmp_path)  # no thinking_default
    sub, err = runner.build("coder", defn=_spec(None))
    assert "thinking" not in sub.model_settings
