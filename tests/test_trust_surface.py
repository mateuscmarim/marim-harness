"""Project gated-surface scanning: what the trust dialog lists, and the
fingerprint that keys stored decisions."""

import json

from marim_harness.trust_surface import ProjectSurface, scan_project_surface


def _mk_project(tmp_path, *, hooks=None, mcp=None, skills=(), agents=()):
    marim = tmp_path / ".marim"
    marim.mkdir(exist_ok=True)
    if hooks is not None:
        (marim / "hooks.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    if mcp is not None:
        (marim / "mcp.json").write_text(json.dumps({"mcpServers": mcp}), encoding="utf-8")
    for name in skills:
        d = marim / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\n")
    for name in agents:
        d = marim / "agents"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.md").write_text("---\ndescription: x\n---\n")
    return tmp_path


def test_empty_workspace_is_empty_surface(tmp_path):
    s = scan_project_surface(tmp_path)
    assert isinstance(s, ProjectSurface)
    assert s.empty
    assert s.fingerprint  # even an empty surface fingerprints deterministically


def test_full_surface_enumerated(tmp_path):
    _mk_project(
        tmp_path,
        hooks={"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
        mcp={"docs": {"command": "python", "args": ["-m", "server"]}},
        skills=("deploy",), agents=("reviewer",),
    )
    s = scan_project_surface(tmp_path)
    assert not s.empty
    assert s.hook_events == ["SessionStart"]
    assert s.mcp_servers == ["docs"]
    assert s.skills == ["deploy"]
    assert s.agents == ["reviewer"]


def test_skills_alone_make_surface_nonempty(tmp_path):
    _mk_project(tmp_path, skills=("deploy",))
    assert not scan_project_surface(tmp_path).empty


def test_fingerprint_changes_on_executable_change_only(tmp_path):
    _mk_project(tmp_path, mcp={"docs": {"command": "python"}}, skills=("a",))
    fp1 = scan_project_surface(tmp_path).fingerprint
    # Editing a skill must NOT flip the fingerprint (inert content).
    skill_md = tmp_path / ".marim" / "skills" / "a" / "SKILL.md"
    skill_md.write_text("---\nname: a\ndescription: y\n---\n")
    assert scan_project_surface(tmp_path).fingerprint == fp1
    # Changing the MCP command MUST flip it.
    _mk_project(tmp_path, mcp={"docs": {"command": "python3"}})
    assert scan_project_surface(tmp_path).fingerprint != fp1


def test_malformed_configs_read_as_empty(tmp_path):
    marim = tmp_path / ".marim"
    marim.mkdir()
    (marim / "hooks.json").write_text("{broken", encoding="utf-8")
    (marim / "mcp.json").write_text("[]", encoding="utf-8")
    s = scan_project_surface(tmp_path)
    assert s.hook_events == [] and s.mcp_servers == []


def test_summary_names_counts(tmp_path):
    _mk_project(tmp_path, mcp={"docs": {"command": "python"}}, skills=("a", "b"))
    text = scan_project_surface(tmp_path).summary()
    assert "mcp: 1" in text and "docs" in text and "skills: 2" in text
