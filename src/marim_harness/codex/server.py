"""One ``codex app-server`` process, shared by the main-loop model, its
ephemeral aux clones and every ``backend: codex-cli`` spawn.

Threads are the unit of isolation: each caller gets a ``ThreadHandle`` whose
``events`` queue receives only that thread's notifications, and whose
``request_handler`` answers only that thread's approval prompts. The process
is respawned lazily on the next ``start()`` after it dies; the death itself
is broadcast to every open handle as a ``CLOSED`` pseudo-notification so an
in-flight turn fails cleanly (the model layer turns it into ``CliModelError``).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import importlib.metadata
import logging
import os
import signal
from dataclasses import dataclass, field
from typing import Any

from .env import (
    INSTALL_HINT,
    CodexUnavailable,
    check_min_version,
    codex_timeout,
    resolve_codex_binary,
)
from .rpc import JsonRpcClient, RpcError, ServerRequestHandler

logger = logging.getLogger(__name__)

CLOSED = "__closed__"
_STDERR_TAIL_LINES = 40
_INIT_TIMEOUT = 30.0
_KILL_GRACE = 2.0
# Methods whose params name the thread under ``thread.id`` instead of ``threadId``.
_THREAD_OBJ_METHODS = frozenset({"thread/started"})


def thread_config() -> dict:
    """``thread/start.config`` overrides that stop the thread from loading the
    user's global Codex MCP servers/plugins (marim owns tool reach; see spec
    §Isolation). PROVISIONAL until Task 0 confirms the key names — if no
    config key works, the fallback is a marim-owned CODEX_HOME (Step 7)."""
    return {"mcp_servers": {}}


@dataclass
class ThreadHandle:
    thread_id: str
    events: asyncio.Queue[tuple[str, dict]]
    request_handler: ServerRequestHandler
    usage_baseline: dict = field(default_factory=dict)
    current_turn_id: str | None = None


@dataclass(frozen=True)
class ThreadOptions:
    """The fields ``thread/start`` and ``thread/resume`` share, bundled so
    each RPC method takes one value object instead of five-to-six loose
    keyword arguments (a ``PLR0913`` the ratchet gate flags). ``resume_thread``
    ignores ``ephemeral`` — Codex has no notion of resuming into an ephemeral
    thread — but taking the same type keeps both call sites uniform rather
    than growing a second, almost-identical options class."""

    cwd: str
    developer_instructions: str | None
    model: str | None
    sandbox: str
    approval_policy: str
    ephemeral: bool = False


@dataclass(frozen=True)
class TurnOptions:
    """Everything ``turn/start`` needs beyond the thread it runs on, bundled
    for the same reason as ``ThreadOptions``."""

    inputs: list[dict]
    model: str | None
    effort: str | None
    approval_policy: str
    sandbox_policy: dict
    output_schema: dict | None = None


def _text_input(text: str) -> dict:
    return {"type": "text", "text": text, "text_elements": []}


def _drop_none(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}


class CodexServer:
    def __init__(
        self,
        *,
        binary: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> None:
        self._binary = binary
        self._env = env
        self._timeout = timeout if timeout is not None else codex_timeout()
        self._proc: asyncio.subprocess.Process | None = None
        self._client: JsonRpcClient | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=_STDERR_TAIL_LINES)
        self._threads: dict[str, ThreadHandle] = {}
        self._start_lock = asyncio.Lock()

    @property
    def alive(self) -> bool:
        return (
            self._proc is not None
            and self._proc.returncode is None
            and self._client is not None
            and not self._client.closed.is_set()
        )

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def thread_ids(self) -> frozenset[str]:
        """Threads registered with the LIVE process (cleared on respawn), so a
        model can tell a stale handle from a usable one."""
        return frozenset(self._threads)

    # --- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        async with self._start_lock:
            if self.alive:
                return
            await self._reap()
            binary = self._binary or resolve_codex_binary()
            if binary is None or not os.path.exists(binary):
                raise CodexUnavailable(f"codex binary not found. {INSTALL_HINT}")
            self._threads.clear()
            await self._spawn(binary)
            await self._handshake()

    async def _spawn(self, binary: str) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                binary,
                "app-server",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
                # Own process group so aclose() can kill helpers codex forks.
                start_new_session=True,
            )
        except OSError as exc:
            raise CodexUnavailable(f"could not launch {binary}: {exc}. {INSTALL_HINT}") from exc
        assert self._proc.stdout is not None and self._proc.stdin is not None
        self._stderr_tail.clear()
        self._client = JsonRpcClient(
            self._proc.stdout,
            self._proc.stdin,
            on_notification=self._on_notification,
            on_server_request=self._on_server_request,
        )
        self._reader_task = asyncio.create_task(self._run_reader(self._client))
        self._stderr_task = asyncio.create_task(self._pump_stderr())

    async def _handshake(self) -> None:
        assert self._client is not None
        try:
            version = importlib.metadata.version("marim-harness")
        except importlib.metadata.PackageNotFoundError:
            version = "dev"
        try:
            result = await self._client.request(
                "initialize",
                {
                    "clientInfo": {"name": "marim-harness", "version": version},
                    "capabilities": {"experimentalApi": False},
                },
                timeout=_INIT_TIMEOUT,
            )
        except (RpcError, asyncio.TimeoutError) as exc:
            await self.aclose()
            raise CodexUnavailable(f"codex app-server initialize failed: {exc}") from exc
        try:
            check_min_version(str(result.get("userAgent", "")))
        except CodexUnavailable:
            await self.aclose()
            raise
        await self._client.notify("initialized")

    async def _run_reader(self, client: JsonRpcClient) -> None:
        try:
            await client.run()
        finally:
            # Process gone (or stdout closed): every open thread learns it once.
            # The stderr pump can lag the stdout EOF by a tick, so give it a
            # short grace window before reading the tail — otherwise the last
            # line or two (often the one explaining the crash) is dropped.
            # Only the *timeout* is swallowed here: a genuine task-level
            # cancellation (delivered by `_reap()` when a respawn races a
            # reader still stuck in this wait) must keep propagating, so this
            # coroutine exits here instead of running on to broadcast a stale
            # CLOSED into `self._threads` after it may already hold the next
            # generation's handles.
            if self._stderr_task is not None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(self._stderr_task), 0.5)
            tail = "\n".join(self._stderr_tail)
            for handle in list(self._threads.values()):
                handle.current_turn_id = None
                handle.events.put_nowait((CLOSED, {"stderr": tail}))

    async def _pump_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        async for raw in self._proc.stderr:
            self._stderr_tail.append(raw.decode("utf-8", "replace").rstrip("\n"))

    async def _reap(self) -> None:
        """Forget a dead process (and its tasks) before spawning a new one.

        Firing `.cancel()` and moving on isn't enough: cancellation only
        lands at a task's *next* suspension point, so returning immediately
        would let `start()` go on to spawn a new process and register new
        threads while the previous generation's `_run_reader` is still
        mid-flight inside its own stderr-grace wait (see that method).
        Awaiting the cancelled tasks to completion closes that window — by
        the time `_reap()` returns, the old reader has either finished its
        broadcast or been genuinely cancelled out of it, so a freshly
        registered thread on the new process can never receive a stale
        CLOSED meant for the generation that died.
        """
        tasks = [t for t in (self._reader_task, self._stderr_task) if t is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_task = self._stderr_task = None
        self._client = None
        self._proc = None

    async def aclose(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), _KILL_GRACE)
            except (ProcessLookupError, PermissionError):
                pass
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
        await self._reap()
        self._threads.clear()

    # --- routing ------------------------------------------------------------
    def _handle_for(self, method: str, params: dict) -> ThreadHandle | None:
        if method in _THREAD_OBJ_METHODS:
            tid = (params.get("thread") or {}).get("id")
        else:
            tid = params.get("threadId")
        return self._threads.get(str(tid)) if tid else None

    async def _on_notification(self, method: str, params: dict) -> None:
        handle = self._handle_for(method, params)
        targets = [handle] if handle is not None else list(self._threads.values())
        if method == "turn/completed" and handle is not None:
            handle.current_turn_id = None
        for h in targets:
            h.events.put_nowait((method, params))

    async def _on_server_request(self, method: str, params: dict) -> dict:
        handle = self._handle_for(method, params)
        if handle is None:
            raise RpcError(-32601, f"no thread for {method}")
        return await handle.request_handler(method, params)

    # --- threads ------------------------------------------------------------
    def _rpc(self) -> JsonRpcClient:
        if self._client is None or not self.alive:
            raise CodexUnavailable("codex app-server is not running")
        return self._client

    def _register(self, thread: dict, request_handler: ServerRequestHandler) -> ThreadHandle:
        handle = ThreadHandle(
            thread_id=str(thread["id"]), events=asyncio.Queue(), request_handler=request_handler
        )
        self._threads[handle.thread_id] = handle
        return handle

    async def start_thread(
        self, *, options: ThreadOptions, request_handler: ServerRequestHandler
    ) -> ThreadHandle:
        params = _drop_none(
            {
                "cwd": options.cwd,
                "developerInstructions": options.developer_instructions,
                "model": options.model,
                "ephemeral": options.ephemeral,
                "sandbox": options.sandbox,
                "approvalPolicy": options.approval_policy,
                "config": thread_config(),
            }
        )
        result = await self._rpc().request("thread/start", params, timeout=self._timeout)
        return self._register(result["thread"], request_handler)

    async def resume_thread(
        self, thread_id: str, *, options: ThreadOptions, request_handler: ServerRequestHandler
    ) -> ThreadHandle | None:
        params = _drop_none(
            {
                "threadId": thread_id,
                "cwd": options.cwd,
                "developerInstructions": options.developer_instructions,
                "model": options.model,
                "sandbox": options.sandbox,
                "approvalPolicy": options.approval_policy,
                "config": thread_config(),
            }
        )
        try:
            result = await self._rpc().request("thread/resume", params, timeout=self._timeout)
        except RpcError as exc:
            logger.info("codex thread %s not resumable: %s", thread_id, exc)
            return None
        return self._register(result["thread"], request_handler)

    def drop_thread(self, handle: ThreadHandle) -> None:
        self._threads.pop(handle.thread_id, None)

    # --- turns --------------------------------------------------------------
    async def start_turn(self, handle: ThreadHandle, *, options: TurnOptions) -> str:
        params = _drop_none(
            {
                "threadId": handle.thread_id,
                "input": options.inputs,
                "model": options.model,
                "effort": options.effort,
                "approvalPolicy": options.approval_policy,
                "sandboxPolicy": options.sandbox_policy,
                "outputSchema": options.output_schema,
            }
        )
        result = await self._rpc().request("turn/start", params, timeout=self._timeout)
        turn_id = str(result["turn"]["id"])
        handle.current_turn_id = turn_id
        return turn_id

    async def interrupt(self, handle: ThreadHandle) -> None:
        turn_id = handle.current_turn_id
        if turn_id is None or not self.alive:
            return
        try:
            await self._rpc().request(
                "turn/interrupt", {"threadId": handle.thread_id, "turnId": turn_id}, timeout=10.0
            )
        except (RpcError, asyncio.TimeoutError, CodexUnavailable) as exc:
            logger.debug("codex interrupt of %s ignored: %s", turn_id, exc)

    async def steer(self, handle: ThreadHandle, text: str) -> bool:
        turn_id = handle.current_turn_id
        if turn_id is None or not self.alive:
            return False
        try:
            await self._rpc().request(
                "turn/steer",
                {
                    "threadId": handle.thread_id,
                    "expectedTurnId": turn_id,
                    "input": [_text_input(text)],
                },
                timeout=10.0,
            )
        except (RpcError, asyncio.TimeoutError, CodexUnavailable) as exc:
            logger.info("codex steer rejected: %s", exc)
            return False
        return True

    async def compact(self, handle: ThreadHandle) -> None:
        if not self.alive:
            return
        try:
            await self._rpc().request(
                "thread/compact/start", {"threadId": handle.thread_id}, timeout=self._timeout
            )
        except (RpcError, asyncio.TimeoutError, CodexUnavailable) as exc:
            logger.info("codex compact skipped: %s", exc)

    async def list_models(self) -> list[dict]:
        result = await self._rpc().request("model/list", {}, timeout=30.0)
        data = result.get("data")
        return [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []


# --- shared instance --------------------------------------------------------
_shared: CodexServer | None = None


def shared_server() -> CodexServer:
    """The process-wide server. Created lazily so importing this module (or
    building a CodexCliModel) never spawns anything; ``start()`` does."""
    global _shared
    if _shared is None:
        _shared = CodexServer()
    return _shared


async def close_shared_server() -> None:
    global _shared
    if _shared is not None:
        await _shared.aclose()
        _shared = None
