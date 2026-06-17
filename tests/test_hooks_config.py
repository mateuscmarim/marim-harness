import json
from pathlib import Path

import pytest

from marim_harness.hooks import config


def _write(path: Path, hooks: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path


def test_missing_files_yield_empty(xdg):
    assert config.load_hooks_config(xdg / "ws", trust_project=True) == {}


def test_loads_global(xdg):
    _write(config.global_hooks_config_path(), {"Stop": [{"hooks": []}]})
    cfg = config.load_hooks_config(xdg / "ws", trust_project=False)
    assert "Stop" in cfg


def test_project_ignored_without_trust(xdg):
    ws = xdg / "ws"
    _write(config.project_hooks_config_path(ws), {"Stop": [{"hooks": []}]})
    assert config.load_hooks_config(ws, trust_project=False) == {}


def test_project_loaded_with_trust(xdg):
    ws = xdg / "ws"
    _write(config.project_hooks_config_path(ws), {"Stop": [{"hooks": []}]})
    assert "Stop" in config.load_hooks_config(ws, trust_project=True)


def test_global_and_project_merge_per_event(xdg):
    ws = xdg / "ws"
    _write(config.global_hooks_config_path(), {"Stop": [{"hooks": [{"command": "g"}]}]})
    _write(config.project_hooks_config_path(ws), {"Stop": [{"hooks": [{"command": "p"}]}]})
    cfg = config.load_hooks_config(ws, trust_project=True)
    assert len(cfg["Stop"]) == 2  # both entries kept, concatenated


def test_malformed_file_is_skipped(xdg):
    p = config.global_hooks_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json", encoding="utf-8")
    assert config.load_hooks_config(xdg / "ws", trust_project=True) == {}
