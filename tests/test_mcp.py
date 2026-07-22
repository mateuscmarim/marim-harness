import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from marim_harness.mcp import (
    build_mcp_servers,
    disabled_server_names,
    load_mcp_config,
    make_approval_hook,
    persist_server_enabled,
)
from marim_harness.mcp.manager import McpManager
from marim_harness.runtime.permissions import Mode

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

    cfg = load_mcp_config(ws, trust_project=True)
    assert set(cfg) == {"files", "shared", "web"}
    assert cfg["shared"]["command"] == "from-project"  # project wins
    assert cfg["files"]["command"] == "global-fs"
    assert cfg["web"]["url"] == "https://example/mcp"


def test_load_missing_files_is_empty(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert load_mcp_config(tmp_path / "ws", trust_project=True) == {}


def test_load_ignores_malformed_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    ws = tmp_path / "ws"
    bad = ws / ".marim" / "mcp.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{ not json", encoding="utf-8")
    # A broken file is skipped, never fatal.
    assert load_mcp_config(ws, trust_project=True) == {}


def test_project_servers_require_trust(tmp_path: Path, monkeypatch):
    # Project-local mcp.json launches subprocesses / connects on the user's behalf
    # at connect time, so it is honored only when the project is trusted — the same
    # gate as project hooks. An untrusted cloned repo can't auto-run its servers.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    ws = tmp_path / "ws"
    _write(ws / ".marim" / "mcp.json", {"evil": {"command": "sh", "args": ["-c", "x"]}})

    assert load_mcp_config(ws) == {}  # default: untrusted, project skipped
    assert "evil" in load_mcp_config(ws, trust_project=True)  # trusted: loaded


def test_global_servers_load_without_trust(tmp_path: Path, monkeypatch):
    # The user's own global config is always honored, trusted or not.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    _write(tmp_path / "xdg" / "marim" / "mcp.json", {"files": {"command": "fs"}})
    assert "files" in load_mcp_config(tmp_path / "ws")  # untrusted still loads global


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


# --- persisting toggles ----------------------------------------------------


def _read_back(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["mcpServers"]


def test_persist_writes_to_global_when_server_is_global(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    gpath = tmp_path / "xdg" / "marim" / "mcp.json"
    _write(gpath, {"mddocs": {"url": "https://x/mcp"}})
    ws = tmp_path / "ws"

    assert persist_server_enabled(ws, "mddocs", False) is True
    assert _read_back(gpath)["mddocs"]["enabled"] is False

    assert persist_server_enabled(ws, "mddocs", True) is True
    assert _read_back(gpath)["mddocs"]["enabled"] is True


def test_persist_prefers_project_when_server_defined_there(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    # The project file is only the winning definition when the project is
    # trusted (load_mcp_config skips it otherwise) — mirror that here.
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "1")
    gpath = tmp_path / "xdg" / "marim" / "mcp.json"
    _write(gpath, {"shared": {"command": "from-global"}})
    ws = tmp_path / "ws"
    ppath = ws / ".marim" / "mcp.json"
    _write(ppath, {"shared": {"command": "from-project"}})

    assert persist_server_enabled(ws, "shared", False) is True
    # The project file (the winning definition) is the one edited...
    assert _read_back(ppath)["shared"]["enabled"] is False
    # ...and the global one is left untouched.
    assert "enabled" not in _read_back(gpath)["shared"]


def test_persist_untrusted_project_collision_writes_to_global(tmp_path: Path, monkeypatch):
    """Regression: an UNTRUSTED repo's ``.marim/mcp.json`` naming a server that
    collides with a global one used to soak up the toggle — the write landed in
    the never-loaded project file, so disabling the live (global-sourced) server
    silently didn't stick across restarts. The write target must follow the same
    trust gate as ``load_mcp_config``: untrusted project file → persist to the
    global file that is actually loaded."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)
    gpath = tmp_path / "xdg" / "marim" / "mcp.json"
    _write(gpath, {"shared": {"command": "from-global"}})
    ws = tmp_path / "ws"
    ppath = ws / ".marim" / "mcp.json"
    _write(ppath, {"shared": {"command": "from-project"}})

    assert persist_server_enabled(ws, "shared", False) is True
    # The loaded (global) definition took the toggle...
    assert _read_back(gpath)["shared"]["enabled"] is False
    # ...and the untrusted, never-loaded project file was not touched.
    assert "enabled" not in _read_back(ppath)["shared"]


def test_persist_untrusted_project_only_server_is_not_persisted(tmp_path: Path, monkeypatch):
    """A server defined ONLY in an untrusted project file is never loaded, so
    there is nothing live to toggle — report False, write nowhere."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)
    ws = tmp_path / "ws"
    ppath = ws / ".marim" / "mcp.json"
    _write(ppath, {"proj-only": {"command": "x"}})

    assert persist_server_enabled(ws, "proj-only", False) is False
    assert "enabled" not in _read_back(ppath)["proj-only"]


def test_persist_explicit_trust_flag_wins_over_env(tmp_path: Path, monkeypatch):
    """An explicit ``trust_project`` decision overrides the env fallback, matching
    the shared ``project_trusted`` predicate's explicit-wins semantics."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)
    ws = tmp_path / "ws"
    ppath = ws / ".marim" / "mcp.json"
    _write(ppath, {"proj": {"command": "x"}})

    assert persist_server_enabled(ws, "proj", False, trust_project=True) is True
    assert _read_back(ppath)["proj"]["enabled"] is False


def test_manager_explicit_distrust_wins_over_env_on_toggle(tmp_path: Path, monkeypatch):
    """McpManager's own trust decision — not the env var — must decide where
    disable_server/enable_server persist. An embedder passing an explicit
    ``trust_project=False`` through HarnessBuilder must see that decision
    honored even when the process env says otherwise; load and persist must
    agree, which they only do if persist consults the SAME explicit flag the
    load path was built with, not a re-derived env read."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "1")
    gpath = tmp_path / "xdg" / "marim" / "mcp.json"
    _write(gpath, {"shared": {"command": "from-global"}})
    ws = tmp_path / "ws"
    ppath = ws / ".marim" / "mcp.json"
    _write(ppath, {"shared": {"command": "from-project"}})

    mgr = McpManager([], set(), trust_project=False)
    mgr.disable_server("shared", ws)

    # Explicit False wins over the truthy env var: the write must land in the
    # global file (the one load_mcp_config(trust_project=False) actually
    # reads), not the project file.
    assert _read_back(gpath)["shared"]["enabled"] is False
    assert "enabled" not in _read_back(ppath)["shared"]


def test_persist_preserves_other_servers_and_fields(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    gpath = tmp_path / "xdg" / "marim" / "mcp.json"
    _write(
        gpath,
        {
            "a": {"command": "x", "args": ["keep"]},
            "b": {"url": "https://y/mcp"},
        },
    )
    ws = tmp_path / "ws"

    persist_server_enabled(ws, "a", False)
    servers = _read_back(gpath)
    assert servers["a"] == {"command": "x", "args": ["keep"], "enabled": False}
    assert servers["b"] == {"url": "https://y/mcp"}  # sibling untouched


def test_persist_no_op_for_unknown_server(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    _write(tmp_path / "xdg" / "marim" / "mcp.json", {"a": {"command": "x"}})
    ws = tmp_path / "ws"
    # A name in no config file can't be persisted; report that, don't crash.
    assert persist_server_enabled(ws, "ghost", False) is False


def test_persist_uses_atomic_write_no_temp_residue(tmp_path: Path, monkeypatch):
    # Regression: persist used a bare path.write_text, which a crash mid-write
    # could truncate. It must go through atomic_write_text now — the result is a
    # valid, complete file with no leftover temp residue.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    gpath = tmp_path / "xdg" / "marim" / "mcp.json"
    _write(gpath, {"a": {"command": "x"}})
    ws = tmp_path / "ws"

    import marim_harness.mcp.config as cfg

    calls: list = []
    real_atomic = cfg.atomic_write_text

    def spy(path, text, **kw):
        calls.append(Path(path))
        return real_atomic(path, text, **kw)

    monkeypatch.setattr(cfg, "atomic_write_text", spy)
    assert persist_server_enabled(ws, "a", True) is True
    # The write went through the atomic path...
    assert gpath in calls
    # ...the file is valid and complete...
    assert _read_back(gpath)["a"]["enabled"] is True
    # ...and no deterministic temp residue was left behind.
    assert not (gpath.parent / "mcp.json.tmp").exists()


# --- server construction ---------------------------------------------------


def test_build_stdio_server_from_command():
    from fastmcp.client.transports import StdioTransport
    from pydantic_ai.mcp import MCPToolset

    servers, warnings = build_mcp_servers(
        {"files": {"command": "npx", "args": ["-y", "fs"], "env": {"A": "1"}}}
    )
    assert warnings == []
    (server,) = servers
    assert isinstance(server, MCPToolset)
    transport = server.client.transport
    assert isinstance(transport, StdioTransport)
    assert transport.command == "npx"
    assert transport.args == ["-y", "fs"]
    assert server.id == "files"  # config name; prefixing happens at compose time
    assert server.process_tool_call is not None  # gated


def test_build_stdio_server_gets_agent_tool_retry_budget():
    # MCP tools must get the same retry budget the agent gives its builtins
    # (retries=2). In pydantic-ai 2.x ``max_retries=None`` inherits the agent's
    # own ``retries`` — exactly that alignment. A hard-coded value here would
    # silently diverge if the agent's budget ever changed.
    servers, _ = build_mcp_servers({"files": {"command": "npx", "args": ["fs"]}})
    (server,) = servers
    assert server.max_retries is None


def test_build_http_server_from_url():
    from fastmcp.client.transports import StreamableHttpTransport
    from pydantic_ai.mcp import MCPToolset

    servers, warnings = build_mcp_servers({"web": {"url": "https://example/mcp"}})
    assert warnings == []
    (server,) = servers
    assert isinstance(server, MCPToolset)
    assert isinstance(server.client.transport, StreamableHttpTransport)
    assert server.id == "web"


def test_build_sse_server_when_type_sse():
    from fastmcp.client.transports import SSETransport

    servers, _ = build_mcp_servers(
        {"events": {"url": "https://example/sse", "type": "sse"}}
    )
    (server,) = servers
    assert isinstance(server.client.transport, SSETransport)
    assert server.id == "events"


def test_server_prompts_in_ask_predicate():
    """The runner decides a spawn's MCP reach up front, so it must be able to
    ask, per built server, "would this server's calls prompt in ask mode?".
    That is exactly the untrusted-hook case in make_approval_hook; the flag is
    stamped where the trust decision is made (build_mcp_servers) and read back
    through the server's process_tool_call field."""
    from marim_harness.mcp.config import server_prompts_in_ask

    servers, _ = build_mcp_servers(
        {"open": {"command": "x"}, "safe": {"command": "x", "trust": True}}
    )
    by_id = {s.id: s for s in servers}
    assert server_prompts_in_ask(by_id["open"]) is True
    assert server_prompts_in_ask(by_id["safe"]) is False
    # A hookless toolset (embedder-supplied via with_mcp_server) has no gate at
    # all — its calls run without prompting, so it must read as non-prompting.
    assert server_prompts_in_ask(SimpleNamespace(id="bare")) is False


def test_build_skips_malformed_spec():
    servers, warnings = build_mcp_servers(
        {"good": {"command": "ok"}, "bad": {"nonsense": True}}
    )
    assert len(servers) == 1  # only the good one built
    assert servers[0].id == "good"
    assert any("bad" in w for w in warnings)  # the bad one is reported, not fatal


def test_stdio_server_routes_stderr_off_terminal():
    """A stdio MCP server's stderr must not reach the parent terminal — otherwise
    a server's startup banner (e.g. '[@agentmemory/mcp] proxying to ...') paints
    over the TUI. The transport must carry a log_file destination that is not
    ``sys.stderr`` (fastmcp wires it to the child's stderr; a ``None`` log_file
    would inherit the terminal)."""
    import sys

    from marim_harness.mcp.config import mcp_stderr_log_path

    servers, _ = build_mcp_servers({"files": {"command": "echo", "args": ["hi"]}})
    (server,) = servers
    transport = server.client.transport
    log_file = transport.log_file
    assert log_file is not None  # None would inherit the terminal
    assert log_file is not sys.stderr
    # The happy path points at marim's capture file; the fallback is a writable
    # null device. Either keeps banners off the TUI.
    assert log_file == mcp_stderr_log_path() or hasattr(log_file, "write")
    # Children must be reaped deterministically on disconnect, not kept alive
    # for cross-session reuse marim never does.
    assert transport.keep_alive is False


# --- McpManager lifecycle --------------------------------------------------


@pytest.mark.anyio
async def test_aclose_resets_state_even_if_teardown_raises():
    """If a server's __aexit__ throws during shutdown (a dead/broken transport),
    aclose must still reset state and not leak the stack — otherwise a later
    reconnect early-returns and the subprocesses are never reaped."""

    class Bad:
        id = "bad"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            raise RuntimeError("dead transport")

    mgr = McpManager([Bad()], set())
    await mgr.connect()
    assert mgr._connected is True

    await mgr.aclose()  # must not raise despite the failing teardown
    assert mgr._connected is False
    assert mgr._mcp_stack is None
    assert mgr._live_servers == []
    assert mgr.mcp_status.connected == [] and mgr.mcp_status.failed == []


@pytest.mark.anyio
async def test_connect_not_marked_connected_when_interrupted():
    """A cancellation mid-connect must leave _connected False so a later connect
    retries — setting the flag before the work strands the manager."""

    class Boom:
        id = "boom"

        async def __aenter__(self):
            raise asyncio.CancelledError()

        async def __aexit__(self, *exc):
            return False

    mgr = McpManager([Boom()], set())
    with pytest.raises(asyncio.CancelledError):
        await mgr.connect()
    assert mgr._connected is False


@pytest.mark.anyio
async def test_connect_runs_concurrently_and_records_all_statuses():
    """connect() fans servers out concurrently (startup latency is the slowest
    server, not the sum), a single server's failure never aborts the rest, and
    every server's status is recorded in config order."""
    started: list[str] = []
    all_good_started = asyncio.Event()

    class Good:
        def __init__(self, name: str):
            self.id = name

        async def __aenter__(self):
            started.append(self.id)
            if len([s for s in started if s != "bad"]) == 3:
                all_good_started.set()
            # Block until every Good server has entered. A serial connect would
            # never start the 2nd before the 1st returns, so this only resolves
            # when the enters overlap — i.e. when they ran concurrently.
            await asyncio.wait_for(all_good_started.wait(), timeout=2.0)
            return self

        async def __aexit__(self, *exc):
            return False

    class Bad:
        id = "bad"

        async def __aenter__(self):
            raise RuntimeError("nope")

        async def __aexit__(self, *exc):
            return False

    servers = [Good("a"), Bad(), Good("b"), Good("c")]
    mgr = McpManager(servers, set())
    status = await mgr.connect()

    assert status["connected"] == ["a", "b", "c"]  # config order preserved
    assert [n for n, _ in status["failed"]] == ["bad"]  # the failure recorded
    assert "nope" in status["failed"][0][1]
    assert mgr._connected is True


@pytest.mark.anyio
async def test_connect_retry_after_partial_does_not_duplicate_toolsets():
    """A cancellation mid-connect leaves already-connected servers in _live_servers
    while _connected stays False. The retry must skip those (like enable_server
    does) — otherwise it re-enters them via _connect_one, which appends
    unconditionally, and live_toolsets() returns duplicates → duplicate tool names
    handed to agent.run."""

    class Good:
        id = "good"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class FlakyOnce:
        """Cancels the first connect (an interruption), succeeds on the retry."""

        id = "flaky"

        def __init__(self):
            self.attempts = 0

        async def __aenter__(self):
            self.attempts += 1
            if self.attempts == 1:
                raise asyncio.CancelledError()
            return self

        async def __aexit__(self, *exc):
            return False

    good = Good()
    flaky = FlakyOnce()
    mgr = McpManager([good, flaky], set())

    # First connect: good succeeds and lands in _live_servers, flaky's cancel
    # re-raises and leaves _connected False.
    with pytest.raises(asyncio.CancelledError):
        await mgr.connect()
    assert mgr._connected is False
    assert good in mgr._live_servers

    # Retry completes; good must not be re-entered.
    await mgr.connect()
    assert mgr._connected is True

    live = mgr.live_toolsets()
    names = sorted(McpManager.server_name(s) for s in live)
    assert names == ["flaky", "good"]  # no duplicate 'good'
    assert len({id(s) for s in live}) == 2
    assert flaky.attempts == 2  # flaky was retried, good was not re-entered
    await mgr.aclose()


# --- approval hook ---------------------------------------------------------


def _ctx(mode, request_approval=None):
    return SimpleNamespace(
        deps=SimpleNamespace(
            workspace=SimpleNamespace(mode=mode),
            ui=SimpleNamespace(request_approval=request_approval),
        )
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


@pytest.mark.anyio
async def test_hook_decodes_stringified_structured_arg(tmp_path: Path):
    """A model that stringifies a structured MCP arg gets it decoded from the tool's
    inputSchema before the server sees it."""
    calls: list = []
    tool = SimpleNamespace(
        name="save_plan",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "suites": {"type": "array", "items": {"type": "object"}},
            },
        },
    )

    async def list_tools():
        return [tool]

    holder = {"server": SimpleNamespace(list_tools=list_tools)}
    hook = make_approval_hook("pw", trusted=True, schema_holder=holder)
    args = {"name": "TodoMVC", "suites": '[{"title": "add"}]'}
    result = await hook(_ctx(Mode.auto), await _runner(calls), "save_plan", args)
    assert result == "RAN"
    assert calls == [("save_plan", {"name": "TodoMVC", "suites": [{"title": "add"}]})]


@pytest.mark.anyio
async def test_hook_without_holder_passes_args_through(tmp_path: Path):
    """No schema holder → no coercion (today's behavior)."""
    calls: list = []
    hook = make_approval_hook("pw", trusted=True)
    await hook(_ctx(Mode.auto), await _runner(calls), "save_plan", {"suites": "[1]"})
    assert calls == [("save_plan", {"suites": "[1]"})]


@pytest.mark.anyio
async def test_hook_schema_fetch_failure_falls_back(tmp_path: Path):
    """If the tool schema can't be fetched, dispatch uncoerced rather than erroring."""
    calls: list = []

    async def boom():
        raise RuntimeError("server down")

    holder = {"server": SimpleNamespace(list_tools=boom)}
    hook = make_approval_hook("pw", trusted=True, schema_holder=holder)
    await hook(_ctx(Mode.auto), await _runner(calls), "save_plan", {"suites": "[1]"})
    assert calls == [("save_plan", {"suites": "[1]"})]


# --- result bounding (context-flood protection) ----------------------------


def test_bound_tool_result_offloads_large_string(tmp_path: Path):
    from marim_harness.mcp.config import _bound_tool_result

    big = "x" * 60_000
    out = _bound_tool_result(big, label="files", name="read", args={"p": "x"},
                             workspace_root=tmp_path)
    assert isinstance(out, str)
    assert "saved to" in out and "preview" in out  # handle + preview, not the body
    assert len(out) < len(big)
    # the full body landed under .marim/output/
    offloaded = list((tmp_path / ".marim" / "output").glob("mcp-*.txt"))
    assert offloaded and offloaded[0].read_text() == big


def test_bound_tool_result_passes_small_string(tmp_path: Path):
    from marim_harness.mcp.config import _bound_tool_result

    out = _bound_tool_result("hi", label="files", name="read", args={}, workspace_root=tmp_path)
    assert out == "hi"


def test_bound_tool_result_offloads_large_structured(tmp_path: Path):
    from marim_harness.mcp.config import _bound_tool_result

    payload = {"rows": ["y" * 100 for _ in range(1000)]}  # well over the inline limit
    out = _bound_tool_result(payload, label="db", name="query", args={"q": "x"},
                             workspace_root=tmp_path)
    assert isinstance(out, str) and "saved to" in out


def test_bound_tool_result_keeps_small_structured(tmp_path: Path):
    from marim_harness.mcp.config import _bound_tool_result

    payload = {"ok": True, "n": 3}
    out = _bound_tool_result(payload, label="db", name="query", args={"q": "x"},
                             workspace_root=tmp_path)
    assert out is payload  # small structured content reaches the model intact


def test_bound_tool_result_passes_binary_through(tmp_path: Path):
    from pydantic_ai.messages import BinaryContent

    from marim_harness.mcp.config import _bound_tool_result

    img = BinaryContent(data=b"\x89PNG" + b"\x00" * 80_000, media_type="image/png")
    out = _bound_tool_result(img, label="cam", name="snap", args={}, workspace_root=tmp_path)
    assert out is img  # binary is never offloaded as text


@pytest.mark.anyio
async def test_hook_offloads_large_result(tmp_path: Path):
    """End to end: an auto-mode call whose server returns a huge body comes back
    as a handle + preview, not the raw flood."""
    from marim_harness.mcp.config import make_approval_hook

    async def call_tool(name, args):
        return "Z" * 60_000

    ctx = SimpleNamespace(
        deps=SimpleNamespace(
            workspace=SimpleNamespace(mode=Mode.auto, root=tmp_path),
            ui=SimpleNamespace(request_approval=None),
        )
    )
    hook = make_approval_hook("files", trusted=True)
    out = await hook(ctx, call_tool, "read", {})
    assert "saved to" in out and len(out) < 60_000


def test_bound_tool_result_distinct_args_dont_collide(tmp_path: Path):
    """Two large results from the same MCP tool with different args must land in
    different offload files — keying on the tool name alone would clobber one
    handle's content with the other's."""
    from marim_harness.mcp.config import _bound_tool_result

    a = "A" * 60_000
    b = "B" * 60_000
    out_a = _bound_tool_result(a, label="db", name="query",
                               args={"sql": "select a"}, workspace_root=tmp_path)
    out_b = _bound_tool_result(b, label="db", name="query",
                               args={"sql": "select b"}, workspace_root=tmp_path)
    files = sorted((tmp_path / ".marim" / "output").glob("mcp-*.txt"))
    assert len(files) == 2  # not clobbered into one
    bodies = {f.read_text() for f in files}
    assert bodies == {a, b}
    # the two handles point at different files
    assert out_a != out_b
