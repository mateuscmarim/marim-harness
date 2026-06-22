import json
from pathlib import Path

import pytest

from marim_harness.plugins.manifest import (
    ManifestError,
    PluginManifest,  # noqa: F401
    load_manifest,
    substitute_root,
    try_load_manifest,
    valid_plugin_name,
)


def _write_manifest(plugin_dir: Path, data: dict) -> None:
    md = plugin_dir / ".marim-plugin"
    md.mkdir(parents=True, exist_ok=True)
    (md / "plugin.json").write_text(json.dumps(data), encoding="utf-8")


def test_load_minimal_manifest(tmp_path):
    _write_manifest(tmp_path, {"name": "my-plugin"})
    m = load_manifest(tmp_path)
    assert m.name == "my-plugin"
    assert m.version is None
    assert m.description == ""
    assert m.skills_dir() == (tmp_path / "skills").resolve()
    assert m.agents_dir() == (tmp_path / "agents").resolve()


def test_load_full_manifest_fields(tmp_path):
    _write_manifest(
        tmp_path,
        {
            "name": "full",
            "version": "1.2.0",
            "description": "does things",
            "author": {"name": "A", "email": "a@example.com"},
            "homepage": "https://h",
            "repository": "https://r",
            "license": "MIT",
            "keywords": ["x", "y"],
        },
    )
    m = load_manifest(tmp_path)
    assert m.version == "1.2.0"
    assert m.description == "does things"
    assert m.author == {"name": "A", "email": "a@example.com"}
    assert m.keywords == ["x", "y"]


def test_custom_component_paths(tmp_path):
    _write_manifest(
        tmp_path,
        {"name": "p", "skills": "./custom/skills/", "hooks": "./h/hooks.json"},
    )
    m = load_manifest(tmp_path)
    assert m.skills_dir() == (tmp_path / "custom" / "skills").resolve()
    assert m.hooks_source() == (tmp_path / "h" / "hooks.json").resolve()


def test_inline_mcp_servers(tmp_path):
    _write_manifest(tmp_path, {"name": "p", "mcpServers": {"web": {"url": "https://x"}}})
    m = load_manifest(tmp_path)
    assert m.mcp_source() == {"web": {"url": "https://x"}}


def test_missing_manifest_strict_raises(tmp_path):
    with pytest.raises(ManifestError):
        load_manifest(tmp_path)


def test_missing_name_strict_raises(tmp_path):
    _write_manifest(tmp_path, {"version": "1.0"})
    with pytest.raises(ManifestError):
        load_manifest(tmp_path)


def test_invalid_name_strict_raises(tmp_path):
    _write_manifest(tmp_path, {"name": "Bad Name"})
    with pytest.raises(ManifestError):
        load_manifest(tmp_path)


def test_path_traversal_rejected(tmp_path):
    _write_manifest(tmp_path, {"name": "p", "skills": "../../etc"})
    with pytest.raises(ManifestError):
        load_manifest(tmp_path)


def test_component_path_equal_to_root_rejected(tmp_path):
    _write_manifest(tmp_path, {"name": "p", "skills": "."})
    with pytest.raises(ManifestError):
        load_manifest(tmp_path)


def test_try_load_returns_none_on_bad(tmp_path):
    assert try_load_manifest(tmp_path) is None
    _write_manifest(tmp_path, {"name": "ok"})
    assert try_load_manifest(tmp_path).name == "ok"


def test_substitute_root_recurses(tmp_path):
    out = substitute_root(
        {"command": "${MARIM_PLUGIN_ROOT}/bin/x", "args": ["${MARIM_PLUGIN_ROOT}/y"]},
        Path("/plugins/p"),
    )
    assert out == {"command": "/plugins/p/bin/x", "args": ["/plugins/p/y"]}


def test_valid_plugin_name():
    assert valid_plugin_name("my-plugin")
    assert not valid_plugin_name("My-Plugin")
    assert not valid_plugin_name("-x")
    assert not valid_plugin_name("")
