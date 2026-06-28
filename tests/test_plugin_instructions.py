"""Tests for plugin AGENTS.md injection into agent instructions."""
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from marim_harness.plugins import InstalledPlugin, save_state
from marim_harness.plugins.discovery import plugin_instruction_texts
from marim_harness.runtime.instructions import register_instructions

# ---------------------------------------------------------------------------
# Helper: build a minimal plugin directory with a manifest + AGENTS.md
# ---------------------------------------------------------------------------


def _make_plugin(plugins_dir: Path, name: str, agents_md_text: str) -> None:
    pdir = plugins_dir / name
    (pdir / ".marim-plugin").mkdir(parents=True, exist_ok=True)
    (pdir / ".marim-plugin" / "plugin.json").write_text(
        json.dumps({"name": name}), encoding="utf-8"
    )
    (pdir / "AGENTS.md").write_text(agents_md_text, encoding="utf-8")
    save_state(
        plugins_dir,
        {name: InstalledPlugin(name=name, version=None, source={"type": "local"}, enabled=True)},
    )


# ---------------------------------------------------------------------------
# Test (a): helper-data — plugin_instruction_texts wired correctly
# ---------------------------------------------------------------------------


def test_plugin_instruction_texts_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _make_plugin(gdir, "p", "plugin says hi")
    assert plugin_instruction_texts(ws) == [("p", "plugin says hi")]


# ---------------------------------------------------------------------------
# Fake objects for the behavioral closure test
# ---------------------------------------------------------------------------


class _FakeAgent:
    """Minimal stand-in for HarnessAgent that records registered closures."""

    def __init__(self) -> None:
        self._closures: list[Any] = []

    def instructions(self, fn):
        self._closures.append(fn)
        return fn


class _FakeMcpManager:
    def mcp_index_text(self) -> str:
        return ""


def _make_ctx(workspace_root: Path):
    """Build a minimal ctx-like object with ctx.deps.workspace_root."""
    deps = SimpleNamespace(workspace=SimpleNamespace(root=workspace_root))
    return SimpleNamespace(deps=deps)


# ---------------------------------------------------------------------------
# Test (b): behavioral — the _plugin_instructions closure actually injects text
# ---------------------------------------------------------------------------


def test_plugin_instructions_closure_injects_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    # Prepare a workspace with one enabled plugin that has an AGENTS.md.
    ws = tmp_path / "ws"
    ws.mkdir()
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _make_plugin(gdir, "my-plugin", "Always write docstrings.")

    # Register closures on the fake agent.
    fake_agent = _FakeAgent()
    register_instructions(fake_agent, _FakeMcpManager(), proactive_memory=False)

    # Find the _plugin_instructions closure by name.
    plugin_closure = None
    for fn in fake_agent._closures:
        if fn.__name__ == "_plugin_instructions":
            plugin_closure = fn
            break

    assert plugin_closure is not None, "_plugin_instructions closure was not registered"

    ctx = _make_ctx(ws)
    result = plugin_closure(ctx)

    assert "Always write docstrings." in result
    assert "my-plugin" in result
    assert "Instructions contributed by installed plugins" in result


def test_plugin_instructions_closure_returns_empty_when_no_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    ws = tmp_path / "ws"
    ws.mkdir()
    # No plugins installed at all.

    fake_agent = _FakeAgent()
    register_instructions(fake_agent, _FakeMcpManager(), proactive_memory=False)

    plugin_closure = None
    for fn in fake_agent._closures:
        if fn.__name__ == "_plugin_instructions":
            plugin_closure = fn
            break

    assert plugin_closure is not None, "_plugin_instructions closure was not registered"

    ctx = _make_ctx(ws)
    result = plugin_closure(ctx)

    assert result == ""
