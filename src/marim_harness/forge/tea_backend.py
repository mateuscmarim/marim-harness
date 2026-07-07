"""The tea (Gitea CLI) ForgeBackend. This task adds only the *pure* pieces:
argv builders, tea-JSON->neutral-model mappers, and a JSON loader. The
subprocess I/O and the TeaBackend class arrive in the next task.

All values from ``tea … -o json --fields`` arrive as strings (``index:"51"``,
``mergeable:"false"``, ``ci:"success"``); the mappers coerce them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .models import CiRun, CiStatus, ForgeError, PullRequest, normalize_ci

# The one field-rich PR endpoint that also carries `ci` and `mergeable`;
# `tea pr <n>` has a different, ci-less shape and is deliberately not used.
PR_FIELDS = "index,title,state,author,head,base,mergeable,url,updated,ci"


def _list_prs_args(state: str, limit: int) -> list[str]:
    return ["pr", "list", "--state", state, "--limit", str(limit),
            "-o", "json", "--fields", PR_FIELDS]


def _create_pr_args(title: str, body: str, base: str | None, draft: bool,
                    head: str) -> list[str]:
    args = ["pr", "create", "--head", head, "--title", title, "--description",
            body]
    if base:
        args += ["--base", base]
    if draft:
        args.append("--draft")
    return args


def _checkout_pr_args(number: int, create_branch: bool) -> list[str]:
    args = ["pr", "checkout", str(number)]
    if create_branch:
        args.append("-b")
    return args


def _runs_args() -> list[str]:
    return ["actions", "runs", "-o", "json"]


def _map_pr(obj: dict[str, Any]) -> PullRequest:
    return PullRequest(
        number=int(obj["index"]),
        title=obj.get("title", ""),
        state=obj.get("state", ""),
        author=obj.get("author", ""),
        head=obj.get("head", ""),
        base=obj.get("base", ""),
        mergeable=str(obj.get("mergeable", "")).strip().lower() == "true",
        url=obj.get("url", ""),
        updated=obj.get("updated", ""),
        ci=normalize_ci(obj.get("ci")),
    )


def _map_run(obj: dict[str, Any]) -> CiRun:
    return CiRun(
        workflow=obj.get("workflow", ""),
        status=obj.get("status", ""),
        event=obj.get("event", ""),
        branch=obj.get("branch", ""),
        started=obj.get("started", ""),
    )


def _loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        first = raw.strip().splitlines()[0] if raw.strip() else "<empty>"
        raise ForgeError(f"could not parse tea output: {first!r}") from exc


async def _run_tea(args: list[str], cwd: Path, timeout: float = 20.0) -> str:
    """Run ``tea <args>`` as an argv list (never a shell string — user values
    like a PR body are inert) in ``cwd``. Returns stdout; raises ForgeError on
    launch failure, timeout, or non-zero exit (message = tea's stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tea", *args, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        raise ForgeError(f"could not launch tea: {exc}") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise ForgeError(f"tea timed out after {timeout}s") from exc
    if proc.returncode != 0:
        msg = err.decode("utf-8", "replace").strip() or f"tea exited {proc.returncode}"
        raise ForgeError(msg)
    return out.decode("utf-8", "replace")


def tea_available() -> bool:
    """True when ``tea`` is on PATH and a tea config file exists (a login is
    configured). Checked once at build time; the toolset attaches only if True."""
    if shutil.which("tea") is None:
        return False
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return (Path(base) / "tea" / "config.yml").is_file()


class TeaBackend:
    """ForgeBackend backed by the tea CLI, rooted at a workspace directory."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def list_prs(self, state: str, limit: int) -> list[PullRequest]:
        raw = await _run_tea(_list_prs_args(state, limit), self._root)
        return [_map_pr(o) for o in _loads(raw)]

    async def view_pr(
        self, number: int | None, branch: str | None
    ) -> PullRequest | None:
        for pr in await self.list_prs("all", 50):
            if number is not None and pr.number == number:
                return pr
            if number is None and branch and pr.head == branch:
                return pr
        return None

    async def ci_status(self, branch: str) -> CiStatus:
        pr = next(
            (p for p in await self.list_prs("all", 50) if p.head == branch), None
        )
        overall = pr.ci if pr else "unknown"
        raw = await _run_tea(_runs_args(), self._root)
        runs = tuple(
            _map_run(o) for o in _loads(raw) if o.get("branch") == branch
        )
        return CiStatus(overall=overall, runs=runs)

    async def create_pr(
        self, title: str, body: str, base: str | None, draft: bool, head: str
    ) -> PullRequest:
        # tea pr create prints text, not JSON; ignore its stdout and re-fetch by
        # head branch so the returned PullRequest has the same shape as list_prs.
        await _run_tea(_create_pr_args(title, body, base, draft, head), self._root)
        pr = await self.view_pr(None, head)
        if pr is None:
            raise ForgeError("PR created but could not be re-fetched by head branch")
        return pr

    async def checkout_pr(self, number: int, create_branch: bool) -> str:
        await _run_tea(_checkout_pr_args(number, create_branch), self._root)
        return f"Checked out PR #{number}."
