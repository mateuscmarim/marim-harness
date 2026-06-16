from pathlib import Path

import pytest

from marim_harness.workspace import (
    discover_skills,
    find_skill,
    read_bundled_file,
    read_skill_body,
    skill_roots,
    skills_index_text,
)


def _make_skill(
    root: Path,
    name: str,
    description: str = "Does a thing. Use when the user wants the thing.",
    body: str = "Step 1. Do the thing.",
    extra_fm: str = "",
    files: dict[str, str] | None = None,
    fm_name: str | None = None,
) -> Path:
    """Create a skill directory with a SKILL.md and optional bundled files.
    ``fm_name`` overrides the frontmatter name (defaults to the dir name)."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    fm = (
        "---\n"
        f"name: {name if fm_name is None else fm_name}\n"
        f"description: {description}\n"
        f"{extra_fm}"
        "---\n"
    )
    (d / "SKILL.md").write_text(fm + "\n" + body + "\n", encoding="utf-8")
    for rel, content in (files or {}).items():
        f = d / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
    return d


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point the global roots (config dir + ~/.claude) at tmp so tests don't see
    the real user's skills."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


def test_skill_roots_order_and_precedence(tmp_path):
    ws = tmp_path / "ws"
    roots = skill_roots(ws)
    sources = [s for s, _ in roots]
    # project before global; marim before claude within a scope
    assert sources == ["project", "project/.claude", "global", "global/.claude"]
    assert roots[0][1] == ws / ".marim" / "skills"
    assert roots[1][1] == ws / ".claude" / "skills"


def test_discover_finds_project_marim_skill(isolated_home):
    ws = isolated_home / "ws"
    _make_skill(ws / ".marim" / "skills", "code-review", description="Reviews diffs.")
    skills = discover_skills(ws)
    assert len(skills) == 1
    assert skills[0].name == "code-review"
    assert skills[0].description == "Reviews diffs."
    assert skills[0].source == "project"


def test_discover_skips_dir_without_skill_md(isolated_home):
    ws = isolated_home / "ws"
    (ws / ".marim" / "skills" / "empty").mkdir(parents=True)
    assert discover_skills(ws) == []


def test_discover_skips_malformed_frontmatter(isolated_home):
    ws = isolated_home / "ws"
    d = ws / ".marim" / "skills" / "broken"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("no frontmatter here\n", encoding="utf-8")
    assert discover_skills(ws) == []


def test_discover_skips_invalid_yaml(isolated_home):
    ws = isolated_home / "ws"
    d = ws / ".marim" / "skills" / "bad-yaml"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: [unclosed\n---\nbody\n", encoding="utf-8")
    assert discover_skills(ws) == []


def test_discover_skips_missing_description(isolated_home):
    ws = isolated_home / "ws"
    d = ws / ".marim" / "skills" / "nodesc"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: nodesc\n---\nbody\n", encoding="utf-8")
    assert discover_skills(ws) == []


def test_discover_skips_name_dir_mismatch(isolated_home):
    ws = isolated_home / "ws"
    _make_skill(ws / ".marim" / "skills", "real-name", fm_name="other-name")
    assert discover_skills(ws) == []


def test_discover_skips_invalid_dir_name(isolated_home):
    ws = isolated_home / "ws"
    # Uppercase and a space are both illegal per the standard.
    _make_skill(ws / ".marim" / "skills", "Bad Name", fm_name="Bad Name")
    assert discover_skills(ws) == []


def test_discover_skips_consecutive_hyphens(isolated_home):
    ws = isolated_home / "ws"
    _make_skill(ws / ".marim" / "skills", "a--b", fm_name="a--b")
    assert discover_skills(ws) == []


def test_discover_claude_skills(isolated_home):
    ws = isolated_home / "ws"
    _make_skill(ws / ".claude" / "skills", "from-claude")
    skills = discover_skills(ws)
    assert [s.name for s in skills] == ["from-claude"]
    assert skills[0].source == "project/.claude"


def test_precedence_project_over_global(isolated_home, monkeypatch):
    ws = isolated_home / "ws"
    cfg = isolated_home / "xdg" / "marim" / "skills"
    _make_skill(ws / ".marim" / "skills", "dup", description="project version")
    _make_skill(cfg, "dup", description="global version")
    skills = discover_skills(ws)
    assert len(skills) == 1
    assert skills[0].source == "project"
    assert skills[0].description == "project version"


def test_precedence_marim_over_claude(isolated_home):
    ws = isolated_home / "ws"
    _make_skill(ws / ".marim" / "skills", "dup", description="marim version")
    _make_skill(ws / ".claude" / "skills", "dup", description="claude version")
    skills = discover_skills(ws)
    assert len(skills) == 1
    assert skills[0].source == "project"
    assert skills[0].description == "marim version"


def test_disable_model_invocation_parsed(isolated_home):
    ws = isolated_home / "ws"
    _make_skill(
        ws / ".marim" / "skills", "deploy",
        extra_fm="disable-model-invocation: true\n",
    )
    skill = find_skill(ws, "deploy")
    assert skill is not None
    assert skill.disable_model_invocation is True


def test_skills_index_text_excludes_disabled(isolated_home):
    ws = isolated_home / "ws"
    _make_skill(ws / ".marim" / "skills", "auto-one", description="auto desc")
    _make_skill(
        ws / ".marim" / "skills", "manual-one", description="manual desc",
        extra_fm="disable-model-invocation: true\n",
    )
    text = skills_index_text(discover_skills(ws))
    assert "auto-one" in text
    assert "auto desc" in text
    assert "manual-one" not in text


def test_skills_index_text_empty_when_none(isolated_home):
    ws = isolated_home / "ws"
    assert skills_index_text(discover_skills(ws)) == ""


def test_find_skill_unknown_returns_none(isolated_home):
    ws = isolated_home / "ws"
    _make_skill(ws / ".marim" / "skills", "present")
    assert find_skill(ws, "absent") is None


def test_read_skill_body_returns_full_file(isolated_home):
    ws = isolated_home / "ws"
    _make_skill(ws / ".marim" / "skills", "greet", body="Say hello warmly.")
    skill = find_skill(ws, "greet")
    text = read_skill_body(skill)
    assert "Say hello warmly." in text
    assert "name: greet" in text  # full SKILL.md, frontmatter included


def test_read_bundled_file(isolated_home):
    ws = isolated_home / "ws"
    _make_skill(
        ws / ".marim" / "skills", "withref",
        files={"references/REFERENCE.md": "deep detail here"},
    )
    skill = find_skill(ws, "withref")
    assert "deep detail here" in read_bundled_file(skill, "references/REFERENCE.md")


def test_read_bundled_file_rejects_escape(isolated_home):
    ws = isolated_home / "ws"
    _make_skill(ws / ".marim" / "skills", "guarded")
    skill = find_skill(ws, "guarded")
    out = read_bundled_file(skill, "../../../../etc/passwd")
    assert "outside" in out.lower()
    assert "root:" not in out


def test_read_bundled_file_missing(isolated_home):
    ws = isolated_home / "ws"
    _make_skill(ws / ".marim" / "skills", "nofile")
    skill = find_skill(ws, "nofile")
    assert "not a file" in read_bundled_file(skill, "references/nope.md").lower()


def test_skill_root_is_absolute_dir(isolated_home):
    ws = isolated_home / "ws"
    _make_skill(ws / ".marim" / "skills", "abs")
    skill = find_skill(ws, "abs")
    assert skill.root.is_absolute()
    assert (skill.root / "SKILL.md").is_file()
