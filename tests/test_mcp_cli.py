import json
from pathlib import Path

import pytest

from marim_harness.interfaces.cli import mcp as mcp_cmd
from marim_harness.mcp import config as mcp_config


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["mcpServers"]


def test_add_server_creates_file(tmp_path):
    path = tmp_path / "sub" / "mcp.json"
    ok = mcp_config.add_server(path, "web", {"url": "https://x/mcp"})
    assert ok is True
    assert _read(path) == {"web": {"url": "https://x/mcp"}}
    # trailing newline + 2-space indent (matches persist_server_enabled output)
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_add_server_rejects_duplicate(tmp_path):
    path = tmp_path / "mcp.json"
    assert mcp_config.add_server(path, "web", {"url": "https://x/mcp"}) is True
    assert mcp_config.add_server(path, "web", {"url": "https://y/mcp"}) is False
    assert _read(path) == {"web": {"url": "https://x/mcp"}}  # unchanged


def test_add_server_overwrite(tmp_path):
    path = tmp_path / "mcp.json"
    mcp_config.add_server(path, "web", {"url": "https://x/mcp"})
    assert mcp_config.add_server(path, "web", {"url": "https://y/mcp"}, overwrite=True) is True
    assert _read(path) == {"web": {"url": "https://y/mcp"}}


def test_add_server_preserves_existing_servers(tmp_path):
    path = tmp_path / "mcp.json"
    mcp_config.add_server(path, "a", {"command": "x"})
    mcp_config.add_server(path, "b", {"command": "y"})
    assert set(_read(path)) == {"a", "b"}


def test_add_server_tolerates_malformed_file(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text("not json", encoding="utf-8")
    assert mcp_config.add_server(path, "web", {"url": "https://x/mcp"}) is True
    assert _read(path) == {"web": {"url": "https://x/mcp"}}


def test_remove_server_present_and_absent(tmp_path):
    path = tmp_path / "mcp.json"
    mcp_config.add_server(path, "a", {"command": "x"})
    mcp_config.add_server(path, "b", {"command": "y"})
    assert mcp_config.remove_server(path, "a") is True
    assert set(_read(path)) == {"b"}
    assert mcp_config.remove_server(path, "missing") is False


def test_remove_server_missing_file(tmp_path):
    assert mcp_config.remove_server(tmp_path / "nope.json", "a") is False


def test_read_servers_with_source_project_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    mcp_config.add_server(mcp_config.global_mcp_config_path(), "g", {"command": "x"})
    mcp_config.add_server(mcp_config.global_mcp_config_path(), "shared", {"command": "global"})
    mcp_config.add_server(mcp_config.project_mcp_config_path(ws), "p", {"command": "y"})
    mcp_config.add_server(mcp_config.project_mcp_config_path(ws), "shared", {"command": "proj"})
    result = mcp_config.read_servers_with_source(ws)
    assert result["g"] == ({"command": "x"}, "user")
    assert result["p"] == ({"command": "y"}, "project")
    assert result["shared"] == ({"command": "proj"}, "project")  # project wins


def test_build_spec_stdio():
    spec = mcp_cmd._build_spec(
        transport="stdio", rest=["node", "x.js", "--port"],
        headers=[], envs=["A=1", "B=2"], trust=False,
    )
    assert spec == {"command": "node", "args": ["x.js", "--port"], "env": {"A": "1", "B": "2"}}


def test_build_spec_stdio_minimal():
    spec = mcp_cmd._build_spec(
        transport="stdio", rest=["mddocs-mcp"], headers=[], envs=[], trust=False,
    )
    assert spec == {"command": "mddocs-mcp"}


def test_build_spec_http_with_header_and_trust():
    spec = mcp_cmd._build_spec(
        transport="http", rest=["https://x/mcp"],
        headers=["Authorization: Bearer t"], envs=[], trust=True,
    )
    assert spec == {"url": "https://x/mcp", "headers": {"Authorization": "Bearer t"}, "trust": True}


def test_build_spec_sse_sets_type():
    spec = mcp_cmd._build_spec(
        transport="sse", rest=["https://x/sse"], headers=[], envs=[], trust=False,
    )
    assert spec == {"url": "https://x/sse", "type": "sse"}


def test_build_spec_rejects_header_on_stdio():
    with pytest.raises(mcp_cmd.SpecError):
        mcp_cmd._build_spec(
            transport="stdio", rest=["node"], headers=["A: b"], envs=[], trust=False,
        )


def test_build_spec_rejects_env_on_http():
    with pytest.raises(mcp_cmd.SpecError):
        mcp_cmd._build_spec(
            transport="http", rest=["https://x/mcp"], headers=[], envs=["A=1"], trust=False,
        )


def test_build_spec_rejects_empty_rest():
    with pytest.raises(mcp_cmd.SpecError):
        mcp_cmd._build_spec(transport="stdio", rest=[], headers=[], envs=[], trust=False)


def test_build_spec_rejects_extra_url_positionals():
    with pytest.raises(mcp_cmd.SpecError):
        mcp_cmd._build_spec(
            transport="http", rest=["https://x/mcp", "junk"], headers=[], envs=[], trust=False,
        )


def test_parse_pairs_bad_token():
    with pytest.raises(mcp_cmd.SpecError):
        mcp_cmd._parse_pairs(["noequals"], "=", "env")
