"""Spawn and run isolated sub-agents on behalf of the harness.

A sub-agent is a fresh Pydantic AI ``Agent`` built on the harness's *current*
model (so it tracks runtime model switches), with its tool reach decided up
front by the approval mode, an optional MCP grant, and an optional soft output
budget. Foreground spawns stream their events to the UI and fold their spend
into the running turn; background spawns run detached and persist their spend
immediately. The harness wires ``run``/``run_background`` onto ``Deps`` so the
``spawn_agent`` tool reaches them the same way other tools reach shared state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pydantic_ai import Agent
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits

if TYPE_CHECKING:
    from pydantic_ai.agent import EventStreamHandler
    from pydantic_ai.models import Model
    from pydantic_ai.run import AgentRunResult

    from ..mcp.manager import McpManager
    from ..session.ctrl import SessionController
    from ..tools.provider import ToolProvider
    from ..workspace.agents import AgentDef
    from .cli_backend import CliResult

from ..compaction import estimate_tokens, last_request_input_tokens, mask_stale_observations
from ..hooks.dispatch import TurnHooks
from ..runtime.deps import Deps, SubAgent
from ..runtime.errors import (
    is_context_overflow_error,
    is_transient_model_error,
    overflow_is_contention,
)
from ..runtime.permissions import Mode
from ..tasks import TaskList
from ..tools.impl import fs
from ..workspace import (
    cap_subagent_output,
    discover_agents,
    effective_tools,
    find_agent,
    subagent_instructions,
)
from .backend import SpawnRun
from .isolation import SpawnWorktree
from .persistence import SpawnTranscripts, count_tool_calls
from .policies import MaskingPolicy, RetryPolicy

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


@contextlib.contextmanager
def _fresh_capture():
    """A message-capture context that is ALWAYS fresh — unlike pydantic-ai's
    public ``capture_run_messages``, which this deliberately bypasses.

    Why the private API: ``capture_run_messages`` REUSES an existing contextvar
    state instead of nesting, and an ``agent.run`` binds to the captured list
    only while the state's ``used`` flag is still False. A foreground spawn runs
    INSIDE the main turn's tool execution, where ``TurnController._run_agent_loop``
    already holds a capture context that the main run has bound (flag set).
    Entering the public context there yields the MAIN turn's message list — and
    the sub-agent's run, finding ``used=True``, records its messages into a list
    nobody holds. A retry in ``_run_to_completion`` would then "resume" the
    sub-agent with the orchestrator's conversation instead of its own. This
    helper reaches for pydantic-ai's private ``_messages_ctx_var`` and
    unconditionally installs a fresh ``_RunMessages`` holder, restoring the
    outer state in ``finally`` so the main turn's capture is untouched.

    The private-name coupling is a considered trade: pydantic-ai offers no
    nested/fresh mode for ``capture_run_messages`` (its docs promise only "the
    first run within the context"), and the alternative — not capturing at all —
    would forfeit sub-agent resumability. ``tests/test_subagent_retry.py`` pins
    both the private names and the outer-capture topology, so a dependency bump
    that changes either fails loudly there instead of silently corrupting
    sub-agent resumes with the main conversation.
    """
    from pydantic_ai import _agent_graph

    messages: list = []
    token = _agent_graph._messages_ctx_var.set(_agent_graph._RunMessages(messages))
    try:
        yield messages
    finally:
        _agent_graph._messages_ctx_var.reset(token)


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
    from ..runtime.harness import _drop_nameless_tool_calls, _repair_unanswered_tool_calls

    repaired = _repair_unanswered_tool_calls(_drop_nameless_tool_calls(messages))
    return repaired or None


@dataclass(frozen=True)
class _SpawnPrep:
    """Shared state returned by ``_prepare_spawn``: the built sub-agent and all
    context the foreground and background tails need to run it and finalize output."""
    sub: SubAgent
    granted: list[object]
    unknown: list[str]
    handler: EventStreamHandler[Deps] | None
    iso: SpawnWorktree | None
    t0: float
    t_built: float
    first_event_at: list[float]  # mutable; ``on_first_event`` probe appends during run
    depth: int  # depth of the spawned sub-agent
    meta: dict | None = None  # sidecar meta template (Task: subagent resume)


class SubagentRunner:
    """Builds and runs sub-agents for one harness. Reads the active model
    through ``get_model`` each spawn, so a runtime ``/model`` switch is picked
    up without rewiring."""

    def __init__(self, provider: ToolProvider, mcp: McpManager, deps: Deps,
                 hooks: TurnHooks, session: SessionController,
                 get_model: Callable[[], Model],
                 model_settings: ModelSettings | None = None,
                 build_model: Callable[[str], Model] | None = None,
                 concurrency: int | None = None,
                 transcript_cap: int = 2000,
                 max_depth: int = 3,
                 retry: RetryPolicy | None = None,
                 masking: MaskingPolicy | None = None,
                 extra_agents: tuple[AgentDef, ...] = ()) -> None:
        self.provider = provider
        self.mcp = mcp
        self.deps = deps
        self.hooks = hooks
        self.session = session
        self._get_model = get_model
        self._model_settings = model_settings
        # Programmatic sub-agent defs (HarnessBuilder.with_subagent), resolved
        # ahead of workspace/built-in discovery — see _resolve_agent.
        self._extra_agents = tuple(extra_agents)
        # Transient-error retry policy (attempts + backoff) and per-run request
        # cap. A permanent error (malformed request, auth) is never retried — see
        # is_transient_model_error.
        self._retry = retry or RetryPolicy()
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
        # Session-bound persistence for a spawn's sidecar transcript + terminal
        # meta. Reads the store off `session` per call, so it follows a /switch.
        self._transcripts = SpawnTranscripts(session, transcript_cap)
        # Hard depth ceiling. Spawns that would produce a sub-agent at
        # depth >= max_depth are refused. Default 3: main → sub → grandchild.
        self._max_depth = max_depth
        # Context masking for spawned sub-agents. A sub-agent does the read-heavy
        # fan-out work, so its history is dominated by tool observations; past a
        # token trigger those are masked per-request by an ObservationMasker (one
        # per spawn — see masking.py for why the state matters). The MaskingPolicy
        # owns the resolver + knobs and the per-spawn trigger resolution (see
        # _mask_trigger_for): a per-spawn model override resolves its own window
        # and budget rather than inheriting the session model's. The reactive
        # overflow backstop in _run_to_completion still covers any late trigger a
        # resolution miss leaves behind.
        self._masking = masking or MaskingPolicy()
        # Stream ids of spawns whose resume is in flight but not yet registered as
        # a job. resume_spawn awaits (limits resolve, subagent_start hook, MCP
        # grants) between its guards and jobs.register, so two rapid `r` presses
        # can both clear the jobs-scan guard and double-spawn. This synchronous
        # set, added-to before the first await, is the race guard for that window;
        # once the job is registered the jobs.list() running-scan takes over.
        self._resuming: set[str] = set()

    def _resolve_agent(self, type_: str) -> AgentDef | None:
        """Programmatic defs (HarnessBuilder.with_subagent) take precedence over
        discovered ones, then fall back to workspace/built-in discovery."""
        for d in self._extra_agents:
            if type_ in (d.name, d.qualified_name):
                return d
        return find_agent(self.deps.workspace.root, type_)

    def _open_worktree(self, stream_id: str) -> tuple[SpawnWorktree | None, str | None]:
        """Open an isolated worktree for a fresh spawn, naming its branch from the
        spawn's stream id (unique per tool call, so parallel spawns don't collide)
        with a monotonic-sequence fallback when there's no id. The worktree's own
        lifecycle (close/discard/teardown) lives on ``SpawnWorktree``; this method
        just owns the branch-naming counter."""
        self._iso_seq += 1
        return SpawnWorktree.open(
            self.deps.workspace.root, _iso_branch(stream_id, self._iso_seq)
        )

    def handler(
        self,
        stream_id: str | None,
        on_first_event: Callable[[], None] | None = None,
    ) -> EventStreamHandler[Deps] | None:
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
        cb = self.deps.ui.on_subagent_event
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

    async def _mask_trigger_for(self, model_id: str | None) -> int:
        """The masking trigger for a spawn, warmed here because spawn prep is
        async. Resolves the spawn's OWN model id (falling back to the session
        model when the spawn didn't override it) and hands it to the
        MaskingPolicy, which applies its resolver — or the fallback when none is
        wired, in which case the model id is never consulted."""
        if self._masking.limits is not None and model_id is None:
            model_id = getattr(self._get_model(), "model_name", None)
        return await self._masking.trigger_for(model_id)

    def _known_window(self) -> int | None:
        """The KNOWN served context window for the session model, or None. Feeds
        the overflow-contention classifier in ``_run_to_completion``; None (no
        limits wired, or nothing discovered) conservatively disables it. Uses
        the session model like ``_mask_trigger_for``'s fallback — a per-spawn
        model override isn't visible here, but a wrong-model window only skews
        a heuristic margin, never correctness."""
        if self._masking.limits is None:
            return None
        model_id = getattr(self._get_model(), "model_name", None)
        return self._masking.limits.window_for(model_id)

    def build(
        self, type: str, max_output_chars: int | None = None,
        model: str | None = None, workspace_root=None, *, defn=None,
        depth: int = 0, mask_trigger: int | None = None,
        checkpoint: Callable[[list], None] | None = None,
    ) -> tuple[SubAgent | None, str | None]:
        """Build an isolated sub-agent of ``type``, with its reach decided up
        front: gated tools only in auto mode, so a run never needs an approval
        round. Runs on the harness's current model unless ``model`` overrides it
        (e.g. a cheap model for read-only fan-out); an override needs a model
        source, else it's reported as unhonorable. ``max_output_chars``, when the
        spawner set one, is folded into the sub-agent's instructions as a soft
        output budget. ``workspace_root`` overrides the path described in the
        sub-agent's instructions (e.g. a worktree for an isolated spawn); agent
        *discovery* still reads the main workspace. ``defn`` is the already-resolved
        agent definition when the caller has one (``_execute_spawn`` resolves it once
        to pick the backend, then threads it through so discovery's filesystem walk
        isn't repeated); when None we resolve it here. ``mask_trigger`` is the
        masking trigger resolved by the caller; None falls back to the legacy
        default. Returns ``(agent, None)`` or, for an unknown type or an
        unresolvable model, ``(None, message)``."""
        if defn is None:
            defn = self._resolve_agent(type)
        if defn is None:
            names = ", ".join(
                a.qualified_name
                for a in (*self._extra_agents, *discover_agents(self.deps.workspace.root))
            )
            return None, f"No sub-agent type {type!r}. Available: {names}."
        instr_root = (
            workspace_root if workspace_root is not None else self.deps.workspace.root
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
        allow_gated = self.deps.workspace.mode is Mode.auto
        # Imported lazily for the same reason _resumable_history does: agent.py
        # imports this module, so a top-level import of the harness would cycle.
        from ..runtime.harness import _drop_nameless_tool_calls

        capabilities: list[ProcessHistory[Deps]] = []
        if checkpoint is not None:
            # Sidecar checkpoint: ProcessHistory runs before EVERY model request,
            # which is exactly the per-model-response boundary the resume design
            # wants — each checkpoint ends at a message boundary. The processor
            # must return the history unchanged; the write is a side effect.
            def _checkpoint_history(messages: list) -> list:
                checkpoint(messages)
                return messages

            capabilities.append(ProcessHistory(_checkpoint_history))
        # Same scrub the main agent runs (harness.py): a flaky sub-agent model
        # can emit a structurally-broken tool call live mid-run (nameless, or
        # args that aren't valid JSON). Without this, the broken part rides in
        # history and the provider 400s the next request ("missing a function
        # name" / "function.arguments must be valid JSON"), crashing the spawn.
        # It runs before EVERY request, so it catches a call buried mid-history
        # that the transient-retry repair (only on the resume path) never sees.
        capabilities.append(ProcessHistory(_drop_nameless_tool_calls))
        # One masker PER SPAWN (it holds the run's committed mask set, so sharing
        # would leak one run's masked tool_call_ids into another's requests); None
        # when masking is disabled.
        masker = self._masking.masker(mask_trigger)
        if masker is not None:
            capabilities.append(ProcessHistory(masker.mask))

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
            # Match the main agent's pin: v2 defaults to 'graceful'; 'early'
            # keeps the v1 stop-on-final-result behavior (see harness.py).
            end_strategy="early",
            model_settings=self._model_settings,
            capabilities=capabilities,
        )
        self.provider.register_subagent(sub, effective_tools(defn, allow_gated=allow_gated))
        # Nested spawning: only register spawn_agent if the child would be
        # able to spawn (depth+1 < max_depth). At the leaf depth, the tool
        # is absent — the grandchild simply cannot recurse. The ceiling itself
        # rides on the child's Deps (subagent_max_depth, stamped alongside
        # subagent_depth at spawn time), never on the tool signature: a
        # partial-bound keyword loses to a caller kwarg, so a model that
        # passed its own max_depth could override the binding.
        if depth + 1 < self._max_depth:
            from ..tools.spawn_tools import spawn_agent
            sub.tool(spawn_agent)
        return sub, None

    def _cap_output(self, output: str, max_output_chars: int | None, ref: str) -> str:
        """Apply a spawner-set output cap to a sub-agent's report. Over budget,
        the full report is spilled to a workspace file and the main agent gets a
        within-budget head + pointer; otherwise the report passes through. The
        cap is lossless — nothing is discarded, only relocated."""
        rel = f".marim/subagent-output/{ref}.md"
        text, spill = cap_subagent_output(output, max_output_chars, rel)
        if spill is not None:
            fs.write_file(self.deps.workspace.root, rel, spill)
        return text

    async def _finalize_spawn(
        self, run: SpawnRun, *, stream_id: str, name: str, stop_task: str,
        note: str, iso: SpawnWorktree | None, max_output_chars: int | None,
        persist_bg: bool, timing: tuple[float, float, list[float]] | None = None,
    ) -> str:
        """The one success tail every spawn shares, regardless of backend or mode.

        Fires the stop hook, persists the spawn's transcript (and any CLI-demuxed
        child transcripts) with its terminal meta, folds spend into the session,
        applies the lossless output cap, and closes the worktree — returning the
        backend ``note`` + capped report + worktree merge-note.

        ``persist_bg`` selects the two mode-axis behaviors a background spawn
        needs: an immediate ``persist(force=True)`` (no ``run_turn`` will fold its
        off-turn spend) and a ``bg-N`` spill ref (a background run has no stream id
        to key its output-spill file on). A foreground spawn passes ``False`` and
        spills under its ``stream_id``. ``timing`` is the native path's phase
        stats; the CLI path passes ``None`` (it keeps no time-to-first-token)."""
        if timing is not None:
            self._log_spawn_timing(name, *timing, failed=False)
        await self.hooks.subagent_stop(name, stop_task, run.output)
        self._transcripts.save(stream_id, run.transcript, meta=run.final_meta)
        # Claude-side sub-agents (CLI Agent/Task spawns) each persist under the
        # stream id their live card streamed on, so the screen can replay them
        # after a resume. Empty for native spawns — a no-op there.
        for child_id, msgs in run.child_transcripts.items():
            self._transcripts.save(child_id, msgs)
        self.session.usage += run.usage
        if persist_bg:
            # A background spawn finishes off-turn, so no run_turn folds its spend;
            # persist right away. force=True: the persist cache keys off
            # history_version, which a background completion never bumps, so an
            # unforced persist would be silently skipped (losing the spend and the
            # settled-jobs history entry).
            self.session.persist(force=True)
            self._bg_seq += 1
            spill_ref = f"bg-{self._bg_seq}"
        else:
            spill_ref = stream_id
        capped = self._cap_output(run.output, max_output_chars, spill_ref)
        iso_note = iso.close() if iso else ""
        return note + capped + iso_note

    def _contain_failure(self, name: str, exc: BaseException) -> str:
        """The error string a *foreground* spawn returns instead of propagating,
        so a sibling fan-out spawn isn't taken down by its neighbor's crash. A
        context overflow (the shed-and-resume backstop already ran and it still
        overflowed) gets an actionable message the orchestrator can act on rather
        than a bare class name."""
        if is_context_overflow_error(exc):
            return (
                f"Sub-agent {name!r} overflowed its context window even after "
                "masking stale tool output. Split the task into smaller "
                "spawns, or narrow the scope so this sub-agent reads less."
            )
        return f"Sub-agent {name!r} failed: {exc.__class__.__name__}: {exc}"

    async def _run_spawn_lifecycle(
        self, run_fn: Callable[[], Any], *, iso: SpawnWorktree | None,
        resumed: bool, background: bool, name: str, stop_task: str, note: str,
        max_output_chars: int | None, stream_id: str,
        timing: tuple[float, float, list[float]] | None = None,
    ) -> str:
        """The one run+failure+finalize lifecycle every spawn shares — native or
        CLI, foreground or background, fresh or resumed. ``run_fn`` is the
        backend-specific coroutine factory that runs the spawn under the
        concurrency slot and returns a ``SpawnRun``; everything around it is
        invariant:

        - **Regular failure** (``Exception``): tear the worktree down — throwaway
          for a fresh spawn, checkout-only for a resumed one (keeps its committed
          work). A background spawn then re-raises to the job registry; a
          foreground spawn is *contained* as an error string so a sibling fan-out
          spawn survives.
        - **Cancellation** (``BaseException`` — Ctrl-C / shutdown): ``close()`` the
          worktree, committing in-progress work and KEEPING the branch as the
          resumable deliverable (dropped only if nothing was produced). This makes
          a cancel no worse than a hard kill and keeps resume working — for the CLI
          path too, which historically ``discard()``ed here and broke its own
          resume offer.
        - **Success**: hand off to the shared ``_finalize_spawn`` tail.

        ``background`` selects both the failure disposition (re-raise vs contain)
        and, via ``persist_bg``, the finalize behaviors a detached spawn needs.
        ``timing`` is the native phase stats (``None`` for CLI, which keeps none)."""
        try:
            # Bound concurrent model runs (the part that hits the provider) so a
            # wide fan-out queues instead of slamming a rate-limited route at once.
            async with self._slot():
                run = await run_fn()
        except Exception as exc:  # noqa: BLE001
            if timing is not None:
                self._log_spawn_timing(name, *timing, failed=True)
            if iso:
                iso.teardown_after_failure(resumed=resumed)
            if background:
                # A background crash propagates to the job registry (marks it
                # failed) rather than being contained — no turn to protect.
                raise
            await self.hooks.subagent_stop(name, stop_task, f"error: {exc}")
            return self._contain_failure(name, exc)
        except BaseException:
            if iso:
                iso.close()
            raise
        return await self._finalize_spawn(
            run, stream_id=stream_id, name=name, stop_task=stop_task, note=note,
            iso=iso, max_output_chars=max_output_chars, persist_bg=background,
            timing=timing,
        )

    async def _retry_backoff(self, attempt: int) -> None:
        """Sleep before the ``attempt``-th retry via the RetryPolicy's backoff.
        Kept as a thin runner method (not an inline ``self._retry.backoff`` call)
        so a test can stub it to skip the real sleep — see test_subagent_retry."""
        await self._retry.backoff(attempt)

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

    async def _run_to_completion(self, sub: SubAgent, task: str, run_deps: Deps,
                                 granted: list[Any], handler: EventStreamHandler[Deps] | None,
                                 stream_id: str | None = None,
                                 history: list | None = None) -> AgentRunResult[str]:
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
        retry so the user sees the card recover rather than silently stall.

        A context-overflow rejection (a permanent 4xx the transient path would
        surface) gets one recovery attempt of its own: the captured conversation
        is resumed with stale tool observations masked (see ``_shed_context``);
        a repeat overflow, or one with nothing left to shed, surfaces normally.

        ``history``, when given, is a persisted transcript to resume from (an
        interrupted spawn continuing after a restart) — the first attempt sends
        both ``task`` (the continuation prompt) as the run's input AND
        ``message_history=history``, so pydantic-ai appends the prompt on top of
        the prior conversation. A later transient-retry resume within the same
        call takes over from ``resume_history`` instead, exactly as before."""
        attempt = 0
        overflow_shed = False
        resume_history: list | None = None
        # One usage accumulator across ALL attempts, mirroring the controller's
        # per-round banking (see _run_with_approval): pydantic-ai mutates it in
        # place as each model step completes, so an attempt that dies mid-run
        # still leaves its spend here. On success the returned ``result.usage``
        # IS this object (agent.run threads ``usage or RunUsage()`` straight
        # into run state), so the callers' ``session.usage += result.usage``
        # already covers the failed attempts; the re-raise path below banks it
        # explicitly since no result reaches the caller there.
        run_usage = RunUsage()
        while True:
            captured: list = []
            try:
                # NOT the public capture_run_messages: a foreground spawn runs
                # inside the main turn's capture context, which the public API
                # would silently reuse — see _fresh_capture's docstring.
                with _fresh_capture() as captured:
                    return await sub.run(
                        task if resume_history is None else None,
                        message_history=(resume_history if resume_history is not None
                                         else history),
                        deps=run_deps, toolsets=granted,
                        event_stream_handler=handler,
                        usage=run_usage,
                        usage_limits=UsageLimits(request_limit=self._retry.request_limit),
                    )
            except Exception as exc:  # noqa: BLE001
                # An overflow whose request is far below the KNOWN served window
                # is pool CONTENTION, not an oversized conversation: local
                # servers (LM Studio/llama.cpp unified KV cache) share one
                # window across n_parallel slots, and a parallel spawn fan-out
                # can exhaust the pool and fail every in-flight request at
                # once. Masking observations can't free pool space held by the
                # OTHER requests — so skip the shed and ride the transient
                # retry path below instead: back off, let siblings finish, and
                # resume the captured conversation intact. An empty capture (a
                # first-request failure) sizes to 0 ⇒ never contention, and an
                # unknown window keeps the old behavior everywhere.
                overflow = is_context_overflow_error(exc)
                contention = overflow and overflow_is_contention(
                    max(
                        last_request_input_tokens(list(captured)) or 0,
                        estimate_tokens(list(captured)),
                    ),
                    self._known_window(),
                )
                # Context overflow is a permanent 4xx, so the transient path below
                # would re-raise it — but unlike a genuine bad request it IS
                # recoverable: shed the bulky old observations from the captured
                # conversation and resume once. Unlike the proactive masker (which
                # rewrites only the outgoing request), the shed is folded into the
                # resume history itself, so the freed tokens stay freed. One shot
                # only: a second overflow means masking already gave all it had.
                if not overflow_shed and overflow and not contention:
                    shed = self._shed_context(list(captured))
                    if shed is not None:
                        overflow_shed = True
                        resume_history = shed
                        logger.info(
                            "sub-agent overflowed its context; masked stale "
                            "observations and resuming"
                        )
                        await self._notice_overflow(stream_id)
                        continue
                if attempt >= self._retry.attempts or not (
                    contention or is_transient_model_error(exc)
                ):
                    # Surfacing the failure loses the result object but must not
                    # lose the spend: the provider billed the failed attempts'
                    # tokens regardless, so bank the accumulator before the
                    # re-raise. (The success path needs no counterpart — the
                    # callers fold result.usage, which IS this accumulator.)
                    self.session.usage += run_usage
                    raise
                attempt += 1
                resume_history = _resumable_history(list(captured))
                logger.info(
                    "sub-agent hit a transient error (%s); resuming, retry %d/%d "
                    "after backoff", exc.__class__.__name__, attempt,
                    self._retry.attempts,
                )
                await self._notice_retry(stream_id, exc, attempt)
                await self._retry_backoff(attempt)

    async def _notice_retry(self, stream_id: str | None, exc: Exception,
                            attempt: int) -> None:
        """Surface a transient-error retry on a foreground spawn's card. A no-op for
        a background spawn (no card) or when no UI is listening."""
        cb = self.deps.ui.on_subagent_notice
        if cb is None or not stream_id:
            return
        await cb(
            stream_id,
            f"transient error ({exc.__class__.__name__}) — "
            f"retrying {attempt}/{self._retry.attempts}…",
        )

    # Shed settings for the overflow backstop: spare only the newest observation
    # (the model may still be acting on it) and mask anything else remotely bulky.
    # Deliberately more aggressive than the proactive masker — by the time we're
    # here the provider has already rejected the request for size.
    _SHED_KEEP_RECENT = 1
    _SHED_MIN_CHARS = 64

    def _shed_context(self, messages: list) -> list | None:
        """The overflow-recovery lever: repair the captured conversation the same
        way a transient resume does, then aggressively mask stale observations.
        Returns the shrunk history to resume from, or None when masking freed
        nothing — the overflow is then unrecoverable here and must surface."""
        repaired = _resumable_history(messages)
        if not repaired:
            return None
        # Known imprecision, accepted: mask_stale_observations counts "recent"
        # newest-first across parts, so a parallel tool round wider than
        # keep_recent(=1) can mask sibling returns the model hasn't acted on
        # yet — and after the repair above, the spared "newest" return can be a
        # repair-synthesized stub rather than real output. Acceptable here: the
        # placeholder text invites the model to re-run the tool, and by this
        # point the provider has already rejected the request outright, so a
        # lossy-but-live resume beats a dead spawn.
        masked, count = mask_stale_observations(
            repaired, self._SHED_KEEP_RECENT, min_chars=self._SHED_MIN_CHARS
        )
        return masked if count else None

    async def _notice_overflow(self, stream_id: str | None) -> None:
        """Surface an overflow recovery on a foreground spawn's card. A no-op for
        a background spawn (no card) or when no UI is listening."""
        cb = self.deps.ui.on_subagent_notice
        if cb is None or not stream_id:
            return
        await cb(stream_id, "context overflow — masked stale tool output, resuming…")

    async def _execute_spawn(
        self, type: str, task: str, mcp_names: list[str] | None,
        max_output_chars: int | None, model: str | None, isolation: str | None,
        *, background: bool, stream_id: str, caller_depth: int = 0,
    ) -> str:
        """Dispatch a spawn through shared setup then the shared run lifecycle.

        Handles worktree open and the CLI early-return inline (both need ``iso``
        before the branch), then delegates everything else to ``_prepare_spawn``
        and ``_execute_native_spawn`` (which drives ``_run_spawn_lifecycle`` for
        both the foreground and background cases).

        ``caller_depth`` is the depth of the agent that called spawn_agent; the
        child runs at ``caller_depth + 1``. It comes from the caller's deps, NOT
        ``self.deps`` — the runner belongs to the main harness and its deps are
        pinned at depth 0, so reading depth off it would mis-size every spawn made
        from a nested sub-agent.
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
        work_root = iso.path if iso else None
        # CLI-backed agents run an external `claude` process instead of the
        # in-process Pydantic AI loop, so they skip the native build+MCP prepare.
        # Branch here to _execute_cli_spawn, which builds its own meta/checkpoint
        # and then rejoins the SAME _run_spawn_lifecycle the native tails use — the
        # run+failure+finalize wrapper is written once, not duplicated per backend.
        # Resolve the agent definition ONCE here (a filesystem discovery walk) and
        # thread it through to _prepare_spawn/build so a native spawn doesn't pay the
        # walk a second time — it matters on a fan-out (2N walks → N).
        defn = self._resolve_agent(type)
        depth = caller_depth + 1
        if defn is not None and defn.backend == "claude-cli":
            return await self._execute_cli_spawn(
                defn, task, work_root, iso, mcp_names, max_output_chars,
                model, stream_id, background=background, depth=depth,
            )
        prep = await self._prepare_spawn(
            type, task, mcp_names, max_output_chars, model,
            iso, work_root, stream_id, debug=debug, t0=t0, defn=defn, depth=depth,
        )
        if isinstance(prep, str):
            return prep
        return await self._execute_native_spawn(
            type, task, stream_id, max_output_chars, prep, background=background,
        )

    async def _prepare_spawn(
        self, type: str, task: str, mcp_names: list[str] | None,
        max_output_chars: int | None, model: str | None,
        iso: SpawnWorktree | None, work_root, stream_id: str,
        *, debug: bool, t0: float, defn=None, depth: int = 0,
        resumed: bool = False,
    ) -> _SpawnPrep | str:
        """Build the sub-agent, grant MCP servers, fire the start hook, and wire the
        event handler. Returns a ``_SpawnPrep`` struct on success, or an error string
        the caller can return directly. Called after worktree open and CLI early-return.
        ``defn`` is the definition the caller already resolved, threaded into ``build``
        so discovery isn't walked twice per spawn.

        ``resumed`` selects the isolated-worktree teardown a build failure follows:
        a fresh spawn discards branch and checkout, a resumed spawn keeps the branch
        (it holds the interrupted run's committed work). This method owns that
        teardown so the branch distinction can't be lost — see the build-failure arm."""
        mask_trigger = await self._mask_trigger_for(model)
        meta: dict | None = None
        checkpoint = None
        if stream_id:
            # The sidecar meta template: everything a resumed session needs to
            # rebuild the card (type/task/model) and re-run the spawn
            # (mcp/depth/isolation/max_output_chars). Status stays "running"
            # for every mid-run checkpoint; the final save stamps the terminal
            # status. parent_id is deliberately absent — the runner doesn't know
            # its caller's stream, so a synthesized interrupted card renders
            # top-level.
            meta = {
                "stream_id": stream_id, "type": type, "task": task,
                "model": model, "mcp": mcp_names, "depth": depth,
                "max_output_chars": max_output_chars,
                "isolation": iso.branch if iso else None,
                "status": "running",
            }

            def _checkpoint(messages: list, _meta=meta) -> None:
                # cap_reasoning=True bounds the mid-run payload: this fires before
                # every model request as the conversation grows, so oversized
                # text/thinking parts are clipped here (the final write leaves them
                # in full). See cap_transcript.
                self._transcripts.save(stream_id, messages, meta=_meta,
                                       cap_reasoning=True)

            checkpoint = _checkpoint
        sub, err = self.build(type, max_output_chars, model, work_root, defn=defn,
                              depth=depth, mask_trigger=mask_trigger,
                              checkpoint=checkpoint)
        if sub is None:
            if iso:
                # Own the failure teardown HERE rather than at the caller: a resumed
                # spawn must keep its branch (prior work), a fresh spawn discards
                # both. If the caller tried to keep the branch after us, it couldn't —
                # a plain discard() here would already have deleted it.
                iso.teardown_after_failure(resumed=resumed)
            return err or f"Failed to build sub-agent {type!r}."
        t_built = time.perf_counter()
        # Apply the same tool-search deferral the main agent uses: a large granted
        # MCP surface is combined behind ToolSearch rather than injected wholesale,
        # so a spawn granted (say) an 86-tool browser server searches for tools on
        # demand instead of carrying every schema. Policy/threshold are the same
        # workspace settings the controller reads for the main turn.
        granted, unknown = await self.mcp.granted_toolsets(
            mcp_names,
            self.deps.workspace.tool_search,
            self.deps.workspace.tool_search_threshold,
        )
        await self.hooks.subagent_start(type, task)
        # Foreground passes its tool_call_id; a background spawn now passes its own
        # stream_id too (Phase 2), so it streams to the UI exactly like foreground.
        # An empty stream_id (headless / no id) forwards nothing — handler() gates
        # on truthiness.
        first_event_at: list[float] = []
        probe = (lambda: first_event_at.append(time.perf_counter())) if debug else None
        handler = self.handler(stream_id, on_first_event=probe)
        return _SpawnPrep(
            sub=sub, granted=granted, unknown=unknown, handler=handler,
            iso=iso, t0=t0, t_built=t_built, first_event_at=first_event_at,
            depth=depth, meta=meta,
        )

    async def _execute_native_spawn(
        self, type: str, task: str, stream_id: str,
        max_output_chars: int | None, prep: _SpawnPrep,
        *, background: bool, history: list | None = None,
    ) -> str:
        """Run a native (in-process) spawn to completion through the shared
        ``_run_spawn_lifecycle``. Foreground and background differ only in three
        knobs, all threaded into the lifecycle here:

        - **Deps** — a background spawn gets its own empty ``TaskList`` so its
          multi-step work never mutates (or persists as) the user's session
          checklist; a foreground spawn shares the session deps. Both redirect
          file ops into the worktree when isolated.
        - **Failure disposition & persist** — carried by ``background`` (contain
          vs re-raise; immediate force-persist for the off-turn spend).
        - **Resume** — ``history`` (a persisted transcript to continue from) is
          background-only; it also keeps a resumed spawn's branch on failure."""
        run_deps = replace(self.deps, tasks=TaskList()) if background else self.deps
        if prep.iso:
            run_deps = replace(
                run_deps, workspace=replace(run_deps.workspace, root=prep.iso.path)
            )
        if prep.depth > 0:
            # Stamp the runner's ceiling alongside the depth: spawn_agent reads both
            # from Deps (see subagent_max_depth in runtime/deps.py for why it is not
            # a tool parameter).
            run_deps = replace(
                run_deps, subagent_depth=prep.depth,
                subagent_max_depth=self._max_depth,
            )
        # A resumed spawn (history set) keeps its branch on failure — it holds prior
        # committed work. The stop hook must see the SAME task the start hook got;
        # on a resumed spawn the local ``task`` is the internal continuation prompt,
        # so read the original off prep.meta (for a fresh spawn prep.meta["task"] ==
        # task, so this is a no-op).
        resumed = history is not None
        stop_task = prep.meta["task"] if prep.meta else task

        async def _run() -> SpawnRun:
            result = await self._run_to_completion(
                prep.sub, task, run_deps, prep.granted, prep.handler, stream_id,
                history=history,
            )
            return SpawnRun(
                output=result.output,
                transcript=result.all_messages(),
                usage=result.usage,
                final_meta=self._transcripts.final_meta(
                    prep.meta, "finished", result.usage, prep.t0, result.all_messages()),
            )

        # (Background persist has a known asymmetry: a /switch mid-flight points
        # self.session at a DIFFERENT session, so the spend + settled-jobs entry
        # land in the current session's payload; jobs are process-scoped, so the
        # summary follows the active session — accepted.)
        return await self._run_spawn_lifecycle(
            _run, iso=prep.iso, resumed=resumed, background=background, name=type,
            stop_task=stop_task, note=self.mcp.grant_note(prep.unknown),
            max_output_chars=max_output_chars, stream_id=stream_id,
            timing=(prep.t0, prep.t_built, prep.first_event_at),
        )

    async def _execute_cli_spawn(
        self, defn, task: str, work_root, iso,
        mcp_names: list[str] | None, max_output_chars: int | None,
        model: str | None, stream_id: str, *, background: bool,
        resume_session_id: str | None = None, original_task: str | None = None,
        depth: int = 1, transcript_prefix: list | None = None,
    ) -> str:
        """Run a ``backend: claude-cli`` agent inside the same lifecycle the native
        path uses: hooks bracketing, output cap/spill, worktree close, background
        persist. Harness MCP grants are NOT forwarded to the CLI (it uses its own
        MCP config); a non-empty ``mcp_names`` is noted, not honored.

        Mirrors _execute_spawn's foreground/background contract: foreground
        contains a failure as an error string (so a sibling fan-out spawn isn't
        taken down); background re-raises to the job registry. Usage is folded into
        the session, and a background spawn persists immediately since no run_turn
        will fold its spend."""
        hook_task = original_task or task
        # Wall-clock start for the terminal meta's duration stat. The native path
        # reads prep.t0 (stamped in _execute_spawn); the CLI early-return branches
        # before prep exists, so stamp its own here. A resumed leg times only
        # itself — same rule as native.
        t0 = time.perf_counter()
        meta: dict | None = None
        checkpoint: Callable[[list, str | None], None] | None = None
        if stream_id:
            # Same template the native path builds in _prepare_spawn, plus the two
            # CLI-only keys: `backend` routes resume_spawn to the CLI branch, and
            # `cli_session_id` (filled by the first checkpoint once the init event
            # arrives) is the `claude -p --resume` key. Mutating the shared
            # template between checkpoints is safe — TranscriptStore.write
            # snapshots the dict before stamping.
            meta = {
                "stream_id": stream_id, "type": defn.name, "task": hook_task,
                "model": model, "mcp": None, "depth": depth,
                "max_output_chars": max_output_chars,
                "isolation": iso.branch if iso else None,
                "status": "running",
                "backend": "claude-cli",
                "cli_session_id": resume_session_id,
            }

            def _checkpoint(messages: list, session_id: str | None,
                           _meta=meta) -> None:
                if session_id:
                    _meta["cli_session_id"] = session_id
                # The resumed process's stream carries only the CONTINUATION —
                # `claude -p --resume` does not re-emit the prior history. Without
                # the prefix, this checkpoint would overwrite the sidecar with
                # tail-only content, destroying the interrupted segment (incl. the
                # demuxed-children entries) the pane replays (spec §4). On a fresh
                # spawn transcript_prefix is None, so this is a plain passthrough.
                # cap_transcript (inside TranscriptStore.write) bounds the combined
                # payload.
                self._transcripts.save(stream_id, (transcript_prefix or []) + messages,
                                       meta=_meta, cap_reasoning=True)

            checkpoint = _checkpoint

        await self.hooks.subagent_start(defn.name, hook_task)
        # A resumed CLI spawn (resume_session_id set) keeps its branch on failure —
        # native-resume parity; a fresh spawn's branch is throwaway.
        resumed = resume_session_id is not None
        async def _run() -> SpawnRun:
            result = await self._run_cli(
                defn, task, work_root, model, stream_id,
                checkpoint=checkpoint, resume_session_id=resume_session_id,
            )
            # Same prefix rule as the checkpoint above: the final write is also
            # tail-only for a resumed run, so prepend the pre-interrupt segment.
            full_transcript = (transcript_prefix or []) + result.transcript
            final_meta = None
            if meta is not None:
                final_meta = {
                    **meta,
                    "status": "finished",
                    "cli_session_id": result.session_id or meta["cli_session_id"],
                    "usage": {"input": result.usage.input_tokens,
                              "output": result.usage.output_tokens},
                    "tool_count": count_tool_calls(full_transcript),
                    "duration": time.perf_counter() - t0,
                }
            # child_transcripts carries the demuxed Claude-side Agent/Task
            # sub-agents for _finalize_spawn to persist under their own stream ids.
            return SpawnRun(
                output=result.output,
                transcript=full_transcript,
                usage=result.usage,
                final_meta=final_meta,
                child_transcripts=result.child_transcripts,
            )

        # The CLI path now rides the SAME lifecycle as native — the deliberate
        # duplication (and its `if background` fork) is gone. timing=None (a CLI
        # spawn keeps no time-to-first-token); note is the not-forwarded-MCP note.
        # A cancelled CLI spawn now close()s its worktree like native (committing
        # in-progress work, keeping the branch) instead of discard()ing it — fixing
        # the resume-after-cancel divergence.
        return await self._run_spawn_lifecycle(
            _run, iso=iso, resumed=resumed, background=background, name=defn.name,
            stop_task=hook_task, note=self._cli_mcp_note(mcp_names),
            max_output_chars=max_output_chars, stream_id=stream_id, timing=None,
        )

    @staticmethod
    def _cli_mcp_note(mcp_names: list[str] | None) -> str:
        """A one-line note when the orchestrator named MCP servers for a CLI spawn:
        they aren't forwarded (the CLI uses its own MCP config), so say so rather
        than silently dropping them."""
        if not mcp_names:
            return ""
        names = ", ".join(mcp_names)
        return (
            f"[note: MCP servers ({names}) are not forwarded to claude-cli "
            "sub-agents; configure them in the CLI's own settings]\n\n"
        )

    async def _run_cli(self, defn, task: str, work_root, model: str | None,
                       stream_id: str, checkpoint=None,
                       resume_session_id: str | None = None) -> CliResult:
        """Resolve binary, tool reach, model, and cwd for a CLI spawn, then run it.
        Raises CliUnavailable when no `claude` binary is found so the caller's
        contained-error path reports it. Reach mirrors the native gate — gated
        tools only in auto mode. Model precedence: per-spawn override, then the
        agent's frontmatter model, then $MARIM_CLAUDE_CLI_MODEL, then the CLI's
        own default."""
        from .cli_backend import (
            CLI_MODEL_ENV,
            ClaudeCliRunner,
            CliUnavailable,
            resolve_cli_binary,
        )

        binary = resolve_cli_binary()
        if binary is None:
            raise CliUnavailable(
                "no `claude` binary found (set MARIM_CLAUDE_CLI_BIN or install "
                "Claude Code)"
            )
        allow_gated = self.deps.workspace.mode is Mode.auto
        tools = effective_tools(defn, allow_gated=allow_gated)
        cwd = str(work_root or self.deps.workspace.root)
        model_name = model or defn.model or os.environ.get(CLI_MODEL_ENV)
        cbs = self.deps.ui
        runner = ClaudeCliRunner(
            cbs.on_subagent_event, cbs.on_subagent_notice, cbs.on_subagent_model
        )
        result = await runner.run(
            binary=binary, prompt=task, system_prompt=defn.prompt, cwd=cwd,
            allow_gated=allow_gated, allowed_tools=tools, model=model_name,
            stream_id=stream_id, checkpoint=checkpoint,
            resume_session_id=resume_session_id,
        )
        if stream_id and cbs.on_subagent_usage is not None:
            await cbs.on_subagent_usage(stream_id, result.usage)
        return result

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
        caller_depth: int = 0,
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
        branch named in the report and the worktree is torn down. ``caller_depth``
        is the nesting depth of the agent that issued the spawn (0 for the main
        agent); the spawned sub-agent runs at ``caller_depth + 1``."""
        return await self._execute_spawn(
            type, task, mcp_names, max_output_chars, model, isolation,
            background=False, stream_id=stream_id, caller_depth=caller_depth,
        )

    async def run_background(
        self, type: str, task: str, mcp_names: list[str] | None = None,
        max_output_chars: int | None = None, model: str | None = None,
        isolation: str | None = None, stream_id: str = "",
        caller_depth: int = 0,
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
        changes to a branch named in the report. ``caller_depth`` is the nesting
        depth of the agent that issued the spawn (0 for the main agent); the
        detached sub-agent runs at ``caller_depth + 1``, so a background spawn from
        a nested sub-agent is sized — and depth-limited — the same as a foreground
        one."""
        return await self._execute_spawn(
            type, task, mcp_names, max_output_chars, model, isolation,
            background=True, stream_id=stream_id, caller_depth=caller_depth,
        )

    _CONTINUATION_PROMPT = (
        "You were interrupted before finishing. The conversation above is your "
        "own earlier progress on this task — continue from where it leaves off "
        "and finish the task, then report as usual."
    )

    async def resume_spawn(self, stream_id: str) -> tuple[str | None, str]:
        """Continue an interrupted spawn from its persisted sidecar as a
        background job. Returns ``(job_id, message)`` on success or
        ``(None, reason)`` on refusal — the reason is always user-renderable.

        Always background, even for an originally-foreground spawn: its owning
        turn is gone after a restart (the main history's dangling spawn_agent
        call was repaired with a synthetic return), so the finished-job digest
        is the only report consumer that still exists — and it already works."""
        # Synchronous in-flight guard, set BEFORE the first await below. Two rapid
        # `r` presses both clear the jobs.list() running-scan (neither has
        # registered a job yet) and then both await _prepare_spawn, double-spawning.
        # This set closes that window: the second concurrent call sees the id
        # already present and refuses. The finally releases it on EVERY exit — once
        # the job is registered the jobs.list() running-scan is the durable guard,
        # and releasing on a refusal/exception keeps a later retry from being
        # permanently locked out.
        if stream_id in self._resuming:
            return None, "Already resuming this spawn — hold on."
        self._resuming.add(stream_id)
        try:
            if not self._transcripts.has_store:
                return None, "No session store — can't resume."
            meta = self._transcripts.read_meta(stream_id)
            if meta is None:
                return None, ("No resumable transcript for this spawn (missing or "
                              "pre-envelope sidecar).")
            status = meta.get("status")
            if status not in ("running", "interrupted"):
                return None, f"Spawn already {status} — nothing to resume."
            for job in self.deps.jobs.list():
                if job.stream_id == stream_id and job.status == "running":
                    return None, f"Already resuming as {job.id}."
            # A claude-cli spawn resumes through the CLI's own session machinery,
            # not the native transcript-repair path: the CLI owns its history and
            # marim's sidecar is a display copy, so reading/repairing it here
            # would be wasted work at best and engine-swapping at worst.
            if meta.get("backend") == "claude-cli":
                return await self._resume_cli_spawn(stream_id, meta)
            messages = self._transcripts.read(stream_id)
            history = _resumable_history(messages or [])
            if history is None:
                return None, "Transcript unreadable or empty — can't resume."
            type_ = str(meta.get("type") or "")
            task = str(meta.get("task") or "")
            iso = None
            branch = meta.get("isolation")
            if branch:
                iso, err = SpawnWorktree.reopen(self.deps.workspace.root, branch)
                if err is not None:
                    return None, err
            prep = await self._prepare_spawn(
                type_, task, meta.get("mcp"), meta.get("max_output_chars"),
                meta.get("model"), iso, iso.path if iso else None, stream_id,
                debug=logger.isEnabledFor(logging.DEBUG), t0=time.perf_counter(),
                depth=int(meta.get("depth") or 1), resumed=True,
            )
            if isinstance(prep, str):
                # _prepare_spawn already tore down the checkout on build failure and,
                # because resumed=True, kept the branch (the interrupted run's work).
                # No teardown here — doing it before was a no-op that ran only after
                # discard() had already deleted the branch it meant to preserve.
                return None, prep
            label = f"{type_}: resumed — {task}"
            job_id = self.deps.jobs.register(
                "agent", label,
                self._execute_native_spawn(
                    type_, self._CONTINUATION_PROMPT, stream_id,
                    meta.get("max_output_chars"), prep,
                    background=True, history=history,
                ),
                stream_id=stream_id,
            )
            return job_id, f"Resumed as {job_id}."
        finally:
            self._resuming.discard(stream_id)

    async def _resume_cli_spawn(self, stream_id: str,
                                meta: dict) -> tuple[str | None, str]:
        """Resume an interrupted claude-cli spawn by relaunching the CLI with
        ``--resume`` on its recorded session id, as a background job. The caller
        (resume_spawn) already holds the ``_resuming`` guard and has verified the
        sidecar status and the absence of a live job. There is deliberately no
        pre-flight check that the CLI session file still exists — its on-disk
        scheme is CLI-internal, so a stale session surfaces as the CLI's own
        error on the failed job instead of a brittle path probe here."""
        session_id = meta.get("cli_session_id")
        if not session_id:
            return None, ("The CLI session id was never recorded (the spawn died "
                          "before its session started) — nothing to resume; "
                          "spawn it again instead.")
        type_ = str(meta.get("type") or "")
        task = str(meta.get("task") or "")
        defn = self._resolve_agent(type_)
        if defn is None:
            return None, f"No sub-agent type {type_!r} anymore — can't resume."
        if defn.backend != "claude-cli":
            return None, (f"Sub-agent type {type_!r} is no longer claude-cli "
                          "backed — can't resume its CLI session.")
        iso = None
        branch = meta.get("isolation")
        if branch:
            iso, err = SpawnWorktree.reopen(self.deps.workspace.root, branch)
            if err is not None:
                return None, err
        # Read the previously persisted transcript before relaunching. The resumed
        # CLI process's stream carries only the continuation (`claude -p --resume`
        # does not re-emit prior history), so the resume's checkpoints and final
        # write must PREPEND this prefix or they'd overwrite the sidecar with
        # tail-only content, destroying the pre-interrupt segment (incl. the
        # demuxed children) the pane replays (spec §4). Best-effort: an unreadable
        # transcript yields [], so the resume proceeds tail-only rather than
        # refusing — resumability trumps a perfect replay.
        prior = self._transcripts.read(stream_id) or []
        label = f"{type_}: resumed — {task}"
        job_id = self.deps.jobs.register(
            "agent", label,
            self._execute_cli_spawn(
                defn, self._CONTINUATION_PROMPT,
                iso.path if iso else None, iso,
                None, meta.get("max_output_chars"), meta.get("model"), stream_id,
                background=True, resume_session_id=session_id,
                original_task=task, depth=int(meta.get("depth") or 1),
                transcript_prefix=prior,
            ),
            stream_id=stream_id,
        )
        return job_id, f"Resumed as {job_id}."
