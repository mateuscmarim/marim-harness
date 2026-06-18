import asyncio
import os
from pathlib import Path

import pytest

from marim_harness.tools import shell


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.anyio
async def test_bash_captures_stdout(tmp_path: Path):
    out = await shell.run_bash(tmp_path, "echo hello")
    assert "hello" in out
    assert "exit 0" in out


@pytest.mark.anyio
async def test_bash_runs_in_workspace_cwd(tmp_path: Path):
    (tmp_path / "marker.txt").write_text("")
    out = await shell.run_bash(tmp_path, "ls")
    assert "marker.txt" in out


@pytest.mark.anyio
async def test_bash_times_out(tmp_path: Path):
    out = await shell.run_bash(tmp_path, "sleep 5", timeout=1)
    assert "timed out" in out


@pytest.mark.anyio
async def test_bash_timeout_reaps_child_processes(tmp_path: Path):
    # The command spawns a long-lived child and records its PID, then hangs so
    # run_bash times out. A timeout must tear down the whole process group, not
    # just the shell, otherwise the backgrounded child is orphaned and survives.
    pid_file = tmp_path / "child.pid"
    # Detach the child's stdio from the captured pipe so run_bash returns as
    # soon as the shell is killed (an inherited pipe would otherwise keep the
    # parent blocked until the child exits, hiding the orphan).
    out = await shell.run_bash(
        tmp_path,
        f"sleep 30 >/dev/null 2>&1 & echo $! > {pid_file}; echo started; wait",
        timeout=1,
    )
    assert "timed out" in out

    child_pid = int(pid_file.read_text().strip())
    # Give the kill a moment to propagate to the child.
    for _ in range(20):
        if not _pid_alive(child_pid):
            break
        await asyncio.sleep(0.1)
    assert not _pid_alive(child_pid), f"child {child_pid} was orphaned, not reaped"


@pytest.mark.anyio
async def test_bash_truncates_long_output(tmp_path: Path):
    out = await shell.run_bash(
        tmp_path, "for i in $(seq 1 5000); do echo line$i; done", max_output=200
    )
    assert "truncated" in out


@pytest.mark.anyio
async def test_bash_truncation_keeps_head_and_tail(tmp_path: Path):
    """Over-cap output is cut from the MIDDLE, not the head: the start and the
    verdict at the very end both survive. For tests/builds the result line lives
    at the tail, which a head-only cut would silently drop."""
    cmd = (
        "echo HEAD-MARKER; "
        "for i in $(seq 1 5000); do echo filler$i; done; "
        "echo TAIL-VERDICT"
    )
    out = await shell.run_bash(tmp_path, cmd, max_output=200)
    assert "HEAD-MARKER" in out  # head kept
    assert "TAIL-VERDICT" in out  # tail kept — the part a head-only cut drops
    assert "truncated" in out


@pytest.mark.anyio
async def test_start_bash_output_keeps_head_and_tail(tmp_path: Path):
    """The live-buffer / background-job path truncates from the middle too, so a
    pulled background result still shows the verdict at the end."""
    bp = await shell.start_bash(
        tmp_path,
        "echo HEAD-MARKER; "
        "for i in $(seq 1 5000); do echo filler$i; done; "
        "echo TAIL-VERDICT",
        max_output=200,
    )
    final = await bp.wait()
    assert "HEAD-MARKER" in final
    assert "TAIL-VERDICT" in final
    assert "truncated" in final


@pytest.mark.anyio
async def test_start_bash_streams_buffer_then_final(tmp_path: Path):
    bp = await shell.start_bash(tmp_path, "echo one; echo two")
    final = await bp.wait()
    assert "one" in bp.output() and "two" in bp.output()  # live buffer filled
    assert "one" in final and "two" in final
    assert "exit 0" in final


@pytest.mark.anyio
async def test_start_bash_kill_stops_process(tmp_path: Path):
    bp = await shell.start_bash(tmp_path, "sleep 30")
    bp.kill()
    final = await bp.wait()  # returns promptly once killed, not after 30s
    assert "exit" in final
    assert bp.returncode is not None
