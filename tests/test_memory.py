from pathlib import Path

import pytest

from marim_harness import memory


def test_global_scope_respects_xdg(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    sc = memory.global_scope()
    assert sc.name == "global"
    assert sc.root == tmp_path / "cfg" / "marim" / "memory"


def test_project_scope_is_under_workspace(tmp_path: Path):
    sc = memory.project_scope(tmp_path)
    assert sc.name == "project"
    assert sc.root == tmp_path / ".marim" / "memory"


def test_slugify_normalizes():
    assert memory._slugify("Hello World") == "hello-world"
    assert memory._slugify("Build uses uv!") == "build-uses-uv"
    assert memory._slugify("  Multiple   Spaces  ") == "multiple-spaces"
    assert memory._slugify("CamelCase/path") == "camelcase-path"
    assert memory._slugify("") == "memory"


def test_load_index_absent_returns_none(tmp_path: Path):
    sc = memory.project_scope(tmp_path)
    assert memory.load_index(sc) is None


def test_load_index_empty_returns_none(tmp_path: Path):
    sc = memory.project_scope(tmp_path)
    sc.root.mkdir(parents=True)
    (sc.root / "MEMORY.md").write_text("   \n  \n")
    assert memory.load_index(sc) is None


def test_save_memory_writes_file_with_frontmatter(tmp_path: Path):
    sc = memory.project_scope(tmp_path)
    path = memory.save_memory(
        sc,
        name="Build tool",
        description="The project builds with uv",
        mem_type="project",
        body="Use `uv run` for everything.",
        title="Build tool",
    )
    assert path.exists()
    text = path.read_text()
    assert text.startswith("---\n")
    assert "name: build-tool" in text
    assert "description: The project builds with uv" in text
    assert "type: project" in text
    assert "Use `uv run` for everything." in text


def test_save_memory_creates_missing_dir(tmp_path: Path):
    sc = memory.project_scope(tmp_path)
    assert not sc.root.exists()
    memory.save_memory(
        sc, name="x", description="d", mem_type="reference", body="b", title="X"
    )
    assert sc.root.is_dir()


def test_save_memory_appends_index_line(tmp_path: Path):
    sc = memory.project_scope(tmp_path)
    memory.save_memory(
        sc, name="Build tool", description="d", mem_type="project",
        body="b", title="Build tool",
    )
    index = (sc.root / "MEMORY.md").read_text()
    assert "[Build tool](build-tool.md)" in index


def test_save_memory_upserts_index_no_duplicate(tmp_path: Path):
    sc = memory.project_scope(tmp_path)
    memory.save_memory(
        sc, name="Build tool", description="first", mem_type="project",
        body="b1", title="Build tool",
    )
    memory.save_memory(
        sc, name="Build tool", description="second", mem_type="project",
        body="b2", title="Build tool",
    )
    index = (sc.root / "MEMORY.md").read_text()
    assert index.count("build-tool.md") == 1
    # the hook reflects the latest description
    assert "second" in index
    # the file body was overwritten
    assert "b2" in (sc.root / "build-tool.md").read_text()


def test_save_memory_index_line_carries_hook(tmp_path: Path):
    sc = memory.project_scope(tmp_path)
    memory.save_memory(
        sc, name="API key", description="stored in .env", mem_type="reference",
        body="b", title="API key",
    )
    index = (sc.root / "MEMORY.md").read_text()
    assert "— stored in .env" in index


def test_read_memory_returns_body(tmp_path: Path):
    sc = memory.project_scope(tmp_path)
    memory.save_memory(
        sc, name="My name", description="hook", mem_type="user",
        body="The user is Mateus Coutinho Marim.", title="My name",
    )
    # by title (what the index shows) and by raw slug — both resolve.
    assert "Mateus Coutinho Marim" in memory.read_memory(sc, "My name")
    assert "Mateus Coutinho Marim" in memory.read_memory(sc, "my-name")


def test_read_memory_missing_returns_notice(tmp_path: Path):
    sc = memory.project_scope(tmp_path)
    out = memory.read_memory(sc, "nope")
    assert "no project memory" in out.lower()


@pytest.mark.parametrize("scope_name", ["project", "global"])
def test_round_trip_index_lists_saved_memory(tmp_path, monkeypatch, scope_name):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    sc = (
        memory.global_scope()
        if scope_name == "global"
        else memory.project_scope(tmp_path)
    )
    memory.save_memory(
        sc, name="Note one", description="hook one", mem_type="user",
        body="body", title="Note one",
    )
    index = memory.load_index(sc)
    assert index is not None
    assert "note-one.md" in index
