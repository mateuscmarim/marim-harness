"""Read-only CLI agent mirrors, fed by the transport even between parent turns.

The display routers are deliberately separate instances: their ledger seals,
replay and consumer lifetime must never settle a real job. Reusing their
translation keeps Agent/Task and Codex collab variants consistent with cards.
These observers neither adopt threads nor execute/cancel backend work.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic_ai.messages import FunctionToolCallEvent, FunctionToolResultEvent, ToolReturnPart

from ..jobs import Job, JobRegistry, Status


class AgentJobMirror:
    def __init__(self, registry: JobRegistry, on_settled: Callable[[], None] | None = None) -> None:
        self.registry = registry
        self.epoch = registry.observation_epoch
        self.jobs: dict[str, Job] = {}
        self.closed = False
        self.on_settled = on_settled

    @property
    def active(self) -> bool:
        return not self.closed and self.epoch == self.registry.observation_epoch

    def start(self, stream_id: str, args: dict) -> None:
        if not self.active or not stream_id:
            return
        job = self.jobs.get(stream_id)
        if job is not None and self.registry.get(job.id) is job:
            if args.get("resumed"):
                self.registry.reopen_observed(job)
            return
        label = str(args.get("description") or args.get("type") or "CLI agent")
        self.jobs[stream_id] = self.registry.observe_agent(
            stream_id, label, str(args.get("task") or "") or None
        )

    def finish(self, stream_id: str, result: str, status: Status) -> None:
        if self.epoch != self.registry.observation_epoch:
            return
        job = self.jobs.get(stream_id)
        if job is not None and self.registry.get(job.id) is job and job.status == "running":
            self.registry.settle_observed(job, status, result)
            if self.on_settled is not None:
                self.on_settled()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for stream_id in self.jobs:
            self.finish(
                stream_id, "Backend tracking ended; final agent status unavailable.", "failed"
            )


class ClaudeJobObserver(AgentJobMirror):
    def __init__(self, registry: JobRegistry, on_settled: Callable[[], None] | None = None) -> None:
        super().__init__(registry, on_settled)
        # Lazy: the demux imports the CLI translator, which imports the model.
        from ..subagents.cli_demux import CliSubagentDemux

        self.demux = CliSubagentDemux()

    def __call__(self, obj: dict) -> None:
        from ..claude.protocol import CLOSED

        if obj.get("type") == CLOSED:
            self.close()
        if not self.active:
            return
        routed, _ = self.demux.route(obj)
        for item in routed:
            event = item.event
            if isinstance(event, FunctionToolCallEvent) and event.part.tool_name == "spawn_agent":
                self.start(event.part.tool_call_id, event.part.args_as_dict())
            elif isinstance(event, FunctionToolResultEvent) and isinstance(
                event.part, ToolReturnPart
            ):
                status: Status = "done" if event.part.outcome == "success" else "failed"
                if obj.get("status") in ("stopped", "cancelled", "interrupted"):
                    status = "cancelled"
                self.finish(event.part.tool_call_id, str(event.part.content), status)


class CodexJobObserver(AgentJobMirror):
    def __init__(
        self, registry: JobRegistry, parent: str, on_settled: Callable[[], None] | None = None
    ) -> None:
        super().__init__(registry, on_settled)
        from ..codex.collab import CollabRouter
        from ..codex.translate import ItemTranslator

        self.router = CollabRouter(parent, adopt=lambda *_: None, release=lambda _: None)
        self.translator = ItemTranslator()

    def __call__(self, method: str, params: dict) -> None:
        from ..codex.server import CLOSED

        if method == CLOSED:
            self.close()
        if not self.active:
            return
        routed = self.router.route(method, params)
        if routed is None:
            routed = []
            for item in self.translator.translate(method, params):
                routed.extend(self.router.route_item(item))
        for item in routed:
            self._record(item)

    def _record(self, item: object) -> None:
        from ..codex.collab import Routed
        from ..codex.translate import ActivityEnd, ActivityStart

        if isinstance(item, Routed):
            item = item.item
        if isinstance(item, ActivityStart) and item.tool_name == "spawn_agent":
            self.start(item.item_id, item.args)
        elif isinstance(item, ActivityEnd):
            statuses: dict[str, Status] = {
                "completed": "done",
                "interrupted": "cancelled",
                "shutdown": "cancelled",
            }
            status = statuses.get(self.router.status_for(item.item_id) or "", "failed")
            self.finish(item.item_id, item.content, status)
