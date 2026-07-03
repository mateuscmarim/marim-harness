import asyncio
import codecs
import contextlib
import os
import signal
from collections import deque
from pathlib import Path

from .offload import MAX_OUTPUT_CHARS, offload_if_large

_DEFAULT_TIMEOUT = 30
_DEFAULT_MAX_OUTPUT = 20_000
# Format the elided-middle marker the same way _truncate_middle does, so the live
# preview and the final body present a truncation identically. ``unit`` is "chars"
# for the decoded background buffer and the foreground byte count alike (the latter
# is bytes, but for the flood case this is a cosmetic count, matching the existing
# "chars" wording).
_TRUNC_MARKER = "\n… ({dropped} chars truncated) …\n"
# Overall wall-clock ceiling for the post-kill drain. The per-read timeout below
# bounds a single read, but a process flushing a burst of output keeps resetting that
# window — this caps the whole drain loop so the timeout path can't stall (we keep
# what we got and move on).
_DRAIN_BUDGET = 2.0  # seconds
# asyncio's StreamReader.readline() raises ValueError on a single line longer than
# its buffer (64 KiB by default) AND discards the buffered bytes — so `cat
# bundle.min.js`, `jq -c .`, or any long-single-line output would crash the tool
# despite the generous MAX_OUTPUT_CHARS budget above. Read fixed-size chunks instead:
# read() never raises on line length, so no output can defeat the read loop. Chunks no
# longer align to line boundaries, which costs only a cosmetic replacement char at a
# multi-MB truncation seam (see the decode notes in run_bash / BashProcess.wait).
_READ_CHUNK = 65536  # bytes per stream read


def _truncate_middle(text: str, max_output: int) -> str:
    """Cap ``text`` to ``max_output`` chars, dropping the MIDDLE rather than the
    tail. The head carries a command's opening (setup, first errors); the tail
    carries its verdict (a test summary, a final traceback) — and for tests and
    builds the result lives at the very end, so a head-only cut would silently hide
    exactly what the reader needs. The elided middle is replaced with a marker
    noting how many chars were dropped."""
    if len(text) <= max_output:
        return text
    head = max_output // 2
    tail = max_output - head
    dropped = len(text) - max_output
    return f"{text[:head]}\n… ({dropped} chars truncated) …\n{text[-tail:]}"


class _BoundedOutput:
    """Accumulates subprocess output under a RUNNING size budget.

    A flood (``yes``, ``cat hugefile``) would otherwise append every line to a list
    and only get capped at the end — buffering hundreds of MB first. This keeps a
    bounded HEAD (the command's opening: setup, first errors) and a bounded sliding
    TAIL (its verdict: a test summary, a final traceback), the same head+tail split
    :func:`_truncate_middle` presents, so middle-truncation still has both ends.
    Memory stays at ~``budget`` regardless of how much the process emits. Callers
    must keep draining the pipe to EOF (the child deadlocks on a full pipe) — this
    never rejects a chunk, it just stops *growing* memory past the budget.

    Type-agnostic: works on ``bytes`` (foreground, decoded once at the end) or
    ``str`` (background, decoded per line). ``parts(empty)`` returns the joined
    ``(head, tail)`` and ``dropped`` the exact count elided between them."""

    def __init__(self, budget: int) -> None:
        half = budget // 2
        self._head_cap = half
        self._tail_cap = budget - half  # headroom so both ends survive truncation
        self._head: list = []
        self._head_len = 0
        self._tail: deque = deque()
        self._tail_len = 0
        self._total = 0

    def add(self, chunk) -> None:
        n = len(chunk)
        self._total += n
        if self._head_len < self._head_cap:
            # Fill the head up to its cap. A read chunk can be far larger than the cap
            # (up to _READ_CHUNK), so slice it at the cap and let the remainder fall
            # through to the sliding tail — appending it whole would overshoot the head
            # by ~64 KiB and blow the memory budget. Chunks are no longer line-aligned,
            # so slicing mid-chunk is fine (decode uses errors="replace").
            room = self._head_cap - self._head_len
            if n <= room:
                self._head.append(chunk)
                self._head_len += n
                return
            self._head.append(chunk[:room])
            self._head_len += room
            chunk = chunk[room:]
            n = len(chunk)
        # One oversized chunk could blow the tail by itself — keep only its last
        # ``tail_cap`` so a single chunk can't exceed it.
        if n > self._tail_cap:
            chunk = chunk[n - self._tail_cap:]
            n = self._tail_cap
        self._tail.append(chunk)
        self._tail_len += n
        # Evict the oldest tail chunks while the most recent still cover ``tail_cap``,
        # so memory stays bounded but we always retain the freshest tail_cap of output.
        while self._tail and self._tail_len - len(self._tail[0]) >= self._tail_cap:
            self._tail_len -= len(self._tail.popleft())

    @property
    def dropped(self) -> int:
        """Exact number of units elided between the retained head and tail."""
        return self._total - self._head_len - self._tail_len

    def parts(self, empty):
        """Return ``(head, tail)`` joined with ``empty`` (``b""`` or ``""``)."""
        return empty.join(self._head), empty.join(self._tail)


async def run_bash(
    root: Path,
    command: str,
    timeout: int = _DEFAULT_TIMEOUT,
    stdin_data: bytes | None = None,
) -> str:
    """Run a shell command in the workspace root, capturing combined output.

    Runs in its own session so a timeout can signal the whole process group and
    take down any children the command spawned, not just the shell.

    ``stdin_data`` (when given) is piped to the command's stdin in one write and
    the pipe is closed immediately, so a reader sees the bytes then EOF. With the
    default ``None`` no stdin pipe is wired at all — identical to the historical
    behavior."""
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(root),
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    if stdin_data is not None and proc.stdin is not None:
        # One small write (a sudo password, a heredoc-ish snippet), then close so
        # the child sees EOF. Suppress pipe errors: a command that exits without
        # reading stdin (or dies at spawn) must not crash the runner — its own
        # exit code / output is the signal the caller cares about.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            proc.stdin.write(stdin_data)
            await proc.stdin.drain()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            proc.stdin.close()
    # Read stdout line-by-line instead of using proc.communicate() so we can
    # retain whatever was read before a timeout kills the process.  communicate()
    # closes its internal reader on cancellation, discarding buffered output.
    # Bound memory while reading: a flood (``yes``, ``cat hugefile``) must not buffer
    # hundreds of MB before the final cap applies. The accumulator keeps a bounded
    # head + sliding tail; we keep draining the pipe to EOF either way (see below).
    chunks = _BoundedOutput(MAX_OUTPUT_CHARS)
    timed_out = False
    if proc.stdout is not None:
        # ``timeout`` is a TOTAL wall-clock ceiling, not a per-read idle gap. A
        # chatty command (e.g. ``pytest -v``) emits output continuously, so a
        # per-read timeout would reset on every chunk and let the command run
        # unbounded — only a silent command would ever trip it. Track one deadline
        # and shrink each read's budget to the time remaining so the whole run
        # is bounded regardless of how talkative the command is.
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    timed_out = True
                    break
                chunk = await asyncio.wait_for(proc.stdout.read(_READ_CHUNK),
                                               timeout=remaining)
                if not chunk:
                    break  # EOF — process exited
                chunks.add(chunk)
        except asyncio.TimeoutError:
            timed_out = True
    else:
        # No pipe (shouldn't happen with stdout=PIPE, but guard gracefully).
        await asyncio.sleep(timeout)
        timed_out = True
    if timed_out:
        # Kill the whole process group (best-effort) so children die too.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        # Drain anything the dying process flushed to the pipe before the kill
        # propagated.  A short deadline keeps the timeout path fast.  The
        # per-read timeout alone isn't a real ceiling: a process spewing output
        # resets that 1s window on every chunk, so the drain could run for as long
        # as it keeps flushing.  Bound the whole loop with a fixed
        # wall-clock budget so the timeout path stays snappy regardless of volume.
        if proc.stdout is not None:
            drain_deadline = asyncio.get_event_loop().time() + _DRAIN_BUDGET
            try:
                while True:
                    remaining = drain_deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break
                    chunk = await asyncio.wait_for(proc.stdout.read(_READ_CHUNK),
                                                   timeout=min(1, remaining))
                    if not chunk:
                        break
                    chunks.add(chunk)
            except (asyncio.TimeoutError, OSError):
                pass
    # Reap the process. After EOF (clean exit) or the SIGKILL above this returns
    # at once — but a process wedged in uninterruptible I/O (D-state: dead NFS, a
    # stuck disk) can ignore even SIGKILL indefinitely. Bound the reap so the tool
    # can never hang with no ceiling; ``returncode`` stays None in that rare case,
    # which surfaces as ``exit None`` alongside the timeout marker.
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=_DRAIN_BUDGET)
    head_b, tail_b = chunks.parts(b"")
    dropped = chunks.dropped
    if dropped > 0:
        # Decode the two ends separately and splice in the truncated-middle marker,
        # the same head+tail presentation the final cap has always produced — memory
        # never exceeded the budget. A chunk boundary can fall mid-multibyte-char, so
        # an end may show a stray replacement char at the seam; harmless, and only in
        # output already past the multi-MB truncation threshold.
        text = (head_b.decode(errors="replace")
                + _TRUNC_MARKER.format(dropped=dropped)
                + tail_b.decode(errors="replace"))
    else:
        # Nothing dropped: decode the full byte string at once so output is identical
        # to the pre-cap behavior (no boundary re-decode).
        text = (head_b + tail_b).decode(errors="replace")
    body = f"exit {proc.returncode}\n{text}"
    if timed_out:
        # Separate the marker from the last line of output so it can't glom onto
        # it (e.g. "...last line(timed out after 30s)").
        if body and not body.endswith("\n"):
            body += "\n"
        body += f"(timed out after {timeout}s)"
    return offload_if_large(body, kind="bash", key=command,
                            workspace_root=root, capped=dropped > 0)


class BashProcess:
    """A running background shell command: the live process, a growing output
    buffer readable any time via :meth:`output`, and :meth:`wait` which pumps
    output to completion and returns the final ``exit N\\n<output>`` text.
    :meth:`kill` terminates the whole process group so children die too."""

    def __init__(self, proc: asyncio.subprocess.Process, max_output: int,
                 root: Path, command: str) -> None:
        self._proc = proc
        self._max_output = max_output
        self._root = root
        self._command = command
        # Bound memory while the background command runs (see _BoundedOutput): a
        # detached flood must not grow the buffer without limit before wait() caps it.
        self._buffer = _BoundedOutput(MAX_OUTPUT_CHARS)

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    def output(self) -> str:
        """The combined output captured so far, truncated (head+tail) to the cap."""
        head, tail = self._buffer.parts("")
        return _truncate_middle(head + tail, self._max_output)

    def kill(self) -> None:
        """Kill the process group (best-effort; already-dead is fine)."""
        try:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()

    async def wait(self) -> str:
        """Read the process's output to EOF into the buffer, then return the
        final result. Safe to call once; the pump owns the stream."""
        # Normally stdout is a PIPE; guard rather than assert so a process built
        # without one degrades to "no output" instead of crashing the caller.
        if self._proc.stdout is not None:
            # Decode incrementally: read() chunks split on a fixed byte count, not on
            # line boundaries, so a multibyte char can straddle two chunks. An
            # incremental decoder carries the partial char across instead of emitting
            # a replacement char at every chunk seam (readline never split a char).
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            while True:
                chunk = await self._proc.stdout.read(_READ_CHUNK)
                if not chunk:
                    break
                # add() keeps memory bounded; we keep reading to EOF regardless so the
                # child never deadlocks on a full pipe.
                self._buffer.add(decoder.decode(chunk))
            self._buffer.add(decoder.decode(b"", final=True))  # flush a trailing partial
        await self._proc.wait()
        head, tail = self._buffer.parts("")
        dropped = self._buffer.dropped
        if dropped > 0:
            # Present the cap as a truncated middle (head + marker + tail) rather than
            # a head-only clip, so the command's verdict at the very end survives.
            text = f"{head}{_TRUNC_MARKER.format(dropped=dropped)}{tail}"
            capped = True
        else:
            text = head + tail
            capped = False
        body = f"exit {self._proc.returncode}\n{text}"
        return offload_if_large(body, kind="bash", key=self._command,
                                workspace_root=self._root, capped=capped)


async def start_bash(
    root: Path, command: str, max_output: int = _DEFAULT_MAX_OUTPUT
) -> BashProcess:
    """Launch a shell command detached (no timeout) and return a BashProcess to
    stream, wait on, or kill. Runs in its own session so the whole tree can be
    signalled as a group."""
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    return BashProcess(proc, max_output, root, command)
