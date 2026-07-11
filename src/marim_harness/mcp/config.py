"""MCP (Model Context Protocol) support: load server specs from a merged
config, build pydantic-ai MCP server toolsets, and gate their tool calls behind
marim's approval flow.

This module is TUI-free and testable on its own. The :class:`~marim_harness.runtime.harness.Harness`
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

Each server carries its config name as the toolset ``id``; the ``<name>_<tool>``
prefixing the model sees is applied at compose time (``runtime.toolsets`` /
``McpManager.granted_toolsets``) via ``AbstractToolset.prefixed``, so raw
toolsets stay the single lifecycle/introspection handle here.
"""

import json
import os
from pathlib import Path

from fastmcp.client.transports import (
    SSETransport,
    StdioTransport,
    StreamableHttpTransport,
)
from pydantic_ai.mcp import MCPToolset

from ..atomic_io import atomic_write_text
from ..config import config_dir
from ..runtime.permissions import Mode
from ..tools.impl.coerce import coerce_by_schema
from ..tools.impl.offload import _INLINE_CHAR_LIMIT, offload_if_large


def _bound_tool_result(result, *, label: str, name: str, args: dict | None,
                       workspace_root: Path | None):
    """Bound an MCP tool result so a huge server response can't flood context.

    MCP results are an open union (``str | BinaryContent | dict | list | …``).
    Unlike the builtin tools — which build their output under our own byte budget
    — this payload is produced by a third-party server we don't control, so it's
    the one tool surface with no inherent size bound. We clamp the two real flood
    vectors and leave everything else untouched:

      * a plain ``str`` (the common case — a text content block) is routed through
        the shared :func:`offload_if_large`, so a large body is written to a file
        and replaced by a handle + preview the model can page;
      * structured content (``dict``) or a ``list`` of parts is sized by its JSON
        serialization and offloaded *whole* when it exceeds the inline limit. Big
        structured blobs are exactly the ones we don't want inline, and the model
        still gets a file handle to read.

    A bare ``BinaryContent`` (image/audio) is passed through verbatim — it isn't a
    text flood and the model needs the bytes. (A *mixed* list of binary + large
    text is rare; it falls into the JSON branch and offloads as text, so the
    binary part is lost — acceptable for that corner.) Anything we can't cheaply
    serialize is left as-is rather than risk corrupting it.

    The offload ``key`` includes ``args`` so two large results from the same tool
    with different arguments land in different files — keying on the tool name
    alone would clobber one handle with the other (the builtins key on inputs for
    the same reason: bash→command, grep→pattern)."""
    # Distinct file per (tool, args) so concurrent/sequential large results don't
    # clobber each other's offload handle.
    try:
        arg_key = json.dumps(args or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        arg_key = repr(args)
    key = f"{label}/{name}\0{arg_key}"
    if isinstance(result, str):
        return offload_if_large(
            result, kind="mcp", key=key, workspace_root=workspace_root
        )
    # Don't touch binary payloads — they aren't a text flood and must reach the
    # model intact. Import lazily so this module stays cheap to import.
    from pydantic_ai.messages import BinaryContent

    if isinstance(result, BinaryContent):
        return result
    if isinstance(result, (dict, list)):
        try:
            serialized = json.dumps(result, default=str)
        except (TypeError, ValueError):
            return result  # can't measure safely — leave the structure intact
        if len(serialized) > _INLINE_CHAR_LIMIT:
            return offload_if_large(
                serialized, kind="mcp", key=key, workspace_root=workspace_root,
            )
    return result


def mcp_stderr_log_path() -> Path:
    """Where stdio MCP servers' stderr is captured, off the terminal."""
    return config_dir() / "mcp-stderr.log"


def _mcp_stderr_log_target():
    """Where to point a stdio MCP server's stderr. A stdio child's stderr is
    otherwise inherited from the parent process, so a startup banner (e.g.
    ``[@agentmemory/mcp] proxying to ...``) prints straight onto the Textual TUI.

    Returns the log :class:`Path` when its directory is usable — fastmcp's
    ``StdioTransport`` opens a Path in append mode and owns the handle's
    lifecycle — and falls back to an opened null device otherwise (a ``TextIO``
    is used as-is, and leaking one devnull handle on this rare path is benign)."""
    try:
        path = mcp_stderr_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
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
    gate as project hooks). Global servers and global plugins are the user's own
    and are always included (plugin servers carry their own enabled+trusted
    gate); *project-scope* plugins ride the same ``trust_project`` gate as
    ``.marim/mcp.json``, since their registry — trust bit included — is
    committed to the repo."""
    from ..plugins import plugin_mcp_specs

    merged = dict(plugin_mcp_specs(workspace_root, trust_project=trust_project))
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
            # Atomic write (matching the rest of the project): a crash mid-write
            # must not truncate the user's mcp.json, which a bare write_text would.
            atomic_write_text(path, json.dumps(data, indent=2) + "\n")
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, KeyError, TypeError):
            return False
        return True
    return False


def add_server(path: Path, name: str, spec: dict, *, overwrite: bool = False) -> bool:
    """Write ``spec`` under ``name`` into the ``mcpServers`` map at ``path``,
    creating the file (and parent dir) if absent. A missing or malformed file is
    treated as empty. Returns False without writing if ``name`` already exists and
    ``overwrite`` is not set; otherwise writes atomically and returns True."""
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            data = {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    if name in servers and not overwrite:
        return False
    servers[name] = spec
    data["mcpServers"] = servers
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")
    return True


def remove_server(path: Path, name: str) -> bool:
    """Delete ``name`` from the ``mcpServers`` map at ``path`` and rewrite
    atomically. Returns False if the file is missing/malformed or has no such
    server (nothing removed)."""
    if name not in _read_servers(path):
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["mcpServers"][name]
        atomic_write_text(path, json.dumps(data, indent=2) + "\n")
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, KeyError, TypeError):
        return False
    return True


def read_servers_with_source(workspace_root: Path) -> dict[str, tuple[dict, str]]:
    """Map each user-editable server to ``(spec, source)`` where ``source`` is
    ``"user"`` (global file) or ``"project"`` (``.marim/mcp.json``). Project wins
    on a name clash, matching ``load_mcp_config`` precedence. Plugin-provided
    servers are not included — only the two files the CLI can edit."""
    result: dict[str, tuple[dict, str]] = {}
    for spec_name, spec in _read_servers(global_mcp_config_path()).items():
        result[spec_name] = (spec, "user")
    for spec_name, spec in _read_servers(project_mcp_config_path(workspace_root)).items():
        result[spec_name] = (spec, "project")
    return result


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


def make_approval_hook(label: str, trusted: bool, *, schema_holder: dict | None = None):  # noqa: C901  # complexity-debt: 2026-07-11 — see docs/superpowers/plans/2026-07-11-cyclomatic-complexity-reduction.md
    """Build a ``process_tool_call`` hook that gates an MCP server's tool calls by
    the live session mode: ``auto`` runs them, ``plan`` denies them (read-only),
    and ``ask`` runs a *trusted* server's calls but prompts for an *untrusted*
    one's via ``deps.request_approval``. A denied call returns a denial string,
    which the model receives as the tool result.

    ``label`` is the server's config name; it prefixes the tool name shown to the
    user so an approval prompt names which server is calling. The mode and the
    approval callback are read from ``ctx.deps`` at call time, so runtime mode
    switches take effect immediately.

    ``schema_holder`` (when given) is a one-key dict the caller populates with
    ``{"server": <MCPServer>}`` right after the server is built. On first dispatch
    the hook reads the server's tool input schemas — cheap, since pydantic-ai has
    already cached them via its own ``list_tools`` at startup — and decodes any
    argument a model stringified when the schema expects a structured value (see
    :func:`coerce_by_schema`). Absent a holder, a fetch error, or an unknown tool,
    the args pass through untouched, exactly as before."""
    schema_cache: dict[str, dict] = {}
    schema_state = {"loaded": False}

    async def _tool_schema(name: str) -> dict | None:
        if schema_holder is None:
            return None
        server = schema_holder.get("server")
        if server is None:
            return None
        if not schema_state["loaded"]:
            try:
                tools = await server.list_tools()
            except Exception:  # noqa: BLE001 - best-effort: fall back to uncoerced dispatch
                return None
            for tool in tools:
                nm = getattr(tool, "name", None)
                sch = getattr(tool, "inputSchema", None)
                if isinstance(nm, str) and isinstance(sch, dict):
                    schema_cache[nm] = sch
            schema_state["loaded"] = True
        return schema_cache.get(name)

    async def _coerce_args(name: str, args: dict | None) -> dict | None:
        if not isinstance(args, dict):
            return args
        schema = await _tool_schema(name)
        if not isinstance(schema, dict):
            return args
        # coerce_by_schema is typed to return `object` (it recurses into scalars
        # too), but fed a dict it always returns a dict — narrow explicitly so the
        # reassignment below doesn't widen `args`'s type for the callers downstream
        # (_bound_tool_result / _McpApprovalCall both expect dict | None).
        coerced = coerce_by_schema(args, schema)
        return coerced if isinstance(coerced, dict) else args

    async def hook(ctx, call_tool, name, args):
        deps = ctx.deps
        display = f"{label}_{name}"
        # Read the workspace root at call time, like mode/request_approval below,
        # so a large result can be offloaded under the project rather than inline.
        ws = getattr(deps, "workspace", None)
        root = getattr(ws, "root", None) if ws is not None else None
        mode = getattr(ws, "mode", None) if ws is not None else None
        if mode is Mode.plan:
            return f"Denied: {display} is blocked in read-only plan mode."
        # Decode any stringified structured arg before the server (and the approval
        # prompt) ever see it, so a model that serialized a nested value as a string
        # doesn't burn a turn on the server's rejection.
        args = await _coerce_args(name, args)
        if mode is Mode.auto or trusted:
            result = await call_tool(name, args)
            return _bound_tool_result(
                result, label=label, name=name, args=args, workspace_root=root
            )
        # ask mode against an untrusted server: prompt the user.
        ui = getattr(deps, "ui", None)
        approve = getattr(ui, "request_approval", None) if ui is not None else None
        if approve is None:
            return f"Denied: {display} needs approval but none is available here."
        decision = await approve(_McpApprovalCall(display, args or {}))
        if decision is True:
            result = await call_tool(name, args)
            return _bound_tool_result(
                result, label=label, name=name, args=args, workspace_root=root
            )
        return f"Denied: the user rejected {display}."

    return hook


def build_mcp_servers(specs: dict) -> tuple[list, list[str]]:
    """Turn a name->spec mapping into pydantic-ai ``MCPToolset``s, each gated by
    an approval hook and carrying its config name as the toolset ``id``. The
    ``<name>_<tool>`` prefixing happens at compose time via ``.prefixed(name)``
    (see the module docstring), so what's built here stays the raw handle.

    Returns ``(servers, warnings)``. A spec that is neither stdio (has
    ``command``) nor HTTP/SSE (has ``url``) is skipped with a warning instead of
    crashing, so one bad entry can't take down the rest.

    ``max_retries`` is left at ``None``, which in pydantic-ai 2.x inherits the
    agent's ``retries`` (the main agent and every sub-agent are built with
    ``retries=2``) — exactly the alignment the old hand-set ``_MCP_TOOL_RETRIES``
    constant maintained. A flaky MCP tool (e.g. a Playwright page-session blip)
    thus gets the same second chance as the builtins instead of raising
    ``UnexpectedModelBehavior`` on the first error.

    Stdio transports are built with ``keep_alive=False`` so disconnecting (the
    manager's ``aclose``) deterministically reaps the child process; fastmcp's
    default keeps the child alive for cross-session reuse, which marim never
    does — it holds one connection for the whole session and reconnects through
    the manager."""
    servers: list = []
    notes: list[str] = []
    for name, spec in specs.items():
        if not isinstance(spec, dict):
            notes.append(f"MCP server {name!r}: spec must be an object; skipped.")
            continue
        holder: dict = {}
        hook = make_approval_hook(
            name, bool(spec.get("trust", False)), schema_holder=holder
        )
        transport: StdioTransport | SSETransport | StreamableHttpTransport
        if "command" in spec:
            transport = StdioTransport(
                command=spec["command"],
                args=[str(a) for a in spec.get("args", [])],
                env=spec.get("env"),
                cwd=spec.get("cwd"),
                keep_alive=False,
                log_file=_mcp_stderr_log_target(),
            )
        elif "url" in spec:
            kind = (
                SSETransport
                if spec.get("type") == "sse"
                else StreamableHttpTransport
            )
            transport = kind(url=spec["url"], headers=spec.get("headers"))
        else:
            notes.append(
                f"MCP server {name!r}: needs 'command' or 'url'; skipped."
            )
            continue
        server = MCPToolset(transport, id=name, process_tool_call=hook)
        # The hook needs the server to read tool inputSchemas, but the server
        # needs the hook at construction — so hand the hook a holder now and
        # fill it once the server exists.
        holder["server"] = server
        servers.append(server)
    return servers, notes
