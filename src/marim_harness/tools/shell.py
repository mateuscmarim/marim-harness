import asyncio
import os
import signal
from pathlib import Path

_DEFAULT_TIMEOUT = 30
_DEFAULT_MAX_OUTPUT = 20_000


async def run_bash(
    root: Path,
    command: str,
    timeout: int = _DEFAULT_TIMEOUT,
    max_output: int = _DEFAULT_MAX_OUTPUT,
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
    if len(text) > max_output:
        text = text[:max_output] + "\n(truncated)"
    return f"exit {proc.returncode}\n{text}"


class BashProcess:
    """A running background shell command: the live process, a growing output
    buffer readable any time via :meth:`output`, and :meth:`wait` which pumps
    output to completion and returns the final ``exit N\\n<output>`` text.
    :meth:`kill` terminates the whole process group so children die too."""

    def __init__(self, proc: asyncio.subprocess.Process, max_output: int) -> None:
        self._proc = proc
        self._max_output = max_output
        self._buffer: list[str] = []

    @property
    def returncode(self):
        return self._proc.returncode

    def output(self) -> str:
        """The combined output captured so far, truncated to the cap."""
        text = "".join(self._buffer)
        if len(text) > self._max_output:
            text = text[: self._max_output] + "\n(truncated)"
        return text

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
        assert self._proc.stdout is not None
        while True:
            chunk = await self._proc.stdout.readline()
            if not chunk:
                break
            self._buffer.append(chunk.decode(errors="replace"))
        await self._proc.wait()
        return f"exit {self._proc.returncode}\n{self.output()}"


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
    return BashProcess(proc, max_output)
