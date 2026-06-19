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
    # The pid is in the process table, but a SIGKILLed child becomes a zombie
    # until its parent reaps it — and when run_bash kills the whole group the
    # parent shell dies too, so the child reparents to PID 1. Under a real init
    # (systemd) that zombie is reaped almost immediately; in a minimal CI
    # container PID 1 doesn't reap orphans, so the dead child lingers as
    # <defunct> indefinitely. A zombie has been killed, which is exactly what
    # callers mean by "not alive" — so read /proc and treat state 'Z' as dead.
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
        # Format: "pid (comm) state ...". comm may contain spaces/parens, so
        # find the LAST ')' (the close of comm); state is the next field.
        state = data[data.rindex(")") + 1:].split()[0]
        if state == "Z":
            return False
    except (FileNotFoundError, ProcessLookupError, ValueError, IndexError):
        return False
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
async def test_start_bash_output_keeps_head_and_tail(tmp_path: Path):
    """The live-buffer output() path truncates from the middle (head+tail), so a
    pulled live preview still shows the verdict at the end."""
    bp = await shell.start_bash(
        tmp_path,
        "echo HEAD-MARKER; "
        "for i in $(seq 1 5000); do echo filler$i; done; "
        "echo TAIL-VERDICT",
        max_output=200,
    )
    final = await bp.wait()
    _ = final  # populate the buffer; assertions are on the live output() path
    assert "HEAD-MARKER" in bp.output()
    assert "TAIL-VERDICT" in bp.output()
    assert "truncated" in bp.output()


@pytest.mark.anyio
async def test_start_bash_streams_buffer_then_final(tmp_path: Path):
    bp = await shell.start_bash(tmp_path, "echo one; echo two")
    final = await bp.wait()
    assert "one" in bp.output() and "two" in bp.output()  # live buffer filled
    assert "one" in final and "two" in final
    assert "exit 0" in final


@pytest.mark.anyio
async def test_bash_process_wait_without_pipe_returns_exit_line(tmp_path: Path):
    """wait() must not crash if the process has no stdout pipe to read. A guard
    (not a bare assert, which would raise AssertionError) skips the read and still
    returns the exit line."""

    class _FakeProc:
        stdout = None
        returncode = 0

        async def wait(self):
            return 0

    bp = shell.BashProcess(_FakeProc(), 200, tmp_path, "noop")
    final = await bp.wait()
    assert "exit 0" in final


@pytest.mark.anyio
async def test_start_bash_kill_stops_process(tmp_path: Path):
    bp = await shell.start_bash(tmp_path, "sleep 30")
    bp.kill()
    final = await bp.wait()  # returns promptly once killed, not after 30s
    assert "exit" in final
    assert bp.returncode is not None


@pytest.mark.anyio
async def test_run_bash_offloads_large_output(tmp_path, monkeypatch):
    from marim_harness.tools import offload
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 100)
    out = await shell.run_bash(tmp_path, "for i in $(seq 1 500); do echo line $i; done")
    assert "full output saved to" in out and "bash result" in out
    saved = list((tmp_path / ".marim" / "output").glob("bash-*.txt"))
    assert len(saved) == 1
    body = saved[0].read_text()
    assert body.startswith("exit 0\n")
    assert body.count("line ") == 500


@pytest.mark.anyio
async def test_run_bash_small_output_inline(tmp_path):
    out = await shell.run_bash(tmp_path, "echo hi")
    assert out == "exit 0\nhi\n"


@pytest.mark.anyio
async def test_background_wait_offloads_but_live_output_truncates(tmp_path, monkeypatch):
    from marim_harness.tools import offload
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 100)
    bp = await shell.start_bash(
        tmp_path, "for i in $(seq 1 500); do echo line $i; done", max_output=80
    )
    final = await bp.wait()
    assert "full output saved to" in final and "bash result" in final
    saved = list((tmp_path / ".marim" / "output").glob("bash-*.txt"))
    assert len(saved) == 1 and saved[0].read_text().count("line ") == 500
    # the live preview path stays bounded by max_output (head+tail truncation)
    assert len(bp.output()) <= 80 + 64  # cap + the "… (N chars truncated) …" marker
