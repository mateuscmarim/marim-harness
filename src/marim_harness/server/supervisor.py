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
from ..session import SessionManager
from .bus import EventBus
from .host import SessionHost
from .workspaces import WorkspaceRecord

logger = logging.getLogger(__name__)

HarnessFactory = Callable[[Path, str, "Mode | None"], Awaitable[Harness]]

_EVICT_POLL_CEILING_SECONDS = 60.0


class SessionBusy(Exception):
    """Raised by set_model when a live host has a turn running."""


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

    def bus_peek(self, ws_id: str, session_id: str) -> EventBus | None:
        """The session's bus if one exists, without creating one — a read for
        callers (e.g. /history) that must not spawn a bus as a side effect."""
        return self._buses.get((ws_id, session_id))

    def set_mode(self, ws_id: str, session_id: str, mode: Mode) -> None:
        self._modes[(ws_id, session_id)] = mode

    def set_model(self, record: WorkspaceRecord, session_id: str, model_id: str) -> None:
        """Apply a model to a session. A loaded host switches live (rebuild +
        persist) unless it is busy, in which case SessionBusy is raised so the
        API can reject with 409. An idle session persists straight to its store."""
        host = self._hosts.get((record.id, session_id))
        if host is not None:
            if host.busy:
                raise SessionBusy(session_id)
            host.harness.set_model(model_id)
            return
        store = SessionManager(Path(record.path)).store(session_id)
        store.model = model_id
        store.save_meta()

    def peek(self, ws_id: str, session_id: str) -> SessionHost | None:
        return self._hosts.get((ws_id, session_id))

    async def host_for(self, record: WorkspaceRecord, session_id: str) -> SessionHost:
        key = (record.id, session_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            host = self._hosts.get(key)
            if host is not None:
                host.touch()
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

    def forget(self, ws_id: str, session_id: str) -> None:
        """Fully reclaim all state for a session that has been permanently
        deleted (not merely idle-evicted) — buses, locks, and any stored mode.
        Idle eviction must NOT call this: a bus intentionally outlives an
        idle-evicted host so a client can still resume/replay a live session's
        stream; this is only for a session that no longer exists at all."""
        key = (ws_id, session_id)
        self._buses.pop(key, None)
        self._locks.pop(key, None)
        self._modes.pop(key, None)

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
        for key in list(self._hosts):
            await self._evict_if_idle(key)

    async def _evict_if_idle(self, key: tuple[str, str]) -> bool:
        """Re-check idle eligibility under the per-key lock, in the same
        critical section as the pop+close. This is what makes eviction
        race-free against host_for: whichever of the two acquires the lock
        first for this key establishes the truth the other observes — a
        host_for that touches the host first makes this check see fresh
        (non-idle) state and abort; an eviction that closes first leaves
        nothing in self._hosts, so host_for correctly rebuilds instead of
        reusing a host mid-teardown."""
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            host = self._hosts.get(key)
            if host is None:
                return False
            bus = self._buses.get(key)
            subscribers = bus.subscriber_count if bus is not None else 0
            if (
                host.busy
                or host.queued != 0
                or subscribers != 0
                or host.idle_seconds < self.idle_ttl
            ):
                return False
            del self._hosts[key]
            await host.aclose()
            return True

    async def aclose(self) -> None:
        if self._evictor is not None:
            self._evictor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._evictor
            self._evictor = None
        for key in list(self._hosts):
            await self.close_host(*key)
