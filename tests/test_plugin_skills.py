import json
from pathlib import Path

from marim_harness.plugins import InstalledPlugin, save_state
from marim_harness.workspace.skills import discover_skills, find_skill, skills_index_text


def _skill(root: Path, name: str, desc: str):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    content = f"---\nname: {name}\ndescription: {desc}\n---\nbody"
    (d / "SKILL.md").write_text(content, encoding="utf-8")


def _install_plugin_with_skill(plugins_dir: Path, plugin: str, skill: str):
    pdir = plugins_dir / plugin
    (pdir / ".marim-plugin").mkdir(parents=True, exist_ok=True)
    manifest = json.dumps({"name": plugin})
    (pdir / ".marim-plugin" / "plugin.json").write_text(manifest, encoding="utf-8")
    _skill(pdir / "skills", skill, "from plugin")
    plugin_entry = InstalledPlugin(
        name=plugin, version=None, source={"type": "local"}, enabled=True
    )
    save_state(plugins_dir, {plugin: plugin_entry})


def test_plugin_skill_is_namespaced(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _install_plugin_with_skill(gdir, "myplugin", "review")
    names = [s.qualified_name for s in discover_skills(ws)]
    assert "myplugin:review" in names
    found = find_skill(ws, "myplugin:review")
    assert found is not None and found.plugin == "myplugin"
    assert "- myplugin:review — from plugin" in skills_index_text(discover_skills(ws))


def test_project_plugin_skill_requires_project_trust(tmp_path, monkeypatch):
    """A project-scope plugin's skills ride the same trust gate as the project's
    own .marim/skills root: absent trust, a cloned repo's committed plugin must
    not inject skill descriptions into the prompt. (This suite runs with
    MARIM_TRUST_PROJECT_HOOKS=1 via conftest, so the explicit flag — which wins
    over the env — is what's exercised here.)"""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    _install_plugin_with_skill(ws / ".marim" / "plugins", "evil", "inject")

    untrusted = [s.qualified_name for s in discover_skills(ws, trust_project=False)]
    assert "evil:inject" not in untrusted
    assert find_skill(ws, "evil:inject", trust_project=False) is None

    trusted = [s.qualified_name for s in discover_skills(ws, trust_project=True)]
    assert "evil:inject" in trusted


def test_user_skill_beats_plugin_same_bare_name(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    (ws / ".marim" / "skills").mkdir(parents=True)
    _skill(ws / ".marim" / "skills", "review", "user owned")
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _install_plugin_with_skill(gdir, "myplugin", "review")
    by_name = {s.qualified_name: s for s in discover_skills(ws)}
    assert by_name["review"].description == "user owned"
    assert by_name["myplugin:review"].description == "from plugin"
