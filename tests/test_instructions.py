from pathlib import Path

from marim_harness.instructions import load_project_instructions


def test_reads_agents_md(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("Use tabs, not spaces.\n")
    assert load_project_instructions(tmp_path) == "Use tabs, not spaces."


def test_missing_file_returns_none(tmp_path: Path):
    assert load_project_instructions(tmp_path) is None


def test_empty_or_whitespace_file_returns_none(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("   \n\t\n")
    assert load_project_instructions(tmp_path) is None


def test_custom_filename(tmp_path: Path):
    (tmp_path / ".marim.md").write_text("project rules")
    assert load_project_instructions(tmp_path, filename=".marim.md") == "project rules"


def test_unreadable_file_returns_none(tmp_path: Path):
    # A directory where the file is expected can't be read as text -> swallow.
    (tmp_path / "AGENTS.md").mkdir()
    assert load_project_instructions(tmp_path) is None
