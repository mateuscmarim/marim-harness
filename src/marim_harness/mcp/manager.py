import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path

from pydantic_ai import CombinedToolset, DeferredLoadingToolset

from .config import persist_server_enabled, server_prompts_in_ask

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

    def __init__(
        self, servers: list[object], disabled: set[str], *, trust_project: bool = False
    ) -> None:
        self.mcp_servers: list[object] = list(servers)
        self._live_servers: list[object] = []
        self._mcp_stack: AsyncExitStack | None = None
        self._connected: bool = False
        self.disabled: set[str] = set(disabled)
        self.mcp_status: McpStatus = McpStatus()
        # The trust decision that gates project-local .marim/mcp.json — passed
        # to persist_server_enabled below on every disable_server/enable_server
        # call, explicitly, never omitted. The invariant this upholds: the
        # trust decision used to WRITE mcp config must be the SAME one used to
        # LOAD it (mcp.config.load_mcp_config's own trust_project). The CLI
        # preset (bootstrap.build_harness) wires this from the single
        # store-aware `trusted` boolean it resolves once per run (via
        # trust.resolve_project_trust) — the very value it already passes to
        # load_mcp_config; an embedder composing HarnessBuilder directly has no
        # project-file loading path at all (with_mcp_server takes ready-built
        # toolsets — load_mcp_config is bootstrap-only), so False (untrusted)
        # is the correct default here too — matching load_mcp_config's own
        # default and keeping persist from independently re-deriving trust from
        # the env (persist_server_enabled's own env fallback only fires when
        # its trust_project kwarg is left None, which this class never does).
        self.trust_project: bool = trust_project

    @staticmethod
    def server_name(server) -> str:
        # MCPToolset carries the config name as its ``id`` (see build_mcp_servers);
        # the getattr stays soft for test doubles and foreign toolsets.
        return str(getattr(server, "id", None) or "?")

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

    async def _safe_list_tools(self, server) -> list | None:
        """One server's raw tool list, best-effort. ``None`` when the server has
        no ``list_tools`` or its call raises (a half-connected server must never
        sink the rest); an empty list is a real "no tools" answer. The single
        source of the tool-listing try/except shared by the count and map paths."""
        lister = getattr(server, "list_tools", None)
        if lister is None:
            return None
        try:
            return list(await lister())
        except Exception as exc:  # noqa: BLE001 - one server's failure must not sink the rest
            logger.debug(
                "tool listing failed for %s: %s", self.server_name(server), exc, exc_info=True
            )
            return None

    async def _tools_per_server(self) -> dict[str, list]:
        """Best-effort map of ``server_name -> its raw tool list`` across
        non-disabled live servers. A server with no ``list_tools`` or one that
        raises contributes nothing rather than failing. Shared by
        ``live_tool_count`` and ``live_tools_by_server``."""
        out: dict[str, list] = {}
        for s in self.live_toolsets():
            tools = await self._safe_list_tools(s)
            if tools is not None:
                out[self.server_name(s)] = tools
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
        ``discovered`` holds the composed (prefixed) tool names, and the prefix is
        the server name by construction (compose applies ``.prefixed(name)``).
        Best-effort: a server with no usable name, no/empty instructions, or one
        whose ``.instructions`` raises before init (``getattr`` → ``None``) is
        skipped, so a quiet/half-connected server never breaks a turn. Sorted by
        server name for deterministic output."""
        out: list[tuple[str, str]] = []
        for s in self.live_toolsets():
            prefix = self.server_name(s)
            if prefix == "?" or not any(t.startswith(f"{prefix}_") for t in discovered):
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

    def ask_prompting_names(self, names: list[str] | None) -> list[str]:
        """Of the resolvable requested server names, those whose tool calls
        would prompt the user per-call in ask mode (``server_prompts_in_ask``).
        Unknown/disabled names are excluded — they resolve to no server at all,
        so there is nothing to withhold and the caller's unknown-servers note
        already covers them. Backs the sub-agent runner's ask-mode grant
        filtering, where reach is decided up front."""
        granted, _ = self.granted_servers(names)
        return [self.server_name(s) for s in granted if server_prompts_in_ask(s)]

    async def _tool_count(self, servers: list) -> int:
        """Best-effort total tool count across ``servers`` (each best-effort: a
        server with no ``list_tools`` or one that raises contributes nothing,
        so a half-connected server never sinks the count). Shares the listing
        try/except with ``_tools_per_server`` via ``_safe_list_tools``."""
        total = 0
        for s in servers:
            tools = await self._safe_list_tools(s)
            if tools is not None:
                total += len(tools)
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
        # Prefix here (not at build time) so the manager keeps one raw handle per
        # server for lifecycle/introspection; the model-facing <name>_<tool> shape
        # is composed exactly where toolsets are handed to a run.
        prefixed = [s.prefixed(self.server_name(s)) for s in granted]
        if should_defer(policy, await self._tool_count(granted), threshold):
            return [DeferredLoadingToolset(CombinedToolset(prefixed))], unknown
        return prefixed, unknown

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
            logger.debug("MCP server connect failed: %s", exc, exc_info=True)
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
        # Known edge (left as-is to protect the cancel/retry invariants above): if
        # a cancel interrupts the gather below AFTER a _connect_one appended its
        # server to _live_servers but BEFORE the result-recording loop ran, that
        # server is live yet absent from mcp_status, and this guard then skips it on
        # retry so it stays unrecorded. It is fully connected and usable — only the
        # status readout under-counts it — so the fix isn't worth risking the
        # duplicate-avoidance / retry contract the guard exists to uphold.
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
            logger.debug("error during MCP shutdown: %s", exc, exc_info=True)
        finally:
            self._mcp_stack = None
            self._live_servers = []
            self._connected = False
            # Nothing is connected once closed; reset the status so a later
            # reconnect/enable starts from a clean slate (and can't double-list).
            self.mcp_status = McpStatus()

    def disable_server(self, name: str, workspace_root: Path) -> None:
        self.disabled.add(name)
        persist_server_enabled(workspace_root, name, False, trust_project=self.trust_project)

    async def enable_server(self, name: str, workspace_root: Path) -> str | None:
        self.disabled.discard(name)
        persist_server_enabled(workspace_root, name, True, trust_project=self.trust_project)
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

    async def add_servers(self, servers: list) -> dict:
        """Register and connect servers that aren't already configured (by name).
        The trust hot-apply path: granting project trust mid-session loads the
        project's .marim/mcp.json servers without a rebuild. Mirrors
        enable_server's per-server bookkeeping; disabled names are registered
        but not connected (a later enable_server picks them up)."""
        known = set(self.configured_names())
        fresh = [s for s in servers if self.server_name(s) not in known]
        self.mcp_servers.extend(fresh)
        for server in fresh:
            name = self.server_name(server)
            if name in self.disabled:
                continue
            err = await self._connect_one(server)
            # Same bookkeeping as enable_server above: record success/failure so
            # status stays accurate for a server connected outside enable_server.
            if err is None:
                self.mcp_status.add_connected(name)
            else:
                self.mcp_status.add_failed(name, err)
        return self.mcp_status.to_dict()
