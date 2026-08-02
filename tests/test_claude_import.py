from pathlib import Path

from marim_harness.workspace import claude_import


def test_project_slug_replaces_slashes_and_dots():
    """Claude names its project dirs by replacing every `/` and `.` with `-`.
    The leading `-` falls out of the leading `/`. Paths here are absolute and
    non-existent, so the internal resolve() is an identity transform."""
    slug = claude_import.claude_project_slug("/home/x/Projects/marim.dev/marim-harness")
    assert slug == "-home-x-Projects-marim-dev-marim-harness"


def test_project_slug_doubles_dash_for_dot_directories():
    """`/.local` contributes both the separator dash and the dot dash, which is
    why real Claude dirs read `-home-x--local-share-...`."""
    slug = claude_import.claude_project_slug("/home/x/.local/share/fcstudio")
    assert slug == "-home-x--local-share-fcstudio"


def test_project_slug_normalizes_trailing_slash_and_dot_segments():
    assert claude_import.claude_project_slug("/home/x/proj/") == "-home-x-proj"
    assert claude_import.claude_project_slug("/home/x/a/../proj") == "-home-x-proj"


def test_config_dir_honors_env_override(tmp_path: Path):
    got = claude_import.claude_config_dir({"CLAUDE_CONFIG_DIR": str(tmp_path / "cc")})
    assert got == tmp_path / "cc"


def test_config_dir_ignores_blank_override_and_falls_back_to_home():
    got = claude_import.claude_config_dir({"CLAUDE_CONFIG_DIR": "   "})
    assert got == Path.home() / ".claude"


def test_config_dir_defaults_to_dot_claude_in_home():
    assert claude_import.claude_config_dir({}) == Path.home() / ".claude"


def test_memory_dir_composes_projects_slug_memory(tmp_path: Path):
    got = claude_import.claude_memory_dir(
        "/home/x/Projects/app", config_dir=tmp_path / "cc"
    )
    assert got == tmp_path / "cc" / "projects" / "-home-x-Projects-app" / "memory"


_FILE = """---
name: deploy-notes
description: How the deploy works.
metadata:
  node_type: memory
  type: project
  originSessionId: abc-123
  modified: 2026-07-28T18:12:01.714Z
---

Body line one.

A separator inside the body:

---

Body after the separator.
"""


def test_parse_memory_file_extracts_fields_and_keeps_body():
    got = claude_import.parse_memory_file(_FILE, slug="deploy-notes", title="Deploy notes")
    assert got is not None
    assert got.slug == "deploy-notes"
    assert got.title == "Deploy notes"
    assert got.description == "How the deploy works."
    assert got.mem_type == "project"
    assert got.body.startswith("Body line one.")
    assert "Body after the separator." in got.body


def test_parse_memory_file_tolerates_missing_description_and_type():
    text = "---\nname: x\n---\n\nBody.\n"
    got = claude_import.parse_memory_file(text, slug="x", title="X")
    assert got is not None
    assert got.description == ""
    assert got.mem_type == "project"


def test_parse_memory_file_rejects_text_without_frontmatter():
    assert claude_import.parse_memory_file("Just a note.\n", slug="x", title="X") is None


def test_parse_memory_file_rejects_non_mapping_frontmatter():
    got = claude_import.parse_memory_file("---\n- a\n- b\n---\nBody\n", slug="x", title="X")
    assert got is None


def _write_source(root, name, description, body, mem_type="project"):
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n"
        f"metadata:\n  type: {mem_type}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_read_source_recovers_titles_from_the_index(tmp_path: Path):
    src = tmp_path / "memory"
    _write_source(src, "alpha", "First fact.", "Alpha body.")
    _write_source(src, "beta", "Second fact.", "Beta body.")
    (src / "MEMORY.md").write_text(
        "# Memory Index\n\n"
        "- [Alpha Fact](alpha.md) — first\n"
        "- [Beta Fact](beta.md) — second\n",
        encoding="utf-8",
    )
    scan = claude_import.read_source(src)
    assert [(m.slug, m.title) for m in scan.memories] == [
        ("alpha", "Alpha Fact"),
        ("beta", "Beta Fact"),
    ]
    assert scan.problems == ()


def test_read_source_does_not_misattribute_a_title_containing_a_link(tmp_path: Path):
    """`index_entries` anchors on the FIRST `](...md)` of a line. An entry whose
    hook text mentions another memory's file must still resolve to its own slug,
    or the importer would hand `beta`'s title to `alpha`."""
    src = tmp_path / "memory"
    _write_source(src, "alpha", "First fact.", "Alpha body.")
    _write_source(src, "beta", "Second fact.", "Beta body.")
    (src / "MEMORY.md").write_text(
        "- [Alpha Fact](alpha.md) — see also [Beta](beta.md)\n"
        "- [Beta Fact](beta.md) — second\n",
        encoding="utf-8",
    )
    scan = claude_import.read_source(src)
    assert [(m.slug, m.title) for m in scan.memories] == [
        ("alpha", "Alpha Fact"),
        ("beta", "Beta Fact"),
    ]


def test_read_source_falls_back_to_the_slug_for_orphan_files(tmp_path: Path):
    """A memory file with no index entry still imports; its slug becomes the
    title, which is what a hand-written memory would have looked like anyway."""
    src = tmp_path / "memory"
    _write_source(src, "orphan", "No index line.", "Orphan body.")
    scan = claude_import.read_source(src)
    assert [(m.slug, m.title) for m in scan.memories] == [("orphan", "orphan")]


def test_read_source_skips_the_index_itself_and_reports_unparseable(tmp_path: Path):
    src = tmp_path / "memory"
    _write_source(src, "good", "Fine.", "Good body.")
    (src / "junk.md").write_text("no frontmatter here\n", encoding="utf-8")
    (src / "MEMORY.md").write_text("- [Good](good.md) — hook\n", encoding="utf-8")
    scan = claude_import.read_source(src)
    assert [m.slug for m in scan.memories] == ["good"]
    assert any("junk.md" in p for p in scan.problems)


def test_read_source_reports_undecodable_file(tmp_path: Path):
    src = tmp_path / "memory"
    _write_source(src, "good", "Fine.", "Good body.")
    (src / "binary.md").write_bytes(b"\xff\xfe\x00bad")
    scan = claude_import.read_source(src)
    assert [m.slug for m in scan.memories] == ["good"]
    assert any("binary.md" in p for p in scan.problems)


def test_read_source_on_empty_dir_is_empty(tmp_path: Path):
    src = tmp_path / "memory"
    src.mkdir()
    scan = claude_import.read_source(src)
    assert scan.memories == () and scan.problems == ()
