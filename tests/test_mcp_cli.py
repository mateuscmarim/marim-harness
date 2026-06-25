import io
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


def _run(argv, **kw):
    out, err = io.StringIO(), io.StringIO()
    code = mcp_cmd.main(argv, out=out, err=err, **kw)
    return code, out.getvalue(), err.getvalue()


def test_main_add_stdio_writes_project_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    code, out, err = _run(["add", "mddocs", "node", "x.js", "-e", "K=v"])
    assert code == 0, err
    data = json.loads((tmp_path / ".marim" / "mcp.json").read_text())["mcpServers"]
    assert data["mddocs"] == {"command": "node", "args": ["x.js"], "env": {"K": "v"}}
    # project-scope trust caveat surfaced on stderr
    assert "trust" in err.lower()


def test_main_add_http_user_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    code, out, err = _run([
        "add", "--transport", "http", "--scope", "user", "remote",
        "https://x/mcp", "-H", "Authorization: Bearer t",
    ])
    assert code == 0, err
    from marim_harness.mcp.config import global_mcp_config_path
    data = json.loads(global_mcp_config_path().read_text())["mcpServers"]
    assert data["remote"] == {"url": "https://x/mcp", "headers": {"Authorization": "Bearer t"}}


def test_main_add_duplicate_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    assert _run(["add", "a", "x"])[0] == 0
    code, out, err = _run(["add", "a", "y"])
    assert code == 1
    assert "already" in err.lower()


def test_main_add_validation_error_exits_2(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    code, out, err = _run(["add", "a", "x", "-H", "K: v"])  # header on stdio
    assert code == 2
    assert "http/sse" in err


def test_main_list_shows_source(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    _run(["add", "--scope", "user", "g", "x"])
    _run(["add", "--scope", "project", "p", "y"])
    code, out, err = _run(["list"])
    assert code == 0
    assert "g" in out and "user" in out
    assert "p" in out and "project" in out


def test_main_list_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    code, out, err = _run(["list"])
    assert code == 0
    assert "no" in out.lower()


def test_main_get_known_and_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    _run(["add", "--scope", "user", "g", "node", "x.js"])
    code, out, err = _run(["get", "g"])
    assert code == 0
    assert "node" in out and "user" in out
    code, out, err = _run(["get", "nope"])
    assert code == 1


def test_main_remove_present_and_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    _run(["add", "--scope", "user", "g", "x"])
    assert _run(["remove", "g"])[0] == 0
    code, out, err = _run(["remove", "g"])
    assert code == 1


def test_main_list_marks_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    global_path = mcp_config.global_mcp_config_path()
    mcp_config.add_server(global_path, "enabled_srv", {"command": "x"})
    mcp_config.add_server(global_path, "disabled_srv", {"command": "y", "enabled": False})
    code, out, err = _run(["list"])
    assert code == 0
    lines = {line.split()[0]: line for line in out.splitlines() if line.strip()}
    assert "(disabled)" in lines["disabled_srv"]
    assert "(disabled)" not in lines["enabled_srv"]


def test_main_no_subcommand_prints_help(tmp_path, monkeypatch):
    code, out, err = _run([])
    assert code == 2
