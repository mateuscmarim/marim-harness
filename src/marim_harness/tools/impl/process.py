"""Process tree cleanup utilities.

:func:`kill_process_tree` walks ``/proc`` to find every descendant of a given
PID, groups them by process group, and SIGKILLs each group atomically.  This
ensures deep grandchildren (e.g.  MCP servers spawned by a ``claude`` CLI
child) are reaped even though they live in their own process groups — the
normal ``os.killpg`` on the parent's group doesn't reach them.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal

logger = logging.getLogger(__name__)


def _read_ppid_map() -> dict[int, list[int]]:
    """Build a ``{ppid: [child_pids]}`` map from ``/proc``.

    O(n) scan of every numeric entry under ``/proc``.  Returns an empty dict
    when ``/proc`` is unreadable (non-Linux, sandboxed, etc.) so callers can
    fall back gracefully."""
    children: dict[int, list[int]] = {}
    try:
        proc_entries = os.listdir("/proc")
    except OSError:
        return children
    for entry in proc_entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8", errors="replace") as f:
                # Format: pid (comm) state ppid ...
                # ``comm`` may contain spaces but is always wrapped in parens,
                # so splitting on ") " reliably reaches field 3 (ppid).
                raw = f.read()
                close_paren = raw.rfind(")")
                if close_paren < 0:
                    continue
                fields = raw[close_paren + 2:].split()
                # Fields after the closing paren: state [0], ppid [1], pgrp [2], ...
                ppid = int(fields[1])
                pid = int(entry)
                children.setdefault(ppid, []).append(pid)
        except (FileNotFoundError, PermissionError, IndexError, ValueError):
            continue
    return children


def _descendants(root: int, ppid_map: dict[int, list[int]]) -> set[int]:
    """Return all PIDs descended from *root* (breadth-first)."""
    result: set[int] = set()
    queue = list(ppid_map.get(root, []))
    while queue:
        pid = queue.pop()
        if pid in result:
            continue
        result.add(pid)
        queue.extend(ppid_map.get(pid, []))
    return result


def kill_process_tree(pid: int) -> None:
    """SIGKILL *pid* and every descendant, grouped by process group.

    Walks ``/proc`` to build a PPID→children map, collects every descendant
    of *pid*, groups them by PGID, and kills each group atomically with
    ``SIGKILL``.  The top process's own group is killed last as a safety net.

    Designed for the common case where a spawned process (e.g. ``claude -p``)
    launches MCP servers in their own process groups — the normal
    ``os.killpg`` on the parent's group doesn't reach them.

    Falls back to killing *pid*'s group alone when ``/proc`` is unreadable or
    the tree walk finds no descendants.  Never raises."""
    # --- Phase 1: enumerate descendants before any kill ---
    try:
        ppid_map = _read_ppid_map()
    except Exception:  # noqa: BLE001 - best-effort: degrade to group-only
        logger.debug("ppid map read failed, falling back to group kill")
        ppid_map = {}

    descendant_pids = _descendants(pid, ppid_map) if ppid_map else set()

    # --- Phase 2: group all targets by PGID ---
    pgid_to_pids: dict[int, list[int]] = {}
    all_pids = list(descendant_pids) + [pid]  # top last (safety net)

    for p in all_pids:
        try:
            pgid = os.getpgid(p)
        except (ProcessLookupError, PermissionError):
            continue
        pgid_to_pids.setdefault(pgid, []).append(p)

    # --- Phase 3: kill each group ---
    top_pgid: int | None = None
    for pgid, members in pgid_to_pids.items():
        try:
            top_pg = os.getpgid(pid)
        except (ProcessLookupError, PermissionError):
            top_pg = None
        if pgid == top_pg:
            top_pgid = pgid  # defer top group to the end
            continue
        _kill_group(pgid, members)

    # Kill the top process's group last
    if top_pgid is not None:
        _kill_group(top_pgid, pgid_to_pids.get(top_pgid, [pid]))


def _kill_group(pgid: int, members: list[int]) -> None:
    """SIGKILL a process group, falling back to individual PIDs."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Group already gone or we lack perms — try individual kills
        for p in members:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(p, signal.SIGKILL)
    except OSError:
        # killpg failed for another reason — fall back to individual
        for p in members:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(p, signal.SIGKILL)
