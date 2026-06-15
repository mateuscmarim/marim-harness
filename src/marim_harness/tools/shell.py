import asyncio
from pathlib import Path

_DEFAULT_TIMEOUT = 30
_DEFAULT_MAX_OUTPUT = 20_000


async def run_bash(
    root: Path,
    command: str,
    timeout: int = _DEFAULT_TIMEOUT,
    max_output: int = _DEFAULT_MAX_OUTPUT,
) -> str:
    """Run a shell command in the workspace root, capturing combined output."""
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"(timed out after {timeout}s)"
    text = stdout.decode(errors="replace")
    if len(text) > max_output:
        text = text[:max_output] + "\n(truncated)"
    return f"exit {proc.returncode}\n{text}"
