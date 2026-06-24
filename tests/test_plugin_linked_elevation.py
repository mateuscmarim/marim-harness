"""Trusted *linked* plugin elevation drift.

A linked plugin loads from a live, mutable source dir every discovery. The
git-update path drops trust when an update introduces hooks/MCP (install.py), but
a linked source can grow executable surface silently with no such gate. Discovery
now re-checks the live surface against the ``executable_at_install`` baseline
recorded when trust was granted, and refuses to auto-honor newly-appeared
hooks/MCP (inert skills/agents/instructions still load).
"""

import json
from pathlib import Path

from marim_harness.plugins.discovery import (
    plugin_hook_entries,
    plugin_mcp_specs,
    plugin_skill_roots,
)
from marim_harness.plugins.state import InstalledPlugin, load_state, save_state


def _make_plugin(plugins_dir: Path, name: str, *, files: dict):
    pdir = plugins_dir / name
    (pdir / ".marim-plugin").mkdir(parents=True, exist_ok=True)
    (pdir / ".marim-plugin" / "plugin.json").write_text(
        json.dumps({"name": name}), encoding="utf-8"
    )
    for rel, content in files.items():
        fp = pdir / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
    return pdir


def _install(plugins_dir: Path, name: str, **kw):
    state = load_state(plugins_dir)
    source = kw.pop("source", {"type": "local"})
    state[name] = InstalledPlugin(name=name, version=None, source=source, **kw)
    save_state(plugins_dir, state)


def _ws(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


_HOOKS = {"hooks": {"Stop": [{"type": "command", "command": "${MARIM_PLUGIN_ROOT}/x.sh"}]}}
_MCP = {"mcpServers": {"web": {"url": "https://u"}}}


def test_linked_plugin_gaining_hooks_after_trust_is_not_honored(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    # Trusted at install time with NO executable surface (baseline False), plus a
    # skill so we can confirm inert contributions still load.
    _make_plugin(
        gdir, "linkdrift",
        files={
            "skills/demo/SKILL.md": "---\nname: demo\ndescription: d\n---\nx",
            "hooks/hooks.json": json.dumps(_HOOKS),   # appeared AFTER trust
            "mcp.json": json.dumps(_MCP),
        },
    )
    _install(
        gdir, "linkdrift", enabled=True, trusted=True, linked=True,
        source={"type": "local", "executable_at_install": False},
    )

    # Executable surface that appeared after trust must NOT be auto-honored.
    assert plugin_hook_entries(ws) == {}
    assert plugin_mcp_specs(ws) == {}
    # Inert contributions are unaffected.
    assert "linkdrift" in dict(plugin_skill_roots(ws))


def test_linked_plugin_executable_at_trust_still_honored(tmp_path, monkeypatch):
    """If hooks/MCP were already present (and vetted) when trust was granted,
    they keep loading — only *new* elevation is gated."""
    ws = _ws(tmp_path, monkeypatch)
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _make_plugin(
        gdir, "linkok",
        files={"hooks/hooks.json": json.dumps(_HOOKS), "mcp.json": json.dumps(_MCP)},
    )
    _install(
        gdir, "linkok", enabled=True, trusted=True, linked=True,
        source={"type": "local", "executable_at_install": True},
    )
    assert "Stop" in plugin_hook_entries(ws)
    assert "linkok_web" in plugin_mcp_specs(ws)


def test_install_records_executable_baseline_for_linked(tmp_path, monkeypatch):
    """A linked install records whether executable surface existed at trust time,
    so discovery has a baseline to detect later elevation."""
    from marim_harness.plugins import install_plugin

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    src = tmp_path / "src"
    (src / ".marim-plugin").mkdir(parents=True)
    (src / ".marim-plugin" / "plugin.json").write_text(
        json.dumps({"name": "lk", "version": "1.0.0"}), encoding="utf-8"
    )
    sk = src / "skills" / "demo"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nx", encoding="utf-8")

    rec = install_plugin(
        str(src), scope="global", workspace_root=ws, trust=True, link=True, now="T"
    )
    assert rec.linked is True
    assert rec.source.get("executable_at_install") is False


def test_non_linked_trusted_plugin_unaffected(tmp_path, monkeypatch):
    """A copied (non-linked) trusted plugin is immutable on disk between updates,
    so the linked-elevation guard never applies to it."""
    ws = _ws(tmp_path, monkeypatch)
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _make_plugin(gdir, "copied", files={"hooks/hooks.json": json.dumps(_HOOKS)})
    _install(
        gdir, "copied", enabled=True, trusted=True, linked=False,
        source={"type": "local"},  # no baseline key — not linked, so irrelevant
    )
    assert "Stop" in plugin_hook_entries(ws)
