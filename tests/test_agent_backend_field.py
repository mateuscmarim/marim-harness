from pathlib import Path

from marim_harness.workspace.agents import find_agent


def _write_agent(tmp_path: Path, name: str, frontmatter: str, body: str = "Do work.") -> None:
    d = tmp_path / ".marim" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")


def test_backend_and_model_parsed_from_frontmatter(tmp_path: Path):
    _write_agent(
        tmp_path, "cli-worker",
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
