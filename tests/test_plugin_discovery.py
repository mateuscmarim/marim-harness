import json
from pathlib import Path

from marim_harness.plugins.discovery import (
    discover_plugins,
    has_executable,
    plugin_agent_roots,
    plugin_bundle_summary,
    plugin_hook_entries,
    plugin_instruction_texts,
    plugin_mcp_specs,
    plugin_skill_roots,
)
from marim_harness.plugins.manifest import load_manifest
from marim_harness.plugins.state import InstalledPlugin, load_state, save_state


def _make_plugin(plugins_dir: Path, name: str, *, manifest: dict, files: dict):
    pdir = plugins_dir / name
    (pdir / ".marim-plugin").mkdir(parents=True, exist_ok=True)
    (pdir / ".marim-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, **manifest}), encoding="utf-8"
    )
    for rel, content in files.items():
        fp = pdir / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
    return pdir


def _install(plugins_dir: Path, name: str, **kw):
    state = load_state(plugins_dir)
    state[name] = InstalledPlugin(name=name, version=None, source={"type": "local"}, **kw)
    save_state(plugins_dir, state)


def _ws(tmp_path, monkeypatch):
    # Isolate both scopes inside tmp_path.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def test_discover_enabled_and_disabled(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _make_plugin(gdir, "p1", manifest={}, files={})
    _install(gdir, "p1", enabled=True)
    found = discover_plugins(ws)
    assert [p.name for p in found] == ["p1"]
    assert found[0].scope == "global"
    assert found[0].enabled is True


def test_project_shadows_global(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    pdir = ws / ".marim" / "plugins"
    _make_plugin(gdir, "dup", manifest={"description": "global"}, files={})
    _install(gdir, "dup", enabled=True)
    _make_plugin(pdir, "dup", manifest={"description": "project"}, files={})
    _install(pdir, "dup", enabled=True)
    found = discover_plugins(ws)
    assert len(found) == 1
    assert found[0].scope == "project"
    assert found[0].manifest.description == "project"


def test_skill_and_agent_roots_only_enabled(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _make_plugin(gdir, "on", manifest={}, files={"skills/.keep": ""})
    _install(gdir, "on", enabled=True)
    _make_plugin(gdir, "off", manifest={}, files={"skills/.keep": ""})
    _install(gdir, "off", enabled=False)
    roots = dict(plugin_skill_roots(ws))
    assert "on" in roots and "off" not in roots
    assert roots["on"] == (gdir / "on" / "skills").resolve()
    assert dict(plugin_agent_roots(ws)).get("on") == (gdir / "on" / "agents").resolve()


def test_hooks_and_mcp_require_trust(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    hooks = {"hooks": {"Stop": [{"type": "command", "command": "${MARIM_PLUGIN_ROOT}/x.sh"}]}}
    _make_plugin(
        gdir, "untrusted",
        manifest={},
        files={"hooks/hooks.json": json.dumps(hooks),
               "mcp.json": json.dumps({"mcpServers": {"web": {"url": "https://u"}}})},
    )
    _install(gdir, "untrusted", enabled=True, trusted=False)
    assert plugin_hook_entries(ws) == {}
    assert plugin_mcp_specs(ws) == {}

    _install(gdir, "untrusted", enabled=True, trusted=True)
    entries = plugin_hook_entries(ws)
    assert entries["Stop"][0]["command"] == str((gdir / "untrusted").resolve()) + "/x.sh"
    specs = plugin_mcp_specs(ws)
    assert "untrusted_web" in specs and specs["untrusted_web"]["url"] == "https://u"


def test_instruction_texts(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _make_plugin(gdir, "p", manifest={}, files={"AGENTS.md": "do the thing"})
    _install(gdir, "p", enabled=True)
    assert plugin_instruction_texts(ws) == [("p", "do the thing")]


def test_bundle_summary_and_has_executable(tmp_path):
    pdir = _make_plugin(
        tmp_path, "p",
        manifest={},
        files={"skills/s/SKILL.md": "x", "hooks/hooks.json": json.dumps({"hooks": {"Stop": [{}]}})},
    )
    m = load_manifest(pdir)
    summary = plugin_bundle_summary(m)
    assert summary["skills"] == 1
    assert summary["hooks"] == 1
    assert has_executable(summary) is True
    assert has_executable({"skills": 2, "agents": 1, "hooks": 0, "mcpServers": 0}) is False


def test_registered_plugin_with_missing_dir_is_skipped(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _install(gdir, "ghost", enabled=True)  # registry entry, but no plugin dir created
    found = discover_plugins(ws)
    assert all(p.name != "ghost" for p in found)
