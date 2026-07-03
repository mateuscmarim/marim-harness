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


_EXEC_FILES = {
    "hooks/hooks.json": json.dumps(
        {"hooks": {"Stop": [{"type": "command", "command": "${MARIM_PLUGIN_ROOT}/x.sh"}]}}
    ),
    "mcp.json": json.dumps({"mcpServers": {"web": {"url": "https://u"}}}),
}


def test_project_plugin_executables_require_project_trust(tmp_path, monkeypatch):
    """The supply-chain hole: a cloned repo can commit .marim/plugins/ with a
    registry marking a plugin enabled+trusted. That committed trust bit is
    attacker-controlled, so project-scope hooks/MCP must additionally require
    the MARIM_TRUST_PROJECT_HOOKS gate that guards .marim/hooks.json."""
    ws = _ws(tmp_path, monkeypatch)
    pdir = ws / ".marim" / "plugins"
    _make_plugin(pdir, "evil", manifest={}, files=_EXEC_FILES)
    _install(pdir, "evil", enabled=True, trusted=True)

    # Untrusted project (the default): the committed trust bit is not honored.
    assert plugin_hook_entries(ws) == {}
    assert plugin_mcp_specs(ws) == {}
    assert plugin_hook_entries(ws, trust_project=False) == {}
    assert plugin_mcp_specs(ws, trust_project=False) == {}

    # Trusted project: per-plugin trust applies as before.
    entries = plugin_hook_entries(ws, trust_project=True)
    assert entries["Stop"][0]["command"] == str((pdir / "evil").resolve()) + "/x.sh"
    assert "evil_web" in plugin_mcp_specs(ws, trust_project=True)


def test_global_plugin_executables_ignore_project_trust(tmp_path, monkeypatch):
    """Global plugins were installed by an explicit user action into the user's
    own config dir — the project trust gate must not silence them."""
    ws = _ws(tmp_path, monkeypatch)
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _make_plugin(gdir, "mine", manifest={}, files=_EXEC_FILES)
    _install(gdir, "mine", enabled=True, trusted=True)

    assert "Stop" in plugin_hook_entries(ws, trust_project=False)
    assert "mine_web" in plugin_mcp_specs(ws, trust_project=False)


def test_untrusted_project_plugin_keeps_inert_contributions(tmp_path, monkeypatch):
    """Skills/agents/instructions are inert text — they stay available from an
    untrusted project plugin; only the executable surface is withheld."""
    ws = _ws(tmp_path, monkeypatch)
    pdir = ws / ".marim" / "plugins"
    _make_plugin(
        pdir, "shared",
        manifest={},
        files={**_EXEC_FILES, "skills/s/SKILL.md": "x", "AGENTS.md": "read me"},
    )
    _install(pdir, "shared", enabled=True, trusted=True)

    assert "shared" in dict(plugin_skill_roots(ws))
    assert plugin_instruction_texts(ws) == [("shared", "read me")]
    assert plugin_hook_entries(ws, trust_project=False) == {}


def test_load_configs_gate_project_plugins(tmp_path, monkeypatch):
    """End to end: the merged hook/MCP configs used by bootstrap honor the gate."""
    from marim_harness.hooks.config import load_hooks_config
    from marim_harness.mcp.config import load_mcp_config

    ws = _ws(tmp_path, monkeypatch)
    pdir = ws / ".marim" / "plugins"
    _make_plugin(pdir, "evil", manifest={}, files=_EXEC_FILES)
    _install(pdir, "evil", enabled=True, trusted=True)

    assert load_hooks_config(ws, trust_project=False) == {}
    assert "evil_web" not in load_mcp_config(ws, trust_project=False)
    assert "Stop" in load_hooks_config(ws, trust_project=True)
    assert "evil_web" in load_mcp_config(ws, trust_project=True)


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


# --- stat-fingerprint discovery cache ---------------------------------------


def test_discover_plugins_caches_and_skips_reparse(tmp_path, monkeypatch):
    """A second discovery with nothing changed on disk is served from cache — the
    manifest json isn't re-parsed."""
    from marim_harness.plugins import discovery as discovery_mod

    ws = _ws(tmp_path, monkeypatch)
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _make_plugin(gdir, "p1", manifest={}, files={})
    _install(gdir, "p1", enabled=True)

    calls = {"n": 0}
    real = discovery_mod.try_load_manifest

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(discovery_mod, "try_load_manifest", counting)
    discover_plugins(ws)
    first = calls["n"]
    assert first >= 1
    discover_plugins(ws)
    assert calls["n"] == first  # no re-parse on the second call


def test_discover_plugins_reflects_manifest_edit(tmp_path, monkeypatch):
    """Editing a plugin's plugin.json invalidates the cache (mtime/size change)."""
    ws = _ws(tmp_path, monkeypatch)
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _make_plugin(gdir, "p", manifest={"description": "before"}, files={})
    _install(gdir, "p", enabled=True)
    assert discover_plugins(ws)[0].manifest.description == "before"
    # Rewrite the manifest with a longer description so the stat fingerprint moves.
    _make_plugin(gdir, "p", manifest={"description": "a clearly different after"}, files={})
    assert discover_plugins(ws)[0].manifest.description == "a clearly different after"


def test_discover_plugins_reflects_registry_change(tmp_path, monkeypatch):
    """Enabling a new plugin (a plugins.json edit) invalidates the cache even
    though existing manifests are untouched."""
    ws = _ws(tmp_path, monkeypatch)
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _make_plugin(gdir, "a", manifest={}, files={})
    _install(gdir, "a", enabled=True)
    assert {p.name for p in discover_plugins(ws)} == {"a"}
    _make_plugin(gdir, "b", manifest={}, files={})
    _install(gdir, "b", enabled=True)
    assert {p.name for p in discover_plugins(ws)} == {"a", "b"}


def test_instruction_texts_cache_invalidates_on_edit(tmp_path, monkeypatch):
    """plugin_instruction_texts re-reads a plugin's AGENTS.md when it changes."""
    ws = _ws(tmp_path, monkeypatch)
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    pdir = _make_plugin(gdir, "p", manifest={}, files={"AGENTS.md": "before text"})
    _install(gdir, "p", enabled=True)
    assert plugin_instruction_texts(ws) == [("p", "before text")]
    (pdir / "AGENTS.md").write_text("a longer after text", encoding="utf-8")
    assert plugin_instruction_texts(ws) == [("p", "a longer after text")]
