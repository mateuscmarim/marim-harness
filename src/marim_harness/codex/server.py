"""One ``codex app-server`` process, shared by the main-loop model, its
ephemeral aux clones and every ``backend: codex-cli`` spawn.

Threads are the unit of isolation: each caller gets a ``ThreadHandle`` whose
``events`` queue receives only that thread's notifications, and whose
``request_handler`` answers only that thread's approval prompts. The process
is respawned lazily on the next ``start()`` after it dies; the death itself
is broadcast to every open handle as a ``CLOSED`` pseudo-notification so an
in-flight turn fails cleanly (the model layer turns it into ``CliModelError``).

The process is launched with config overrides that keep it from loading the
user's own Codex extensions (spec §Isolation): every MCP server the user's
config declares is disabled by name — enumerated first through
``codex mcp list --json`` — and the plugin system plus the built-in apps
connector are switched off. See ``env.isolation_overrides`` for why it has
to be per server. Disabled servers still APPEAR in ``mcpServerStatus/list``
(with no ``serverInfo`` and no tools), so "loaded" is measured by
``connected_mcp_servers``, not by presence in that list.
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
    isolation_overrides,
    parse_mcp_server_names,
    resolve_codex_binary,
)
from .rpc import JsonRpcClient, RpcError, ServerRequestHandler

logger = logging.getLogger(__name__)

CLOSED = "__closed__"
_STDERR_TAIL_LINES = 40
_INIT_TIMEOUT = 30.0
_KILL_GRACE = 2.0
# `codex mcp list --json` is a config read (~0.1 s); anything longer is a
# wedged CLI, and the app-server start should not hang on it.
_MCP_LIST_TIMEOUT = 10.0
# Methods whose params name the thread under ``thread.id`` instead of ``threadId``.
_THREAD_OBJ_METHODS = frozenset({"thread/started"})


async def configured_mcp_servers(binary: str, env: dict[str, str] | None) -> list[str]:
    """Names of the MCP servers the user's Codex config would load, read
    through the CLI's own ``mcp list --json`` so marim never parses
    ``config.toml`` itself. Best-effort: any failure yields ``[]`` with a
    warning — the app-server then starts with only the plugin override and
    the isolation gap is visible in the log rather than fatal."""
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "mcp",
            "list",
            "--json",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        logger.warning("codex mcp list could not launch: %s", exc)
        return []
    try:
        out, err = await asyncio.wait_for(proc.communicate(), _MCP_LIST_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("codex mcp list timed out after %.0fs", _MCP_LIST_TIMEOUT)
        return []
    if proc.returncode != 0:
        tail = err.decode("utf-8", "replace").strip()[-400:]
        logger.warning("codex mcp list exited %s: %s", proc.returncode, tail)
        return []
    return parse_mcp_server_names(out)


@dataclass
class ThreadHandle:
    thread_id: str
    events: asyncio.Queue[tuple[str, dict]]
    request_handler: ServerRequestHandler
    usage_baseline: dict = field(default_factory=dict)
    current_turn_id: str | None = None
    last_completed_turn_id: str | None = None
    # Set on a handle made by ``CodexServer.adopt_thread``: the thread id of
    # the parent whose ``events`` queue and ``request_handler`` this child
    # shares. ``drop_thread(parent)`` drops every handle pointing at it.
    parent_id: str | None = None
    # The agent name Codex announced for an adopted child (``thread/started``
    # ``agentNickname``/``agentRole``), recorded on the reader task so an
    # approval request the child sends before the consumer has dequeued the
    # announcement can still be labelled (``CollabRouter.label_for``).
    label: str | None = None

    def note_turn_completed(self, params: dict) -> None:
        """Bookkeeping for ``turn/completed``.

        The completed id is remembered so ``start_turn`` can tell that the
        turn it just got the response for has ALREADY finished (see there).
        The current id is cleared only when it is the one that completed: an
        interrupted turn's completion can still be in flight when the NEXT
        turn starts on the same thread (``turn.py`` filters it on the consumer
        side for the same reason), and clearing unconditionally would blind
        ``interrupt``/``steer`` to the turn that is actually running. A
        notification without a turn id cannot be matched and clears as before.
        """
        turn = params.get("turn")
        completed = str(turn["id"]) if isinstance(turn, dict) and turn.get("id") else None
        if completed is not None:
            self.last_completed_turn_id = completed
        if completed is None or completed == self.current_turn_id:
            self.current_turn_id = None


@dataclass(frozen=True)
class ThreadOptions:
    """The fields ``thread/start`` and ``thread/resume`` share, bundled so
    each RPC method takes one value object instead of five-to-six loose
    keyword arguments (a ``PLR0913`` the ratchet gate flags). Both RPCs now
    forward ``ephemeral`` symmetrically (final review Minor #7) even though
    no caller resumes into an ephemeral thread today (ephemeral models never
    persist a ref to resume) — keeping the params in sync now means whoever
    adds a resumable ephemeral path later isn't bitten by a silently-dropped
    field."""

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


def thread_id_for(method: str, params: dict) -> str | None:
    """The thread a notification/server request belongs to (``threadId``, or
    ``thread.id`` for the methods that carry a whole Thread object); None for
    a global one. Shared with ``codex/collab.py``, which keys child-thread
    traffic on the same field."""
    if method in _THREAD_OBJ_METHODS:
        tid = (params.get("thread") or {}).get("id")
    else:
        tid = params.get("threadId")
    return str(tid) if tid else None


def thread_label(thread: dict) -> str:
    """The agent name a ``thread/started`` Thread object carries for a collab
    child (nickname, else role); empty for a top-level thread."""
    return str(thread.get("agentNickname") or thread.get("agentRole") or "")


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
        self._starting = 0  # thread/start + thread/resume requests in flight
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

    def handle_for(self, thread_id: str) -> ThreadHandle | None:
        """The handle registered for ``thread_id`` (a started thread or an
        adopted child), None once dropped or never known."""
        return self._threads.get(thread_id)

    def thread_label(self, thread_id: str) -> str | None:
        """The announced agent name of an adopted child, if Codex sent one
        (``ThreadHandle.label``); None for unknown or top-level threads."""
        handle = self._threads.get(thread_id)
        return handle.label if handle is not None else None

    @property
    def idle(self) -> bool:
        """No thread registered AND none being started. A thread is only
        registered once ``thread/start``/``thread/resume`` answers, so
        ``thread_ids`` alone reads as empty while a caller is mid-request —
        exactly when ``close_shared_server_if_idle`` must NOT pull the
        process out from under it (a daemon starts sessions concurrently)."""
        return not self._threads and self._starting == 0

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
            await self._spawn(binary, await self._isolation_argv(binary))
            await self._handshake()

    async def _isolation_argv(self, binary: str) -> list[str]:
        names = await configured_mcp_servers(binary, self._env)
        argv, skipped = isolation_overrides(names)
        if skipped:
            logger.warning(
                "codex MCP servers whose names are not bare TOML keys cannot be "
                "disabled by override and WILL load in marim threads: %s",
                ", ".join(skipped),
            )
        logger.info(
            "codex app-server isolation: plugins/apps off, %d of %d MCP server(s) disabled",
            len(names) - len(skipped),
            len(names),
        )
        return argv

    async def _spawn(self, binary: str, overrides: list[str]) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                binary,
                *overrides,
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
        tid = thread_id_for(method, params)
        return self._threads.get(tid) if tid is not None else None

    async def _on_notification(self, method: str, params: dict) -> None:
        if method == "thread/started":
            self._adopt_announced(params)
        elif method == "item/started":
            self._adopt_pinged(params)
        tid = thread_id_for(method, params)
        if tid is None:
            # No threadId at all: a genuinely global notification (nothing in
            # the wire protocol names one today, but nothing rules it out
            # either) — fan out to every live thread.
            queues: set[int] = set()
            for h in list(self._threads.values()):
                if id(h.events) not in queues:
                    queues.add(id(h.events))
                    h.events.put_nowait((method, params))
            return
        handle = self._threads.get(tid)
        if handle is None:
            # A thread-scoped notification for a thread we no longer have
            # registered — most commonly a spawn's trailing item/turn deltas
            # still in flight when its `finally: server.drop_thread(handle)`
            # ran (cancellation, or a fast completion racing the last few
            # events). This is expected, not an error: broadcasting it instead
            # would fold a deregistered spawn's prose/tool-cards, or even a
            # `turn/failed`, into every OTHER thread sharing this process —
            # see Important #2 of the final review.
            logger.debug("codex notification %r for unknown thread %s dropped", method, tid)
            return
        if method == "turn/completed":
            handle.note_turn_completed(params)
        handle.events.put_nowait((method, params))

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
            }
        )
        result = await self._request_thread("thread/start", params)
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
                "ephemeral": options.ephemeral,
                "sandbox": options.sandbox,
                "approvalPolicy": options.approval_policy,
            }
        )
        try:
            result = await self._request_thread("thread/resume", params)
        except RpcError as exc:
            logger.info("codex thread %s not resumable: %s", thread_id, exc)
            return None
        return self._register(result["thread"], request_handler)

    async def _request_thread(self, method: str, params: dict) -> dict:
        """A thread-creating request, counted so ``idle`` stays False until
        the thread is registered (or the request failed)."""
        self._starting += 1
        try:
            return await self._rpc().request(method, params, timeout=self._timeout)
        finally:
            self._starting -= 1

    def _adopt_announced(self, params: dict) -> None:
        """Adopt a thread Codex announces as a registered thread's child
        (``thread/started`` with ``parentThreadId``) right here on the reader
        task, BEFORE its first item can arrive. The collab router adopts too,
        when it sees the parent's ``spawnAgent`` item — but that happens on
        the consuming task, and the child's ``thread/started`` (or its first
        approval request) can be dispatched before the consumer has dequeued
        the spawn item; without this, that early traffic would be dropped as
        an unknown thread (deterministically so against the scripted fake).
        Idempotent with the router's later adopt — and either way the
        announced agent name lands on the child's handle here, so a request
        in that same window is labelled rather than a bare ``agent``."""
        thread = params.get("thread") or {}
        child_id, parent_id = str(thread.get("id") or ""), str(thread.get("parentThreadId") or "")
        parent = self._threads.get(parent_id) if parent_id else None
        if parent is None or not child_id:
            return
        handle = self.adopt_thread(parent, child_id)
        if handle.parent_id == parent.thread_id:  # not a top-level thread left alone
            handle.label = thread_label(thread) or handle.label

    def _adopt_pinged(self, params: dict) -> None:
        """The same reader-task adoption for the shape codex 0.154 actually
        sends (PR #128's live probe): no ``thread/started`` for the child
        at all, the spawn reported as a ``subAgentActivity`` ``started``
        ping on the parent, with the child's first ``turn/started`` right
        behind it — the router's own adopt (on the consuming task) may not
        have run yet. The label comes from ``agentPath``'s last segment."""
        item = params.get("item") or {}
        if item.get("type") != "subAgentActivity" or item.get("kind") != "started":
            return
        parent = self._threads.get(str(params.get("threadId") or ""))
        child_id = str(item.get("agentThreadId") or "")
        if parent is None or not child_id:
            return
        handle = self.adopt_thread(parent, child_id)
        if handle.parent_id == parent.thread_id and handle.label is None:
            path = str(item.get("agentPath") or "")
            handle.label = path.rsplit("/", 1)[-1] or None

    def adopt_thread(self, parent: ThreadHandle, child_id: str) -> ThreadHandle:
        """Register a thread Codex spawned on the parent's behalf (a collab
        sub-agent, ``codex/collab.py``) so its notifications and approval
        requests are no longer dropped as "unknown thread".

        The child handle SHARES the parent's ``events`` queue and
        ``request_handler``. Sharing the queue is the whole trick: the
        parent's ``turn_events`` loop already drains it, and every
        notification carries its ``threadId``, so child traffic interleaves
        with the parent's in arrival order — no second consumer task, no
        cross-task lifetime to manage. Sharing the handler gives the child's
        approvals the parent's ``ApprovalBroker``: same mode, same panel,
        same serialization lock. Idempotent: adopting a registered child
        again returns its existing handle; a thread registered as a
        top-level one is left alone."""
        existing = self._threads.get(child_id)
        if existing is not None:
            return existing
        handle = ThreadHandle(
            thread_id=child_id,
            events=parent.events,
            request_handler=parent.request_handler,
            parent_id=parent.thread_id,
        )
        self._threads[child_id] = handle
        return handle

    def drop_thread(self, handle: ThreadHandle) -> None:
        """Deregister ``handle`` and every child adopted under it, so a
        dropped parent's sub-agents cannot keep feeding a queue nobody drains
        (or pin the shared server open through ``idle``)."""
        self._threads.pop(handle.thread_id, None)
        for child in [h for h in self._threads.values() if h.parent_id == handle.thread_id]:
            self.drop_thread(child)

    def release_thread(self, thread_id: str) -> None:
        """``drop_thread`` by id — the collab router's ``release`` hook for a
        child Codex reports gone (``shutdown``/``notFound``). Unknown ids are
        a no-op."""
        handle = self._threads.get(thread_id)
        if handle is not None:
            self.drop_thread(handle)

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
        # The reader resolves this response's future and keeps draining the
        # pipe before this coroutine resumes, so a turn that finishes fast
        # can have its `turn/completed` dispatched BEFORE we get here. Marking
        # it current then would leave a stale id behind (an interrupt would
        # target a finished turn) — the handle remembers what completed.
        if handle.last_completed_turn_id != turn_id:
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

    async def steer(
        self, handle: ThreadHandle, text: str, *, inputs: list[dict] | None = None
    ) -> bool:
        turn_id = handle.current_turn_id
        if turn_id is None or not self.alive:
            return False
        try:
            await self._rpc().request(
                "turn/steer",
                {
                    "threadId": handle.thread_id,
                    "expectedTurnId": turn_id,
                    "input": inputs if inputs is not None else [_text_input(text)],
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
        if not isinstance(data, list):
            return []
        return [m for m in data if isinstance(m, dict)]

    async def connected_mcp_servers(self) -> list[str]:
        """Names of the MCP servers the LIVE app-server actually connected
        (``mcpServerStatus/list``, first page) — the ground truth the launch
        overrides are measured against; the live smoke asserts it is empty.

        The list enumerates every configured server, disabled ones included:
        a disabled entry carries ``serverInfo: null`` and an empty ``tools``
        table, a connected one has both populated. Presence alone therefore
        proves nothing; only entries with server info or tools count."""
        result = await self._rpc().request("mcpServerStatus/list", {}, timeout=30.0)
        data = result.get("data")
        if not isinstance(data, list):
            return []
        return [
            str(entry["name"])
            for entry in data
            if isinstance(entry, dict)
            and "name" in entry
            and (entry.get("serverInfo") or entry.get("tools"))
        ]

    async def read_rate_limits(self) -> dict | None:
        """The account's ``RateLimitSnapshot`` (``account/rateLimits/read``),
        or None when the response carries none. Raises like any RPC — the
        caller decides whether a failure matters (the status-line quota hint
        ignores it; see ``codex/quota.py``)."""
        # Short: this runs between the last token and the turn completing,
        # so a wedged app-server must not add a long tail to every turn.
        result = await self._rpc().request("account/rateLimits/read", {}, timeout=3.0)
        limits = result.get("rateLimits")
        return limits if isinstance(limits, dict) else None


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


def peek_shared_server() -> CodexServer | None:
    """The process-wide singleton if one already exists, or ``None`` —
    unlike ``shared_server()``, never creates one as a side effect of
    merely asking. Used by callers (``codex/catalog.py``'s ``list_codex_models``)
    that must reuse an already-running singleton without becoming its owner,
    while still falling back to a private, throwaway server when none
    exists yet."""
    return _shared


def is_shared_server(server: CodexServer) -> bool:
    """True when ``server`` is the current process-wide singleton, whether or
    not it has any threads registered — used by ``CodexCliModel.aclose()`` to
    tell "my own private CodexServer" (a test, or an embedder wiring one up
    directly) from "the shared one every codex-cli harness/spawn/ephemeral
    clone in this process resolves to", without ever calling
    ``shared_server()`` itself (which would create one as a side effect of
    merely asking)."""
    return server is _shared


async def close_shared_server_if_idle() -> None:
    """Close the shared app-server only once no thread is registered on it.

    ``close_shared_server()`` closes unconditionally — right for tests and
    explicit teardown (live-smoke), wrong for a single ``Harness.aclose()``
    to call, because the shared server is process-wide: a daemon holds many
    concurrent ``Harness``es (and their sub-agent spawns, and ephemeral aux
    clones) over ONE app-server, so closing it whenever any one of them
    exits would sever every other session's thread mid-flight. A harness
    should drop its own thread first (``server.drop_thread(handle)``), then
    call this — the server goes away only once every last thread using it
    is gone, same lifecycle promise ``close_shared_server()``'s callers
    already relied on when there was only ever one user of the singleton.
    """
    global _shared
    if _shared is None or not _shared.idle:
        return
    # Release the slot BEFORE the (awaiting) close: a harness resolving the
    # singleton meanwhile gets a fresh server instead of joining this dying
    # one — its `thread/start` would otherwise land on a process mid-SIGTERM.
    dying, _shared = _shared, None
    await dying.aclose()
