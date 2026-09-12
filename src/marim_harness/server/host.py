"""One live session: a Harness, its turn queue, and its parked asks.

SessionHost is the server-side implementation of the ``bind_ui`` contract the
TUI fills interactively. Stream events, sub-agent events, and lifecycle
notices publish onto the session's EventBus; ``request_approval`` and
``ask_user`` park as PendingAsk futures any authenticated client can answer
(no timeout — spec: park and wait).

One turn at a time: submissions enter a bounded queue drained by a single
worker task, mirroring the TUI's exclusive-worker discipline (a Harness is not
safe under concurrent run_turn calls). Interrupt cancels the running turn's
task; the TurnController's existing resumable-flush machinery handles rollback,
and the dirty mid-approval history is never persisted — so a daemon crash with
a parked ask simply rolls the session back to its last clean baseline."""

import asyncio
import contextlib
import dataclasses
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic_ai import ToolDenied

from ..ask_user import Choice, Question
from ..runtime.deps import PlanDecision
from ..runtime.errors import format_provider_error
from ..runtime.harness import Harness
from ..runtime.outcome import TurnOutcome
from ..runtime.wake import WakeController
from ..runtime.wake_driver import WakeDriver
from ..session.claim import SessionClaim
from ..stream_events import event_to_dict
from ..usage import usage_summary
from .bus import EventBus
from .schema import STREAM_EVENT_TYPES

logger = logging.getLogger(__name__)


class TurnQueueFull(Exception):
    """submit() refused: the per-session turn queue is at capacity."""


class HostClosed(Exception):
    """submit() refused: the host has already been torn down."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump_usage(usage: object) -> dict:
    """JSON-friendly dump of a sub-agent's usage object. Pydantic-ai's
    ``RunUsage`` is a plain dataclass (no ``model_dump``); a pydantic model
    wins if one is ever passed instead."""
    model_dump = getattr(usage, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, dict):
            return dumped
    if dataclasses.is_dataclass(usage) and not isinstance(usage, type):
        return dataclasses.asdict(usage)
    return {}


@dataclass
class PendingAsk:
    id: str
    kind: str  # "approval" | "question" | "plan"
    payload: dict
    created: str
    future: "asyncio.Future[dict]" = field(repr=False)

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "payload": self.payload, "created": self.created}


class SessionHost:
    """Must be constructed inside a running event loop (it starts its worker
    task immediately)."""

    def __init__(
        self,
        harness: Harness,
        bus: EventBus,
        *,
        queue_limit: int = 8,
        claim: "SessionClaim | None" = None,
        autonomous_wake: bool = True,
    ) -> None:
        self.harness = harness
        self.bus = bus
        # Ownership of the session file for this host's whole lifetime. Released
        # in aclose(), which every teardown path funnels through — including idle
        # eviction, where giving up ownership is correct: the harness is gone and
        # the session is resumable from disk, so nobody owns it.
        self._claim = claim
        self._queue: asyncio.Queue[tuple[str, str, list | None, str]] = asyncio.Queue(
            maxsize=queue_limit
        )
        self._pending: dict[str, PendingAsk] = {}
        self._turn_task: asyncio.Task | None = None
        self._closing = False
        loop = asyncio.get_running_loop()
        self._idle_since = loop.time()
        jobs = harness.deps.jobs
        self._wake = WakeDriver(
            WakeController(harness.wake_depth_cap),
            # The daemon owns its autonomous wake; the in-process TUI host does
            # not — HarnessApp's ActivityMonitor drives wake there (posts the
            # "Resumed" notice, arms its own depth counter, mounts the turn
            # through the app). If both drivers were live, this one would
            # silently eat job-finished digests and run turns that never reach
            # the UI.
            is_enabled=lambda: autonomous_wake and harness.autonomous_wake,
            # "a turn is in flight" — NOT status == "running": a turn parked on an
            # ask reports "waiting_ask" while its task is still live, and a wake
            # turn must not queue behind it.
            turn_busy=lambda: self.status != "idle",
            has_finished_pending=jobs.has_finished_pending,
            all_jobs_settled=lambda: not jobs.any_running(),
            enqueue_digest_turn=self._enqueue_autonomous_turn,
        )
        harness.bind_ui(
            request_approval=self._request_approval,
            ask_user=self._ask_user,
            on_present_plan=self._present_plan,
            on_subagent_event=self._on_subagent_event,
            on_subagent_notice=self._on_subagent_notice,
            on_subagent_model=self._on_subagent_model,
            on_subagent_thinking=self._on_subagent_thinking,
            on_subagent_usage=self._on_subagent_usage,
            on_cli_activity=self._on_cli_activity,
            on_ttft=lambda seconds: self._publish("session.ttft", {"seconds": seconds}),
            on_mode_change=lambda: self._publish(
                "session.mode_changed", {"mode": harness.deps.workspace.mode.value}
            ),
            on_workflow_spawn=self._on_workflow_spawn,
            on_workflow_log=self._on_workflow_log,
            on_workflow_spawn_done=self._on_workflow_spawn_done,
            on_workflow_start=self._on_workflow_start,
            on_workflow_done=self._on_workflow_done,
            on_tasks_changed=lambda: self._publish("tasks.changed", {}),
            on_jobs_changed=self._on_jobs_changed,
            on_rename=lambda old, new: self._publish("session.renamed", {"from": old, "to": new}),
            on_compact_start=lambda: self._publish("compaction.started", {}),
            on_compact=lambda before, after: self._publish(
                "compaction.finished", {"before": before, "after": after}
            ),
            on_notice=lambda message: self._publish("session.notice", {"message": message}),
        )
        self._worker = loop.create_task(self._worker_loop())

    # ------------------------------------------------------------- state --
    @property
    def status(self) -> str:
        if self._pending:
            return "waiting_ask"
        if self._turn_task is not None or not self._queue.empty():
            return "running"
        return "idle"

    @property
    def busy(self) -> bool:
        return self.status != "idle"

    @property
    def queued(self) -> int:
        return self._queue.qsize()

    @property
    def idle_seconds(self) -> float:
        if self.busy:
            return 0.0
        return asyncio.get_running_loop().time() - self._idle_since

    # ----------------------------------------------------------- control --
    def submit(self, prompt: str, attachments: list | None = None) -> str:
        if self._closing:
            raise HostClosed()
        turn_id = secrets.token_hex(8)
        self._wake.note_user_turn()  # a user turn resets the autonomous-wake chain
        try:
            self._queue.put_nowait((turn_id, prompt, attachments, "user"))
        except asyncio.QueueFull:
            raise TurnQueueFull() from None
        return turn_id

    def _enqueue_autonomous_turn(self) -> None:
        """Queue one digest-only turn (empty prompt) marked autonomous. Best-effort:
        a full queue drops the wake rather than raising into a job callback — the
        pending digest survives and a later trigger can still fire it."""
        turn_id = secrets.token_hex(8)
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait((turn_id, "", None, "autonomous"))

    def interrupt(self) -> bool:
        """Cancel the running turn. Returns False when nothing is running."""
        if self._turn_task is None:
            return False
        self._turn_task.cancel()
        return True

    def steer(self, text: str) -> None:
        self.harness.steer(text)
        self.bus.publish("steer.accepted", {"text": text})

    def touch(self) -> None:
        """Reset the idle clock. Called by the supervisor when handing out an
        already-live host, so a fresh checkout is never immediately mistaken
        for continued inactivity by the idle-eviction sweep."""
        self._idle_since = asyncio.get_running_loop().time()

    def pending_asks(self) -> list[dict]:
        return [ask.as_dict() for ask in self._pending.values()]

    def answer_ask(self, ask_id: str, answer: dict) -> bool:
        ask = self._pending.pop(ask_id, None)
        if ask is None or ask.future.done():
            return False
        ask.future.set_result(answer)
        self.bus.publish("ask.resolved", {"id": ask_id, "answer": answer})
        return True

    # ---------------------------------------------------- bind_ui bridge --
    def _park(self, kind: str, payload: dict) -> PendingAsk:
        ask = PendingAsk(
            id=secrets.token_hex(8),
            kind=kind,
            payload=payload,
            created=_now(),
            future=asyncio.get_running_loop().create_future(),
        )
        self._pending[ask.id] = ask
        self.bus.publish("ask.pending", ask.as_dict())
        self._publish_status()
        return ask

    async def _await_ask(self, ask: PendingAsk) -> dict:
        """Wait for a parked ask's answer, owning its resolution on the way out.

        Two exits. Answered (answer_ask): the ask was popped and ``ask.resolved``
        published there — nothing to do. Interrupted: the turn task is cancelled
        while awaiting, which cancels the future *and* runs this frame's cleanup
        before the turn's ``finally`` reaches ``_cancel_pending`` — so by then the
        ask is gone from ``_pending`` and nobody would publish its resolution.
        The client's panel would sit open forever against a turn that is already
        finished. Publish it here, on the exit that actually observes the cancel;
        popping first keeps it single-shot when ``_cancel_pending`` (aclose) got
        there first and already announced it.
        """
        try:
            return await ask.future
        except asyncio.CancelledError:
            if self._pending.pop(ask.id, None) is not None:
                self.bus.publish(
                    "ask.resolved", {"id": ask.id, "cancelled": True, "reason": "interrupted"}
                )
            raise
        finally:
            self._pending.pop(ask.id, None)
            self._publish_status()

    async def _request_approval(self, call: object):
        payload = {
            "tool_name": getattr(call, "tool_name", None),
            "args": getattr(call, "args", None),
            "tool_call_id": getattr(call, "tool_call_id", None),
        }
        ask = self._park("approval", payload)
        answer = await self._await_ask(ask)
        if answer.get("approve"):
            return True
        return ToolDenied(str(answer.get("reason") or "denied by client"))

    async def _ask_user(self, questions: list[Question]) -> dict | None:
        payload = {
            "questions": [
                {
                    "question": q.question,
                    "header": q.header,
                    "multi": q.multi,
                    "options": [
                        {"label": c.label, "description": c.description} for c in q.options
                    ],
                }
                for q in questions
            ]
        }
        ask = self._park("question", payload)
        answer = await self._await_ask(ask)
        if answer.get("cancel"):
            return None
        answers = answer.get("answers")
        return answers if isinstance(answers, dict) else None

    async def _present_plan(
        self, summary: str, steps: list[str], choices: list[Choice]
    ) -> PlanDecision:
        payload = {
            "summary": summary,
            "steps": steps,
            "choices": [{"label": c.label, "description": c.description} for c in choices],
        }
        ask = self._park("plan", payload)
        answer = await self._await_ask(ask)
        if answer.get("cancel"):
            # Mirrors PlanCard.action_dismiss_card: Escape means "keep planning",
            # not a bare cancel — present_plan reads the choice label, not a
            # separate cancelled flag.
            return PlanDecision(choice="Keep planning")
        return PlanDecision(choice=str(answer.get("choice") or ""), feedback=answer.get("feedback"))

    async def _on_subagent_event(self, stream_id: str, event: object, usage: object) -> None:
        obj = event_to_dict(event)
        if obj is None:
            return
        data = {"stream_id": stream_id, "event": obj}
        if usage is not None:
            data["usage"] = _dump_usage(usage)
        self.bus.publish("subagent.event", data)

    async def _on_subagent_notice(self, stream_id: str, message: str) -> None:
        self._publish("subagent.notice", {"stream_id": stream_id, "message": message})

    async def _on_subagent_model(self, stream_id: str, model: str) -> None:
        self._publish("subagent.model", {"stream_id": stream_id, "model": model})

    async def _on_subagent_thinking(self, stream_id: str, level: str) -> None:
        self._publish("subagent.thinking", {"stream_id": stream_id, "level": level})

    async def _on_subagent_usage(self, stream_id: str, usage: object) -> None:
        self._publish("subagent.usage", {"stream_id": stream_id, "usage": _dump_usage(usage)})

    async def _on_cli_activity(self, events: list) -> None:
        wire = []
        for event in events:
            obj = event_to_dict(event)
            if obj is None:
                continue
            wire_type = STREAM_EVENT_TYPES.get(obj.pop("type"))
            if wire_type is not None:
                wire.append({"type": wire_type, **obj})
        if wire:
            self._publish("subagent.cli_activity", {"events": wire})

    async def _on_workflow_spawn(
        self, stream_id: str, spawn_type: str, task: str, parent_tool_call_id: str
    ) -> None:
        self._publish(
            "workflow.spawned",
            {
                "stream_id": stream_id,
                "spawn_type": spawn_type,
                "task": task,
                "parent_tool_call_id": parent_tool_call_id,
            },
        )

    def _on_workflow_start(self, tool_call_id: str, title: str) -> None:
        self._publish("workflow.started", {"tool_call_id": tool_call_id, "title": title})

    def _on_workflow_log(self, tool_call_id: str, message: str) -> None:
        self._publish("workflow.logged", {"tool_call_id": tool_call_id, "message": message})

    def _on_workflow_done(self, tool_call_id: str, outcome: str, failed: bool) -> None:
        self._publish(
            "workflow.finished",
            {"tool_call_id": tool_call_id, "outcome": outcome, "failed": failed},
        )

    def _on_workflow_spawn_done(self, stream_id: str, report: str) -> None:
        self._publish("workflow.spawn_finished", {"stream_id": stream_id, "report": report})

    def _on_jobs_changed(self) -> None:
        """A job launched or settled. Poke the jobs view, then let the wake driver
        decide whether a completion warrants an autonomous digest turn (trigger 1
        of 2 — the other is the turn-end check in _worker_loop)."""
        self._publish("jobs.changed", {})
        # Symmetry with the turn-end trigger: never enqueue a wake into a worker
        # being torn down. Keep publishing jobs.changed so a late settle still
        # updates the jobs view.
        if not self._closing:
            self._wake.maybe_wake()

    def _publish_status(self) -> None:
        self.bus.publish("session.status", {"status": self.status})

    def _publish(self, type: str, data: dict) -> None:
        """Fire-and-forget publish for bind_ui callbacks typed to return None
        (EventBus.publish returns the Event, which those callback signatures
        don't accept)."""
        self.bus.publish(type, data)

    # ------------------------------------------------------------- turns --
    async def _worker_loop(self) -> None:
        while True:
            turn_id, prompt, attachments, trigger = await self._queue.get()
            self._turn_task = asyncio.get_running_loop().create_task(
                self._run_one_turn(turn_id, prompt, attachments, trigger)
            )
            try:
                await self._turn_task
            except asyncio.CancelledError:
                if self._closing:
                    raise
                self.bus.publish("turn.finished", {"turn_id": turn_id, "interrupted": True})
            finally:
                self._turn_task = None
                self._cancel_pending("interrupted")
                self._idle_since = asyncio.get_running_loop().time()
                self._publish_status()
                # Trigger 2: a job that settled while this turn was busy left a
                # pending digest the settle-time check had to skip. Re-check now
                # that the worker is idle. Guard on _closing so teardown never
                # enqueues a turn into a worker being cancelled.
                if not self._closing:
                    self._wake.maybe_wake()

    def _cancel_pending(self, reason: str) -> None:
        """Clear asks left behind by an interrupted turn (a clean turn leaves
        none — each ask is popped where it is awaited)."""
        for ask in list(self._pending.values()):
            if not ask.future.done():
                ask.future.cancel()
            self.bus.publish("ask.resolved", {"id": ask.id, "cancelled": True, "reason": reason})
        self._pending.clear()

    async def _turn_body(self, turn_id: str, prompt: str, attachments, trigger: str) -> TurnOutcome:
        self.bus.publish("turn.started", {"turn_id": turn_id, "prompt": prompt, "trigger": trigger})
        self._publish_status()

        async def handler(ctx, events):
            # ctx.usage is the run's live running total (each agent.run round
            # gets its own, cumulative for that round). It moves once per model
            # response rather than per delta, so publishing on change keeps the
            # bus at ~one turn.usage per request instead of one per event.
            last_total = None
            async for event in events:
                total = getattr(getattr(ctx, "usage", None), "total_tokens", 0) or 0
                if total != last_total:
                    last_total = total
                    self.bus.publish("turn.usage", {"turn_id": turn_id, "total_tokens": total})
                obj = event_to_dict(event)
                if obj is None:
                    continue
                wire_type = STREAM_EVENT_TYPES.get(obj.pop("type"))
                if wire_type is not None:
                    self.bus.publish(wire_type, obj)

        return await self.harness.run_turn(
            prompt, event_stream_handler=handler, attachments=attachments
        )

    def _publish_finished(self, turn_id: str, outcome: TurnOutcome) -> None:
        # `turn.finished.output` is a wire string: the CLI preset never configures
        # structured output, so result is always the turn's text. Coalesce anyway
        # rather than emit a null.
        self.bus.publish(
            "turn.finished",
            {
                "turn_id": turn_id,
                "output": outcome.result or "",
                "usage": usage_summary(self.harness.session.usage, self.harness.model_id),
            },
        )

    async def _run_one_turn(
        self, turn_id: str, prompt: str, attachments, trigger: str = "user"
    ) -> None:
        try:
            outcome = await self._turn_body(turn_id, prompt, attachments, trigger)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # surface, don't crash the worker
            detail = format_provider_error(exc) or f"{type(exc).__name__}: {exc}"
            logger.warning("turn %s failed: %s", turn_id, detail, exc_info=True)
            self.bus.publish("turn.error", {"turn_id": turn_id, "error": detail})
            return
        self._publish_finished(turn_id, outcome)

    async def run_turn(
        self, prompt: str, attachments: list[tuple[bytes, str]] | None = None
    ) -> TurnOutcome:
        """Provisional (phase 3a): drive ONE turn synchronously for the
        in-process TUI, which still awaits turn completion. Bypasses the
        queue. Publishes the same vocabulary as the queued path. Raises on
        provider error (the TUI's error arms own the UX) — unlike the queued
        path, which swallows into turn.error. 3b removes this when turn
        completion inverts onto the host."""
        if self._turn_task is not None:
            raise RuntimeError("a turn is already in flight")
        loop = asyncio.get_running_loop()
        turn_id = secrets.token_hex(8)
        self._turn_task = loop.create_task(self._turn_body(turn_id, prompt, attachments, "user"))
        try:
            try:
                outcome = await self._turn_task
            except asyncio.CancelledError:
                if not self._closing:
                    self.bus.publish("turn.finished", {"turn_id": turn_id, "interrupted": True})
                raise
            except Exception as exc:  # publish for event consumers, then surface
                detail = format_provider_error(exc) or f"{type(exc).__name__}: {exc}"
                logger.warning("turn %s failed: %s", turn_id, detail, exc_info=True)
                self.bus.publish("turn.error", {"turn_id": turn_id, "error": detail})
                raise
        finally:
            self._turn_task = None
            self._cancel_pending("interrupted")
            self._idle_since = loop.time()
            self._publish_status()
            if not self._closing:
                self._wake.maybe_wake()
        self._publish_finished(turn_id, outcome)
        return outcome

    # ---------------------------------------------------------- teardown --
    async def aclose(self) -> None:
        """Interrupt anything running, then run the same guarded teardown the
        headless CLI does (autoname, final persist, session_end, aclose)."""
        self._closing = True
        # The whole teardown runs inside try/finally so the release below is
        # unconditional: `await self._worker` only suppresses CancelledError, and
        # the worker's own finally block can raise. Without this, such an escape
        # would leave the claim held by an fd nothing references — the host is
        # already popped from the supervisor — and 409 the session for the
        # daemon's whole life, with no host left to close and nothing to retry.
        try:
            if self._turn_task is not None:
                self._turn_task.cancel()
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            for label, step in (
                ("wait_autoname", self.harness.session.wait_autoname),
                ("finalize_active_time", self.harness.session.finalize_active_time),
                ("persist", lambda: self.harness.session.persist(force=True)),
            ):
                try:
                    result = step()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:  # noqa: BLE001 - teardown is best-effort
                    logger.warning("host teardown step %s failed: %s", label, exc, exc_info=True)
            for label, coro_fn in (
                ("session_end", lambda: self.harness.session_end("exit")),
                ("aclose", self.harness.aclose),
            ):
                try:
                    await coro_fn()
                except Exception as exc:  # noqa: BLE001 - teardown is best-effort
                    logger.warning("host teardown step %s failed: %s", label, exc, exc_info=True)
        finally:
            # Last, so ownership outlives every write above: the final persist
            # must complete while we still hold the session. Nothing is
            # swallowed here — an escaping exception still propagates, but only
            # after the session is free again.
            if self._claim is not None:
                self._claim.release()
                self._claim = None
