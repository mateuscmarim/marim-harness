"""One long-lived bidirectional ``claude`` process (spec §Process supervisor).

``ClaudeProcess`` spawns the CLI with the isolation argv, runs the
``StreamJsonClient`` reader as a task, routes every stdout object to the
*current* turn's queue, and owns the three clocks the spec names: the
silence timeout (per turn, paused while an approval prompt is open), the
idle reaper (between turns; the next turn resumes by session id), and the
shutdown grace (SIGTERM the group, then ``kill_process_tree``).

The model layer (``config/claude_cli_model.py``) and the spawn runner
(``subagents/cli_backend.py``) both consume it through ``send_turn`` +
``turn_objects``; neither touches the pipes.

A turn is always addressed by the ``TurnHandle`` ``send_turn`` returned, never
by asking the process which turn is "current": on a loaded machine the CLI's
terminal ``result`` can be read, routed and the turn closed *before*
``send_turn`` even returns to its caller, so any lookup through process state
would race.

Claude Code also runs turns marim never asked for. Its Agent tool launches
sub-agents in the background (``system/task_started`` with
``is_backgrounded``); when one finishes while no turn is open, the CLI
injects the ``task_notification`` into its own history and runs a model turn
on it — a fresh ``system/init``, the assistant's reaction, a ``result`` —
with no user message from marim in between (probed 2026-09-13). Those
objects are buffered on an *unsolicited* ``TurnHandle`` (``take_unsolicited``
hands it to the consumer that runs marim's matching autonomous turn) instead
of being dropped, and the idle reaper holds off while a background sub-agent
is still running — closing the process would kill it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from ..config.external_cli import CliModelError
from ..tools.impl.process import kill_process_tree
from .env import INSTALL_HINT
from .protocol import CLOSED, ProcessClosed, RequestHandler, StreamJsonClient

logger = logging.getLogger(__name__)

_INIT_TIMEOUT = 30.0
_INTERRUPT_GRACE = 2.0
# Cap on the once-per-turn `get_usage` poll: a status-line nicety must never
# hold the turn's settle for the default 30 s control timeout.
_USAGE_TIMEOUT = 5.0
_TERM_GRACE = 2.0
# stdout EOF and the child reaper race: without a short settle the synthetic
# CLOSED object would carry a half-read stderr tail and returncode None.
_EXIT_SETTLE = 0.5
_STDERR_LINES = 40
_STDERR_TAIL_CHARS = 2000

# Isolation + framing flags every launch carries (spec §Isolation). Never
# ``--bare`` (breaks subscription auth) and never ``--permission-mode`` —
# every permission decision is brokered over stdio instead.
ISOLATION_ARGV: tuple[str, ...] = (
    "--strict-mcp-config",
    "--setting-sources",
    "",
    "--safe-mode",
    "--permission-prompts",
    "host",
    "--permission-prompt-tool",
    "stdio",
    "--input-format",
    "stream-json",
    "--output-format",
    "stream-json",
    "--verbose",
    "--include-partial-messages",
    "--replay-user-messages",
)

# A marim launched from inside Claude Code inherits these; a child claude that
# sees them thinks it is nested and refuses or misroutes.
_STRIPPED_ENV = ("CLAUDE_CODE_SSE_PORT", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")

# Objects the CLI emits between turns (a task notification, a background-tasks
# status patch) are kept for the next turn to see; a CLI that chattered for
# ever with nobody consuming would otherwise grow the buffer without bound.
_PRELUDE_LIMIT = 64
# While a background sub-agent runs the idle clock is stretched by this factor
# rather than stopped: Claude Code always ends an agent with a notification, so
# the hold is only ever a bound on a report that will never come.
_BACKGROUND_HOLD_FACTOR = 10

# The callback fired when the CLI opens a turn of its own: receives the
# objects the turn opened with (any inter-turn prelude — the task
# notification that triggered it — plus its ``system/init``).
UnsolicitedHandler = Callable[[list[dict]], None]


@dataclass(frozen=True)
class ProcessOptions:
    binary: str
    cwd: str
    model: str | None = None
    resume_id: str | None = None
    tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    append_system: str | None = None
    persist: bool = True
    env: dict[str, str] | None = None


def build_process_argv(options: ProcessOptions) -> list[str]:
    argv = [options.binary, *ISOLATION_ARGV]
    if options.model:
        argv += ["--model", options.model]
    if options.resume_id:
        argv += ["--resume", options.resume_id]
    if options.tools:
        argv += ["--tools", ",".join(options.tools)]
    if options.disallowed_tools:
        argv += ["--disallowedTools", ",".join(options.disallowed_tools)]
    if options.append_system:
        argv += ["--append-system-prompt", options.append_system]
    if not options.persist:
        argv.append("--no-session-persistence")
    return argv


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if base is None else base
    return {k: v for k, v in source.items() if k not in _STRIPPED_ENV}


@dataclass
class TurnHandle:
    """The receiving side of one turn. ``events`` carries every stdout object
    routed to the turn, ending with a ``result`` or a synthetic ``CLOSED``."""

    events: asyncio.Queue[dict] = field(default_factory=asyncio.Queue)
    open: bool = True
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    def finish(self) -> None:
        self.open = False
        self.closed.set()


def _is_terminal(obj: dict) -> bool:
    return obj.get("type") in ("result", CLOSED)


async def _drain(*futures: asyncio.Future[Any]) -> None:
    """Cancel anything still running and retrieve every outcome.

    Retrieving matters: a fire-and-forget control write that fails would
    otherwise surface as "exception was never retrieved" at GC, and one left
    merely cancelled (never awaited) as "Task was destroyed but it is
    pending" at loop teardown. Both are noise that hides real defects.
    """
    for fut in futures:
        if not fut.done():
            fut.cancel()
    for fut in futures:
        try:
            await fut
        except asyncio.CancelledError:
            # Expected for the futures we just cancelled. The callers that run
            # inside a cancelled task (``turn_objects``) re-raise explicitly,
            # so swallowing here cannot lose their own cancellation.
            pass
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            logger.debug("claude control future failed: %s", exc)


class ClaudeProcess:
    def __init__(
        self,
        options: ProcessOptions,
        *,
        on_request: RequestHandler | None = None,
        silence_timeout: float = 0.0,
        idle_timeout: float = 0.0,
        on_unsolicited: UnsolicitedHandler | None = None,
    ) -> None:
        self._opts = options
        self._on_request = on_request
        self._on_unsolicited = on_unsolicited
        self.silence_timeout = silence_timeout
        self._idle_timeout = idle_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._client: StreamJsonClient | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_LINES)
        self._turn: TurnHandle | None = None
        # Turns the CLI ran on its own (see the module docstring): `_own_turn`
        # is the one currently streaming (routing goes there, taken or not),
        # `_unsolicited` the ones not yet handed to a consumer, oldest first.
        # Objects that arrived between turns without opening one (a
        # notification the CLI did not react to yet) wait in the prelude for
        # whichever turn opens next. `_background` is the set of sub-agents
        # still running in the background — while it is non-empty the idle
        # clock runs `_BACKGROUND_HOLD_FACTOR` times longer.
        self._own_turn: TurnHandle | None = None
        self._unsolicited: deque[TurnHandle] = deque()
        self._prelude: deque[dict] = deque(maxlen=_PRELUDE_LIMIT)
        self._background: set[str] = set()
        # Bumped on every control_request handler entry AND exit, so the turn's
        # silence clock can tell that a prompt was open at some point inside a
        # window even when it has already closed again (see next_turn_object).
        self._prompt_epoch = 0
        self.session_id: str | None = options.resume_id
        self.init_info: dict = {}
        # The `initialize` control response (commands, models, account, ...);
        # {} until the handshake answers or when it never does.
        self.init_result: dict = {}
        self.closed = asyncio.Event()
        # True while the idle reaper is inside aclose(). A close in flight is
        # NOT a usable process — see `alive` and `wait_closing`.
        self._closing = False
        self._close_done = asyncio.Event()

    # --- state ---------------------------------------------------------------
    @property
    def alive(self) -> bool:
        proc = self._proc
        if proc is None or proc.returncode is not None or self.closed.is_set():
            return False
        # A process the idle reaper is already tearing down looks alive for the
        # whole of aclose() (the child is signalled, not yet reaped). Sending a
        # turn to it would cancel the reaper mid-close and leave a half-closed
        # process behind, so it counts as dead from here on; the caller waits
        # the close out via `wait_closing` and respawns on the session id.
        return not self._closing

    @property
    def closing(self) -> bool:
        """True while the idle reaper is closing this process."""
        return self._closing

    async def wait_closing(self) -> None:
        """Block until an in-flight idle close has finished. A no-op when none
        is running, so callers can await it unconditionally."""
        if self._closing:
            await self._close_done.wait()

    @property
    def turn_open(self) -> bool:
        return self._turn is not None and self._turn.open

    @property
    def background_tasks(self) -> frozenset[str]:
        """tool_use ids of the CLI's sub-agents still running in the background."""
        return frozenset(self._background)

    @property
    def has_unsolicited(self) -> bool:
        """True while a turn the CLI ran on its own waits to be consumed."""
        return bool(self._unsolicited)

    def take_unsolicited(self) -> TurnHandle | None:
        """The oldest turn the CLI ran on its own (finished or still
        streaming), for the consumer that runs marim's autonomous turn over
        it; None when there is none. Read it like any other handle — through
        ``next_turn_object``/``turn_objects``."""
        return self._unsolicited.popleft() if self._unsolicited else None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def last_stderr(self) -> str:
        return "".join(self._stderr_tail)[-_STDERR_TAIL_CHARS:]

    @property
    def prompts_open(self) -> int:
        return self._client.prompts_open if self._client is not None else 0

    @property
    def prompt_epoch(self) -> int:
        """Monotonic counter of control_request handler edges (one bump when a
        prompt opens, one when it closes). ``prompts_open`` alone cannot tell
        a silence window apart from one that held a prompt which has since
        been answered; comparing this across the window can."""
        return self._prompt_epoch

    async def _serve_request(self, request_id: str, request: dict) -> dict:
        assert self._on_request is not None
        self._prompt_epoch += 1
        try:
            return await self._on_request(request_id, request)
        finally:
            self._prompt_epoch += 1

    # --- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        argv = build_process_argv(self._opts)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self._opts.cwd,
                env=child_env(self._opts.env),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Own session/process group so aclose() can signal the whole
                # tree (the CLI forks MCP servers of its own).
                start_new_session=True,
            )
        except OSError as exc:
            raise CliModelError(f"could not launch claude ({exc}). {INSTALL_HINT}") from exc
        proc = self._proc
        assert proc.stdout is not None and proc.stdin is not None and proc.stderr is not None
        self._client = StreamJsonClient(
            proc.stdout,
            proc.stdin,
            on_event=self._on_event,
            # Left None when nothing is bound, so the protocol layer keeps
            # answering "no request handler bound" instead of hanging.
            on_request=None if self._on_request is None else self._serve_request,
        )
        loop = asyncio.get_running_loop()
        self._reader_task = loop.create_task(self._run_reader(self._client))
        self._stderr_task = loop.create_task(self._pump_stderr(proc.stderr))
        await self._initialize(self._client)

    async def _initialize(self, client: StreamJsonClient) -> None:
        # Best-effort handshake: the CLI serializes stdin, so the first user
        # message is ordered after `initialize` whether or not the answer has
        # arrived. Waiting (bounded) just keeps the logs honest and lets a
        # launch that dies immediately surface on the first `send_turn`.
        init = asyncio.ensure_future(client.control("initialize", timeout=_INIT_TIMEOUT, hooks={}))
        closing = asyncio.ensure_future(self.closed.wait())
        try:
            await asyncio.wait(
                {init, closing}, timeout=_INIT_TIMEOUT, return_when=asyncio.FIRST_COMPLETED
            )
            if init.done() and not init.cancelled() and init.exception() is None:
                self._record_init(init.result())
        finally:
            await _drain(init, closing)

    def _record_init(self, response: dict) -> None:
        # The handshake carries the CLI's model menu; publishing it to the
        # catalog cache means a picker opened during this session lists the
        # current models without launching a probe process of its own.
        from .catalog import remember

        self.init_result = response if isinstance(response, dict) else {}
        remember(self.init_result, binary=self._opts.binary)

    async def _run_reader(self, client: StreamJsonClient) -> None:
        try:
            await client.run()
        finally:
            # Delivering CLOSED is the invariant, settling the exit is only
            # polish: nested so that nothing `_settle_exit` does — however it
            # fails — can leave the open turn without its CLOSED object. Skip
            # it and the consumer blocks for the whole silence timeout (600 s
            # by default) while `alive` still reads True.
            try:
                await self._settle_exit()
            finally:
                self._deliver_closed()
                self.closed.set()

    async def _settle_exit(self) -> None:
        """stdout EOF means the process is gone (or going): give stderr and the
        child reaper a moment to land so the synthetic CLOSED object carries
        the real reason and the real exit code. It runs concurrently with
        ``aclose``'s own wait on the process, so it adds no shutdown latency.

        Every failure is swallowed (not just the timeouts): a re-raised
        exception on the shielded stderr task, or a ProcessLookupError/OSError
        from a `proc.wait()` whose child another reaper already took, would
        otherwise abort a best-effort step whose only product is a nicer
        CLOSED payload. The caller's `finally` delivers CLOSED either way; a
        missing stderr tail or returncode is the whole cost."""
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._stderr_task), _EXIT_SETTLE)
            except Exception:
                logger.debug("claude stderr settle failed", exc_info=True)
        proc = self._proc
        if proc is not None:
            try:
                await asyncio.wait_for(proc.wait(), _EXIT_SETTLE)
            except Exception:
                logger.debug("claude exit settle failed", exc_info=True)

    async def _pump_stderr(self, stream: asyncio.StreamReader) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            self._stderr_tail.append(line.decode("utf-8", "replace"))

    def _closed_object(self) -> dict:
        code = self._proc.returncode if self._proc is not None else None
        return {"type": CLOSED, "stderr": self.last_stderr, "returncode": code}

    def _deliver_closed(self) -> None:
        turn = self._turn
        if turn is not None and turn.open:
            turn.events.put_nowait(self._closed_object())
            turn.finish()
        self._turn = None
        # A CLI-initiated turn cut short by the exit ends the same way, so its
        # (future) consumer sees a failed turn rather than a queue nothing
        # ever finishes. Finished ones stay consumable: their text is real.
        own = self._own_turn
        if own is not None and own.open:
            own.events.put_nowait(self._closed_object())
            own.finish()
        self._own_turn = None
        # The sub-agents died with the process; the resumed CLI carries the
        # notifications it did see in its own history.
        self._background.clear()

    async def aclose(self) -> None:
        self._cancel_idle()
        proc = self._proc
        if proc is None:
            return
        if self._client is not None:
            await self._client.aclose()
        if proc.returncode is None:
            await self._terminate(proc)
        await _drain(*[t for t in (self._reader_task, self._stderr_task) if t is not None])
        self._deliver_closed()
        self.closed.set()
        self._proc = None

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(Exception):
            assert proc.stdin is not None
            proc.stdin.close()
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), _TERM_GRACE)
        except (asyncio.TimeoutError, TimeoutError):
            # The CLI launches MCP servers in their own process groups, so
            # killpg on ours alone can leak them: walk /proc instead.
            kill_process_tree(proc.pid)
            with contextlib.suppress(Exception):
                await proc.wait()

    # --- turns ---------------------------------------------------------------
    def _on_event(self, obj: dict) -> None:
        kind = obj.get("type")
        if kind == CLOSED:
            # The client publishes a *bare* CLOSED at stdout EOF; the enriched
            # one (stderr tail + exit code) follows from `_run_reader`'s
            # finally once the exit has settled. Queueing this one too would
            # end the turn early with an object missing the very fields the
            # consumer reports.
            return
        if kind == "system" and obj.get("subtype") == "init":
            # Captured before the object is queued, so a consumer that only
            # wants the id can read `process.session_id` the moment it arrives.
            self.session_id = str(obj.get("session_id") or self.session_id or "") or None
            self.init_info = obj
        self._track_background(obj)
        turn = self._turn
        if turn is None or not turn.open:
            self._route_unsolicited(obj)
            return
        turn.events.put_nowait(obj)
        if kind == "result":
            turn.finish()
            self._turn = None
            self._arm_idle()

    def _track_background(self, obj: dict) -> None:
        """Keep ``_background`` current: a backgrounded ``task_started`` adds
        the sub-agent, its ``task_notification`` (any status) removes it."""
        if obj.get("type") != "system":
            return
        tid = str(obj.get("tool_use_id") or "")
        subtype = obj.get("subtype")
        if subtype == "task_started" and obj.get("is_backgrounded") and tid:
            self._background.add(tid)
        elif subtype == "task_notification":
            self._background.discard(tid)

    def _route_unsolicited(self, obj: dict) -> None:
        """An object with no turn open. A ``system/init`` opens a turn the CLI
        is running on its own; anything before one waits in the prelude and
        opens *with* it (or rides into the next turn marim sends — a
        notification the CLI never reacted to still settles its card there).
        The turn ends at its ``result`` like any other."""
        handle = self._own_turn
        if handle is None or not handle.open:
            if obj.get("type") != "system":
                # Not a lifecycle event and not a turn: a stray replay. The
                # CLI stays alive, so dropping it costs the next turn nothing.
                logger.debug("claude object with no open turn dropped: %s", obj.get("type"))
                return
            if obj.get("subtype") != "init":
                self._prelude.append(obj)
                # No turn is open, so the clock is (re)armed for the new
                # state: the last sub-agent's notification with no turn to
                # follow it ends the hold `_arm_idle` kept.
                self._arm_idle()
                return
            handle = self._open_own_turn(obj)
        handle.events.put_nowait(obj)
        if obj.get("type") == "result":
            handle.finish()
            self._own_turn = None
            self._arm_idle()

    def _open_own_turn(self, init: dict) -> TurnHandle:
        """A ``system/init`` with no turn open: the CLI started one of its own.
        The prelude opens it (queued ahead of the init, in arrival order)."""
        # A turn is running: the idle clock a notification-only prelude may
        # have started must not fire underneath it.
        self._cancel_idle()
        opening = [*self._prelude, init]
        self._prelude.clear()
        handle = self._own_turn = TurnHandle()
        self._unsolicited.append(handle)
        for pre in opening[:-1]:
            handle.events.put_nowait(pre)
        logger.info("claude opened a turn of its own (%d objects in prelude)", len(opening) - 1)
        if self._on_unsolicited is not None:
            self._on_unsolicited(opening)
        return handle

    async def _await_unsolicited(self) -> None:
        """Let a turn the CLI is running on its own finish before marim sends
        one: the CLI serializes them anyway, and a ``result`` arriving with a
        marim turn open would be routed to — and end — the wrong turn. Bounded
        by the silence timeout; a turn that outlives it is *interrupted* like
        a silent marim turn would be, so that it really ends on the CLI's side
        too — finishing only the local handle would leave the CLI's eventual
        ``result`` to land in, and end, the next marim turn. The interrupt's
        aborted ``result`` (or the CLOSED of a CLI that ignored it and was
        killed) closes the handle, so its consumer is not stranded."""
        handle = self._own_turn
        if handle is None or not handle.open:
            return
        timeout = self.silence_timeout if self.silence_timeout > 0 else None
        try:
            await asyncio.wait_for(handle.closed.wait(), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning("claude's own turn did not finish in %.0fs; interrupting it", timeout)
            await self.interrupt(handle)

    async def send_turn(self, text: str) -> TurnHandle:
        """Open a turn and send its user message. A dead process still returns
        a handle — one whose queue already holds the ``CLOSED`` object — so the
        consumer has a single code path."""
        # One turn at a time: both callers (`ClaudeCliModel._start_turn` and
        # `ClaudeCliRunner`) finish or interrupt a turn before opening the
        # next, and overwriting `self._turn` here would silently strand the
        # previous handle's consumer on a queue nothing ever finishes.
        assert not self.turn_open, "send_turn while a turn is open"
        await self._await_unsolicited()
        self._cancel_idle()
        handle = TurnHandle()
        self._turn = handle
        # Whatever arrived between turns (a sub-agent's notification the CLI
        # has not reacted to) is this turn's to see first, so the demux
        # settles the card before the new prose starts.
        for pre in self._prelude:
            handle.events.put_nowait(pre)
        self._prelude.clear()
        if self._client is None or self.closed.is_set():
            self._deliver_closed()
            return handle
        try:
            await self._client.user(text)
        except ProcessClosed:
            self._deliver_closed()
        return handle

    async def send_user(self, text: str) -> None:
        """A user message while a turn is open folds into that turn (steer)."""
        if self._client is None or self.closed.is_set():
            raise ProcessClosed("claude is not running")
        await self._client.user(text)

    async def read_usage(self, timeout: float = _USAGE_TIMEOUT) -> dict:
        """The ``get_usage`` control answer: the subscription's rate-limit
        windows (see ``claude/quota.py``) plus the process's running totals.
        ``skip_behaviors`` keeps the CLI from acting on the reading (it is a
        status-line poll, not a user command). Raises like ``control`` —
        the caller treats it as best-effort."""
        if self._client is None or self.closed.is_set():
            raise ProcessClosed("claude process is closed")
        return await self._client.control("get_usage", timeout=timeout, skip_behaviors=True)

    async def interrupt(self, handle: TurnHandle, grace: float = _INTERRUPT_GRACE) -> None:
        """Send ``interrupt`` and wait up to ``grace`` for the turn's aborted
        ``result``. A CLI that does not answer in time is killed — the next
        turn resumes by session id (spec §Interrupt)."""
        if not handle.open or self._client is None or self.closed.is_set():
            return
        # The wait is on the turn's own aborted `result`, not on the control
        # answer: one grace covers both, because awaiting them in sequence
        # would cost a silent CLI two graces in a row. The request future is
        # drained (never sequenced) for the same reason.
        request = asyncio.ensure_future(self._client.control("interrupt", timeout=grace))
        try:
            await asyncio.wait_for(handle.closed.wait(), grace)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        finally:
            await _drain(request)
        if handle.open:
            logger.warning("claude ignored interrupt for %.0fs; killing it", grace)
            await self.aclose()

    # --- idle reaper ---------------------------------------------------------
    def _arm_idle(self) -> None:
        if self._idle_timeout <= 0:
            return
        delay = self._idle_timeout
        if self._background:
            # A sub-agent is still working in the background; an idle close
            # would kill it and its report would never arrive. The normal
            # clock starts when its notification lands (or when the CLI's
            # turn on that notification ends); the stretched one only bounds
            # a notification that never comes, since a closed process resumes
            # by id on the next turn anyway.
            delay *= _BACKGROUND_HOLD_FACTOR
        self._cancel_idle()
        self._idle_task = asyncio.get_running_loop().create_task(self._idle_close(delay))

    def _cancel_idle(self) -> None:
        task = self._idle_task
        self._idle_task = None
        # `is not current_task()` because the idle task itself calls aclose():
        # cancelling there would abort the close half-done.
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _idle_close(self, delay: float) -> None:
        await asyncio.sleep(delay)
        # Set BEFORE the first await inside the close: from here on `alive` is
        # False, so a turn that starts concurrently waits this close out and
        # respawns instead of cancelling it half-done. (Everything between the
        # sleep returning and this line is synchronous, so there is no window
        # where a turn can see the reaper as neither armed nor closing.)
        self._closing = True
        self._close_done.clear()
        logger.info("claude idle for %.0fs; closing (the next turn resumes by id)", delay)
        try:
            await self.aclose()
        finally:
            self._closing = False
            self._close_done.set()


# --- consuming a turn -------------------------------------------------------


async def next_turn_object(process: ClaudeProcess, handle: TurnHandle) -> dict:
    """The next object of the turn, honoring the process's silence timeout.
    The clock counts only while no control_request handler is open, so an
    approval panel waiting on the user never trips it. On timeout the turn is
    interrupted and ``CliModelError`` raised."""
    timeout = process.silence_timeout
    while True:
        epoch = process.prompt_epoch
        try:
            return await asyncio.wait_for(handle.events.get(), timeout if timeout > 0 else None)
        except (asyncio.TimeoutError, TimeoutError):
            # Still deciding, or decided *inside* this window: either way the
            # CLI was waiting on a human, not silent. Re-arm from scratch —
            # the object answering the prompt is usually microseconds behind
            # the handler returning, and a window whose tail straddles that
            # moment must not be scored as a timeout.
            if process.prompts_open > 0 or process.prompt_epoch != epoch:
                continue
            await process.interrupt(handle)
            raise CliModelError(
                f"claude timed out after {timeout:.0f}s of silence (interrupted)"
            ) from None


async def turn_objects(
    process: ClaudeProcess, handle: TurnHandle, first: dict | None = None
) -> AsyncIterator[dict]:
    """Every object of the turn through its terminal ``result``/``CLOSED``.
    ``first`` re-injects an object a caller already pulled (the resume probe in
    the model layer). A cancelled consumer interrupts the turn so the CLI stops
    working on an answer nobody will read."""
    if first is not None:
        yield first
        if _is_terminal(first):
            return
    while True:
        try:
            obj = await next_turn_object(process, handle)
        except asyncio.CancelledError:
            await process.interrupt(handle)
            raise
        yield obj
        if _is_terminal(obj):
            return
