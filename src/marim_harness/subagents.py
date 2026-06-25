"""Spawn and run isolated sub-agents on behalf of the harness.

A sub-agent is a fresh Pydantic AI ``Agent`` built on the harness's *current*
model (so it tracks runtime model switches), with its tool reach decided up
front by the approval mode, an optional MCP grant, and an optional soft output
budget. Foreground spawns stream their events to the UI and fold their spend
into the running turn; background spawns run detached and persist their spend
immediately. The harness wires ``run``/``run_background`` onto ``Deps`` so the
``spawn_agent`` tool reaches them the same way other tools reach shared state.
"""

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

if TYPE_CHECKING:
    from .mcp.manager import McpManager
    from .session.ctrl import SessionController
    from .tools.provider import ToolProvider

from .deps import Deps, SubAgent
from .errors import is_transient_model_error
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

logger = logging.getLogger(__name__)

# Characters allowed in the slug taken from a stream id when naming an isolation
# branch; everything else collapses to a hyphen.
_ISO_SLUG = re.compile(r"[^a-z0-9._-]+")


def _iso_branch(stream_id: str, seq: int) -> str:
    """The branch name for an isolated spawn: ``subagent/<slug>``, where the slug
    comes from the spawn's stream id (unique per tool call, so parallel spawns
    don't collide) and falls back to a sequence number when there's no id."""
    base = _ISO_SLUG.sub("-", (stream_id or "").lower()).strip("-")
    return f"subagent/{base or f'anon-{seq}'}"


def _resumable_history(messages: list) -> list | None:
    """Turn the conversation captured from a failed sub-agent attempt into a
    history safe to resume from, or ``None`` when there's nothing to carry (the
    request failed before any message was recorded — resume by re-sending the
    task). Reuses the main turn's two repairs so a sub-agent resume obeys the same
    provider invariant: drop a half-streamed nameless tool call, then synthesize a
    return for any tool call left unanswered when the attempt died. Imported lazily
    because ``agent`` imports this module — a top-level import would cycle."""
    if not messages:
        return None
    from .agent import _drop_nameless_tool_calls, _repair_unanswered_tool_calls

    repaired = _repair_unanswered_tool_calls(_drop_nameless_tool_calls(messages))
    return repaired or None


class SubagentRunner:
    """Builds and runs sub-agents for one harness. Reads the active model
    through ``get_model`` each spawn, so a runtime ``/model`` switch is picked
    up without rewiring."""

    def __init__(self, provider: "ToolProvider", mcp: "McpManager", deps: Deps,
                 hooks: TurnHooks, session: "SessionController",
                 get_model: Callable[[], Any],
                 model_settings: ModelSettings | None = None,
                 request_limit: int = 50,
                 retry_attempts: int = 2,
                 build_model: Callable[[str], Any] | None = None,
                 concurrency: int | None = None) -> None:
        self.provider = provider
        self.mcp = mcp
        self.deps = deps
        self.hooks = hooks
        self.session = session
        self._get_model = get_model
        self._model_settings = model_settings
        self._request_limit = request_limit
        # How many times a sub-agent run is re-issued after a *transient* model
        # error (gateway/server hiccup, request timeout, rate limit) before the
        # failure is allowed to surface. A permanent error (malformed request,
        # auth) is never retried — see is_transient_model_error.
        self._retry_attempts = retry_attempts
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
        # Optional cap on how many spawns may run their model loop at once. A
        # fan-out fires every spawn's request concurrently, which is exactly what
        # trips a shared provider route's upstream rate limit; the cap queues the
        # excess instead. None ⇒ unbounded (the historical behavior). The semaphore
        # is built lazily on first use so the runner can be constructed off-loop.
        self._concurrency = concurrency if (concurrency and concurrency > 0) else None
        self._sem: asyncio.Semaphore | None = None

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
        with contextlib.suppress(WorktreeError):
            remove_worktree(iso["repo"], iso["branch"], force=force)
        if drop_branch:
            with contextlib.suppress(WorktreeError):
                delete_branch(iso["repo"], iso["branch"])

    def handler(self, stream_id: str | None,
                on_first_event: Callable[[], None] | None = None):
        """An event_stream_handler for a sub-agent run. For each streamed event it
        fires the Pre/PostToolUse hooks (so a sub-agent's autonomous tool calls run
        under the same hooks engine as the main agent's — guardrails apply to
        delegated work too), and — when a UI is listening and this is a foreground
        spawn (``stream_id`` set) — forwards the event to the UI tagged with
        ``stream_id`` so it streams nested under the spawn. ``on_first_event``, when
        given, is called exactly once on the first streamed event — the spawn's
        time-to-first-token, used by the debug timing line in ``_execute_spawn``.
        The probe never *creates* a handler on its own: measuring must not turn a
        non-streamed spawn into a streamed one, so when there's nothing else to do
        this still returns None and the spawn's ttft is reported as n/a. (Every
        foreground fan-out spawn forwards to the UI, so it streams and is timed.)
        Returns None only when there's nothing to do: no hooks configured and no
        UI listener (e.g. a headless background run with hooks off)."""
        cb = self.deps.on_subagent_event
        hooks_on = self.deps.hooks is not None
        forward = cb is not None and bool(stream_id)
        if not hooks_on and not forward:
            return None
        # Per-run correlation map (tool_call_id → input) so a PostToolUse event
        # carries the args from its matching PreToolUse, as the main turn does.
        call_inputs: dict = {}
        seen_first = False

        async def handler(ctx, events) -> None:
            nonlocal seen_first
            async for event in events:
                if not seen_first:
                    seen_first = True
                    if on_first_event is not None:
                        on_first_event()
                if hooks_on:
                    await self.hooks.tool_event(event, call_inputs)
                # Forward the whole usage (not just a token total) so the UI can
                # render the cache split and cost, not only the running count.
                if cb is not None and stream_id:
                    await cb(stream_id, event, getattr(ctx, "usage", None))

        return handler

    def build(
        self, type: str, max_output_chars: int | None = None,
        model: str | None = None, workspace_root=None,
    ) -> "tuple[SubAgent | None, str | None]":
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
            # Match the main agent's tool-retry budget (agent.py builds it with
            # retries=2). pydantic-ai defaults to 1, which gives the model a single
            # attempt to correct a malformed tool argument before the whole turn
            # dies with UnexpectedModelBehavior. Sub-agents hit this constantly:
            # models carry strong priors for Claude Code's tool interfaces (bash
            # timeout in ms, grep's -i/output_mode/head_limit), and a sub-agent at
            # budget 1 dies on the first mispredict where the main agent recovers.
            retries=2,
            model_settings=self._model_settings,
        )
        self.provider.register_subagent(sub, effective_tools(defn, allow_gated=allow_gated))
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

    # Backoff before a transient-error retry: exponential from a small base, capped,
    # so a brief upstream blip is ridden out without stalling the spawn for long.
    _RETRY_BASE_DELAY = 0.5
    _RETRY_MAX_DELAY = 8.0

    async def _retry_backoff(self, attempt: int) -> None:
        """Sleep before the ``attempt``-th retry (1-based): exponential backoff,
        capped. Split out so tests can stub it without real time passing."""
        delay = min(self._RETRY_BASE_DELAY * 2 ** (attempt - 1), self._RETRY_MAX_DELAY)
        await asyncio.sleep(delay)

    def _slot(self):
        """Acquire-context bounding concurrent spawn runs to ``_concurrency``; a
        no-op ``nullcontext`` when unbounded. The semaphore is created on first use
        (binds to the running loop), and the single-threaded event loop makes the
        lazy ``is None`` check race-free."""
        if self._concurrency is None:
            return contextlib.nullcontext()
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._concurrency)
        return self._sem

    async def _run_to_completion(self, sub, task, run_deps, granted, handler,
                                 stream_id: str | None = None):
        """Run a built sub-agent to its final result, retrying *transient* model
        errors (gateway/server hiccups, timeouts, rate limits) with backoff. A
        permanent error, or exhausting the retry budget, re-raises for the caller's
        contain/propagate path.

        A retry *resumes* the run rather than restarting it: the conversation the
        failed attempt produced (captured even though it raised) is carried forward
        as ``message_history``, so a transient blip on step 20 of a multi-step spawn
        doesn't throw away — and re-pay for — the first 19 steps. The captured
        history is sanitized and repaired the same way the main turn does before a
        resumed request (drop a half-streamed nameless tool call, synthesize a
        return for any unanswered call), or every provider rejects it. A mutating
        isolated spawn keeps whatever files the failed attempt already wrote, which
        is fine — its worktree is a throwaway branch.

        A foreground spawn (``stream_id`` set) gets an out-of-band UI notice on each
        retry so the user sees the card recover rather than silently stall."""
        attempt = 0
        resume_history: list | None = None
        while True:
            captured: list = []
            try:
                with capture_run_messages() as captured:
                    return await sub.run(
                        task if resume_history is None else None,
                        message_history=resume_history,
                        deps=run_deps, toolsets=granted,
                        event_stream_handler=handler,
                        usage_limits=UsageLimits(request_limit=self._request_limit),
                    )
            except Exception as exc:  # noqa: BLE001
                if attempt >= self._retry_attempts or not is_transient_model_error(exc):
                    raise
                attempt += 1
                resume_history = _resumable_history(list(captured))
                logger.info(
                    "sub-agent hit a transient error (%s); resuming, retry %d/%d "
                    "after backoff", exc.__class__.__name__, attempt,
                    self._retry_attempts,
                )
                await self._notice_retry(stream_id, exc, attempt)
                await self._retry_backoff(attempt)

    async def _notice_retry(self, stream_id: str | None, exc: Exception,
                            attempt: int) -> None:
        """Surface a transient-error retry on a foreground spawn's card. A no-op for
        a background spawn (no card) or when no UI is listening."""
        cb = self.deps.on_subagent_notice
        if cb is None or not stream_id:
            return
        await cb(
            stream_id,
            f"transient error ({exc.__class__.__name__}) — "
            f"retrying {attempt}/{self._retry_attempts}…",
        )

    async def _execute_spawn(
        self, type: str, task: str, mcp_names: list[str] | None,
        max_output_chars: int | None, model: str | None, isolation: str | None,
        *, background: bool, stream_id: str,
    ) -> str:
        """Common lifecycle for foreground and background spawns.

        Handles: worktree open → build → MCP grant → hooks start → run →
        hooks stop → usage → cap output → worktree close.

        ``background=False`` (foreground): exceptions are contained and returned
        as an error string; usage is folded into the session but NOT persisted
        (the caller's ``run_turn`` persists it). ``stream_id`` names the spill
        file and is passed to the event handler.

        ``background=True``: exceptions propagate to the job registry; usage is
        persisted immediately; deps get an isolated ``TaskList()``; the spill
        file is named after the ``_bg_seq`` counter.
        """
        iso = None
        # Phase timing for the spawn (harness setup vs. model time-to-first-token).
        # Only wired up under DEBUG so a normal run keeps the exact event-handler
        # path it had before (passing an on_first_event probe would otherwise force
        # a headless hooks-off spawn to iterate its event stream just to time it).
        debug = logger.isEnabledFor(logging.DEBUG)
        t0 = time.perf_counter()
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
        t_built = time.perf_counter()
        granted, unknown = self.mcp.granted_servers(mcp_names)
        if background:
            # A background sub-agent runs detached and concurrently with the
            # user's turn. Give it its own empty TaskList so its multi-step work
            # never mutates — or persists as — the user's session checklist; an
            # isolated run also redirects its file ops into the worktree. Every
            # other Deps field (jobs, hooks, lsp, …) stays shared.
            run_deps = replace(self.deps, tasks=TaskList())
            if iso:
                run_deps = replace(run_deps, workspace_root=iso["path"])
        else:
            run_deps = replace(self.deps, workspace_root=work_root) if iso else self.deps
        await self.hooks.subagent_start(type, task)
        # Foreground passes its tool_call_id; a background spawn now passes its own
        # stream_id too (Phase 2), so it streams to the UI exactly like foreground.
        # An empty stream_id (headless / no id) forwards nothing — handler() gates
        # on truthiness.
        first_event_at: list[float] = []
        probe = (lambda: first_event_at.append(time.perf_counter())) if debug else None
        handler = self.handler(stream_id, on_first_event=probe)
        try:
            # Bound concurrent model runs (the part that hits the provider) so a
            # wide fan-out queues instead of slamming a rate-limited route at once.
            async with self._slot():
                result = await self._run_to_completion(
                    sub, task, run_deps, granted, handler, stream_id,
                )
        except Exception as exc:  # noqa: BLE001
            self._log_spawn_timing(type, t0, t_built, first_event_at, failed=True)
            if iso:
                self._discard_worktree(iso)
            if background:
                # A background crash is intentionally NOT contained: it
                # propagates to the job registry, which marks the job failed.
                raise
            # A foreground spawn runs inside the turn's tool execution; letting
            # its crash propagate would fail the whole turn and take down any
            # sibling spawns fanning out alongside it. Contain it.
            await self.hooks.subagent_stop(type, task, f"error: {exc}")
            return f"Sub-agent {type!r} failed: {exc.__class__.__name__}: {exc}"
        self._log_spawn_timing(type, t0, t_built, first_event_at, failed=False)
        await self.hooks.subagent_stop(type, task, result.output)
        self.session.usage += result.usage
        if background:
            # A background spawn finishes off-turn, so no run_turn will fold in
            # its spend — persist right away so the saved session reflects it
            # even if the process exits before the next turn.
            self.session.persist()
            self._bg_seq += 1
            spill_ref = f"bg-{self._bg_seq}"
        else:
            # A foreground spawn's spend is persisted by run_turn's _persist.
            spill_ref = stream_id
        capped = self._cap_output(result.output, max_output_chars, spill_ref)
        iso_note = self._close_worktree(iso) if iso else ""
        return self.mcp.grant_note(unknown) + capped + iso_note

    def _log_spawn_timing(
        self, type: str, t0: float, t_built: float,
        first_event_at: list[float], *, failed: bool,
    ) -> None:
        """Emit a DEBUG line splitting a spawn's wall time into ``setup`` (all the
        harness-side work before the model is asked: worktree open, discovery,
        Agent build, tool registration) and ``ttft`` (time from spawn start to the
        first streamed event — the provider's time-to-first-token, where the real
        cost lives). ``total`` is the whole spawn. A no-op unless DEBUG logging is
        on (set ``MARIM_DEBUG=1``), so a normal run pays nothing. Fan out a few
        spawns and read these side by side to see whether a slow spawn is the
        harness or the model — and whether parallel spawns serialize."""
        if not logger.isEnabledFor(logging.DEBUG):
            return
        now = time.perf_counter()
        setup_ms = (t_built - t0) * 1000
        total_ms = (now - t0) * 1000
        ttft = f"{(first_event_at[0] - t0) * 1000:.0f}ms" if first_event_at else "n/a"
        logger.debug(
            "spawn %r timing%s: setup=%.0fms ttft=%s total=%.0fms",
            type, " (failed)" if failed else "", setup_ms, ttft, total_ms,
        )

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
        return await self._execute_spawn(
            type, task, mcp_names, max_output_chars, model, isolation,
            background=False, stream_id=stream_id,
        )

    async def run_background(
        self, type: str, task: str, mcp_names: list[str] | None = None,
        max_output_chars: int | None = None, model: str | None = None,
        isolation: str | None = None, stream_id: str = "",
    ) -> str:
        """Run a sub-agent as a detached background job: same isolation, mode-based
        reach, and MCP grant as a foreground spawn. When ``stream_id`` is set (the
        launching spawn's tool_call_id) and a UI listener exists, the run streams
        its events to that card live — identical to a foreground spawn (Phase 2);
        with no ``stream_id`` (headless) it streams nothing and the job's result is
        its final report, surfaced when the agent pulls it. Any unknown-server note
        rides along on that report. ``max_output_chars`` applies only as a soft
        instruction here (the report is pulled later via the jobs API, which has no
        spill hook), so a background report is not hard-capped, with the over-budget
        remainder spilled to a workspace file the same way a foreground one is.
        ``model`` optionally overrides the model this spawn runs on.
        ``isolation="worktree"`` runs it in its own git worktree, committing its
        changes to a branch named in the report."""
        return await self._execute_spawn(
            type, task, mcp_names, max_output_chars, model, isolation,
            background=True, stream_id=stream_id,
        )
