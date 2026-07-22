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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .models import CiRun, CiStatus, ForgeError, PullRequest, normalize_ci

# The one field-rich PR endpoint that also carries `ci` and `mergeable`;
# `tea pr <n>` has a different, ci-less shape and is deliberately not used.
PR_FIELDS = "index,title,state,author,head,base,mergeable,url,updated,ci"


def _list_prs_args(state: str, limit: int) -> list[str]:
    return ["pr", "list", "--state", state, "--limit", str(limit),
            "-o", "json", "--fields", PR_FIELDS]


def _list_prs_page_args(state: str, page: int) -> list[str]:
    """Same shape as ``_list_prs_args`` but with a fixed page size and an
    explicit ``--page`` (1-indexed; ``tea pulls list`` supports it, confirmed
    against the installed tea CLI's ``--help``). Used only by ``_find_pr``'s
    paging scan below, which needs a page-offset flag rather than a growing
    ``--limit`` — see ``_PAGE_SIZE``'s comment for why."""
    return ["pr", "list", "--state", state, "--limit", str(_PAGE_SIZE), "--page", str(page),
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
    # tea's PR number arrives as a string ``index``. A missing key (KeyError),
    # a non-numeric value (ValueError), or a non-str/int (TypeError) must become
    # a ForgeError here — otherwise the bare exception sails past the tool
    # layer's ForgeError-only handler and violates the "never raise into a tool"
    # contract.
    try:
        number = int(obj["index"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ForgeError(
            f"tea PR row has a missing or non-numeric 'index': {obj.get('index')!r}"
        ) from exc
    return PullRequest(
        number=number,
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


def _loads_dict_list(raw: str, what: str) -> list[dict[str, Any]]:
    """Parse tea JSON that is expected to be a list of objects, folding shape
    surprises into ForgeError so nothing bare escapes the tool layer. tea can
    emit ``null`` (some versions / an empty repo), a bare object, or rows that
    aren't objects; each of those would otherwise raise a TypeError /
    AttributeError downstream (iterating ``None``, ``obj.get`` on a string) and
    slip past the ForgeError-only tool handler."""
    payload = _loads(raw)
    if not isinstance(payload, list):
        raise ForgeError(
            f"expected a JSON list of {what} from tea, got {type(payload).__name__}"
        )
    for elem in payload:
        if not isinstance(elem, dict):
            raise ForgeError(
                f"expected {what} objects from tea, got a {type(elem).__name__}"
            )
    return payload


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


# Page size for the paging scan below. Real Gitea clamps the effective page
# size server-side to `api.MAX_RESPONSE_ITEMS` (default 50) regardless of what
# `--limit` asks for, so growing `--limit` and re-querying (the previous
# approach) just re-fetches the same capped newest-50 window forever — an old
# PR past that window is falsely reported missing. Instead we page with a
# fixed size and tea's `--page` flag (`tea pulls list --help` confirms it),
# terminating on an empty page rather than a short one: a short page is only a
# reliable "last page" signal when the server's actual per-page cap equals
# what we asked for, which we can't verify from here, whereas an empty page
# unambiguously means there is nothing left, however the server clamps.
_PAGE_SIZE = 50

# Safety net against a pathological/misbehaving server that never returns an
# empty page (e.g. loops through the same items). 40 pages * 50/page = 2000
# PRs scanned, comfortably beyond any real repo's history, before giving up.
_MAX_PAGES = 40


class TeaBackend:
    """ForgeBackend backed by the tea CLI, rooted at a workspace directory."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def list_prs(self, state: str, limit: int) -> list[PullRequest]:
        raw = await _run_tea(_list_prs_args(state, limit), self._root)
        return [_map_pr(o) for o in _loads_dict_list(raw, "pull requests")]

    async def _find_pr(
        self, state: str, match: Callable[[PullRequest], bool]
    ) -> PullRequest | None:
        """First PR satisfying ``match``, paging past the newest page. tea's
        field-rich ``pr list`` is the only endpoint carrying ``ci``/``mergeable``
        (``tea pr <n>`` has a ci-less shape and is deliberately avoided — see the
        module header), so we walk ``--page`` at a fixed ``--limit`` until the
        target is found or an empty page proves the list exhausted. This keeps
        an old PR (e.g. #5 in a repo with hundreds) findable instead of falsely
        'missing', and stays correct however the server clamps the per-page
        size (see ``_PAGE_SIZE``'s comment)."""
        for page in range(1, _MAX_PAGES + 1):
            raw = await _run_tea(_list_prs_page_args(state, page), self._root)
            prs = [_map_pr(o) for o in _loads_dict_list(raw, "pull requests")]
            if not prs:
                return None  # empty page: we've walked past the end of the list
            found = next((p for p in prs if match(p)), None)
            if found is not None:
                return found
        return None  # gave up after _MAX_PAGES pages without an empty page

    async def view_pr(
        self, number: int | None, branch: str | None
    ) -> PullRequest | None:
        def match(pr: PullRequest) -> bool:
            if number is not None:
                return pr.number == number
            return branch is not None and pr.head == branch

        return await self._find_pr("all", match)

    async def ci_status(self, branch: str) -> CiStatus:
        pr = await self._find_pr("all", lambda p: p.head == branch)
        overall = pr.ci if pr else "unknown"
        raw = await _run_tea(_runs_args(), self._root)
        runs = tuple(
            _map_run(o)
            for o in _loads_dict_list(raw, "action runs")
            if o.get("branch") == branch
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
