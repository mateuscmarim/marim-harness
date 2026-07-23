"""Forge-agnostic PR/CI tools + toolset assembly.

Each tool closes over the selected ForgeBackend (bound at build time), so the
tool bodies never mention tea/gh. Read tools are ungated; create_pr/checkout_pr
gate for approval (create/checkout mutate remote/working-tree state — the same
boundary as net_tools/bash). ``forge_toolsets`` lives here rather than in the
``forge`` package so ``forge`` never imports the tools layer."""

from __future__ import annotations

from collections.abc import Awaitable
from pathlib import Path
from typing import TypeVar

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from ..forge.backend import ForgeBackend
from ..forge.gitref import branch_pushed, current_branch
from ..forge.models import ForgeError, PullRequest
from ..forge.select import select_backend
from ..runtime.deps import Deps

_T = TypeVar("_T")


async def _forge_call(coro: Awaitable[_T]) -> _T | str:
    """Run a backend call, folding its ``ForgeError`` into the tool-result
    string every forge tool returns on failure. Shared by all five tools so
    the try/except appears once instead of per-closure."""
    try:
        return await coro
    except ForgeError as exc:
        return f"Forge error: {exc}"


async def _list_prs(
    backend: ForgeBackend, ctx: RunContext[Deps], state: str = "open", limit: int = 30
) -> str:
    """List pull requests. `state` is open|closed|all (default open). Returns
    one line per PR: number, state, title, and overall CI conclusion."""
    prs = await _forge_call(backend.list_prs(state, limit))
    if isinstance(prs, str):
        return prs
    if not prs:
        return f"No {state} pull requests."
    return "\n".join(f"#{p.number} [{p.state}] {p.title} (ci: {p.ci})" for p in prs)


async def _view_pr(
    backend: ForgeBackend, ctx: RunContext[Deps], number: int | None = None
) -> str:
    """Show one pull request. With no `number`, resolves the PR for the
    current branch. Reports head→base, mergeability, CI conclusion, and URL."""
    branch = None
    if number is None:
        branch = await current_branch(ctx.deps.workspace.root)
        if branch is None:
            return "Not on a branch — pass a PR number."
    pr = await _forge_call(backend.view_pr(number, branch))
    if isinstance(pr, str):
        return pr
    if pr is None:
        what = f"#{number}" if number is not None else f"branch '{branch}'"
        return f"No PR found for {what}."
    return (f"#{pr.number} [{pr.state}] {pr.title}\n"
            f"{pr.head} → {pr.base}\n"
            f"mergeable: {pr.mergeable} | ci: {pr.ci}\n{pr.url}")


async def _ci_status(
    backend: ForgeBackend, ctx: RunContext[Deps], branch: str | None = None
) -> str:
    """Report CI for a branch (defaults to the current branch). Shows the
    overall conclusion plus recent workflow runs (most recent first)."""
    b = branch or await current_branch(ctx.deps.workspace.root)
    if b is None:
        return "Not on a branch — pass branch=."
    st = await _forge_call(backend.ci_status(b))
    if isinstance(st, str):
        return st
    lines = [f"CI for {b}: {st.overall}"]
    lines += [f"  {r.workflow} [{r.status}] ({r.event} {r.started})" for r in st.runs[:10]]
    return "\n".join(lines)


async def _create_pr(
    backend: ForgeBackend, ctx: RunContext[Deps], title: str, body: str = "",
    base: str | None = None, draft: bool = False,
) -> str:
    """Open a pull request from the current branch. Requires the branch to be
    pushed first (it will not push for you) and refuses if an open PR already
    exists for it. `base` defaults to the repo's default branch."""
    root = ctx.deps.workspace.root
    branch = await current_branch(root)
    if branch is None:
        return "Not on a branch — cannot open a PR."
    if not await branch_pushed(root, branch):
        return f"Branch '{branch}' is not pushed. Run: git push -u origin {branch}"

    async def _open() -> str | PullRequest:
        # The backend pages past Gitea's ~50-item server clamp so an open PR
        # older than the newest page is still found — otherwise create_pr would
        # open a duplicate over it.
        existing = await backend.find_open_pr_for_branch(branch)
        if existing is not None:
            return (f"An open PR already exists for '{branch}': "
                    f"#{existing.number} {existing.url}")
        return await backend.create_pr(title, body, base, draft, branch)

    result = await _forge_call(_open())
    if isinstance(result, str):
        return result
    pr = result
    return f"Created PR #{pr.number}: {pr.url}"


async def _checkout_pr(
    backend: ForgeBackend, ctx: RunContext[Deps], number: int, create_branch: bool = True
) -> str:
    """Check out a pull request locally (fetches and switches the working
    tree). `create_branch` makes a local branch if one doesn't exist yet."""
    return await _forge_call(backend.checkout_pr(number, create_branch))


def build_forge_toolset(backend: ForgeBackend) -> FunctionToolset[Deps]:
    ts: FunctionToolset[Deps] = FunctionToolset()

    async def list_prs(ctx: RunContext[Deps], state: str = "open", limit: int = 30) -> str:
        return await _list_prs(backend, ctx, state, limit)

    list_prs.__doc__ = _list_prs.__doc__

    async def view_pr(ctx: RunContext[Deps], number: int | None = None) -> str:
        return await _view_pr(backend, ctx, number)

    view_pr.__doc__ = _view_pr.__doc__

    async def ci_status(ctx: RunContext[Deps], branch: str | None = None) -> str:
        return await _ci_status(backend, ctx, branch)

    ci_status.__doc__ = _ci_status.__doc__

    async def create_pr(
        ctx: RunContext[Deps], title: str, body: str = "", base: str | None = None,
        draft: bool = False,
    ) -> str:
        return await _create_pr(backend, ctx, title, body, base, draft)

    create_pr.__doc__ = _create_pr.__doc__

    async def checkout_pr(
        ctx: RunContext[Deps], number: int, create_branch: bool = True
    ) -> str:
        return await _checkout_pr(backend, ctx, number, create_branch)

    checkout_pr.__doc__ = _checkout_pr.__doc__

    ts.add_function(list_prs)
    ts.add_function(view_pr)
    ts.add_function(ci_status)
    ts.add_function(create_pr, requires_approval=True)
    ts.add_function(checkout_pr, requires_approval=True)
    return ts


def forge_toolsets(forge_enabled: bool, root: Path) -> list[FunctionToolset[Deps]]:
    """The forge toolsets to attach to the Agent: a single-element list when a
    backend is selected, else empty. This is the one wiring seam build_harness
    calls."""
    backend = select_backend(forge_enabled, root)
    return [build_forge_toolset(backend)] if backend else []
