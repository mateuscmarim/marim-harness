import asyncio
import os
import signal
from pathlib import Path

from .offload import MAX_OUTPUT_BYTES, offload_if_large

_DEFAULT_TIMEOUT = 30
_DEFAULT_MAX_OUTPUT = 20_000


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


async def run_bash(
    root: Path,
    command: str,
    timeout: int = _DEFAULT_TIMEOUT,
) -> str:
    """Run a shell command in the workspace root, capturing combined output.

    Runs in its own session so a timeout can signal the whole process group and
    take down any children the command spawned, not just the shell."""
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        # Kill the whole process group (best-effort) so children die too.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()
        return f"(timed out after {timeout}s)"
    text = stdout.decode(errors="replace")
    if len(text) > MAX_OUTPUT_BYTES:
        text = text[:MAX_OUTPUT_BYTES]
        capped = True
    else:
        capped = False
    body = f"exit {proc.returncode}\n{text}"
    return offload_if_large(body, kind="bash", key=command,
                            workspace_root=root, capped=capped)


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
        self._buffer: list[str] = []

    @property
    def returncode(self):
        return self._proc.returncode

    def output(self) -> str:
        """The combined output captured so far, truncated (head+tail) to the cap."""
        return _truncate_middle("".join(self._buffer), self._max_output)

    def kill(self) -> None:
        """Kill the process group (best-effort; already-dead is fine)."""
        try:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass

    async def wait(self) -> str:
        """Read the process's output to EOF into the buffer, then return the
        final result. Safe to call once; the pump owns the stream."""
        # Normally stdout is a PIPE; guard rather than assert so a process built
        # without one degrades to "no output" instead of crashing the caller.
        if self._proc.stdout is not None:
            while True:
                chunk = await self._proc.stdout.readline()
                if not chunk:
                    break
                self._buffer.append(chunk.decode(errors="replace"))
        await self._proc.wait()
        text = "".join(self._buffer)
        if len(text) > MAX_OUTPUT_BYTES:
            text = text[:MAX_OUTPUT_BYTES]
            capped = True
        else:
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
