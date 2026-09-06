"""Claude Code's stream-json wire, host side: framing and control correlation.

One JSON object per line in each direction. Outbound ``control_request``s are
keyed by a host-minted ``request_id`` and answered by a ``control_response``
carrying the same id; inbound ``control_request``s (``can_use_tool``) are
answered the same way in reverse. Everything else on stdout (``system``,
``stream_event``, ``assistant``, ``user``, ``result``) is an *event* and goes
to ``on_event`` in arrival order. This module knows only the envelope — what a
``can_use_tool`` means is ``approvals.py``'s business, what a ``result`` means
is ``process.py``'s.

Request handlers run as their own tasks so the reader loop never blocks
behind an approval prompt: the ``control_cancel_request`` for that prompt
(the CLI sends one ahead of an interrupt's aborted ``result``) must still be
read while the prompt is open.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable

from ..ndjson import iter_ndjson_lines

logger = logging.getLogger(__name__)

CLOSED = "__closed__"
_CONTROL_TIMEOUT = 30.0

RequestHandler = Callable[[str, dict], Awaitable[dict]]


class ControlError(Exception):
    """The CLI answered a control_request with ``subtype: "error"``."""


class ProcessClosed(Exception):
    """stdout reached EOF (or stdin broke) while talking to the CLI."""


class StreamJsonClient:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        on_event: Callable[[dict], None],
        on_request: RequestHandler | None = None,
        on_cancel: Callable[[str], None] | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._on_event = on_event
        self._on_request = on_request
        self._on_cancel = on_cancel
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._handlers: dict[str, asyncio.Task[None]] = {}
        self._write_lock = asyncio.Lock()
        self.closed = asyncio.Event()
        # can_use_tool requests currently awaiting an answer. The turn's
        # silence clock (process.turn_objects) pauses while this is > 0: a
        # human deciding in the panel is not the CLI being silent.
        self.prompts_open = 0

    # --- outbound -------------------------------------------------------------
    async def write(self, obj: dict) -> None:
        data = (json.dumps(obj) + "\n").encode()
        async with self._write_lock:
            if self._writer.is_closing():
                raise ProcessClosed("claude stdin is closed")
            self._writer.write(data)
            try:
                await self._writer.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise ProcessClosed(str(exc)) from exc

    async def user(self, text: str) -> None:
        await self.write(
            {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            }
        )

    async def control(self, subtype: str, *, timeout: float | None = None, **fields) -> dict:
        """Send one control_request and await its body. Raises ControlError
        on an ``error`` reply, ProcessClosed on EOF, ``asyncio.TimeoutError``
        after ``timeout`` (default 30 s)."""
        if self.closed.is_set():
            raise ProcessClosed("claude process is closed")
        request_id = uuid.uuid4().hex
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        try:
            await self.write(
                {
                    "type": "control_request",
                    "request_id": request_id,
                    "request": {"subtype": subtype, **fields},
                }
            )
            return await asyncio.wait_for(fut, timeout or _CONTROL_TIMEOUT)
        finally:
            self._pending.pop(request_id, None)

    async def respond(self, request_id: str, response: dict) -> None:
        await self.write(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": request_id, "response": response},
            }
        )

    async def respond_error(self, request_id: str, message: str) -> None:
        await self.write(
            {
                "type": "control_response",
                "response": {"subtype": "error", "request_id": request_id, "error": message},
            }
        )

    # --- inbound --------------------------------------------------------------
    async def run(self) -> None:
        """Read stdout until EOF, routing each object. Always ends by failing
        pending control futures and publishing the CLOSED pseudo-event."""
        try:
            async for raw in iter_ndjson_lines(self._reader):
                obj = _parse(raw)
                if obj is not None:
                    self._route(obj)
        finally:
            self._fail_pending("claude exited")
            self._on_event({"type": CLOSED})

    def _fail_pending(self, why: str) -> None:
        self.closed.set()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ProcessClosed(why))
        self._pending.clear()

    def _route(self, obj: dict) -> None:
        kind = obj.get("type")
        if kind == "control_response":
            self._on_control_response(obj.get("response") or {})
        elif kind == "control_request":
            self._on_control_request(str(obj.get("request_id", "")), obj.get("request") or {})
        elif kind == "control_cancel_request":
            self._on_cancel_request(str(obj.get("request_id", "")))
        else:
            self._on_event(obj)

    def _on_control_response(self, response: dict) -> None:
        fut = self._pending.get(str(response.get("request_id", "")))
        if fut is None or fut.done():
            return
        if response.get("subtype") == "error":
            fut.set_exception(ControlError(str(response.get("error") or "control request failed")))
        else:
            fut.set_result(response.get("response") or {})

    def _on_control_request(self, request_id: str, request: dict) -> None:
        task = asyncio.create_task(self._serve(request_id, request))
        self._handlers[request_id] = task
        task.add_done_callback(lambda _t: self._handlers.pop(request_id, None))

    def _on_cancel_request(self, request_id: str) -> None:
        task = self._handlers.get(request_id)
        if task is not None and not task.done():
            task.cancel()
        if self._on_cancel is not None:
            self._on_cancel(request_id)

    async def _serve(self, request_id: str, request: dict) -> None:
        """One inbound control_request as its own task. A cancelled handler
        (control_cancel_request, or aclose) writes nothing — the CLI has
        already moved on."""
        self.prompts_open += 1
        try:
            with contextlib.suppress(ProcessClosed):
                await self._answer(request_id, request)
        finally:
            self.prompts_open -= 1

    async def _answer(self, request_id: str, request: dict) -> None:
        if self._on_request is None:
            await self.respond_error(request_id, "no request handler bound")
            return
        try:
            body = await self._on_request(request_id, request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the CLI must always get an answer
            logger.warning("claude control_request %s handler failed: %s", request_id, exc)
            await self.respond_error(request_id, str(exc))
            return
        await self.respond(request_id, body)

    async def aclose(self) -> None:
        for task in list(self._handlers.values()):
            task.cancel()
        if self._handlers:
            await asyncio.gather(*self._handlers.values(), return_exceptions=True)
        self._handlers.clear()
        self._fail_pending("client closed")


def _parse(raw: str) -> dict | None:
    line = raw.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        logger.debug("claude stdout noise: %.200s", line)
        return None
    return obj if isinstance(obj, dict) else None
