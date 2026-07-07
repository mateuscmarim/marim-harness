"""Small git helpers used by the forge tools for branch resolution and the
create_pr preflight. Forge-neutral (pure git), so they are shared across every
backend. All subprocess access goes through ``_git`` for easy stubbing."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path


async def _git(args: list[str], root: Path) -> str | None:
    """Run ``git <args>`` in ``root``; return stdout, or None on any failure
    (missing git, non-zero exit). Best-effort — never raises into a tool."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(root),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return None
    if proc.returncode != 0:
        return None
    return out.decode("utf-8", "replace")


async def current_branch(root: Path) -> str | None:
    """The checked-out branch name, or None when detached / not a repo."""
    raw = await _git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    if raw is None:
        return None
    branch = raw.strip()
    return None if branch in ("", "HEAD") else branch


async def branch_pushed(root: Path, branch: str) -> bool:
    """True if the local remote-tracking ref ``origin/<branch>`` exists — i.e.
    the branch has been pushed (as of the last fetch/push). No network."""
    raw = await _git(
        ["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], root
    )
    return bool(raw and raw.strip())
