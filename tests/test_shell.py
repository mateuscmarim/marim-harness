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


def test_bounded_output_keeps_head_and_tail_under_budget():
    """The running cap keeps a bounded head + sliding tail so a flood never grows
    memory past ~budget, while both ends (opening + verdict) survive."""
    acc = shell._BoundedOutput(budget=100)  # head_cap=50, tail_cap=50
    acc.add(b"HEAD-MARKER\n")  # lands in the head
    for i in range(1000):
        acc.add(b"filler-%d\n" % i)
    acc.add(b"TAIL-VERDICT\n")  # most recent → retained in the sliding tail
    head, tail = acc.parts(b"")
    assert head.startswith(b"HEAD-MARKER")
    assert tail.endswith(b"TAIL-VERDICT\n")
    # Memory is bounded: head ~head_cap (+ one crossing chunk), tail ~tail_cap.
    assert len(head) < 50 + 32
    assert len(tail) < 50 + 32
    # dropped accounts for exactly what was elided between the two ends.
    assert acc.dropped == acc._total - len(head) - len(tail)
    assert acc.dropped > 0


def test_bounded_output_no_drop_is_lossless():
    acc = shell._BoundedOutput(budget=1000)
    acc.add(b"one\n")
    acc.add(b"two\n")
    head, tail = acc.parts(b"")
    assert acc.dropped == 0
    assert head + tail == b"one\ntwo\n"


def test_bounded_output_single_oversized_chunk_bounded():
    """A single line larger than the whole budget (no newline) must not blow memory:
    after the head fills, an oversized chunk is clipped to the last tail_cap."""
    acc = shell._BoundedOutput(budget=100)
    acc.add(b"x" * 60)            # fills the head (>= head_cap of 50)
    acc.add(b"y" * 10_000)        # giant chunk → clipped to tail_cap in the tail
    head, tail = acc.parts(b"")
    assert len(head) < 200 and len(tail) <= 50
    assert acc.dropped > 0


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
async def test_bash_timeout_is_total_not_idle(tmp_path: Path):
    """The timeout is a TOTAL wall-clock ceiling, not a per-line idle gap. A
    command that emits output continuously (never idle) must still stop at the
    limit; a per-readline timeout would reset on every line and run unbounded."""
    loop = asyncio.get_event_loop()
    start = loop.time()
    out = await shell.run_bash(
        tmp_path,
        "while true; do echo tick; sleep 0.05; done",
        timeout=1,
    )
    elapsed = loop.time() - start
    assert "timed out" in out
    assert "tick" in out  # output before the deadline is preserved
    assert elapsed < 5, f"chatty command ran {elapsed:.1f}s past its 1s total timeout"


@pytest.mark.anyio
async def test_bash_timeout_preserves_output_before_timeout(tmp_path: Path):
    """Output written before a timeout must not be discarded. The process
    produces visible output, then hangs — the kill should drain what's in
    the pipe so the user sees what happened before the hang."""
    out = await shell.run_bash(
        tmp_path,
        "echo STEP1; echo STEP2; sleep 30",
        timeout=1,
    )
    assert "timed out" in out
    assert "STEP1" in out, "output produced before timeout was discarded"
    assert "STEP2" in out, "output produced before timeout was discarded"


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
async def test_bash_timeout_drain_is_bounded(tmp_path: Path, monkeypatch):
    """After a timeout kills the process, the post-kill drain must finish within a
    bounded wall-clock budget even when the process keeps flushing short lines: the
    per-readline timeout resets on every line, so a steady burst would let the drain
    run forever. We drive run_bash with a fake process whose stdout never EOFs and
    always returns a line quickly — exactly that pathological burst — and assert the
    drain loop respects the wall-clock budget instead of looping unbounded.

    A real subprocess can't reproduce this deterministically: a command that streams
    fast enough to defeat the drain also streams fast enough that the *main* read
    loop never hits its timeout, so the kill path is never entered. The fake forces
    the timeout path (returncode set, lines always available) to test the drain in
    isolation."""
    import time

    shrunk_budget = 0.3
    monkeypatch.setattr(shell, "_DRAIN_BUDGET", shrunk_budget)

    class _FloodStdout:
        """A stdout that always has another line available almost instantly — the
        burst that resets the per-readline timeout indefinitely."""

        async def readline(self):
            await asyncio.sleep(0.001)
            return b"tick\n"

    class _FakeProc:
        pid = -1  # killpg will raise ProcessLookupError → falls back to .kill()
        returncode = -9
        stdout = _FloodStdout()

        def kill(self):
            pass

        async def wait(self):
            return -9

    async def _fake_create(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_create)

    start = time.monotonic()
    # timeout=0 forces the main read loop straight into the timeout/kill path.
    out = await shell.run_bash(tmp_path, "irrelevant", timeout=0)
    elapsed = time.monotonic() - start

    assert "timed out" in out
    # The drain must honor the wall-clock budget, not loop on the endless burst.
    assert elapsed < shrunk_budget + 1.0, f"drain not bounded: {elapsed:.2f}s"


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
async def test_run_bash_foreground_caps_running_memory(tmp_path, monkeypatch):
    """A flood must be middle-truncated to the running budget (not buffered whole):
    both ends survive, the marker is present, and the saved body stays ~budget-sized."""
    monkeypatch.setattr(shell, "MAX_OUTPUT_CHARS", 2_000)
    from marim_harness.tools import offload
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 100)
    out = await shell.run_bash(
        tmp_path,
        "echo HEAD-MARKER; for i in $(seq 1 20000); do echo filler$i; done; "
        "echo TAIL-VERDICT",
    )
    # Offloaded (large), and flagged as having hit the ceiling.
    assert "full output saved to" in out
    saved = list((tmp_path / ".marim" / "output").glob("bash-*.txt"))
    assert len(saved) == 1
    body = saved[0].read_text()
    # The body is bounded to ~budget + the exit line + the truncation marker, NOT the
    # full ~150 KB the command emitted.
    assert len(body) < 2_000 + 200
    assert "HEAD-MARKER" in body
    assert "TAIL-VERDICT" in body
    assert "chars truncated" in body


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


@pytest.mark.anyio
async def test_run_bash_stdin_data_reaches_the_process(tmp_path: Path):
    """stdin_data is piped to the command's stdin, written once, then closed
    (cat exits on EOF instead of hanging)."""
    out = await shell.run_bash(tmp_path, "cat", stdin_data=b"hello-stdin\n")
    assert out.startswith("exit 0")
    assert "hello-stdin" in out


@pytest.mark.anyio
async def test_run_bash_without_stdin_data_is_unchanged(tmp_path: Path):
    """Default None wires no stdin pipe — a command reading stdin sees EOF-ish
    inherited stdin, and plain commands behave exactly as before."""
    out = await shell.run_bash(tmp_path, "echo no-stdin")
    assert out.startswith("exit 0")
    assert "no-stdin" in out
