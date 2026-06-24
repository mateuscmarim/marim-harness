"""MCP (Model Context Protocol) support: load server specs from a merged
config, build pydantic-ai MCP server toolsets, and gate their tool calls behind
marim's approval flow.

This module is TUI-free and testable on its own. The :class:`~marim_harness.agent.Harness`
owns the live connections (open with ``connect``, close with ``aclose``) and the
wiring lives in ``bootstrap`` (load + build) and the TUI app (connect on mount).

Config format (Claude-style), merged from the global ``~/.config/marim/mcp.json``
and the project's ``.marim/mcp.json`` (project wins by name)::

    {
      "mcpServers": {
        "files": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-fs"]},
        "web":   {"url": "https://example.com/mcp"},
        "events":{"url": "https://example.com/sse", "type": "sse"},
        "trusted-one": {"command": "...", "trust": true}
      }
    }

Each server is tool-prefixed with its config name, so its tools surface as
``<name>_<tool>`` and never collide with the builtins or each other.
"""

import json
import os
import warnings
from contextlib import asynccontextmanager
from pathlib import Path

from ..config import config_dir
from ..permissions import Mode


def mcp_stderr_log_path() -> Path:
    """Where stdio MCP servers' stderr is captured, off the terminal."""
    return config_dir() / "mcp-stderr.log"


def _open_mcp_stderr_log():
    """Open the capture file for an MCP server's stderr. A stdio server's stderr
    is otherwise inherited from the parent process, so a startup banner (e.g.
    ``[@agentmemory/mcp] proxying to ...``) prints straight onto the Textual TUI.
    Falls back to the null device if the log file can't be created."""
    try:
        path = mcp_stderr_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        return open(path, "a", encoding="utf-8")
    except OSError:
        return open(os.devnull, "w", encoding="utf-8")


def global_mcp_config_path() -> Path:
    """The global MCP config, a sibling of the global ``.env`` under the config dir."""
    return config_dir() / "mcp.json"


def project_mcp_config_path(workspace_root: Path) -> Path:
    """The project-local MCP config, under the workspace's ``.marim/`` directory."""
    return Path(workspace_root) / ".marim" / "mcp.json"


def _read_servers(path: Path) -> dict:
    """Read the ``mcpServers`` mapping from a config file. A missing or malformed
    file yields ``{}`` — a broken config is skipped, never fatal."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else {}


def load_mcp_config(workspace_root: Path, *, trust_project: bool = False) -> dict:
    """Merge MCP server specs into one name->spec mapping. Precedence, lowest
    first: enabled+trusted plugin servers (namespaced ``<plugin>_<server>``),
    then global, then project — so a user's own server wins on name.

    Project-local ``.marim/mcp.json`` servers launch subprocesses (stdio) or
    connect to endpoints on the user's behalf *at connect time*, before any
    tool-call approval gate applies — so a cloned, untrusted repo's config would
    otherwise run arbitrary commands on first launch. They are therefore honored
    only when ``trust_project`` is set (the same ``MARIM_TRUST_PROJECT_HOOKS``
    gate as project hooks). Plugin and global servers are the user's own and are
    always included (plugin servers carry their own enabled+trusted gate)."""
    from ..plugins import plugin_mcp_specs

    merged = dict(plugin_mcp_specs(workspace_root))
    merged.update(_read_servers(global_mcp_config_path()))
    if trust_project:
        merged.update(_read_servers(project_mcp_config_path(workspace_root)))
    return merged


def persist_server_enabled(workspace_root: Path, name: str, enabled: bool) -> bool:
    """Write ``enabled`` into the config file that defines ``name``, so a runtime
    toggle survives restarts. Prefers the project file (the winning definition)
    over the global one. Returns True if a file was updated, False if no config
    defines the server (nothing to persist). Best-effort: a missing or malformed
    file is treated as not defining the server."""
    for path in (project_mcp_config_path(workspace_root), global_mcp_config_path()):
        if name not in _read_servers(path):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["mcpServers"][name]["enabled"] = enabled
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, KeyError, TypeError):
            return False
        return True
    return False


def disabled_server_names(specs: dict) -> set[str]:
    """Names of servers turned off in the config via ``"enabled": false``. Only an
    explicit ``false`` disables — an absent or true flag (or a non-dict spec) keeps
    the server on. These seed the Harness's runtime ``disabled`` set so a
    file-disabled server is built but never launched, yet stays runtime-toggleable.
    """
    return {
        name
        for name, spec in specs.items()
        if isinstance(spec, dict) and spec.get("enabled") is False
    }


class _McpApprovalCall:
    """Minimal stand-in for a tool call, shaped for marim's ``request_approval``
    callback, which reads ``.tool_name`` and ``.args_as_dict()``."""

    def __init__(self, tool_name: str, args: dict) -> None:
        self.tool_name = tool_name
        self._args = args

    def args_as_dict(self) -> dict:
        return self._args


def make_approval_hook(label: str, trusted: bool):
    """Build a ``process_tool_call`` hook that gates an MCP server's tool calls by
    the live session mode: ``auto`` runs them, ``plan`` denies them (read-only),
    and ``ask`` runs a *trusted* server's calls but prompts for an *untrusted*
    one's via ``deps.request_approval``. A denied call returns a denial string,
    which the model receives as the tool result.

    ``label`` is the server's config name; it prefixes the tool name shown to the
    user so an approval prompt names which server is calling. The mode and the
    approval callback are read from ``ctx.deps`` at call time, so runtime mode
    switches take effect immediately."""

    async def hook(ctx, call_tool, name, args):
        deps = ctx.deps
        display = f"{label}_{name}"
        mode = getattr(deps, "mode", None)
        if mode is Mode.plan:
            return f"Denied: {display} is blocked in read-only plan mode."
        if mode is Mode.auto or trusted:
            return await call_tool(name, args)
        # ask mode against an untrusted server: prompt the user.
        approve = getattr(deps, "request_approval", None)
        if approve is None:
            return f"Denied: {display} needs approval but none is available here."
        decision = await approve(_McpApprovalCall(display, args or {}))
        if decision is True:
            return await call_tool(name, args)
        return f"Denied: the user rejected {display}."

    return hook


def build_mcp_servers(specs: dict) -> tuple[list, list[str]]:
    """Turn a name->spec mapping into pydantic-ai MCP server toolsets, each gated
    by an approval hook and tool-prefixed with its name.

    Returns ``(servers, warnings)``. A spec that is neither stdio (has
    ``command``) nor HTTP/SSE (has ``url``) is skipped with a warning instead of
    crashing, so one bad entry can't take down the rest."""
    # MCPServerStdio/StreamableHTTP/SSE are deprecated in favour of MCPToolset in
    # pydantic-ai 2.x, but they remain the only variants with the simple
    # command/url + tool_prefix kwargs this config maps onto. Silence the noisy
    # construction-time DeprecationWarning; revisit when MCPToolset gains prefix
    # support.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from pydantic_ai.mcp import (
            MCPServerSSE,
            MCPServerStdio,
            MCPServerStreamableHTTP,
        )

        class _QuietStdioServer(MCPServerStdio):
            """``MCPServerStdio`` that routes the child's stderr to a log file
            instead of inheriting the parent terminal. pydantic-ai builds
            ``stdio_client`` with the default ``errlog=sys.stderr``; we override
            ``client_streams`` to pass our own so server banners never paint the
            TUI. Resolves ``stdio_client`` off its module at call time so the
            errlog destination stays observable/patchable."""

            @asynccontextmanager
            async def client_streams(self):
                import mcp.client.stdio as mcp_stdio
                from mcp.client.stdio import StdioServerParameters

                params = StdioServerParameters(
                    command=self.command,
                    args=list(self.args),
                    env=self.env,
                    cwd=self.cwd,
                )
                errlog = _open_mcp_stderr_log()
                try:
                    async with mcp_stdio.stdio_client(
                        server=params, errlog=errlog
                    ) as streams:
                        yield streams
                finally:
                    errlog.close()

        servers: list = []
        notes: list[str] = []
        for name, spec in specs.items():
            if not isinstance(spec, dict):
                notes.append(f"MCP server {name!r}: spec must be an object; skipped.")
                continue
            hook = make_approval_hook(name, bool(spec.get("trust", False)))
            if "command" in spec:
                server = _QuietStdioServer(
                    command=spec["command"],
                    args=list(spec.get("args", [])),
                    env=spec.get("env"),
                    cwd=spec.get("cwd"),
                    tool_prefix=name,
                    process_tool_call=hook,
                )
            elif "url" in spec:
                kind = (
                    MCPServerSSE
                    if spec.get("type") == "sse"
                    else MCPServerStreamableHTTP
                )
                server = kind(
                    url=spec["url"],
                    headers=spec.get("headers"),
                    tool_prefix=name,
                    process_tool_call=hook,
                )
            else:
                notes.append(
                    f"MCP server {name!r}: needs 'command' or 'url'; skipped."
                )
                continue
            servers.append(server)
    return servers, notes
