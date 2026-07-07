"""Forge-agnostic PR/CI tools + toolset assembly.

Each tool closes over the selected ForgeBackend (bound at build time), so the
tool bodies never mention tea/gh. Read tools are ungated; create_pr/checkout_pr
gate for approval (create/checkout mutate remote/working-tree state — the same
boundary as net_tools/bash). ``forge_toolsets`` lives here rather than in the
``forge`` package so ``forge`` never imports the tools layer."""

from __future__ import annotations

from pathlib import Path

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from ..forge.backend import ForgeBackend
from ..forge.gitref import branch_pushed, current_branch
from ..forge.models import ForgeError
from ..forge.select import select_backend
from ..runtime.deps import Deps


def build_forge_toolset(backend: ForgeBackend) -> FunctionToolset[Deps]:
    ts: FunctionToolset[Deps] = FunctionToolset()

    async def list_prs(ctx: RunContext[Deps], state: str = "open", limit: int = 30) -> str:
        """List pull requests. `state` is open|closed|all (default open). Returns
        one line per PR: number, state, title, and overall CI conclusion."""
        try:
            prs = await backend.list_prs(state, limit)
        except ForgeError as exc:
            return f"Forge error: {exc}"
        if not prs:
            return f"No {state} pull requests."
        return "\n".join(f"#{p.number} [{p.state}] {p.title} (ci: {p.ci})" for p in prs)

    async def view_pr(ctx: RunContext[Deps], number: int | None = None) -> str:
        """Show one pull request. With no `number`, resolves the PR for the
        current branch. Reports head→base, mergeability, CI conclusion, and URL."""
        branch = None
        if number is None:
            branch = await current_branch(ctx.deps.workspace.root)
            if branch is None:
                return "Not on a branch — pass a PR number."
        try:
            pr = await backend.view_pr(number, branch)
        except ForgeError as exc:
            return f"Forge error: {exc}"
        if pr is None:
            what = f"#{number}" if number is not None else f"branch '{branch}'"
            return f"No PR found for {what}."
        return (f"#{pr.number} [{pr.state}] {pr.title}\n"
                f"{pr.head} → {pr.base}\n"
                f"mergeable: {pr.mergeable} | ci: {pr.ci}\n{pr.url}")

    async def ci_status(ctx: RunContext[Deps], branch: str | None = None) -> str:
        """Report CI for a branch (defaults to the current branch). Shows the
        overall conclusion plus recent workflow runs (most recent first)."""
        b = branch or await current_branch(ctx.deps.workspace.root)
        if b is None:
            return "Not on a branch — pass branch=."
        try:
            st = await backend.ci_status(b)
        except ForgeError as exc:
            return f"Forge error: {exc}"
        lines = [f"CI for {b}: {st.overall}"]
        lines += [f"  {r.workflow} [{r.status}] ({r.event} {r.started})" for r in st.runs[:10]]
        return "\n".join(lines)

    async def create_pr(
        ctx: RunContext[Deps], title: str, body: str = "", base: str | None = None,
        draft: bool = False,
    ) -> str:
        """Open a pull request from the current branch. Requires the branch to be
        pushed first (it will not push for you) and refuses if a PR already
        exists for it. `base` defaults to the repo's default branch."""
        root = ctx.deps.workspace.root
        branch = await current_branch(root)
        if branch is None:
            return "Not on a branch — cannot open a PR."
        if not await branch_pushed(root, branch):
            return f"Branch '{branch}' is not pushed. Run: git push -u origin {branch}"
        try:
            existing = await backend.view_pr(None, branch)
            if existing is not None:
                return f"A PR already exists for '{branch}': #{existing.number} {existing.url}"
            pr = await backend.create_pr(title, body, base, draft, branch)
        except ForgeError as exc:
            return f"Forge error: {exc}"
        return f"Created PR #{pr.number}: {pr.url}"

    async def checkout_pr(
        ctx: RunContext[Deps], number: int, create_branch: bool = True
    ) -> str:
        """Check out a pull request locally (fetches and switches the working
        tree). `create_branch` makes a local branch if one doesn't exist yet."""
        try:
            return await backend.checkout_pr(number, create_branch)
        except ForgeError as exc:
            return f"Forge error: {exc}"

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
