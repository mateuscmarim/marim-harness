"""Spawn and run isolated sub-agents on behalf of the harness.

A sub-agent is a fresh Pydantic AI ``Agent`` built on the harness's *current*
model (so it tracks runtime model switches), with its tool reach decided up
front by the approval mode, an optional MCP grant, and an optional soft output
budget. Foreground spawns stream their events to the UI and fold their spend
into the running turn; background spawns run detached and persist their spend
immediately. The harness wires ``run``/``run_background`` onto ``Deps`` so the
``spawn_agent`` tool reaches them the same way other tools reach shared state.
"""

from typing import Optional

from pydantic_ai import Agent

from .deps import Deps, SubAgent
from .hooks.dispatch import TurnHooks
from .permissions import Mode
from .tools import fs
from .workspace import (
    cap_subagent_output,
    discover_agents,
    effective_tools,
    find_agent,
    subagent_instructions,
)


class SubagentRunner:
    """Builds and runs sub-agents for one harness. Reads the active model
    through ``get_model`` each spawn, so a runtime ``/model`` switch is picked
    up without rewiring."""

    def __init__(self, provider, mcp, deps, hooks: TurnHooks, session,
                 get_model, model_settings=None):
        self.provider = provider
        self.mcp = mcp
        self.deps = deps
        self.hooks = hooks
        self.session = session
        self._get_model = get_model
        self._model_settings = model_settings

    def handler(self, stream_id: str):
        """An event_stream_handler for a sub-agent run that forwards each event to
        the UI, tagged with ``stream_id`` so it can stream nested under the spawn.
        None when no UI is listening (headless) — the run just doesn't stream."""
        cb = self.deps.on_subagent_event
        if cb is None:
            return None

        async def handler(ctx, events) -> None:
            async for event in events:
                # Forward the whole usage (not just a token total) so the UI can
                # render the cache split and cost, not only the running count.
                await cb(stream_id, event, getattr(ctx, "usage", None))

        return handler

    def build(
        self, type: str, max_output_chars: int | None = None
    ) -> "tuple[Optional[SubAgent], Optional[str]]":
        """Build an isolated sub-agent of ``type`` on the current model, with its
        reach decided up front: gated tools only in auto mode, so a run never
        needs an approval round. ``max_output_chars``, when the spawner set one,
        is folded into the sub-agent's instructions as a soft output budget.
        Returns ``(agent, None)`` or, for an unknown type, ``(None, message)``
        listing what's available."""
        defn = find_agent(self.deps.workspace_root, type)
        if defn is None:
            names = ", ".join(a.name for a in discover_agents(self.deps.workspace_root))
            return None, f"No sub-agent type {type!r}. Available: {names}."
        allow_gated = self.deps.mode is Mode.auto
        sub = Agent(
            self._get_model(),
            deps_type=Deps,
            instructions=subagent_instructions(
                defn, self.deps.workspace_root, max_output_chars
            ),
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

    async def run(
        self, type: str, task: str, stream_id: str,
        mcp_names: list[str] | None = None, max_output_chars: int | None = None,
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
        pointer so the inflow stays bounded."""
        sub, err = self.build(type, max_output_chars)
        if err is not None:
            return err
        assert sub is not None  # err is None ⇒ build returned an agent
        granted, unknown = self.mcp.granted_servers(mcp_names)
        await self.hooks.subagent_start(type, task)
        result = await sub.run(
            task, deps=self.deps, toolsets=granted,
            event_stream_handler=self.handler(stream_id),
        )
        await self.hooks.subagent_stop(type, task, result.output)
        # A foreground spawn runs inside the current turn, so its spend is folded
        # into the session total here and persisted by run_turn's _persist.
        self.session.usage += result.usage
        capped = self._cap_output(result.output, max_output_chars, stream_id)
        return self.mcp.grant_note(unknown) + capped

    async def run_background(
        self, type: str, task: str, mcp_names: list[str] | None = None,
        max_output_chars: int | None = None,
    ) -> str:
        """Run a sub-agent as a detached background job: same isolation, mode-based
        reach, and MCP grant as a foreground spawn, but with no event streaming —
        the job's result is its final report, surfaced when the agent pulls it.
        Any unknown-server note rides along on that report. ``max_output_chars``
        applies only as a soft instruction here (the report is pulled later via
        the jobs API, which has no spill hook), so a background report is not
        hard-capped the way a foreground one is."""
        sub, err = self.build(type, max_output_chars)
        if err is not None:
            return err
        assert sub is not None  # err is None ⇒ build returned an agent
        granted, unknown = self.mcp.granted_servers(mcp_names)
        await self.hooks.subagent_start(type, task)
        result = await sub.run(task, deps=self.deps, toolsets=granted)
        await self.hooks.subagent_stop(type, task, result.output)
        # A background spawn finishes off-turn, so no run_turn will fold in its
        # spend — count it here and persist right away so the saved session
        # reflects it even if the process exits before the next turn.
        self.session.usage += result.usage
        self.session.persist()
        return self.mcp.grant_note(unknown) + result.output
