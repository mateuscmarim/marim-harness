import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path

from pydantic_ai import CombinedToolset, DeferredLoadingToolset

from .config import persist_server_enabled

logger = logging.getLogger(__name__)


def should_defer(policy: str, count: int, threshold: int) -> bool:
    """Whether to defer the MCP tool surface behind tool search. ``on`` always
    defers; ``auto`` defers only when ``count`` strictly exceeds ``threshold``;
    anything else (``off`` or an unknown value) never defers."""
    if policy == "on":
        return True
    if policy == "auto":
        return count > threshold
    return False


@dataclass
class McpStatus:
    """Live connection state for all MCP servers in this session."""

    connected: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def add_connected(self, name: str) -> None:
        if name not in self.connected:
            self.connected.append(name)
        self.failed = [(n, e) for n, e in self.failed if n != name]

    def add_failed(self, name: str, err: str) -> None:
        self.connected = [n for n in self.connected if n != name]
        self.failed = [(n, e) for n, e in self.failed if n != name]
        self.failed.append((name, err))

    def remove(self, name: str) -> None:
        self.connected = [n for n in self.connected if n != name]
        self.failed = [(n, e) for n, e in self.failed if n != name]

    def to_dict(self) -> dict:
        return {"connected": list(self.connected), "failed": list(self.failed)}


class McpManager:
    """Owns MCP server lifecycle: connections, enable/disable, grant resolution."""

    def __init__(self, servers: list[object], disabled: set[str]) -> None:
        self.mcp_servers: list[object] = list(servers)
        self._live_servers: list[object] = []
        self._mcp_stack: AsyncExitStack | None = None
        self._connected: bool = False
        self.disabled: set[str] = set(disabled)
        self.mcp_status: McpStatus = McpStatus()

    @staticmethod
    def server_name(server) -> str:
        return str(getattr(server, "id", None) or getattr(server, "tool_prefix", "?"))

    def configured_names(self) -> list[str]:
        return [self.server_name(s) for s in self.mcp_servers]

    def enabled_names(self) -> list[str]:
        return [
            n for s in self._live_servers
            if (n := self.server_name(s)) not in self.disabled
        ]

    def live_toolsets(self) -> list:
        return [
            s for s in self._live_servers
            if self.server_name(s) not in self.disabled
        ]

    async def _tools_per_server(self) -> dict[str, list]:
        """Best-effort map of ``server_name -> its raw tool list`` across
        non-disabled live servers. A server with no ``list_tools`` or one that
        raises contributes nothing rather than failing. Shared by
        ``live_tool_count`` and ``live_tools_by_server``."""
        out: dict[str, list] = {}
        for s in self.live_toolsets():
            lister = getattr(s, "list_tools", None)
            if lister is None:
                continue
            try:
                out[self.server_name(s)] = list(await lister())
            except Exception:  # noqa: BLE001 - one server's failure must not sink the rest
                logger.debug("tool listing failed for %s", self.server_name(s), exc_info=True)
        return out

    async def live_tool_count(self) -> int:
        """Best-effort count of tools across non-disabled live MCP servers."""
        return sum(len(v) for v in (await self._tools_per_server()).values())

    async def live_tools_by_server(self) -> dict[str, list[str]]:
        """``server_name -> sorted tool names`` across non-disabled live servers
        (best-effort). Backs the discovery catalog; servers whose tools have no
        usable name are omitted."""
        groups: dict[str, list[str]] = {}
        for name, tools in (await self._tools_per_server()).items():
            tool_names = sorted(
                str(getattr(t, "name", "")) for t in tools if getattr(t, "name", "")
            )
            if tool_names:
                groups[name] = tool_names
        return groups

    def discovered_server_instructions(
        self, discovered: set[str]
    ) -> list[tuple[str, str]]:
        """For each non-disabled live server whose tools appear in ``discovered``,
        return ``(server_name, instructions)`` — the server's init-time usage guide.
        Best-effort: a server with no ``tool_prefix``, no/empty instructions, or one
        whose ``.instructions`` raises before init (``getattr`` → ``None``) is
        skipped, so a quiet/half-connected server never breaks a turn. Sorted by
        server name for deterministic output."""
        out: list[tuple[str, str]] = []
        for s in self.live_toolsets():
            prefix = getattr(s, "tool_prefix", None)
            if not prefix or not any(t.startswith(f"{prefix}_") for t in discovered):
                continue
            text = getattr(s, "instructions", None)
            if isinstance(text, str) and text.strip():
                out.append((self.server_name(s), text))
        out.sort(key=lambda pair: pair[0])
        return out

    def mcp_index_text(self) -> str:
        names = self.enabled_names()
        if not names:
            return ""
        return (
            "MCP servers you can grant to a sub-agent via spawn_agent's `mcp` "
            "argument (e.g. mcp=[" + repr(names[0]) + "]): "
            + ", ".join(names)
        )

    def granted_servers(self, names: list[str] | None) -> tuple[list, list[str]]:
        if not names:
            return [], []
        by_name = {self.server_name(s): s for s in self._live_servers}
        granted: list = []
        unknown: list[str] = []
        for name in dict.fromkeys(names):
            server = by_name.get(name)
            if server is None or name in self.disabled:
                unknown.append(name)
            else:
                granted.append(server)
        return granted, unknown

    async def _tool_count(self, servers: list) -> int:
        """Best-effort total tool count across ``servers`` (each best-effort: a
        server with no ``list_tools`` or one that raises contributes nothing,
        so a half-connected server never sinks the count)."""
        total = 0
        for s in servers:
            lister = getattr(s, "list_tools", None)
            if lister is None:
                continue
            try:
                total += len(list(await lister()))
            except Exception:  # noqa: BLE001 - one server's failure must not sink the rest
                logger.debug("tool listing failed for %s", self.server_name(s), exc_info=True)
        return total

    async def granted_toolsets(
        self, names: list[str] | None, policy: str, threshold: int
    ) -> tuple[list, list[str]]:
        """Like :meth:`granted_servers`, but applies the same ``should_defer``
        tool-search deferral the main agent's per-turn toolset composition
        (``runtime.toolsets.compose_turn_toolsets``) uses — computed over the
        *granted subset only*. When ``should_defer`` fires for the granted servers'
        combined tool count, they are combined behind Pydantic AI's ToolSearch, so
        a sub-agent granted a large MCP surface searches for tools on demand
        instead of carrying every schema in its context; otherwise the raw granted
        servers pass through. ``unknown`` is forwarded unchanged for the caller's
        grant note. Mirrors what the main agent gets, so a server deferred for the
        main loop isn't dumped wholesale into every spawn that's granted it."""
        granted, unknown = self.granted_servers(names)
        if not granted:
            return granted, unknown
        if should_defer(policy, await self._tool_count(granted), threshold):
            return [DeferredLoadingToolset(CombinedToolset(granted))], unknown
        return granted, unknown

    def grant_note(self, unknown: list[str]) -> str:
        if not unknown:
            return ""
        bad = ", ".join(f"'{n}'" for n in unknown)
        enabled = self.enabled_names()
        avail = ", ".join(enabled) if enabled else "none"
        return f"(note: ignored unknown MCP server(s) {bad}; enabled: {avail})\n\n"

    async def _connect_one(self, server) -> str | None:
        if self._mcp_stack is None:
            self._mcp_stack = AsyncExitStack()
        try:
            await self._mcp_stack.enter_async_context(server)
        except Exception as exc:
            return str(exc)
        self._live_servers.append(server)
        return None

    async def connect(self) -> dict:
        if self._connected or not self.mcp_servers:
            return self.mcp_status.to_dict()
        # Skip servers already live from an earlier, interrupted connect(). A
        # KeyboardInterrupt/cancel mid-connect re-raises below with ``_connected``
        # still False, yet the servers that already succeeded stay in
        # ``_live_servers``. Without this guard the retry re-enters those same
        # servers via _connect_one, which *appends unconditionally*, so
        # live_toolsets() ends up with duplicates → duplicate tool names handed to
        # agent.run. Match enable_server's already-live check so a retry only
        # connects what's still missing.
        live_names = {self.server_name(s) for s in self._live_servers}
        servers = [
            s
            for s in self.mcp_servers
            if self.server_name(s) not in self.disabled and self.server_name(s) not in live_names
        ]
        if servers:
            # Connect concurrently: startup latency is the slowest server, not the
            # sum across all of them. Create the shared AsyncExitStack ONCE up front
            # so the gathered tasks don't race on the lazy ``self._mcp_stack is None``
            # init inside _connect_one (concurrent coroutines could otherwise each
            # build a stack and the first would be dropped, leaking its servers).
            if self._mcp_stack is None:
                self._mcp_stack = AsyncExitStack()
            # return_exceptions keeps one server's failure from cancelling the rest;
            # _connect_one already contains its own errors as a string, but this also
            # guards against an unexpected raise. Results line up with ``servers`` by
            # index (gather preserves input order), so status is recorded in config
            # order exactly as the serial loop did.
            results = await asyncio.gather(
                *(self._connect_one(s) for s in servers), return_exceptions=True
            )
            for server, result in zip(servers, results, strict=True):
                name = self.server_name(server)
                # A BaseException that isn't an Exception (CancelledError,
                # KeyboardInterrupt, SystemExit) is an interruption, not a server
                # fault. return_exceptions captures it like any other result; re-raise
                # so the flag stays False below and a later connect() retries — the
                # exact contract the serial loop had.
                if isinstance(result, BaseException) and not isinstance(result, Exception):
                    raise result
                if result is None:
                    self.mcp_status.add_connected(name)
                else:
                    # ``result`` is _connect_one's contained error string, or — if it
                    # raised unexpectedly — an Exception. Either way it's a per-server
                    # fault: record it so one bad server can't sink the others.
                    self.mcp_status.add_failed(name, str(result))
        # Mark connected only after every server resolves. If a cancellation or
        # BaseException interrupts mid-connect, the flag stays False so a later
        # connect() retries (and reaps the servers already in the stack via
        # aclose) instead of early-returning into a half-connected state.
        self._connected = True
        return self.mcp_status.to_dict()

    async def aclose(self) -> None:
        # Reset state regardless of whether teardown succeeds: a server whose
        # __aexit__ raises (dead transport / already-gone child) must not leave
        # the stack non-None and _connected stuck True, which would make a later
        # reconnect early-return and leak the stdio subprocesses.
        try:
            if self._mcp_stack is not None:
                await self._mcp_stack.aclose()
        except Exception as exc:
            logger.debug("error during MCP shutdown: %s", exc)
        finally:
            self._mcp_stack = None
            self._live_servers = []
            self._connected = False
            # Nothing is connected once closed; reset the status so a later
            # reconnect/enable starts from a clean slate (and can't double-list).
            self.mcp_status = McpStatus()

    def disable_server(self, name: str, workspace_root: Path) -> None:
        self.disabled.add(name)
        persist_server_enabled(workspace_root, name, False)

    async def enable_server(self, name: str, workspace_root: Path) -> str | None:
        self.disabled.discard(name)
        persist_server_enabled(workspace_root, name, True)
        if any(self.server_name(s) == name for s in self._live_servers):
            return None
        server = next(
            (s for s in self.mcp_servers if self.server_name(s) == name), None
        )
        if server is None:
            return f"no such server {name!r}"
        err = await self._connect_one(server)
        if err is None:
            self.mcp_status.add_connected(name)
        else:
            # Mirror connect()'s bookkeeping on the failure path so status stays
            # accurate: a server that failed to (re)connect must not linger in
            # "connected" from an earlier successful session, and the new failure
            # must be recorded (de-duped) so the UI/status reflects it.
            self.mcp_status.add_failed(name, err)
        return err
