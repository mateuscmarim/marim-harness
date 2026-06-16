import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from marim_harness.mcp import (
    build_mcp_servers,
    disabled_server_names,
    load_mcp_config,
    make_approval_hook,
)
from marim_harness.permissions import Mode

# --- config loading & merging ---------------------------------------------


def _write(path: Path, servers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def test_load_merges_project_over_global(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    _write(
        tmp_path / "xdg" / "marim" / "mcp.json",
        {
            "files": {"command": "global-fs"},
            "shared": {"command": "from-global"},
        },
    )
    ws = tmp_path / "ws"
    _write(
        ws / ".marim" / "mcp.json",
        {
            "web": {"url": "https://example/mcp"},
            "shared": {"command": "from-project"},  # overrides global
        },
    )

    cfg = load_mcp_config(ws)
    assert set(cfg) == {"files", "shared", "web"}
    assert cfg["shared"]["command"] == "from-project"  # project wins
    assert cfg["files"]["command"] == "global-fs"
    assert cfg["web"]["url"] == "https://example/mcp"


def test_load_missing_files_is_empty(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert load_mcp_config(tmp_path / "ws") == {}


def test_load_ignores_malformed_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    ws = tmp_path / "ws"
    bad = ws / ".marim" / "mcp.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{ not json", encoding="utf-8")
    # A broken file is skipped, never fatal.
    assert load_mcp_config(ws) == {}


# --- config-disabled servers -----------------------------------------------


def test_disabled_server_names_picks_enabled_false():
    specs = {
        "on": {"command": "a"},
        "off": {"command": "b", "enabled": False},
        "default-on": {"command": "c", "enabled": True},
        "bad": "not-a-dict",
    }
    # Only an explicit ``enabled: false`` disables; absent/true/non-dict do not.
    assert disabled_server_names(specs) == {"off"}


def test_disabled_server_names_empty_when_none():
    assert disabled_server_names({"on": {"command": "a"}}) == set()


# --- server construction ---------------------------------------------------


def test_build_stdio_server_from_command():
    from pydantic_ai.mcp import MCPServerStdio

    servers, warnings = build_mcp_servers(
        {"files": {"command": "npx", "args": ["-y", "fs"], "env": {"A": "1"}}}
    )
    assert warnings == []
    (server,) = servers
    assert isinstance(server, MCPServerStdio)
    assert server.command == "npx"
    assert server.args == ["-y", "fs"]
    assert server.tool_prefix == "files"  # prefixed by its config name
    assert server.process_tool_call is not None  # gated


def test_build_http_server_from_url():
    from pydantic_ai.mcp import MCPServerStreamableHTTP

    servers, warnings = build_mcp_servers({"web": {"url": "https://example/mcp"}})
    assert warnings == []
    (server,) = servers
    assert isinstance(server, MCPServerStreamableHTTP)
    assert server.tool_prefix == "web"


def test_build_sse_server_when_type_sse():
    from pydantic_ai.mcp import MCPServerSSE

    servers, _ = build_mcp_servers(
        {"events": {"url": "https://example/sse", "type": "sse"}}
    )
    (server,) = servers
    assert isinstance(server, MCPServerSSE)
    assert server.tool_prefix == "events"


def test_build_skips_malformed_spec():
    servers, warnings = build_mcp_servers(
        {"good": {"command": "ok"}, "bad": {"nonsense": True}}
    )
    assert len(servers) == 1  # only the good one built
    assert servers[0].tool_prefix == "good"
    assert any("bad" in w for w in warnings)  # the bad one is reported, not fatal


# --- approval hook ---------------------------------------------------------


def _ctx(mode, request_approval=None):
    return SimpleNamespace(
        deps=SimpleNamespace(mode=mode, request_approval=request_approval)
    )


async def _runner(calls):
    async def call_tool(name, args):
        calls.append((name, args))
        return "RAN"

    return call_tool


@pytest.mark.anyio
async def test_hook_auto_runs_without_prompt(tmp_path: Path):
    calls: list = []
    hook = make_approval_hook("files", trusted=False)
    result = await hook(_ctx(Mode.auto), await _runner(calls), "read", {"p": "x"})
    assert result == "RAN"
    assert calls == [("read", {"p": "x"})]


@pytest.mark.anyio
async def test_hook_plan_denies(tmp_path: Path):
    calls: list = []
    hook = make_approval_hook("files", trusted=False)
    result = await hook(_ctx(Mode.plan), await _runner(calls), "write", {})
    assert calls == []  # never ran the tool
    assert "plan" in result.lower()


@pytest.mark.anyio
async def test_hook_ask_trusted_runs(tmp_path: Path):
    calls: list = []
    hook = make_approval_hook("files", trusted=True)
    result = await hook(_ctx(Mode.ask), await _runner(calls), "read", {})
    assert result == "RAN"
    assert calls == [("read", {})]


@pytest.mark.anyio
async def test_hook_ask_untrusted_prompts_and_runs_on_approve(tmp_path: Path):
    calls: list = []
    seen: list = []

    async def approve(call):
        seen.append((call.tool_name, call.args_as_dict()))
        return True

    hook = make_approval_hook("files", trusted=False)
    result = await hook(_ctx(Mode.ask, approve), await _runner(calls), "write", {"a": 1})
    assert result == "RAN"
    assert calls == [("write", {"a": 1})]
    # The user sees the server-prefixed name and the args.
    assert seen == [("files_write", {"a": 1})]


@pytest.mark.anyio
async def test_hook_ask_untrusted_denied_blocks(tmp_path: Path):
    from pydantic_ai import ToolDenied

    calls: list = []

    async def deny(call):
        return ToolDenied("nope")

    hook = make_approval_hook("files", trusted=False)
    result = await hook(_ctx(Mode.ask, deny), await _runner(calls), "write", {})
    assert calls == []  # never ran
    assert "denied" in result.lower() or "reject" in result.lower()


@pytest.mark.anyio
async def test_hook_ask_untrusted_no_callback_denies(tmp_path: Path):
    calls: list = []
    hook = make_approval_hook("files", trusted=False)
    result = await hook(_ctx(Mode.ask, None), await _runner(calls), "write", {})
    assert calls == []
    assert "approval" in result.lower() or "denied" in result.lower()
