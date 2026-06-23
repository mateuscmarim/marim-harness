"""Spawn and run isolated sub-agents on behalf of the harness.

A sub-agent is a fresh Pydantic AI ``Agent`` built on the harness's *current*
model (so it tracks runtime model switches), with its tool reach decided up
front by the approval mode, an optional MCP grant, and an optional soft output
budget. Foreground spawns stream their events to the UI and fold their spend
into the running turn; background spawns run detached and persist their spend
immediately. The harness wires ``run``/``run_background`` onto ``Deps`` so the
``spawn_agent`` tool reaches them the same way other tools reach shared state.
"""

import re
from dataclasses import replace
from typing import Optional

from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

from .deps import Deps, SubAgent
from .hooks.dispatch import TurnHooks
from .permissions import Mode
from .tasks import TaskList
from .tools import fs
from .workspace import (
    cap_subagent_output,
    discover_agents,
    effective_tools,
    find_agent,
    subagent_instructions,
)
from .workspace.worktree import (
    WorktreeError,
    commit_worktree,
    create_or_reuse_worktree,
    delete_branch,
    remove_worktree,
    repo_root,
)

# Characters allowed in the slug taken from a stream id when naming an isolation
# branch; everything else collapses to a hyphen.
_ISO_SLUG = re.compile(r"[^a-z0-9._-]+")


def _iso_branch(stream_id: str, seq: int) -> str:
    """The branch name for an isolated spawn: ``subagent/<slug>``, where the slug
    comes from the spawn's stream id (unique per tool call, so parallel spawns
    don't collide) and falls back to a sequence number when there's no id."""
    base = _ISO_SLUG.sub("-", (stream_id or "").lower()).strip("-")
    return f"subagent/{base or f'anon-{seq}'}"


class SubagentRunner:
    """Builds and runs sub-agents for one harness. Reads the active model
    through ``get_model`` each spawn, so a runtime ``/model`` switch is picked
    up without rewiring."""

    def __init__(self, provider, mcp, deps, hooks: TurnHooks, session,
                 get_model, model_settings=None, request_limit: int = 50,
                 build_model=None):
        self.provider = provider
        self.mcp = mcp
        self.deps = deps
        self.hooks = hooks
        self.session = session
        self._get_model = get_model
        self._model_settings = model_settings
        self._request_limit = request_limit
        # Resolves a per-spawn model id to a model object on the current provider.
        # None when the harness has no model source (e.g. a fixed-model embed), in
        # which case a spawn that asks for an override is told it can't be honored.
        self._build_model = build_model
        # Monotonic counter for naming isolation branches when a spawn has no
        # stream id (e.g. a background run), so concurrent ones never collide.
        self._iso_seq = 0
        # Monotonic counter for naming a background spawn's output-spill file —
        # a background run has no stream id to key the spill on.
        self._bg_seq = 0

    def _open_worktree(self, stream_id: str):
        """Create an isolated git worktree for a spawn off the repo's HEAD.
        Returns ``(info, None)`` where ``info`` is a dict with ``repo``,
        ``branch`` and ``path``, or ``(None, message)`` when the workspace isn't a
        git repo or git refuses (the message is surfaced to the orchestrator)."""
        repo = repo_root(self.deps.workspace_root)
        if repo is None:
            return None, (
                "Isolated spawn needs a git repo, but this workspace isn't one. "
                "Re-run without isolation, or initialize git first."
            )
        self._iso_seq += 1
        branch = _iso_branch(stream_id, self._iso_seq)
        try:
            path = create_or_reuse_worktree(repo, branch)
        except WorktreeError as exc:
            return None, f"Couldn't create an isolated worktree: {exc}"
        return {"repo": repo, "branch": branch, "path": path}, None

    def _close_worktree(self, iso: dict) -> str:
        """Commit the spawn's changes to its branch, tear down the worktree, and
        return a note pointing the orchestrator at the branch (empty-ish when the
        spawn changed nothing). Never raises — cleanup problems become notes."""
        branch = iso["branch"]
        try:
            summary = commit_worktree(iso["path"], f"sub-agent work on {branch}")
        except WorktreeError as exc:
            return (f"\n\n[isolated run on branch {branch}: commit failed ({exc}); "
                    f"worktree left at {iso['path']}]")
        if summary is None:
            # Nothing was produced: drop the worktree (force, since gitignored
            # leftovers may remain) and the empty branch, so spawns that change
            # nothing don't leave a trail of dead branches behind.
            self._teardown_worktree(iso, force=True, drop_branch=True)
            return "\n\n[isolated run made no file changes]"
        self._teardown_worktree(iso)  # keep the branch — it's the deliverable
        return (f"\n\n[isolated run committed to branch {branch}:\n{summary}\n"
                f"merge with `git merge {branch}` or review `git diff {branch}`]")

    def _discard_worktree(self, iso: dict) -> None:
        """Teardown for a worktree whose spawn errored before it could report:
        force-remove it (the partial work is dirty and unwanted) and drop the
        branch, so a crashed isolated spawn leaves nothing behind."""
        self._teardown_worktree(iso, force=True, drop_branch=True)

    def _teardown_worktree(self, iso: dict, *, force: bool = False,
                           drop_branch: bool = False) -> None:
        """Best-effort removal of a spawn's worktree (and optionally its branch).
        Cleanup failures are swallowed — a stuck worktree is untidy, not fatal."""
        try:
            remove_worktree(iso["repo"], iso["branch"], force=force)
        except WorktreeError:
            pass
        if drop_branch:
            try:
                delete_branch(iso["repo"], iso["branch"])
            except WorktreeError:
                pass

    def handler(self, stream_id: str | None):
        """An event_stream_handler for a sub-agent run. For each streamed event it
        fires the Pre/PostToolUse hooks (so a sub-agent's autonomous tool calls run
        under the same hooks engine as the main agent's — guardrails apply to
        delegated work too), and — when a UI is listening and this is a foreground
        spawn (``stream_id`` set) — forwards the event to the UI tagged with
        ``stream_id`` so it streams nested under the spawn. Returns None only when
        there's nothing to do: no hooks configured and no UI listener (e.g. a
        headless background run with hooks off)."""
        cb = self.deps.on_subagent_event
        hooks_on = self.deps.hooks is not None
        forward = cb is not None and stream_id is not None
        if not hooks_on and not forward:
            return None
        # Per-run correlation map (tool_call_id → input) so a PostToolUse event
        # carries the args from its matching PreToolUse, as the main turn does.
        call_inputs: dict = {}

        async def handler(ctx, events) -> None:
            async for event in events:
                if hooks_on:
                    await self.hooks.tool_event(event, call_inputs)
                # Forward the whole usage (not just a token total) so the UI can
                # render the cache split and cost, not only the running count.
                if forward:
                    await cb(stream_id, event, getattr(ctx, "usage", None))

        return handler

    def build(
        self, type: str, max_output_chars: int | None = None,
        model: str | None = None, workspace_root=None,
    ) -> "tuple[Optional[SubAgent], Optional[str]]":
        """Build an isolated sub-agent of ``type``, with its reach decided up
        front: gated tools only in auto mode, so a run never needs an approval
        round. Runs on the harness's current model unless ``model`` overrides it
        (e.g. a cheap model for read-only fan-out); an override needs a model
        source, else it's reported as unhonorable. ``max_output_chars``, when the
        spawner set one, is folded into the sub-agent's instructions as a soft
        output budget. ``workspace_root`` overrides the path described in the
        sub-agent's instructions (e.g. a worktree for an isolated spawn); agent
        *discovery* still reads the main workspace. Returns ``(agent, None)`` or,
        for an unknown type or an unresolvable model, ``(None, message)``."""
        defn = find_agent(self.deps.workspace_root, type)
        if defn is None:
            names = ", ".join(
                a.qualified_name for a in discover_agents(self.deps.workspace_root)
            )
            return None, f"No sub-agent type {type!r}. Available: {names}."
        instr_root = (
            workspace_root if workspace_root is not None else self.deps.workspace_root
        )
        if model is None:
            model_obj = self._get_model()
        elif self._build_model is None:
            return None, (
                f"Can't run sub-agent on model {model!r}: no model source is "
                "available to resolve an override here."
            )
        else:
            model_obj = self._build_model(model)
        allow_gated = self.deps.mode is Mode.auto
        sub = Agent(
            model_obj,
            deps_type=Deps,
            instructions=subagent_instructions(
                defn, instr_root, max_output_chars
            ),
            model_settings=self._model_settings,
        )
        self.provider.register_subagent(sub, effective_tools(defn, allow_gated=allow_gated))
        assert sub is not None, "build must return (sub, err) with exactly one non-None"
        return sub, None

    def _cap_output(self, output: str, max_output_chars: int | None, ref: str) -> str:
        """Apply a spawner-set output cap to a sub-agent's report. Over budget,
        the full report is spilled to a workspace file and the main agent gets a
        within-budget head + pointer; otherwise the report passes through. The
        cap is lossless — nothing is discarded, only relocated."""
        rel = f".marim/subagent-output/{ref}.md"
        text, spill = cap_subagent_output(output, max_output_chars, rel)
        if spill is not None:
            fs.write_file(self.deps.workspace_root, rel, spill)
        return text

    async def run(
        self, type: str, task: str, stream_id: str,
        mcp_names: list[str] | None = None, max_output_chars: int | None = None,
        model: str | None = None, isolation: str | None = None,
    ) -> str:
        """Spawn one isolated sub-agent of ``type``, run it to completion on
        ``task``, and return its final report — streaming its events to the UI
        nested under the spawn. Shares the workspace Deps (read-only use) but
        starts a fresh conversation, so the sub-agent gets a clean context.
        ``mcp_names`` is the MCP servers the main agent granted this spawn (none
        by default); granted servers gate via the same approval hook as the main
        agent's. ``max_output_chars`` is an optional soft output budget the
        spawner sets: the sub-agent is told to distill toward it, and any report
        over budget is spilled to a file and replaced with a within-budget
        pointer so the inflow stays bounded. ``model`` optionally overrides the
        model this spawn runs on. ``isolation="worktree"`` runs the spawn in its
        own git worktree (branched from HEAD) so parallel mutating spawns can't
        clobber each other or the main tree; its changes are committed to a
        branch named in the report and the worktree is torn down."""
        iso = None
        if isolation == "worktree":
            iso, err = self._open_worktree(stream_id)
            if err is not None:
                return err
        work_root = iso["path"] if iso else None
        sub, err = self.build(type, max_output_chars, model, work_root)
        if sub is None:
            if iso:
                self._discard_worktree(iso)
            return err or f"Failed to build sub-agent {type!r}."
        granted, unknown = self.mcp.granted_servers(mcp_names)
        run_deps = replace(self.deps, workspace_root=work_root) if iso else self.deps
        await self.hooks.subagent_start(type, task)
        try:
            result = await sub.run(
                task, deps=run_deps, toolsets=granted,
                event_stream_handler=self.handler(stream_id),
                usage_limits=UsageLimits(request_limit=self._request_limit),
            )
        except Exception as exc:  # noqa: BLE001
            # A foreground spawn runs inside the turn's tool execution; letting its
            # crash propagate would fail the whole turn and take down any sibling
            # spawns fanning out alongside it. Contain it: report the failure as
            # this spawn's result so the orchestrator can route around it.
            if iso:
                self._discard_worktree(iso)
            await self.hooks.subagent_stop(type, task, f"error: {exc}")
            return f"Sub-agent {type!r} failed: {exc.__class__.__name__}: {exc}"
        await self.hooks.subagent_stop(type, task, result.output)
        # A foreground spawn runs inside the current turn, so its spend is folded
        # into the session total here and persisted by run_turn's _persist.
        self.session.usage += result.usage
        capped = self._cap_output(result.output, max_output_chars, stream_id)
        iso_note = self._close_worktree(iso) if iso else ""
        return self.mcp.grant_note(unknown) + capped + iso_note

    async def run_background(
        self, type: str, task: str, mcp_names: list[str] | None = None,
        max_output_chars: int | None = None, model: str | None = None,
        isolation: str | None = None,
    ) -> str:
        """Run a sub-agent as a detached background job: same isolation, mode-based
        reach, and MCP grant as a foreground spawn, but with no event streaming —
        the job's result is its final report, surfaced when the agent pulls it.
        Any unknown-server note rides along on that report. ``max_output_chars``
        applies only as a soft instruction here (the report is pulled later via
        the jobs API, which has no spill hook), so a background report is not
        hard-capped, with the over-budget remainder spilled to a workspace file
        the same way a foreground one is. ``model`` optionally overrides the model
        this spawn runs on. ``isolation="worktree"`` runs it in its own git
        worktree, committing its changes to a branch named in the report."""
        iso = None
        if isolation == "worktree":
            iso, err = self._open_worktree("")
            if err is not None:
                return err
        work_root = iso["path"] if iso else None
        sub, err = self.build(type, max_output_chars, model, work_root)
        if sub is None:
            if iso:
                self._discard_worktree(iso)
            return err or f"Failed to build sub-agent {type!r}."
        granted, unknown = self.mcp.granted_servers(mcp_names)
        await self.hooks.subagent_start(type, task)
        # A background sub-agent runs detached and concurrently with the user's
        # turn. Give it its own empty TaskList so its multi-step work never
        # mutates — or persists as — the user's session checklist; an isolated run
        # also redirects its file ops into the worktree. Every other Deps field
        # (jobs, hooks, lsp, …) stays shared.
        bg_deps = replace(self.deps, tasks=TaskList())
        if iso:
            bg_deps = replace(bg_deps, workspace_root=iso["path"])
        # No UI streaming for a background run, but still pass a handler so its
        # tool calls fire Pre/PostToolUse hooks (handler returns None when hooks
        # are off, so this is free otherwise). A crash here is intentionally NOT
        # contained: it propagates to the job registry, which marks the job failed.
        try:
            result = await sub.run(
                task, deps=bg_deps, toolsets=granted,
                event_stream_handler=self.handler(None),
                usage_limits=UsageLimits(request_limit=self._request_limit),
            )
        except Exception:
            if iso:
                self._discard_worktree(iso)
            raise
        await self.hooks.subagent_stop(type, task, result.output)
        # A background spawn finishes off-turn, so no run_turn will fold in its
        # spend — count it here and persist right away so the saved session
        # reflects it even if the process exits before the next turn.
        self.session.usage += result.usage
        self.session.persist()
        self._bg_seq += 1
        capped = self._cap_output(result.output, max_output_chars, f"bg-{self._bg_seq}")
        iso_note = self._close_worktree(iso) if iso else ""
        return self.mcp.grant_note(unknown) + capped + iso_note
