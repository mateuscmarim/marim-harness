import json
from pathlib import Path

from marim_harness import bootstrap
from marim_harness.permissions import Mode


def _stub_model_plumbing(monkeypatch):
    """Keep build_harness off the provider packages and any network."""
    from pydantic_ai.models.test import TestModel

    monkeypatch.setattr(bootstrap, "build_model", lambda cfg: TestModel())
    monkeypatch.setattr(bootstrap, "make_summarizer", lambda model: None)
    monkeypatch.setattr(bootstrap, "make_titler", lambda model: None)


def test_build_harness_wires_mcp_servers(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    _stub_model_plumbing(monkeypatch)

    ws = tmp_path / "ws"
    cfg = ws / ".marim" / "mcp.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps({"mcpServers": {"files": {"command": "npx", "args": ["fs"]}}}),
        encoding="utf-8",
    )

    harness = bootstrap.build_harness(ws, mode=Mode.ask)
    assert [s.tool_prefix for s in harness.mcp_servers] == ["files"]


def test_build_harness_no_mcp_config_is_empty(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    _stub_model_plumbing(monkeypatch)
    harness = bootstrap.build_harness(tmp_path / "ws", mode=Mode.ask)
    assert harness.mcp_servers == []
