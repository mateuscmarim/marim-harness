"""Demultiplex Claude Code stream-json into per-sub-agent event streams.

A ``claude`` process (whether a ``backend: claude-cli`` spawn or the claude-cli
main-loop provider) can spawn its own sub-agents with Claude Code's Agent tool
(``Task`` on older CLIs). Its stream-json interleaves the children's traffic
with the parent's, tagging every child line with a top-level
``parent_tool_use_id``. This module turns that interleaved stream into the
exact shapes marim's TUI already renders for native spawns, so Claude-side
sub-agents get first-class cards in the sub-agents screen:

- an Agent/Task ``tool_use`` becomes a synthesized ``spawn_agent``
  ``FunctionToolCallEvent`` — the renderer's sinks claim those and build a live
  card keyed by the tool_call_id, which is exactly the id children are tagged
  with, so no renderer change is needed;
- child messages are translated per child (one ``CliStreamTranslator`` each)
  and routed to their card's stream, with a live accumulated ``RunUsage``
  (deduped by ``message.id`` — stream-json repeats the same usage on every
  event of one API message) and the child's reported model (surfaced once);
- completion is either a ``task_notification`` system event (async spawns —
  the spawn's immediate ``tool_result`` is launch metadata and is suppressed)
  or the spawn's real ``tool_result`` (legacy sync Task); both become a
  ``spawn_agent`` ``ToolReturnPart`` that settles the card.

Consumers call :meth:`CliSubagentDemux.route` per parsed object and get back
``(events, passthrough)``: the routed events to deliver, plus the object they
should still feed their own main-stream pipeline — the original when nothing
here applied, a stripped copy when spawn blocks were claimed out of a mixed
message, or ``None`` when fully claimed. Pure translation, no I/O; imported
lazily by its consumers (``cli_backend`` imports this module inside ``run()``
because this module imports from ``cli_backend``)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.usage import RunUsage

from .cli_backend import CliStreamTranslator, _flatten_tool_result, synth_usage

# Claude Code's sub-agent spawn tool: "Agent" since CLI 2.1.x, "Task" before.
SPAWN_TOOL_NAMES = frozenset({"Agent", "Task"})


@dataclass
class RoutedEvent:
    """One translated event plus its destination stream.

    ``stream_id`` None means the *containing* stream (the caller's own main
    stream); otherwise it is the spawn's tool_use id — which is also the card's
    stream id. ``usage`` is the child stream's accumulated RunUsage snapshot
    (child events only) so the card can price live; ``model`` is set on the
    first event after the child reports which model it runs on."""

    stream_id: str | None
    event: object
    usage: RunUsage | None = None
    model: str | None = None


@dataclass
class _Spawn:
    """Lifecycle state for one Agent/Task spawn seen in the stream."""

    container: str | None  # stream its tool_use appeared in (None = main)
    async_started: bool = False  # task_started seen → its tool_result is launch noise
    finished: bool = False


class CliSubagentDemux:
    """Stateful router for one CLI process's stream (see module docstring)."""

    def __init__(self) -> None:
        self._spawns: dict[str, _Spawn] = {}
        self._translators: dict[str, CliStreamTranslator] = {}
        self._usage: dict[str, RunUsage] = {}
        self._usage_msgs: dict[str, set[str]] = {}
        self._model_sent: set[str] = set()

    def route(self, obj: dict) -> tuple[list[RoutedEvent], dict | None]:
        kind = obj.get("type")
        if kind == "system":
            return self._system(obj)
        parent = obj.get("parent_tool_use_id")
        if parent and kind in ("assistant", "user"):
            return self._child(obj, str(parent)), None
        events: list[RoutedEvent] = []
        if kind == "assistant":
            return events, self._claim_spawn_calls(obj, None, events)
        if kind == "user":
            return events, self._claim_spawn_results(obj, None, events)
        return [], obj

    # -- system lifecycle events -------------------------------------------

    def _system(self, obj: dict) -> tuple[list[RoutedEvent], dict | None]:
        subtype = obj.get("subtype")
        if subtype == "task_started":
            tid = str(obj.get("tool_use_id") or "")
            if tid:
                self._spawns.setdefault(tid, _Spawn(container=None)).async_started = True
            return [], None
        if subtype == "task_notification":
            tid = str(obj.get("tool_use_id") or "")
            status = str(obj.get("status") or "completed")
            content = str(obj.get("summary") or "") or f"(sub-agent {status})"
            return self._finish(tid, content, failed=status != "completed"), None
        if subtype == "task_updated":
            return [], None  # progress patches; the notification carries the report
        return [], obj

    def _finish(self, tid: str, content: str, *, failed: bool) -> list[RoutedEvent]:
        spawn = self._spawns.get(tid)
        if spawn is None or spawn.finished:
            return []
        spawn.finished = True
        part = ToolReturnPart(
            tool_name="spawn_agent",
            content=content,
            tool_call_id=tid,
            timestamp=datetime.now(tz=timezone.utc),
            outcome="failed" if failed else "success",
        )
        if spawn.container is not None:
            self._translator(spawn.container).record_return(part)
        return [self._routed(spawn.container, FunctionToolResultEvent(part=part))]

    # -- child streams -------------------------------------------------------

    def _child(self, obj: dict, sid: str) -> list[RoutedEvent]:
        # Defensive: a child line for a spawn whose tool_use we never saw still
        # gets a stream (its events will just find no card and no-op in the UI).
        self._spawns.setdefault(sid, _Spawn(container=None))
        events: list[RoutedEvent] = []
        model: str | None = None
        remainder: dict | None
        if obj.get("type") == "assistant":
            self._accumulate_usage(sid, obj)
            model = self._peek_model(sid, obj)
            remainder = self._claim_spawn_calls(obj, sid, events)
        else:
            remainder = self._claim_spawn_results(obj, sid, events)
        if remainder is not None:
            usage = self._usage.get(sid)
            for ev in self._translator(sid).translate(remainder):
                events.append(RoutedEvent(sid, ev, usage=usage, model=model))
                if model:
                    self._model_sent.add(sid)
                model = None
        return events

    # -- spawn claim helpers (shared by main and child streams) --------------

    def _claim_spawn_calls(
        self, obj: dict, container: str | None, out: list[RoutedEvent]
    ) -> dict | None:
        """Claim Agent/Task tool_use blocks out of an assistant message:
        register the spawn, synthesize its spawn_agent call into ``out``, and
        return the message with those blocks removed (the original when none
        matched; None when nothing else remains). Never mutates ``obj``."""
        msg = obj.get("message") or {}
        blocks = msg.get("content") or []
        kept = []
        for block in blocks:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") in SPAWN_TOOL_NAMES
            ):
                out.extend(self._spawn_call(block, container))
            else:
                kept.append(block)
        if len(kept) == len(blocks):
            return obj
        if not kept:
            return None
        return {**obj, "message": {**msg, "content": kept}}

    def _spawn_call(self, block: dict, container: str | None) -> list[RoutedEvent]:
        tid = str(block.get("id") or "")
        if not tid:
            return []
        inp = block.get("input") or {}
        spawn = self._spawns.setdefault(tid, _Spawn(container=container))
        spawn.container = container
        args = {
            "type": str(inp.get("subagent_type") or "agent"),
            "task": str(inp.get("prompt") or inp.get("description") or ""),
            "description": str(inp.get("description") or ""),
        }
        part = ToolCallPart(tool_name="spawn_agent", args=args, tool_call_id=tid)
        if container is not None:
            self._translator(container).record_call(part)
        return [self._routed(container, FunctionToolCallEvent(part=part))]

    def _claim_spawn_results(
        self, obj: dict, container: str | None, out: list[RoutedEvent]
    ) -> dict | None:
        """Claim tool_result blocks answering a known spawn. An async spawn's
        immediate tool_result is launch metadata — swallowed (the
        task_notification settles the card). A sync (legacy Task) spawn's
        tool_result IS the report — converted to the spawn_agent return."""
        msg = obj.get("message") or {}
        blocks = msg.get("content") or []
        kept = []
        for block in blocks:
            tid = (
                str(block.get("tool_use_id") or "")
                if isinstance(block, dict) and block.get("type") == "tool_result"
                else ""
            )
            spawn = self._spawns.get(tid) if tid else None
            if spawn is None:
                kept.append(block)
                continue
            if spawn.async_started:
                continue  # launch metadata — drop
            out.extend(self._finish(
                tid,
                _flatten_tool_result(block.get("content")),
                failed=bool(block.get("is_error")),
            ))
        if len(kept) == len(blocks):
            return obj
        if not kept:
            return None
        return {**obj, "message": {**msg, "content": kept}}

    # -- per-child bookkeeping ------------------------------------------------

    def _accumulate_usage(self, sid: str, obj: dict) -> None:
        """Fold one assistant message's usage into the child's running total.
        stream-json repeats the same ``message.usage`` on every event of one
        API message, so dedupe by message id; ``synth_usage`` folds the cache
        buckets into ``input_tokens`` per the harness convention."""
        msg = obj.get("message") or {}
        u, mid = msg.get("usage"), str(msg.get("id") or "")
        if not u or not mid or mid in self._usage_msgs.setdefault(sid, set()):
            return
        self._usage_msgs[sid].add(mid)
        add = synth_usage(u, 1)
        total = self._usage.get(sid, RunUsage())
        self._usage[sid] = RunUsage(
            input_tokens=total.input_tokens + add.input_tokens,
            output_tokens=total.output_tokens + add.output_tokens,
            cache_read_tokens=total.cache_read_tokens + add.cache_read_tokens,
            cache_write_tokens=total.cache_write_tokens + add.cache_write_tokens,
            requests=total.requests + add.requests,
        )

    def _peek_model(self, sid: str, obj: dict) -> str | None:
        if sid in self._model_sent:
            return None
        model = (obj.get("message") or {}).get("model")
        return str(model) if model else None

    def _translator(self, sid: str) -> CliStreamTranslator:
        return self._translators.setdefault(sid, CliStreamTranslator())

    def _routed(self, sid: str | None, event: object) -> RoutedEvent:
        usage = self._usage.get(sid) if sid is not None else None
        return RoutedEvent(sid, event, usage=usage)

    def child_transcripts(self) -> dict[str, list]:
        """Each child stream's transcript (pydantic-ai messages) keyed by its
        stream id, for sidecar persistence — so the sub-agents screen can
        replay a Claude-side child after a session resume. Empty streams are
        omitted."""
        return {
            sid: t.transcript()
            for sid, t in self._translators.items()
            if t.transcript()
        }
