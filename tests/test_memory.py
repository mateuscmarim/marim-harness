import threading
from pathlib import Path

import pytest

from marim_harness.workspace import memory


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


def test_slugify_transliterates_accents():
    assert memory._slugify("Nome do usuário") == "nome-do-usuario"
    assert memory._slugify("São Paulo") == "sao-paulo"
    # accented and unaccented spellings now collapse to the same slug
    assert memory._slugify("São Paulo") == memory._slugify("Sao Paulo")
    assert memory._slugify("Mateus Coutinho Marim") == "mateus-coutinho-marim"


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


def test_concurrent_save_memory_keeps_every_index_entry(tmp_path: Path):
    """Regression: the index read-modify-write was unlocked, so two concurrent
    save_memory calls each read the old index, added their line, and wrote —
    last writer wins, silently dropping the other entry. The advisory lock in
    _upsert_index_line serializes them so both survive."""
    sc = memory.project_scope(tmp_path)
    sc.root.mkdir(parents=True, exist_ok=True)
    names = [f"Fact {i}" for i in range(12)]

    def save(name: str):
        memory.save_memory(
            sc, name=name, description=f"desc {name}", mem_type="project",
            body="b", title=name,
        )

    threads = [threading.Thread(target=save, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    index = (sc.root / "MEMORY.md").read_text()
    for n in names:
        slug = memory._slugify(n)
        assert f"({slug}.md)" in index, f"lost index entry for {slug}"


def test_upsert_index_does_not_clobber_entry_whose_hook_links_elsewhere(tmp_path: Path):
    """A substring match (``](slug.md) in raw``) replaced the wrong line when
    another entry's hook text linked to ``slug.md``. Anchor to the entry's own
    link so only the real ``slug`` line is refreshed."""
    sc = memory.project_scope(tmp_path)
    sc.root.mkdir(parents=True, exist_ok=True)
    (sc.root / "MEMORY.md").write_text(
        "# Memory Index\n"
        "- [Build](build.md) — see [[auth]] at [link](auth.md) for the token flow\n",
        encoding="utf-8",
    )
    memory._upsert_index_line(sc, slug="auth", title="Auth", hook="how login works")
    index = (sc.root / "MEMORY.md").read_text()
    # The build entry, whose hook merely mentions auth.md, is left intact …
    assert "- [Build](build.md) — see [[auth]] at [link](auth.md) for the token flow" in index
    # … and the real auth entry is appended as its own line.
    assert "- [Auth](auth.md) — how login works" in index


def test_upsert_index_still_refreshes_the_matching_entry(tmp_path: Path):
    sc = memory.project_scope(tmp_path)
    sc.root.mkdir(parents=True, exist_ok=True)
    (sc.root / "MEMORY.md").write_text(
        "# Memory Index\n- [Auth](auth.md) — old hook\n", encoding="utf-8"
    )
    memory._upsert_index_line(sc, slug="auth", title="Auth", hook="new hook")
    index = (sc.root / "MEMORY.md").read_text()
    assert index.count("(auth.md)") == 1
    assert "new hook" in index and "old hook" not in index


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


def test_read_memory_resolves_pasted_slug(tmp_path: Path):
    """If the model pastes the slug from the index link, it resolves (slugify is
    idempotent on its own output)."""
    sc = memory.project_scope(tmp_path)
    sc.root.mkdir(parents=True)
    (sc.root / "build-tool.md").write_text("body text")
    assert "body text" in memory.read_memory(sc, "build-tool")


def test_read_memory_resolves_title_via_slugify(tmp_path: Path):
    sc = memory.project_scope(tmp_path)
    memory.save_memory(
        sc, name="Nome do usuário", description="hook", mem_type="user",
        body="O nome é Mateus.", title="Nome do usuário",
    )
    # passing the human title (with accent) still finds the file
    assert "O nome é Mateus." in memory.read_memory(sc, "Nome do usuário")


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
