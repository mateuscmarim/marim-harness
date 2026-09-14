"""Tests for tools/impl/process.py — kill_process_tree."""

from __future__ import annotations

import contextlib
import os
import select
import signal
import time

from marim_harness.tools.impl.process import kill_process_tree


def _make_process_tree():
    """Fork a 3-level process tree, each in its own session/process group.

    Returns ``(root_pid, [child_pid, grandchild_pid])`.  All processes are
    ``sleep infinity`` so they stay alive until killed.

    The tree mirrors the real-world pattern:
        marim → claude (new session) → uv (new session) → python
    """
    # Root: new session + new process group
    root = os.fork()
    if root == 0:
        os.setsid()
        # Child: new session + new process group
        child = os.fork()
        if child == 0:
            os.setsid()
            # Grandchild: new session + new process group
            grandchild = os.fork()
            if grandchild == 0:
                os.setsid()
                os.execvp("sleep", ["sleep", "infinity"])
            # Child waits for grandchild to exec, then sleeps
            time.sleep(0.2)
            os.execvp("sleep", ["sleep", "infinity"])
        # Root waits for child to exec, then sleeps
        time.sleep(0.3)
        os.execvp("sleep", ["sleep", "infinity"])

    # Wait for the tree to fully start
    time.sleep(0.5)

    # Discover children via /proc (they may have different PIDs than
    # the fork returns due to exec)
    descendants = _find_descendants(root)
    return root, descendants


def _find_descendants(pid: int) -> list[int]:
    """Find direct+indirect child PIDs via /proc."""
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    result = []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                raw = f.read()
                close = raw.rfind(")")
                fields = raw[close + 2 :].split()
                ppid = int(fields[1])
                if ppid == pid:
                    result.append(int(entry))
        except (FileNotFoundError, PermissionError, IndexError, ValueError):
            continue
    return result


def _alive(pid: int) -> bool:
    """Check if a process is still alive (not a zombie)."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    # os.kill(pid, 0) succeeds for zombies — check actual state
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("State:"):
                    return "Z" not in line  # Z = zombie
    except (FileNotFoundError, PermissionError):
        pass
    return True


def _reap(pid: int) -> None:
    """Wait for a zombie to be reaped."""
    with contextlib.suppress(Exception):
        os.waitpid(pid, os.WNOHANG)


class TestKillProcessTree:
    def test_kills_deep_tree(self):
        """kill_process_tree reaps all descendants across process groups."""
        root, descendants = _make_process_tree()
        assert len(descendants) >= 1, "expected at least one descendant"

        kill_process_tree(root)

        # Give SIGKILL time to take effect
        time.sleep(0.1)

        assert not _alive(root), f"root {root} still alive"
        for d in descendants:
            assert not _alive(d), f"descendant {d} still alive"

        # Clean up zombies
        _reap(root)
        for d in descendants:
            _reap(d)

    def test_already_dead_no_crash(self):
        """Calling on an already-dead PID doesn't raise."""
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        # PID is now dead and reaped
        kill_process_tree(pid)  # should not raise

    def test_no_descendants_still_kills_root(self):
        """A leaf process with no children is still killed."""
        pid = os.fork()
        if pid == 0:
            os.setsid()
            os.execvp("sleep", ["sleep", "infinity"])
        time.sleep(0.1)

        kill_process_tree(pid)
        time.sleep(0.1)

        assert not _alive(pid)
        _reap(pid)

    def test_single_process_in_custom_group(self):
        """Kills a process that created its own process group via setsid."""
        ready_read, ready_write = os.pipe()
        try:
            pid = os.fork()
        except BaseException:
            os.close(ready_read)
            os.close(ready_write)
            raise
        if pid == 0:
            os.close(ready_read)
            try:
                os.setsid()
                os.write(ready_write, b"1")
                os.close(ready_write)
                os.execvp("sleep", ["sleep", "infinity"])
            finally:
                os._exit(1)  # Never return a failed child setup into pytest.
        os.close(ready_write)
        try:
            # A loaded runner may not schedule the child within a fixed sleep.
            # Only exercise group killing after the child confirms setsid ran.
            readable, _, _ = select.select([ready_read], [], [], 5)
            assert readable, "child did not establish its process group within 5s"
            assert os.read(ready_read, 1) == b"1", "child exited before readiness"
            assert os.getpgid(pid) == pid

            kill_process_tree(pid)
            deadline = time.monotonic() + 5
            while _alive(pid) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not _alive(pid)
        finally:
            os.close(ready_read)
            # On readiness failure it may still share pytest's group: target
            # only the known child PID, then reap even if an assertion failed.
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
