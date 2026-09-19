from pathlib import Path

import pytest

from marim_harness.workspace.agents import find_agent


def _write_agent(tmp_path: Path, name: str, frontmatter: str, body: str = "Do work.") -> None:
    d = tmp_path / ".marim" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")


def test_backend_and_model_parsed_from_frontmatter(tmp_path: Path):
    _write_agent(
        tmp_path,
        "cli-worker",
        "description: CLI worker\nbackend: claude-cli\nmodel: opus\ntools: read_file, edit_file",
    )
    defn = find_agent(tmp_path, "cli-worker")
    assert defn is not None
    assert defn.backend == "claude-cli"
    assert defn.model == "opus"


def test_backend_defaults_to_native(tmp_path: Path):
    _write_agent(tmp_path, "plain", "description: Plain agent")
    defn = find_agent(tmp_path, "plain")
    assert defn is not None
    assert defn.backend == "native"
    assert defn.model is None


def test_builtins_are_native(tmp_path: Path):
    defn = find_agent(tmp_path, "explore")
    assert defn is not None and defn.backend == "native" and defn.model is None


def test_example_cli_agent_parses_as_claude_cli(tmp_path: Path):
    import shutil

    src = Path("docs/examples/agents/cli-worker.md")
    dst = tmp_path / ".marim" / "agents" / "cli-worker.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    defn = find_agent(tmp_path, "cli-worker")
    assert defn is not None
    assert defn.backend == "claude-cli"


# The tiered CLI workers (docs/examples/agents/<backend>-<tier>.md, for the
# claude-cli and codex-cli backends x fast/general/deep) are the copy-to-config
# examples the sub-agents guide points at; a frontmatter typo there would only
# surface for a user at spawn time, so parse them in CI.
_TIERED_EXAMPLES = [
    ("claude-fast", "claude-cli", "haiku", None),
    ("claude-general", "claude-cli", "sonnet", None),
    ("claude-deep", "claude-cli", "opus", None),
]


@pytest.mark.parametrize(("name", "backend", "model", "thinking"), _TIERED_EXAMPLES)
def test_tiered_cli_examples_parse(
    tmp_path: Path, name: str, backend: str, model: str, thinking: str | None
):
    import shutil

    dst = tmp_path / ".marim" / "agents" / f"{name}.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path("docs/examples/agents") / f"{name}.md", dst)
    defn = find_agent(tmp_path, name)
    assert defn is not None
    assert defn.backend == backend
    assert defn.model == model
    assert defn.thinking == thinking
    # Every tier can write: the codex sandbox and claude's tool set key off this.
    assert {"edit_file", "write_file", "bash"} <= set(defn.tools)
    assert defn.description
