import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path

from .config import persist_server_enabled

logger = logging.getLogger(__name__)


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
        for server in self.mcp_servers:
            name = self.server_name(server)
            if name in self.disabled:
                continue
            err = await self._connect_one(server)
            if err is None:
                self.mcp_status.add_connected(name)
            else:
                self.mcp_status.add_failed(name, err)
        # Mark connected only after the loop completes. If a cancellation or
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
