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
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pydantic_ai import Agent, StructuredDict
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.settings import ModelSettings

if TYPE_CHECKING:
    from pydantic_ai.agent import EventStreamHandler
    from pydantic_ai.models import Model

    from ..mcp.manager import McpManager
    from ..session.ctrl import SessionController
    from ..tools.provider import ToolProvider
    from ..workspace.agents import AgentDef

from ..config.model import SubagentTiers
from ..hooks.dispatch import TurnHooks
from ..runtime.deps import Deps, SubAgent
from ..runtime.errors import is_context_overflow_error
from ..runtime.permissions import Mode
from ..tasks import TaskList
from ..thinking import resolve_thinking, settings_for
from ..tools.names import GATED_TOOLS
from ..workspace import (
    cap_subagent_output,
    discover_agents,
    effective_tools,
    find_agent,
    spill_target,
    subagent_instructions,
    write_spill,
)
from .backend import CONTINUATION_PROMPT, SpawnRun
from .cli_spawn import CliSpawnOrchestrator
from .isolation import SpawnWorktree
from .output_schema import resolve_output_schema
from .persistence import SpawnTranscripts
from .policies import MaskingPolicy, RetryPolicy
from .run_driver import SpawnRunDriver, _resumable_history
from .tiers import resolve_tier

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


def _resolve_spawn_model_id(
    override_tier: str | None,
    slug: str | None,
    spec_tier: str | None,
    read_only: bool,
    tiers: SubagentTiers,
) -> str | None:
    """The model id a spawn should run on, or None to inherit the main model.

    A raw ``slug`` override is the escape hatch: honored as-is when no tier is
    configured (legacy behavior, preserved), but bounded to the tier allowlist
    once tiers exist — an out-of-allowlist slug is dropped in favor of the
    tier-resolved model (the caller logs the drop). With no slug, the tier is
    resolved from ``override_tier`` → ``spec_tier`` → tool reach, and mapped to
    its configured model (None ⇒ inherit main). Pure; unit-tested directly."""
    allow = tiers.allowlist()
    if slug and (not allow or slug in allow):
        return slug
    # else: out-of-allowlist slug falls through to the tier default.
    name = resolve_tier(override_tier, spec_tier, read_only)
    return tiers.model_for(name)


class _SpawnPreflightError(RuntimeError):
    """A background spawn failed before its run lifecycle even started (worktree
    open or sub-agent build). A foreground spawn returns such an error as a
    string so a sibling fan-out survives, but a *background* spawn has no caller
    to read that string — the JobRegistry does, and a plain return settles the
    job ``done`` with the error text masquerading as its result. Raising this
    instead settles the job ``failed``, symmetric with a mid-run background
    crash (which already re-raises)."""


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
    mcp_withheld: bool = False  # mode withheld part/all of a requested MCP grant
    # The ask-mode subset: requested servers withheld because their calls would
    # prompt per-call. Empty on a plan withhold (mcp_withheld True + empty here
    # ⇒ plan took the whole grant) — the pair disambiguates the spawner note
    # without re-reading the live mode, which may have flipped since spawn.
    mcp_ask_withheld: tuple[str, ...] = ()


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
                 tiers: SubagentTiers | None = None,
                 thinking_default: Callable[[], str | None] | None = None,
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
        # The claude-cli spawn path: external-process execute/resume live there;
        # it rejoins this runner's _run_spawn_lifecycle (passed bound) so the
        # run+failure+finalize invariants stay written once.
        self._cli = CliSpawnOrchestrator(
            deps=deps, hooks=hooks, transcripts=self._transcripts,
            lifecycle=self._run_spawn_lifecycle, resolve_agent=self._resolve_agent,
        )
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
        # overflow backstop in SpawnRunDriver.run_to_completion still covers any
        # late trigger a resolution miss leaves behind.
        self._masking = masking or MaskingPolicy()
        # The user-curated model per tier. Empty (the default) ⇒ every spawn
        # inherits the main model and a slug override keeps its legacy passthrough.
        self._tiers = tiers or SubagentTiers()
        # The inherited session thinking level, read LAZILY per spawn (closes
        # over the live Harness.thinking_level_id) so a /think switch reaches
        # later spawns. None ⇒ nothing to inherit (spec/override still apply).
        self._thinking_default = thinking_default
        # The model-loop driver: retry/overflow/contention recovery lives there,
        # keeping this class the spawn-lifecycle coordinator. known_window is
        # passed as a callable because it reads the *current* session model.
        self._driver = SpawnRunDriver(deps, session, self._retry,
                                      self._known_window)
        # Stream ids of spawns whose resume is in flight but not yet registered as
        # a job. resume_spawn awaits (limits resolve, subagent_start hook, MCP
        # grants) between its guards and jobs.register, so two rapid `r` presses
        # can both clear the jobs-scan guard and double-spawn. This synchronous
        # set, added-to before the first await, is the race guard for that window;
        # once the job is registered the jobs.list() running-scan takes over.
        self._resuming: set[str] = set()

    def set_tiering_enabled(self, enabled: bool) -> None:
        """Flip the tier set's master switch in place. Off ⇒ every subsequent
        spawn resolves to the main model (``model_for``/``allowlist`` short-circuit
        to None/empty) while the curated per-tier slugs are preserved, so turning
        it back on restores routing without re-entry. Read per spawn, so only
        spawns started after this call see the change."""
        self._tiers = replace(self._tiers, enabled=enabled)

    def _resolve_agent(self, type_: str) -> AgentDef | None:
        """Programmatic defs (HarnessBuilder.with_subagent) take precedence over
        discovered ones, then fall back to workspace/built-in discovery."""
        for d in self._extra_agents:
            if type_ in (d.name, d.qualified_name):
                return d
        return find_agent(self.deps.workspace.root, type_, trust_project=self.deps.trust.project)

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
        the overflow-contention classifier in ``SpawnRunDriver.run_to_completion``;
        None (no limits wired, or nothing discovered) conservatively disables it. Uses
        the session model like ``_mask_trigger_for``'s fallback — a per-spawn
        model override isn't visible here, but a wrong-model window only skews
        a heuristic margin, never correctness."""
        if self._masking.limits is None:
            return None
        model_id = getattr(self._get_model(), "model_name", None)
        return self._masking.limits.window_for(model_id)

    def _spawn_thinking_settings(self, override: str | None, defn) -> ModelSettings | None:
        """The per-spawn model settings: the runner's base settings with the
        resolved thinking level folded in. Precedence: spawn override → spec
        ``thinking:`` → inherited session level. off/None returns the base
        UNCHANGED (settings_for omits the key), so a spawn with no thinking
        anywhere is byte-identical to today's behavior."""
        inherited = self._thinking_default() if self._thinking_default is not None else None
        level = resolve_thinking(override, defn.thinking, inherited)
        if not level or level == "off":
            return self._model_settings
        # base may be None for an embedder that composed no default settings;
        # settings_for needs a concrete mapping to copy.
        return settings_for(level, self._model_settings or ModelSettings())

    async def _report_spawn_thinking(self, stream_id: str, override: str | None,
                                     spawn_defn) -> None:
        """Report the spawn's resolved thinking level to the UI, the same seam
        the model report uses — so the card can annotate the reasoning effort it
        actually ran with. Fires ONLY for a real level (off/none is the default;
        no annotation). Extracted so the resolve+fire is unit-testable without
        the full spawn machinery."""
        inherited = (
            self._thinking_default() if self._thinking_default is not None else None
        )
        level = resolve_thinking(
            override, spawn_defn.thinking if spawn_defn is not None else None, inherited
        )
        if not level or level == "off":
            return
        report = self.deps.ui.on_subagent_thinking
        if report is not None and stream_id:
            await report(stream_id, level)

    def build(
        self, type: str, max_output_chars: int | None = None,
        model: str | None = None, workspace_root=None, *, defn=None,
        depth: int = 0, mask_trigger: int | None = None,
        checkpoint: Callable[[list], None] | None = None,
        output_schema: dict | None = None, tier: str | None = None,
        thinking: str | None = None,
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
        default. ``output_schema``, when set (already resolved by the caller to an
        object-rooted schema), makes the sub-agent's output structured: its
        ``output_type`` becomes ``StructuredDict(output_schema)``. ``tier`` is
        the named tier (cheap/med/high) selecting the spawn's model, resolved
        override → spec ``tier:`` → tool-reach default (read-only spawns default
        to cheap, mutating ones to high); ``None`` lets that resolution run
        automatically rather than forcing a tier. ``thinking`` is the spawn-call
        thinking override; it wins over the spec's ``thinking:`` frontmatter and
        the inherited session level (resolved in ``_spawn_thinking_settings``).
        ``None`` lets that resolution run; a resolved ``off``/none leaves the
        base settings intact. Returns ``(agent, None)`` or,
        for an unknown type or an unresolvable model, ``(None, message)``."""
        if defn is None:
            defn = self._resolve_agent(type)
        if defn is None:
            names = ", ".join(
                a.qualified_name
                for a in (
                    *self._extra_agents,
                    *discover_agents(
                        self.deps.workspace.root, trust_project=self.deps.trust.project
                    ),
                )
            )
            return None, f"No sub-agent type {type!r}. Available: {names}."
        instr_root = (
            workspace_root if workspace_root is not None else self.deps.workspace.root
        )
        read_only = not (defn.tools & GATED_TOOLS)
        model_id = _resolve_spawn_model_id(
            override_tier=tier, slug=model, spec_tier=defn.tier,
            read_only=read_only, tiers=self._tiers,
        )
        if model and model_id != model:
            logger.debug(
                "sub-agent slug override %r not in tier allowlist; using %r",
                model, model_id,
            )
        if model_id is None:
            model_obj = self._get_model()
        elif self._build_model is None:
            return None, (
                f"Can't run sub-agent on model {model_id!r}: no model source is "
                "available to resolve an override here."
            )
        else:
            model_obj = self._build_model(model_id)
        # Reach is decided HERE, at spawn time, from the live session mode
        # (WorkspaceConfig.mode is rewritten in place by set_mode/cycle_mode, and
        # self.deps is the main harness's Deps — the live object — even for a
        # nested spawn, whose caller carries a replace()d copy). Spawn time is
        # the right evaluation point: a sub-agent's tools are registered plain
        # (no approval round), so its grant must be fixed when it launches; a
        # mode flip mid-run affects the NEXT spawn, not a running one — exactly
        # how allow_gated has always behaved. allow_net mirrors the main agent's
        # plan-mode egress denial (_plan_decision): without it, a plan-mode
        # spawn of a net-granting role would be an unapproved exfiltration path.
        mode = self.deps.workspace.mode
        allow_gated = mode is Mode.auto
        allow_net = mode is not Mode.plan
        # Imported lazily for the same reason as run_driver.py's own harness
        # import: agent.py imports this module, so a top-level import of the
        # harness would cycle.
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

        get_scratchpad = self.deps.services.get_scratchpad
        scratch = get_scratchpad() if get_scratchpad is not None else None
        # Computed once and reused for both the tool registration and the
        # prompt line below: register_subagent strips write_file/edit_file
        # from read-only agent types and from every spawn outside auto mode
        # (effective_tools), so the scratchpad line must match that same set —
        # advertising a write the spawn doesn't have makes the model call it
        # and hard-fail (same rationale as the files_write gate on the main
        # agent's _scratchpad instructions block, commit d0d038c).
        tools = effective_tools(defn, allow_gated=allow_gated, allow_net=allow_net)
        scratchpad_writable = "write_file" in tools
        sub_settings = self._spawn_thinking_settings(thinking, defn)

        sub = Agent(
            model_obj,
            deps_type=Deps,
            # Schema'd spawns enforce their output natively: StructuredDict
            # validates the final output against the schema and retries
            # IN-RUN on mismatch, instead of the caller re-running the whole
            # spawn. Everything downstream still sees str —
            # _execute_native_spawn serializes a dict result.
            output_type=StructuredDict(output_schema) if output_schema else str,
            instructions=subagent_instructions(
                defn, instr_root, max_output_chars,
                scratchpad=scratch, scratchpad_writable=scratchpad_writable,
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
            model_settings=sub_settings,
            capabilities=capabilities,
        )
        self.provider.register_subagent(sub, tools)
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
        # Prefer the session scratchpad (session-scoped, auto-cleaned) over
        # the workspace-rooted `.marim/subagent-output/` fallback. The note's
        # path is absolute either way (see spill_target).
        scratchpad = None
        getter = self.deps.services.get_scratchpad
        if getter is not None:
            scratchpad = getter()
        spill_path, rel = spill_target(
            scratchpad, self.deps.workspace.root, "subagent-output", f"{ref}.md"
        )
        text, spill = cap_subagent_output(output, max_output_chars, spill_path)
        if spill is not None:
            write_spill(self.deps.workspace.root, spill_path, rel, spill)
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
            #
            # BUT NOT while the main turn is parked in an approval round: at that
            # instant the session's in-memory history is dirty — it ends on a
            # deferred ToolCallPart whose ToolReturnPart doesn't exist yet — and a
            # force=True persist writes exactly that unresumable history to disk
            # (every provider rejects a history ending on an unanswered tool call
            # on the next request). We skip the write here; nothing is lost that
            # matters: this spawn's spend is already folded into
            # ``self.session.usage`` above and its settled-job entry lives in the
            # JobRegistry, and the controller reads BOTH live at its next persist
            # (the clean one after the approval round, or the rollback/flush on an
            # aborted round). The only casualty is off-turn spend on a hard *kill*
            # during the approval wait — the accepted lesser evil versus flushing
            # a history no session could resume from.
            if self.deps.approval_round_active:
                logger.debug(
                    "background spawn finished during an approval round; deferring "
                    "its force-persist so the dirty in-memory history is not "
                    "written (the controller's next persist banks the spend/jobs)"
                )
            else:
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
        self, run_fn: Callable[[], Awaitable[SpawnRun]], *, iso: SpawnWorktree | None,
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
            logger.warning("sub-agent %r spawn failed: %s", name, exc, exc_info=True)
            if timing is not None:
                self._log_spawn_timing(name, *timing, failed=True)
            if iso:
                iso.teardown_after_failure(resumed=resumed)
            # Fire SubagentStop on failure for BOTH modes, symmetric with success
            # (_finalize_spawn always fires it). A background crash used to skip
            # the hook entirely, so observers (and the transcript of hook events)
            # saw a foreground failure stop but never a background one — an
            # asymmetry no hook author could account for. Fire it first, then let
            # the mode decide disposition: a background crash re-raises to the job
            # registry (which marks the job failed — no turn to protect), a
            # foreground crash is contained as an error string so a sibling
            # fan-out spawn survives its neighbor.
            await self.hooks.subagent_stop(name, stop_task, f"error: {exc}")
            if background:
                raise
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

    async def _execute_spawn(
        self, type: str, task: str, mcp_names: list[str] | None,
        max_output_chars: int | None, model: str | None, isolation: str | None,
        *, background: bool, stream_id: str, caller_depth: int = 0,
        output_schema: dict | None = None, tier: str | None = None,
        thinking: str | None = None,
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
                return self._preflight_failure(err, background)
        work_root = iso.path if iso else None
        # CLI-backed agents run an external `claude` process instead of the
        # in-process Pydantic AI loop, so they skip the native build+MCP prepare.
        # Branch here to self._cli.execute, which builds its own meta/checkpoint
        # and then rejoins the SAME _run_spawn_lifecycle the native tails use — the
        # run+failure+finalize wrapper is written once, not duplicated per backend.
        # Resolve the agent definition ONCE here (a filesystem discovery walk) and
        # thread it through to _prepare_spawn/build so a native spawn doesn't pay the
        # walk a second time — it matters on a fan-out (2N walks → N).
        defn = self._resolve_agent(type)
        depth = caller_depth + 1
        # Decide the schema enforcement path ONCE, where the backend is
        # known: object-rooted schemas on native spawns ride structured
        # output (build() below sets output_type); the claude-cli backend
        # and non-object roots get the prompt contract appended to the task
        # instead — see subagents/output_schema.py.
        output_schema, contract = resolve_output_schema(
            output_schema, defn.backend if defn is not None else None
        )
        task = task + contract
        if defn is not None and defn.backend == "claude-cli":
            return await self._cli.execute(
                defn, task, work_root, iso, mcp_names, max_output_chars,
                model, stream_id, background=background, depth=depth,
            )
        prep = await self._prepare_spawn(
            type, task, mcp_names, max_output_chars, model,
            iso, work_root, stream_id, debug=debug, t0=t0, defn=defn, depth=depth,
            output_schema=output_schema, tier=tier, thinking=thinking,
        )
        if isinstance(prep, str):
            return self._preflight_failure(prep, background)
        return await self._execute_native_spawn(
            type, task, stream_id, max_output_chars, prep, background=background,
        )

    def _preflight_failure(self, err: str, background: bool) -> str:
        """Disposition for a pre-flight failure (worktree open / sub-agent build)
        that struck before the run lifecycle. A foreground spawn returns the
        error string to its caller (a sibling fan-out survives its neighbor); a
        background spawn raises so the JobRegistry settles the job ``failed``
        rather than ``done`` with the error as a fake result. See
        ``_SpawnPreflightError``."""
        if background:
            raise _SpawnPreflightError(err)
        return err

    async def _prepare_spawn(
        self, type: str, task: str, mcp_names: list[str] | None,
        max_output_chars: int | None, model: str | None,
        iso: SpawnWorktree | None, work_root, stream_id: str,
        *, debug: bool, t0: float, defn=None, depth: int = 0,
        resumed: bool = False, output_schema: dict | None = None,
        tier: str | None = None, thinking: str | None = None,
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
        # Resolve the model the spawn will ACTUALLY run on before anything that
        # depends on it. The masking trigger sizes its token budget/window from
        # the spawn's model window (MaskingPolicy.trigger_for), so it must see the
        # tier-resolved model — a read-only fan-out routed to a small cheap model
        # otherwise gets the main model's much larger window and masks far too
        # late (or never), letting the cheap model overflow. spawn_defn is
        # resolved once here and reused for the model report and thinking report
        # below (and, on the CLI/non-defn paths, falls back to a discovery walk).
        spawn_defn = defn if defn is not None else self._resolve_agent(type)
        resolved_model = (
            _resolve_spawn_model_id(
                override_tier=tier, slug=model, spec_tier=spawn_defn.tier,
                read_only=not (spawn_defn.tools & GATED_TOOLS), tiers=self._tiers,
            )
            if spawn_defn is not None
            else None
        )
        # None ⇒ inherit the session model; _mask_trigger_for then falls back to
        # it, so pass resolved_model straight through in both cases.
        mask_trigger = await self._mask_trigger_for(resolved_model)
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
            #
            # output_schema/tier/thinking are recorded so a resume reproduces the
            # SAME run shape: without them a resumed spawn dropped its structured-
            # output enforcement, tier-based model routing, and reasoning effort,
            # silently continuing as a plain default-effort spawn. output_schema
            # is the already-resolved schema (a plain JSON dict or None — the
            # prompt-contract path stored None and its contract already rides in
            # ``task`` above), so it re-feeds _prepare_spawn directly on resume.
            meta = {
                "stream_id": stream_id, "type": type, "task": task,
                "model": model, "mcp": mcp_names, "depth": depth,
                "max_output_chars": max_output_chars,
                "isolation": iso.branch if iso else None,
                "output_schema": output_schema, "tier": tier,
                "thinking": thinking,
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
                              checkpoint=checkpoint, output_schema=output_schema,
                              tier=tier, thinking=thinking)
        if sub is None:
            if iso:
                # Own the failure teardown HERE rather than at the caller: a resumed
                # spawn must keep its branch (prior work), a fresh spawn discards
                # both. If the caller tried to keep the branch after us, it couldn't —
                # a plain discard() here would already have deleted it.
                iso.teardown_after_failure(resumed=resumed)
            return err or f"Failed to build sub-agent {type!r}."
        # The spawn's card/pane were created from the spawn_agent tool args, which
        # carry at most a raw ``model=`` slug — never the tier-resolved model. So a
        # tier-routed spawn (or any spec/tool-reach default) renders with the MAIN
        # model as a fallback, misreporting the model it actually runs on. Report the
        # resolved model (computed up top, alongside the masking trigger) to the UI
        # relabel callback (the same seam claude-cli uses) and stamp it into the
        # sidecar meta, so both the live card and a resumed card name the real model.
        # ``None`` means "inherit main", which the existing fallback already shows
        # correctly — so only override when a model was chosen.
        if resolved_model is not None:
            if meta is not None:
                meta["model"] = resolved_model
            report_model = self.deps.ui.on_subagent_model
            if report_model is not None and stream_id:
                await report_model(stream_id, resolved_model)
        await self._report_spawn_thinking(stream_id, thinking, spawn_defn)
        t_built = time.perf_counter()
        granted, unknown, mcp_withheld, ask_withheld = await self._spawn_mcp_grant(
            mcp_names
        )
        if meta is not None:
            # Display-only, so a future resumed card/stats view can show that
            # THIS run's grant was (partly) withheld. Resume logic must never
            # read it to decide the grant — resume re-evaluates the mode live
            # through _prepare_spawn/_spawn_mcp_grant on every call, exactly
            # like a fresh spawn (see _spawn_mcp_grant's docstring), so a stale
            # True here can never suppress a grant the CURRENT mode allows.
            meta["mcp_withheld"] = mcp_withheld
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
            depth=depth, meta=meta, mcp_withheld=mcp_withheld,
            mcp_ask_withheld=ask_withheld,
        )

    async def _spawn_mcp_grant(
        self, mcp_names: list[str] | None,
    ) -> tuple[list[object], list[str], bool, tuple[str, ...]]:
        """The MCP toolsets a spawn is granted, decided ENTIRELY up front —
        a sub-agent's tools run with no approval round, so its reach is fixed
        at spawn time (same doctrine as ``allow_gated``/``allow_net`` in
        ``build``). Per mode, snapshot at spawn/resume time (a mode flip
        affects the NEXT spawn, never a running one):

        - **auto**: the full requested grant — every MCP call is auto-approved
          on the main loop too, so nothing here could ever prompt.
        - **ask**: withholds exactly the servers whose calls would prompt the
          user per-call (``McpManager.ask_prompting_names`` — untrusted
          config-built servers); a mid-spawn prompt through the main-loop UI
          would be an approval round the spawn is defined not to have.
          Trusted and hookless servers run without prompting, so they stay
          granted — parity with what the session's mode already allows.
        - **plan**: withholds the whole grant. The main agent's own MCP calls
          are already denied per-call in plan mode by the
          ``process_tool_call`` hook ``build_mcp_servers`` attaches
          (mcp/config.py ``make_approval_hook``), but a spawn must not rely on
          that hook: it exists only on config-built servers — an
          embedder-supplied one (``HarnessBuilder.with_mcp_server``) carries
          no hook, so granting it would open an egress/mutation channel with
          zero user gate. Withholding up front closes the channel structurally
          (same rationale as ``allow_net`` mirroring ``_plan_decision``'s
          net-egress denial), is deliberately stricter than the main loop's
          call-time deny, and spares a plan spawn from burning its turns on
          denials.

        Either way the sidecar meta still records the REQUESTED names, so a
        spawn interrupted here and resumed under a laxer mode gets its grant
        back — resume funnels through this method again via ``_prepare_spawn``.

        Whatever survives gets the same tool-search deferral the main agent
        uses: a large granted MCP surface is combined behind ToolSearch rather
        than injected wholesale, so a spawn granted (say) an 86-tool browser
        server searches for tools on demand instead of carrying every schema.
        Policy/threshold are the same workspace settings the controller reads
        for the main turn.

        Returns ``(granted, unknown, withheld, ask_withheld)``; ``withheld``
        is True when mode dropped any part of a non-empty request, and
        ``ask_withheld`` names the ask-mode subset (empty on a plan withhold)
        — together they drive the spawner-facing note in
        ``_withheld_mcp_note``. Withheld names are deliberately NOT folded
        into ``unknown`` — they exist, they're just ungrantable here, and the
        unknown-servers note would misname them."""
        if self.deps.workspace.mode is Mode.plan:
            return [], [], bool(mcp_names), ()
        names = mcp_names
        ask_withheld: tuple[str, ...] = ()
        if self.deps.workspace.mode is Mode.ask:
            ask_withheld = tuple(self.mcp.ask_prompting_names(mcp_names))
            if ask_withheld:
                names = [n for n in (mcp_names or []) if n not in ask_withheld]
        granted, unknown = await self.mcp.granted_toolsets(
            names,
            self.deps.workspace.tool_search,
            self.deps.workspace.tool_search_threshold,
        )
        return granted, unknown, bool(ask_withheld), ask_withheld

    @staticmethod
    def _withheld_mcp_note(prep: _SpawnPrep) -> str:
        """A one-line note on the spawn's report when the session mode withheld
        part or all of a requested MCP grant (see ``_spawn_mcp_grant``): the
        spawner asked for servers and must be told why its sub-agent lacked
        them, rather than the grant silently vanishing — mirrors the CLI
        path's not-forwarded ``_mcp_note``. The ask/plan distinction is read
        off the prep snapshot, never the live mode, which may have flipped
        while the spawn ran."""
        if not prep.mcp_withheld:
            return ""
        if prep.mcp_ask_withheld:
            named = ", ".join(prep.mcp_ask_withheld)
            return (
                f"[note: MCP server(s) {named} were withheld from this "
                "sub-agent — in ask mode their tool calls would each need "
                "mid-run user approval, and sub-agents run with no approval "
                "round. Switch to auto mode, or mark the server trusted in "
                "its MCP config, to grant them]\n\n"
            )
        return (
            "[note: MCP server grants are withheld from sub-agents in plan "
            "mode (local research only) — switch out of plan mode to grant "
            "MCP servers]\n\n"
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
            result = await self._driver.run_to_completion(
                prep.sub, task, run_deps, prep.granted, prep.handler, stream_id,
                history=history,
            )
            # The card accrues usage only from mid-stream events, which carry the
            # run's *running* total. A spawn that makes a single model request (no
            # tool calls) streams every event BEFORE that one response's usage is
            # tallied, so the card never sees a non-zero reading and shows blank
            # tokens/cost; a multi-request (tool-using) spawn is masked only because
            # an earlier request's usage rides on a later request's events. Push the
            # final, authoritative usage through the same seam the CLI backend uses so
            # every native spawn's card lands on its true total.
            report_usage = self.deps.ui.on_subagent_usage
            if report_usage is not None and stream_id and result.usage is not None:
                await report_usage(stream_id, result.usage)
            out = result.output
            return SpawnRun(
                # A schema'd spawn (output_type=StructuredDict) finishes with
                # a dict; every seam above returns str, so serialize HERE —
                # the caller gets the same JSON text a prompt-contracted
                # spawn would produce, minus the extraction guesswork.
                output=out if isinstance(out, str) else json.dumps(out),
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
            stop_task=stop_task,
            note=self._withheld_mcp_note(prep) + self.mcp.grant_note(prep.unknown),
            max_output_chars=max_output_chars, stream_id=stream_id,
            timing=(prep.t0, prep.t_built, prep.first_event_at),
        )

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
        caller_depth: int = 0, tier: str | None = None,
        output_schema: dict | None = None, thinking: str | None = None,
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
        agent); the spawned sub-agent runs at ``caller_depth + 1``.

        ``output_schema`` is an optional JSON Schema the spawn's final report
        must satisfy. Object-rooted schemas on native spawns are enforced with
        pydantic-ai structured output (mismatches retry in-run); claude-cli
        spawns and non-object roots fall back to a contract paragraph appended
        to the task. The report is always returned as str — a structured
        result is JSON-serialized. ``tier`` is the named tier (cheap/med/high)
        selecting the spawn's model, resolved override → spec ``tier:`` →
        tool-reach default; ``None`` leaves that resolution automatic.
        ``thinking`` is the spawn-call thinking-level override, resolved
        override → spec ``thinking:`` → inherited session level; ``None``
        leaves that resolution automatic.
        """
        return await self._execute_spawn(
            type, task, mcp_names, max_output_chars, model, isolation,
            background=False, stream_id=stream_id, caller_depth=caller_depth,
            output_schema=output_schema, tier=tier, thinking=thinking,
        )

    async def run_background(
        self, type: str, task: str, mcp_names: list[str] | None = None,
        max_output_chars: int | None = None, model: str | None = None,
        isolation: str | None = None, stream_id: str = "",
        caller_depth: int = 0, tier: str | None = None,
        thinking: str | None = None,
    ) -> str:
        """Run a sub-agent as a detached background job: same isolation, mode-based
        reach, and MCP grant as a foreground spawn. When ``stream_id`` is set (the
        launching spawn's tool_call_id) and a UI listener exists, the run streams
        its events to that card live — identical to a foreground spawn (Phase 2);
        with no ``stream_id`` (headless) it streams nothing and the job's result is
        its final report, surfaced when the agent pulls it. Any unknown-server note
        rides along on that report. ``max_output_chars`` is enforced the same way as
        a foreground spawn: it is a soft instruction to the sub-agent AND a hard
        post-cap — an over-budget report is truncated and the remainder spilled to a
        workspace file (keyed ``bg-N`` rather than the foreground ``stream_id``,
        since a detached run has no stream id), so the pulled report stays bounded.
        ``model`` optionally overrides the model this spawn runs on.
        ``isolation="worktree"`` runs it in its own git worktree, committing its
        changes to a branch named in the report. ``caller_depth`` is the nesting
        depth of the agent that issued the spawn (0 for the main agent); the
        detached sub-agent runs at ``caller_depth + 1``, so a background spawn from
        a nested sub-agent is sized — and depth-limited — the same as a foreground
        one. ``tier`` is the named tier (cheap/med/high) selecting the spawn's
        model, resolved override → spec ``tier:`` → tool-reach default; ``None``
        leaves that resolution automatic. ``thinking`` is the spawn-call
        thinking-level override, resolved override → spec ``thinking:`` →
        inherited session level; ``None`` leaves that resolution automatic."""
        return await self._execute_spawn(
            type, task, mcp_names, max_output_chars, model, isolation,
            background=True, stream_id=stream_id, caller_depth=caller_depth,
            tier=tier, thinking=thinking,
        )

    def _resume_preconditions(self, stream_id: str) -> tuple[dict | None, str | None]:
        """Guard-fold for `resume_spawn`: is there a session store at all, does
        this spawn have a resumable (v2-envelope) sidecar, is that sidecar's
        status actually resumable, and is no other job already resuming it.
        Returns ``(meta, None)`` when every guard passes, else ``(None,
        refusal)`` with a user-renderable refusal string — the same messages
        `resume_spawn` returned inline before this was split out."""
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
        return meta, None

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
            meta, refusal = self._resume_preconditions(stream_id)
            if refusal is not None:
                return None, refusal
            assert meta is not None
            # A claude-cli spawn resumes through the CLI's own session machinery,
            # not the native transcript-repair path: the CLI owns its history and
            # marim's sidecar is a display copy, so reading/repairing it here
            # would be wasted work at best and engine-swapping at worst.
            if meta.get("backend") == "claude-cli":
                return await self._cli.resume(stream_id, meta)
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
            # Resolve the agent definition ONCE here and thread it into
            # _prepare_spawn (which reuses it for both model resolution and
            # build()), so the resume path doesn't pay the discovery filesystem
            # walk twice — matching the fresh-spawn path, which resolves it in
            # _execute_spawn. The claude-cli backend was already peeled off above,
            # so this is always a native definition.
            defn = self._resolve_agent(type_)
            # Rehydrate the run-shape knobs (output_schema/tier/thinking) the
            # original spawn recorded, so the resumed run enforces the same
            # schema, routes to the same tier model, and thinks at the same
            # effort. ``.get`` defaults keep a pre-envelope sidecar (recorded
            # before these keys existed) resuming as a plain spawn rather than
            # KeyError-ing.
            prep = await self._prepare_spawn(
                type_, task, meta.get("mcp"), meta.get("max_output_chars"),
                meta.get("model"), iso, iso.path if iso else None, stream_id,
                debug=logger.isEnabledFor(logging.DEBUG), t0=time.perf_counter(),
                depth=int(meta.get("depth") or 1), resumed=True, defn=defn,
                output_schema=meta.get("output_schema"), tier=meta.get("tier"),
                thinking=meta.get("thinking"),
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
                    type_, CONTINUATION_PROMPT, stream_id,
                    meta.get("max_output_chars"), prep,
                    background=True, history=history,
                ),
                stream_id=stream_id,
                prompt=task,
            )
            return job_id, f"Resumed as {job_id}."
        finally:
            self._resuming.discard(stream_id)
