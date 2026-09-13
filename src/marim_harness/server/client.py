"""The remote side of the TUI's session seam (phase 4a): drive a daemon-owned
session over the daemon's REST routes and read its events over the WebSocket.

``RemoteSessionHost`` is the same surface ``interfaces/tui/link.py``'s
``LocalSessionLink`` presents over the in-process ``SessionHost`` — submit,
interrupt, steer, answer_ask, pending_asks, set_mode, set_model, history,
attach, close — so the app cannot tell which one it holds. Errors keep their
meaning across the seam: ``429 queue_full`` and ``404 host_closed`` become
the host's own ``TurnQueueFull`` / ``HostClosed``, and ``409 claimed`` becomes
``SessionClaimed``, so ``_submit_turn``'s re-stage-and-pause behaviour is
untouched.

``RemoteSubscription`` is the transport seam the parent spec named: it
implements ``next_event(timeout)`` / ``close()`` over a WebSocket and nothing
else, so the app's pump does not change. Reconnect is internal — on socket
loss it reconnects with ``?after_seq=<last delivered>`` on a capped backoff
and reports ``reconnecting`` / ``connected`` / ``lost`` through one callback.

This module imports httpx (core) and websockets (the ``tui`` extra), never
starlette: a TUI install without the ``[serve]`` extra can attach.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from pydantic_ai.usage import RunUsage

from ..compaction import estimate_tokens, repair_masked_narrowed_returns
from ..images import rehydrate_images
from ..session.claim import SessionClaimed
from .attach import RemoteTarget
from .host import HostClosed, TurnQueueFull
from .schema import Event

logger = logging.getLogger(__name__)

HISTORY_PAGE = 200
RECONNECT_BACKOFF = (0.5, 8.0)
LOST_AFTER = 60.0
# Close codes the server sends on the upgrade for a bad token / unknown
# session. Retrying cannot fix either, so the feed reports lost at once. On
# the real wire a close *before* accept surfaces as a rejected handshake
# (uvicorn answers HTTP 403/404 rather than a WebSocket close frame), so the
# equivalent HTTP statuses are fatal too.
_FATAL_CLOSE_CODES = frozenset({4401, 4404})
_FATAL_HTTP_STATUSES = frozenset({401, 403, 404})

StateCallback = Callable[[str], None]


class RemoteUnavailable(HostClosed):
    """The daemon did not answer a command. A subclass of ``HostClosed`` so
    the app's existing "host is gone" handling applies; the message names
    the endpoint so the notice is actionable."""


@dataclass
class HistorySnapshot:
    """A session's persisted transcript plus the bus seq it is consistent up
    to (None locally, where the history IS the live object)."""

    messages: list[ModelMessage]
    history_seq: int | None = None


@dataclass
class RemoteInfo:
    """The read model the widgets and replay need, filled from ``GET
    session`` at attach and kept current from events (``observe``) and the
    idle-edge ``refresh``. Structurally the same surface as the local link's
    live view (``interfaces/tui/link.py``'s ``LinkInfo``)."""

    workspace_root: Path
    session_id: str
    session_name: str | None = None
    mode: str = "ask"
    model_id: str | None = None
    model_label: str = ""
    advisor_model_id: str | None = None
    thinking_level_id: str | None = None
    usage: RunUsage = field(default_factory=RunUsage)
    history_tokens: int = 0
    message_count: int = 0
    compact_threshold: int = 0
    duration_seconds: float = 0.0
    quota_hint: str | None = None
    # The remote TUI has no model catalog of its own; the picker is local-only.
    model_source: Any = None

    def apply_session(self, payload: dict) -> None:
        """Fold a ``GET session`` body in. Every field is optional on the
        wire (an older daemon omits the phase-4a additions), so each read
        falls back to what is already known."""
        session = payload.get("session") or {}
        self.session_name = session.get("name", self.session_name)
        self.mode = session.get("mode") or self.mode
        self.model_id = session.get("model") or self.model_id
        self.model_label = session.get("model_label") or self.model_id or ""
        self.advisor_model_id = session.get("advisor_model", self.advisor_model_id)
        self.thinking_level_id = session.get("thinking", self.thinking_level_id)
        self.message_count = int(session.get("message_count", self.message_count) or 0)
        self.duration_seconds = float(session.get("duration_seconds") or self.duration_seconds)
        if payload.get("usage") is not None:
            self.usage = usage_from_summary(payload["usage"])
        if payload.get("compact_threshold") is not None:
            self.compact_threshold = int(payload["compact_threshold"])

    def observe(self, event: Event) -> None:
        """Keep the read model current from the events the pump delivers.
        Cheap per-event bookkeeping only; anything needing a round trip waits
        for ``refresh``."""
        data = event.data
        if event.type == "session.renamed":
            self.session_name = data.get("to", self.session_name)
        elif event.type == "session.mode_changed":
            self.mode = data.get("mode", self.mode)
        elif event.type == "turn.finished" and isinstance(data.get("usage"), dict):
            # turn.finished carries the session-cumulative summary, not a
            # delta: replace, never add.
            self.usage = usage_from_summary(data["usage"])
        elif event.type == "compaction.finished" and data.get("after") is not None:
            self.history_tokens = int(data["after"])


def usage_from_summary(summary: dict) -> RunUsage:
    """Rebuild a ``RunUsage`` from the ``usage_summary`` dict the wire
    carries (``GET session`` and ``turn.finished``). Only the token counts
    round-trip; cost is re-derived locally from the model id, as it is for a
    local session."""

    def count(key: str) -> int:
        try:
            return int(summary.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    return RunUsage(
        input_tokens=count("input_tokens"),
        output_tokens=count("output_tokens"),
        cache_read_tokens=count("cache_read_tokens"),
        cache_write_tokens=count("cache_write_tokens"),
    )


def _error_code(response: httpx.Response) -> str:
    try:
        return str(response.json()["error"]["code"])
    except (ValueError, KeyError, TypeError):
        return ""


def _error_message(response: httpx.Response) -> str:
    try:
        return str(response.json()["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return response.text[:200]


class RemoteSessionHost:
    """One daemon-owned session, driven over HTTP. See the module docstring
    for the error mapping; every method is a coroutine because each is a
    round trip."""

    kind = "remote"

    def __init__(
        self,
        target: RemoteTarget,
        workspace_root: Path,
        *,
        client: httpx.AsyncClient | None = None,
        on_state: StateCallback | None = None,
    ) -> None:
        self.target = target
        self.info = RemoteInfo(workspace_root=workspace_root, session_id=target.session_id)
        self._client = client or httpx.AsyncClient(
            base_url=target.endpoint,
            headers={"Authorization": f"Bearer {target.token}"},
            timeout=httpx.Timeout(10.0, read=30.0),
        )
        self._owns_client = client is None
        self._on_state = on_state
        self._subscription: RemoteSubscription | None = None
        # How many persisted messages ``history_tokens`` already accounts for;
        # ``refresh`` fetches only the tail past it.
        self._counted_messages = 0

    # ------------------------------------------------------------- paths --
    @property
    def _base(self) -> str:
        t = self.target
        return f"/v1/workspaces/{t.workspace_id}/sessions/{t.session_id}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._client.request(method, f"{self._base}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise RemoteUnavailable(
                f"daemon at {self.target.endpoint} did not answer: {exc}"
            ) from exc

    # ---------------------------------------------------------- commands --
    async def submit(
        self,
        prompt: str,
        attachments: list[tuple[bytes, str]] | None = None,
        *,
        trigger: str = "user",
    ) -> str:
        body: dict[str, Any] = {"prompt": prompt, "trigger": trigger}
        if attachments:
            body["attachments"] = [
                {"data_b64": base64.b64encode(data).decode("ascii"), "media_type": media}
                for data, media in attachments
            ]
        response = await self._request("POST", "/messages", json=body)
        if response.status_code == 202:
            return str(response.json()["turn_id"])
        code = _error_code(response)
        if response.status_code == 429 and code == "queue_full":
            raise TurnQueueFull()
        if response.status_code == 404 and code == "host_closed":
            raise HostClosed()
        if response.status_code == 409 and code == "claimed":
            raise SessionClaimed(self.target.session_id, None)
        raise RemoteUnavailable(
            f"submit rejected by the daemon ({response.status_code}): {_error_message(response)}"
        )

    async def interrupt(self) -> bool:
        response = await self._request("POST", "/interrupt")
        return response.status_code == 200 and bool(response.json().get("interrupted"))

    async def steer(self, text: str, attachments: list[tuple[bytes, str]] | None = None) -> None:
        # SteerIn is text-only (4a non-goal); the app refuses attachments with
        # a notice before it gets here.
        response = await self._request("POST", "/steer", json={"text": text})
        if response.status_code != 200:
            # Typically 409 not_running: the turn ended between the keypress
            # and the request. The text would otherwise vanish silently.
            raise RemoteUnavailable(f"steer not delivered: {_error_message(response)}")

    async def answer_ask(self, ask_id: str, answer: dict) -> bool:
        response = await self._request("POST", f"/asks/{ask_id}", json=answer)
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            # Unknown or already answered: the same False the local host
            # returns when another client won the race.
            return False
        # Anything else (a refused token, a daemon-side failure) means the
        # verdict was NOT taken and the ask is still parked: raise, as
        # set_mode/set_model do, so the app can say so instead of leaving
        # the user with a panel that looks answered.
        raise RemoteUnavailable(f"answer not delivered: {_error_message(response)}")

    async def pending_asks(self) -> list[dict]:
        response = await self._request("GET", "/asks")
        if response.status_code != 200:
            # Not "no asks": a failed read. Returning [] here would have the
            # app's reconciliation dismiss every mounted panel as stale.
            raise RemoteUnavailable(f"asks not readable: {_error_message(response)}")
        return list(response.json().get("asks", []))

    async def set_mode(self, mode: str) -> None:
        response = await self._request("POST", "/mode", json={"mode": mode})
        if response.status_code != 200:
            raise RemoteUnavailable(f"mode not switched: {_error_message(response)}")
        self.info.mode = mode

    async def set_model(self, model_id: str) -> None:
        response = await self._request("POST", "/model", json={"model": model_id})
        if response.status_code != 200:
            raise RemoteUnavailable(f"model not switched: {_error_message(response)}")
        self.info.model_id = model_id
        self.info.model_label = model_id

    # ---------------------------------------------------- read / replay --
    async def load_session(self) -> str:
        """``GET session`` → seed the read model; returns the host status
        (``idle`` | ``running`` | ``waiting_ask``) for the busy tracker."""
        response = await self._request("GET", "")
        if response.status_code != 200:
            raise RemoteUnavailable(f"session not readable: {_error_message(response)}")
        payload = response.json()
        self.info.apply_session(payload)
        return str(payload.get("status", "idle"))

    async def _history_page(self, offset: int, limit: int) -> dict:
        response = await self._request("GET", "/history", params={"offset": offset, "limit": limit})
        if response.status_code != 200:
            raise RemoteUnavailable(f"history not readable: {_error_message(response)}")
        return response.json()

    async def _raw_history(self, offset: int) -> tuple[list[dict], int | None]:
        raw: list[dict] = []
        history_seq: int | None = None
        while True:
            page = await self._history_page(offset + len(raw), HISTORY_PAGE)
            history_seq = page.get("history_seq")
            chunk = page.get("messages", [])
            raw.extend(chunk)
            if not chunk or offset + len(raw) >= int(page.get("message_count", 0)):
                break
        return raw, history_seq

    async def history(self) -> HistorySnapshot:
        """The persisted transcript, deserialised the way a local resume
        does it (image refs re-hydrated from the shared cache, legacy typed
        returns repaired, then pydantic-ai's adapter)."""
        raw, history_seq = await self._raw_history(0)
        messages = _deserialise(raw, self.target.session_id)
        self.info.history_tokens = estimate_tokens(messages)
        self.info.message_count = len(messages)
        self._counted_messages = len(messages)
        return HistorySnapshot(messages, history_seq)

    async def refresh(self) -> None:
        """Idle-edge re-read: ``GET session`` for the fields only the daemon
        knows (usage, threshold, name), plus the persisted tail since the
        last count so the context gauge advances. A shrunk transcript
        (compaction, rewind) recounts from the start."""
        await self.load_session()
        if self.info.message_count < self._counted_messages:
            self._counted_messages = 0
            self.info.history_tokens = 0
        if self.info.message_count > self._counted_messages:
            raw, _ = await self._raw_history(self._counted_messages)
            self.info.history_tokens += estimate_tokens(_deserialise(raw, self.target.session_id))
            self._counted_messages += len(raw)

    # -------------------------------------------------------------- feed --
    def attach(self, after_seq: int | None = None) -> RemoteSubscription:
        if self._subscription is not None:
            self._subscription.close()
        self._subscription = RemoteSubscription(
            self._open_socket,
            after_seq=after_seq,
            on_state=self._on_state,
            on_event=self.info.observe,
        )
        return self._subscription

    def _open_socket(self, after_seq: int | None):
        from websockets.asyncio.client import connect

        scheme = "wss" if self.target.endpoint.startswith("https") else "ws"
        host = self.target.endpoint.split("://", 1)[-1]
        uri = f"{scheme}://{host}{self._base}/ws"
        if after_seq is not None:
            uri += f"?after_seq={after_seq}"
        return connect(
            uri,
            additional_headers={"Authorization": f"Bearer {self.target.token}"},
            open_timeout=10,
        )

    async def close(self) -> None:
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None
        if self._owns_client:
            await self._client.aclose()


def _deserialise(raw: list[dict], session_id: str) -> list[ModelMessage]:
    raw = rehydrate_images(raw, session_id)
    repair_masked_narrowed_returns(raw)
    return ModelMessagesTypeAdapter.validate_python(raw)


# ``open_socket(after_seq)`` returns an async context manager yielding an
# async iterator of frames (str | bytes) — websockets' ``connect`` is exactly
# that; the tests substitute a fake.
OpenSocket = Callable[["int | None"], Any]


@dataclass(frozen=True)
class ReconnectPolicy:
    """How a :class:`RemoteSubscription` rides out an outage: the backoff
    bounds, how long before it gives up, and the clock/sleep it measures
    with (injectable so the tests never wait on wall time)."""

    lost_after: float = LOST_AFTER
    backoff: tuple[float, float] = RECONNECT_BACKOFF
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Any] = asyncio.sleep


class RemoteSubscription:
    """A ``bus.Subscription`` look-alike fed by the daemon's WebSocket.

    One task owns the socket. Frames are decoded into ``Event``s and queued;
    ``next_event`` reads the queue under the local ``Subscription``'s
    timeout contract. On loss the task reconnects with ``?after_seq=<last
    delivered>`` (the bus replays what the ring still holds) on a backoff
    from 0.5s to 8s, reporting ``reconnecting`` once per outage and
    ``connected`` on recovery. After ``lost_after`` seconds without a
    connection it reports ``lost`` and stops. A daemon restart resets the bus
    seq; a delivered seq at or below the last one seen is turned into a
    synthetic ``stream.gap`` so the app resyncs from history exactly as it
    does when the resume point fell off the ring."""

    def __init__(
        self,
        open_socket: OpenSocket,
        *,
        after_seq: int | None = None,
        on_state: StateCallback | None = None,
        on_event: Callable[[Event], None] | None = None,
        policy: ReconnectPolicy | None = None,
    ) -> None:
        self._open_socket = open_socket
        self._after_seq = after_seq
        self._on_state = on_state
        self._on_event = on_event
        policy = policy if policy is not None else ReconnectPolicy()
        self._lost_after = policy.lost_after
        self._backoff = policy.backoff
        self._clock = policy.clock
        self._sleep = policy.sleep
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self.last_seq: int | None = None
        self.state = "connecting"
        self._task = asyncio.ensure_future(self._run())

    async def next_event(self, timeout: float | None = None) -> Event | None:
        if timeout is None:
            return await self._queue.get()
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        self._task.cancel()

    # ----------------------------------------------------------- driver --
    def _report(self, state: str) -> None:
        self.state = state
        if self._on_state is not None:
            self._on_state(state)

    def _deliver(self, frame: str | bytes) -> None:
        try:
            data = json.loads(frame)
            event = Event(
                seq=int(data["seq"]),
                ts=str(data.get("ts", "")),
                type=str(data["type"]),
                data=dict(data.get("data") or {}),
            )
        except (ValueError, KeyError, TypeError):
            logger.warning("remote feed: unreadable frame dropped")
            return
        if self.last_seq is not None and event.seq <= self.last_seq and event.type != "stream.gap":
            # The bus restarted (daemon restart): its ring no longer holds
            # what we saw. Say so the way the bus itself does.
            self._queue.put_nowait(
                Event(seq=event.seq, ts=event.ts, type="stream.gap", data={"resync": "history"})
            )
        self.last_seq = event.seq
        if self._on_event is not None:
            self._on_event(event)
        self._queue.put_nowait(event)

    async def _read_until_closed(self) -> None:
        after = self.last_seq if self.last_seq is not None else self._after_seq
        async with self._open_socket(after) as socket:
            self._report("connected")
            async for frame in _frames(socket):
                self._deliver(frame)

    async def _run(self) -> None:
        delay = self._backoff[0]
        down_since: float | None = None
        while True:
            try:
                await self._read_until_closed()
                # A clean server-side close (daemon shutting down) is an
                # outage like any other: reconnect until the cap.
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if _fatal(exc):
                    logger.warning("remote feed closed for good: %s", exc)
                    self._report("lost")
                    return
                logger.info("remote feed dropped: %s", exc)
            now = self._clock()
            if self.state == "connected" or down_since is None:
                # A fresh outage: the last attempt connected (or nothing has
                # yet). The outage clock and the backoff start here.
                down_since = now
                delay = self._backoff[0]
                self._report("reconnecting")
            elif now - down_since >= self._lost_after:
                self._report("lost")
                return
            await self._sleep(delay)
            delay = min(delay * 2, self._backoff[1])


def _frames(socket: Any) -> AsyncIterator[str | bytes]:
    """websockets' connection is itself async-iterable; a fake may hand the
    iterator over directly."""
    if hasattr(socket, "__aiter__"):
        return socket.__aiter__()
    return socket


def _fatal(exc: BaseException) -> bool:
    rcvd = getattr(exc, "rcvd", None)
    if getattr(rcvd, "code", None) in _FATAL_CLOSE_CODES:
        return True
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) in _FATAL_HTTP_STATUSES
