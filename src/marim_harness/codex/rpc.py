"""Newline-delimited JSON-RPC client for ``codex app-server``.

One reader task (``run``) demultiplexes three message kinds by shape:
``{"id", "result"|"error"}`` completes a pending request; ``{"id", "method"}``
is a server→client request answered through ``on_server_request`` (in its own
task, so a slow approval prompt never blocks the read loop); ``{"method"}``
alone is a notification. The wire carries no ``jsonrpc`` key — the server
omits it and tolerates its absence — so neither do we.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ..subagents.cli_backend import iter_ndjson_lines

logger = logging.getLogger(__name__)

NotificationHandler = Callable[[str, dict], Awaitable[None]]
ServerRequestHandler = Callable[[str, dict], Awaitable[dict]]

CLOSED_CODE = -32000
INTERNAL_ERROR_CODE = -32603


class WriterLike(Protocol):
    def write(self, data: bytes) -> None: ...
    async def drain(self) -> None: ...


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: object | None = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class JsonRpcClient:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: WriterLike,
        *,
        on_notification: NotificationHandler,
        on_server_request: ServerRequestHandler,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._on_notification = on_notification
        self._on_server_request = on_server_request
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._write_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self.closed = asyncio.Event()

    async def _send(self, obj: dict) -> None:
        # One writer at a time: two coroutines interleaving partial lines would
        # corrupt the NDJSON framing the server parses.
        async with self._write_lock:
            self._writer.write((json.dumps(obj) + "\n").encode())
            await self._writer.drain()

    async def request(
        self, method: str, params: dict | None = None, *, timeout: float | None = None
    ) -> dict:
        if self.closed.is_set():
            raise RpcError(CLOSED_CODE, "app-server closed")
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg: dict[str, Any] = {"id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            await self._send(msg)
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        msg: dict[str, Any] = {"method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    async def run(self) -> None:
        """Read until EOF, dispatching each line. Always sets ``closed`` and
        fails every pending request on exit so callers never hang on a dead
        process."""
        try:
            async for line in iter_ndjson_lines(self._reader):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    logger.debug("codex rpc: skipping non-JSON line: %.200s", line)
                    continue
                if isinstance(obj, dict):
                    self._dispatch(obj)
        finally:
            self.closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RpcError(CLOSED_CODE, "app-server closed"))
            self._pending.clear()

    def _dispatch(self, obj: dict) -> None:
        method = obj.get("method")
        if "id" in obj and method is None:
            self._complete(obj)
        elif "id" in obj:
            self._spawn(self._answer(obj["id"], str(method), obj.get("params") or {}))
        elif method is not None:
            self._spawn(self._handle_notification(str(method), obj.get("params") or {}))

    def _complete(self, obj: dict) -> None:
        fut = self._pending.get(obj["id"]) if isinstance(obj["id"], int) else None
        if fut is None or fut.done():
            return
        if "error" in obj:
            err = obj["error"] or {}
            fut.set_exception(
                RpcError(int(err.get("code", -1)), str(err.get("message", "")), err.get("data"))
            )
        else:
            fut.set_result(obj.get("result") or {})

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer(self, rid: object, method: str, params: dict) -> None:
        try:
            result = await self._on_server_request(method, params)
            reply: dict[str, Any] = {"id": rid, "result": result}
        except RpcError as exc:
            reply = {"id": rid, "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:  # noqa: BLE001 - a handler bug must not kill the read loop
            logger.exception("codex rpc: server request %s failed", method)
            reply = {"id": rid, "error": {"code": INTERNAL_ERROR_CODE, "message": str(exc)}}
        await self._send(reply)

    async def _handle_notification(self, method: str, params: dict) -> None:
        """Wrap notification handler to prevent exceptions from killing the read loop."""
        try:
            await self._on_notification(method, params)
        except Exception:  # noqa: BLE001 - a handler bug must not kill the read loop
            logger.exception("codex rpc: notification %s failed", method)
