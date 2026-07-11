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
        # Trim the front so the tail holds EXACTLY the most recent ``tail_cap`` bytes
        # (the freshest verdict): evict whole oldest chunks, then slice the boundary
        # chunk. Whole-chunk-only eviction (the old approach) could leave up to
        # ~2*tail_cap when a big read() chunk was the oldest, so the capped output
        # size depended on OS pipe-chunking timing — a flaky budget overshoot. Slicing
        # mid-chunk is fine here (the ends decode with errors="replace").
        excess = self._tail_len - self._tail_cap
        while excess > 0:
            oldest = self._tail[0]
            if len(oldest) <= excess:
                self._tail.popleft()
                self._tail_len -= len(oldest)
                excess -= len(oldest)
            else:
                self._tail[0] = oldest[excess:]
                self._tail_len -= excess
                excess = 0

    @property
    def dropped(self) -> int:
        """Exact number of units elided between the retained head and tail."""
        return self._total - self._head_len - self._tail_len

    def parts(self, empty):
        """Return ``(head, tail)`` joined with ``empty`` (``b""`` or ``""``)."""
        return empty.join(self._head), empty.join(self._tail)


async def _feed_stdin(proc: "asyncio.subprocess.Process", stdin_data: bytes | None) -> None:
    """Write ``stdin_data`` to ``proc``'s stdin in one shot, then close the pipe
    so a reader sees the bytes then EOF. A ``None`` proc.stdin (no pipe was
    wired, or ``stdin_data`` is ``None``) is a no-op. One small write (a sudo
    password, a heredoc-ish snippet). Suppress pipe errors: a command that exits
    without reading stdin (or dies at spawn) must not crash the runner — its own
    exit code / output is the signal the caller cares about."""
    if stdin_data is None or proc.stdin is None:
        return
    with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
        proc.stdin.write(stdin_data)
        await proc.stdin.drain()
    with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
        proc.stdin.close()


async def _read_until(
    stream, deadline: float, acc: "_BoundedOutput", *,
    per_read_cap: float | None = None, catch_oserror: bool = False,
) -> bool:
    """Read from ``stream`` into ``acc`` until EOF or ``deadline`` (an
    ``event_loop.time()`` value) is reached. Returns whether the deadline was
    hit (``timed_out``). Each read's timeout is the time remaining, capped to
    ``per_read_cap`` when given — the main read loop passes ``None`` (bounded
    only by the overall deadline); the post-kill drain loop passes ``1`` so a
    process still flushing output can't reset an unbounded per-read window
    (see the wall-clock-budget note at the drain call site). When
    ``catch_oserror`` is set (the drain call), an ``OSError`` from the read
    ends the loop the same as a timeout rather than propagating — the main
    read loop leaves ``OSError`` uncaught, unchanged from its original
    behavior."""
    loop = asyncio.get_event_loop()
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return True
            read_timeout = min(per_read_cap, remaining) if per_read_cap is not None else remaining
            chunk = await asyncio.wait_for(stream.read(_READ_CHUNK), timeout=read_timeout)
            if not chunk:
                return False  # EOF — process exited
            acc.add(chunk)
    except asyncio.TimeoutError:
        return True
    except OSError:
        if catch_oserror:
            return False
        raise


def _kill_group(proc: "asyncio.subprocess.Process") -> None:
    """Kill the whole process group (best-effort) so children die too."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


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
    await _feed_stdin(proc, stdin_data)
    # Read stdout line-by-line instead of using proc.communicate() so we can
    # retain whatever was read before a timeout kills the process.  communicate()
    # closes its internal reader on cancellation, discarding buffered output.
    # Bound memory while reading: a flood (``yes``, ``cat hugefile``) must not buffer
    # hundreds of MB before the final cap applies. The accumulator keeps a bounded
    # head + sliding tail; we keep draining the pipe to EOF either way (see below).
    chunks = _BoundedOutput(MAX_OUTPUT_CHARS)
    if proc.stdout is not None:
        # ``timeout`` is a TOTAL wall-clock ceiling, not a per-read idle gap. A
        # chatty command (e.g. ``pytest -v``) emits output continuously, so a
        # per-read timeout would reset on every chunk and let the command run
        # unbounded — only a silent command would ever trip it. Track one deadline
        # and shrink each read's budget to the time remaining so the whole run
        # is bounded regardless of how talkative the command is.
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        timed_out = await _read_until(proc.stdout, deadline, chunks)
    else:
        # No pipe (shouldn't happen with stdout=PIPE, but guard gracefully).
        await asyncio.sleep(timeout)
        timed_out = True
    if timed_out:
        # Kill the whole process group (best-effort) so children die too.
        _kill_group(proc)
        # Drain anything the dying process flushed to the pipe before the kill
        # propagated.  A short deadline keeps the timeout path fast.  The
        # per-read timeout alone isn't a real ceiling: a process spewing output
        # resets that 1s window on every chunk, so the drain could run for as long
        # as it keeps flushing.  Bound the whole loop with a fixed
        # wall-clock budget so the timeout path stays snappy regardless of volume.
        if proc.stdout is not None:
            drain_deadline = asyncio.get_event_loop().time() + _DRAIN_BUDGET
            await _read_until(
                proc.stdout, drain_deadline, chunks, per_read_cap=1, catch_oserror=True
            )
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
    # timeout shapes `body` too (the trailing "(timed out after {timeout}s)"
    # marker), and stdin_data shapes it just as much — a command like `cat` echoes
    # its stdin straight into the body. Fold every output-affecting parameter into
    # the key so two otherwise-identical commands that differ only in timeout or
    # stdin_data don't collapse onto the same sha-derived offload file (see fs.py's
    # grep key for the same reasoning). ``!r`` (not the raw bytes) distinguishes
    # None from b"" — both would otherwise render as the same empty segment.
    key = f"{command}\0{timeout}\0{stdin_data!r}"
    # Note: `root` is not folded in — it's not a shaping parameter, it's the
    # offload *namespace*: offload_if_large writes under `workspace_root/.marim/
    # output/`, so two different roots already land in physically different
    # directories and can't collide regardless of what's in `key`.
    return offload_if_large(body, kind="bash", key=key,
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
        dropped = self._buffer.dropped
        if dropped > 0:
            # The buffer already elided the middle (a >budget flood). Gluing head
            # straight onto tail here would present two discontinuous regions as
            # one continuous stream, and _truncate_middle can't rescue it — head+tail
            # may already be within the cap, so no marker gets spliced. Mirror wait():
            # splice the same elided-middle marker in, bounding each end to half the
            # preview cap so the marker survives (a plain slice, not _truncate_middle,
            # avoids nesting a second marker inside an end).
            head_cap = self._max_output // 2
            return (
                head[:head_cap]
                + _TRUNC_MARKER.format(dropped=dropped)
                + (tail[-(self._max_output - head_cap):] if head_cap < self._max_output else "")
            )
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
