from contextlib import AsyncExitStack
from pathlib import Path
from typing import Optional

from .config import persist_server_enabled


class McpManager:
    """Owns MCP server lifecycle: connections, enable/disable, grant resolution."""

    def __init__(self, servers: list, disabled: set[str]) -> None:
        self.mcp_servers: list = list(servers)
        self._live_servers: list = []
        self._mcp_stack: Optional[AsyncExitStack] = None
        self._connected: bool = False
        self.disabled: set[str] = set(disabled)
        self.mcp_status: dict = {"connected": [], "failed": []}

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

    async def _connect_one(self, server) -> Optional[str]:
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
            return self.mcp_status
        self._connected = True
        connected: list[str] = []
        failed: list[tuple[str, str]] = []
        for server in self.mcp_servers:
            name = self.server_name(server)
            if name in self.disabled:
                continue
            err = await self._connect_one(server)
            if err is None:
                connected.append(name)
            else:
                failed.append((name, err))
        self.mcp_status["connected"] = connected
        self.mcp_status["failed"] = failed
        return self.mcp_status

    async def aclose(self) -> None:
        if self._mcp_stack is not None:
            await self._mcp_stack.aclose()
            self._mcp_stack = None
        self._live_servers = []
        self._connected = False

    def disable_server(self, name: str, workspace_root: Path) -> None:
        self.disabled.add(name)
        persist_server_enabled(workspace_root, name, False)

    async def enable_server(self, name: str, workspace_root: Path) -> Optional[str]:
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
            self.mcp_status["connected"].append(name)
            self.mcp_status["failed"] = [
                f for f in self.mcp_status["failed"] if f[0] != name
            ]
        return err
