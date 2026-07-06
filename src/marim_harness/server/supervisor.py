"""Registry of live SessionHosts, one per (workspace, session).

Hosts are created lazily on the first prompt and evicted after sitting idle
(no running turn, nothing queued, no SSE subscriber) — the harness is torn
down cleanly and the session stays resumable from disk. Buses are keyed
separately and OUTLIVE hosts: an SSE client can stay attached (or resume with
Last-Event-ID) across an eviction, and a live subscriber blocks eviction so a
watching client never sees its stream silently reset.

``set_mode`` is in-memory only: a mode chosen at session creation survives
until the daemon restarts, after which the configured default applies
(documented v1 limitation — the session file doesn't persist a mode)."""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..runtime.harness import Harness
from ..runtime.permissions import Mode
from .bus import EventBus
from .host import SessionHost
from .workspaces import WorkspaceRecord

logger = logging.getLogger(__name__)

HarnessFactory = Callable[[Path, str, "Mode | None"], Awaitable[Harness]]

_EVICT_POLL_CEILING_SECONDS = 60.0


async def default_harness_factory(
    workspace: Path, session_id: str, mode: Mode | None
) -> Harness:
    """Build a full production harness for one session: the same wiring as the
    TUI/headless (models, MCP, LSP, hooks) via build_harness, plus the connect
    + session_start lifecycle headless performs around a run."""
    from ..runtime.bootstrap import build_harness

    harness = build_harness(workspace, mode=mode, session_id=session_id)
    await harness.connect()
    await harness.session_start("resume" if harness.session.history else "startup")
    return harness


class SessionSupervisor:
    def __init__(
        self,
        factory: HarnessFactory = default_harness_factory,
        *,
        idle_ttl: float = 900.0,
        ring_size: int = 1000,
    ) -> None:
        self._factory = factory
        self.idle_ttl = idle_ttl
        self._ring_size = ring_size
        self._buses: dict[tuple[str, str], EventBus] = {}
        self._hosts: dict[tuple[str, str], SessionHost] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._modes: dict[tuple[str, str], Mode] = {}
        self._evictor: asyncio.Task | None = None

    def bus_for(self, ws_id: str, session_id: str) -> EventBus:
        key = (ws_id, session_id)
        if key not in self._buses:
            self._buses[key] = EventBus(ring_size=self._ring_size)
        return self._buses[key]

    def set_mode(self, ws_id: str, session_id: str, mode: Mode) -> None:
        self._modes[(ws_id, session_id)] = mode

    def peek(self, ws_id: str, session_id: str) -> SessionHost | None:
        return self._hosts.get((ws_id, session_id))

    async def host_for(self, record: WorkspaceRecord, session_id: str) -> SessionHost:
        key = (record.id, session_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            host = self._hosts.get(key)
            if host is not None:
                return host
            harness = await self._factory(Path(record.path), session_id, self._modes.get(key))
            host = SessionHost(harness, self.bus_for(*key))
            self._hosts[key] = host
            return host

    async def close_host(self, ws_id: str, session_id: str) -> bool:
        key = (ws_id, session_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            host = self._hosts.pop(key, None)
        if host is None:
            return False
        await host.aclose()
        return True

    def start_evictor(self) -> None:
        if self._evictor is None:
            self._evictor = asyncio.get_running_loop().create_task(self._evict_loop())

    async def _evict_loop(self) -> None:
        interval = min(self.idle_ttl, _EVICT_POLL_CEILING_SECONDS) or 1.0
        while True:
            await asyncio.sleep(interval)
            try:
                await self.evict_idle()
            except Exception:  # noqa: BLE001 - the sweep must never die
                logger.warning("idle-eviction sweep failed", exc_info=True)

    async def evict_idle(self) -> None:
        for key, host in list(self._hosts.items()):
            bus = self._buses.get(key)
            subscribers = bus.subscriber_count if bus is not None else 0
            if (
                not host.busy
                and host.queued == 0
                and subscribers == 0
                and host.idle_seconds >= self.idle_ttl
            ):
                await self.close_host(*key)

    async def aclose(self) -> None:
        if self._evictor is not None:
            self._evictor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._evictor
            self._evictor = None
        for key in list(self._hosts):
            await self.close_host(*key)
