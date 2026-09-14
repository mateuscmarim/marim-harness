"""Codex-side sub-agents as first-class ``spawn_agent`` cards.

Codex can spawn its own agents with its *collab* tools (``spawnAgent``,
``sendInput``, ``wait``, ``closeAgent``, ...). Each spawned agent is a
separate app-server thread whose traffic the server would otherwise drop as
"unknown thread". This module is the codex counterpart of
``subagents/cli_demux.py`` (the claude-cli demux): it sits between
``turn.turn_events`` and the consumer and turns the parent thread's collab
items plus the adopted children's traffic into the exact shapes marim's
sub-agents screen already renders for native spawns —

- a ``spawnAgent`` becomes a synthesized ``spawn_agent`` ``ActivityStart`` on
  the stream the spawn happened in (the parent's, or a child's for a nested
  spawn). ``activity_events`` turns it into a ``spawn_agent``
  ``FunctionToolCallEvent``, the renderer's sinks build a card keyed by the
  item id, and the activity ledger records the call;
- the child's thread is *adopted* (``CodexServer.adopt_thread``): its
  notifications land on the parent's queue and are translated here with the
  child's own ``ItemTranslator``, then routed to the card's stream as
  ``Routed(stream_id, item, usage, model)``;
- collab follow-ups (``sendInput``, ``wait``, ...) and ``subAgentActivity``
  pings become notices on the card, never cards of their own;
- the card settles (a ``spawn_agent`` ``ActivityEnd``) when the agent reaches
  a terminal state in ``agentsStates`` or its activity ping says so, with the
  child's last message as the result.

Children outlive a parent turn (Codex keeps spawned agents alive across
turns; ``wait``/``sendInput`` can come later), so the router lives on the
model per parent thread, not on ``TurnState``. Turn boundaries are handled
in the persisted ledger only: ``seal_open`` closes every still-open card
*ledger-only* at the end of a turn (the persisted response never carries an
unmatched ``spawn_agent`` call, while the live card keeps streaming), and the
first traffic in a later turn re-opens it ledger-only with ``resumed: true``
so that turn's history is self-consistent too. Pure translation, no I/O.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic_ai.usage import RunUsage

from .server import CodexServer, ThreadHandle, thread_id_for
from .transcript import ItemTranscript
from .translate import (
    ActivityEnd,
    ActivityStart,
    AgentPing,
    CollabCall,
    CollabDone,
    ItemTranslator,
    Notice,
    TextDelta,
    TurnDone,
    TurnFailure,
    UsageUpdate,
)
from .turn import delta_since

logger = logging.getLogger(__name__)

SPAWN_TOOL = "spawn_agent"
BACKEND = "codex-cli"
SPAWN_AGENT = "spawnAgent"
DETACHED_RESULT = "running (detached; continues next turn)"
CLOSED_RESULT = "still running when the spawn finished (thread closed)"

# ``CollabAgentStatus`` values after which the agent has nothing more to say
# for now (its card settles); the subset that reads as a failure; and the
# subset after which the thread is gone for good (its handle is released).
_TERMINAL = frozenset({"completed", "errored", "shutdown", "interrupted", "notFound"})
_FAILED = frozenset({"errored", "notFound"})
_GONE = frozenset({"shutdown", "notFound"})
# Collab tools that address an existing agent (a notice on its card).
_FOLLOW_UPS = frozenset(
    {"sendInput", "resumeAgent", "wait", "closeAgent", "sendMessage", "followupTask"}
)
# Activity pings whose kind settles the card.
_PING_SETTLES = frozenset({"completed", "interrupted"})


@dataclass(frozen=True)
class Routed:
    """A translated item bound for a child's card stream (``stream_id`` is
    the ``spawn_agent`` item id the card is keyed by). ``usage`` is the
    child's accumulated ``RunUsage`` snapshot so the card can price live;
    ``model`` is set on the child's first routed item only."""

    stream_id: str
    item: object
    usage: RunUsage | None = None
    model: str | None = None


@dataclass(frozen=True)
class LedgerOnly:
    """A parent-stream ``spawn_agent`` activity to record in the persisted
    ledger WITHOUT forwarding to the live UI: the end-of-turn seal of a card
    whose agent is still running, and the ``resumed`` re-open when that
    agent produces more in a later turn."""

    item: ActivityStart | ActivityEnd


@dataclass
class _Child:
    stream_id: str  # the spawnAgent item id == the card's tool_call_id
    container: str | None  # stream the spawn happened in (None = parent)
    args: dict
    thread_id: str | None = None
    translator: ItemTranslator = field(default_factory=ItemTranslator)
    usage: RunUsage | None = None
    baseline: dict | None = None
    requests: int = 0
    model: str | None = None
    model_sent: bool = False
    label: str | None = None  # agentNickname / agentPath, for approvals + notices
    last_text_id: str | None = None
    last_text: list[str] = field(default_factory=list)
    settled: bool = False  # the card's ActivityEnd went out
    ledger_open: bool = False  # the current turn's ledger has an open call


class CollabRouter:
    """Stateful router for one parent thread (see module docstring).

    ``adopt``/``release`` are the server hooks (``adopt_thread`` /
    ``drop_thread`` for the child id) so the router itself stays pure."""

    def __init__(
        self,
        parent_thread_id: str,
        *,
        adopt: Callable[[str], object],
        release: Callable[[str], object],
    ) -> None:
        self._parent = parent_thread_id
        self._adopt = adopt
        self._release = release
        self._by_stream: dict[str, _Child] = {}
        self._by_thread: dict[str, _Child] = {}
        # thread/started metadata seen for a thread before its spawn item
        # mapped it (only possible when the thread was adopted first).
        self._stash: dict[str, str] = {}

    @property
    def open_children(self) -> frozenset[str]:
        """Thread ids of the adopted children whose cards are not settled."""
        return frozenset(tid for tid, child in self._by_thread.items() if not child.settled)

    def label_for(self, thread_id: str) -> str | None:
        """The approval-panel prefix for a child's request (``agent <name>``),
        None for the parent's own. Any other thread reaching the parent's
        broker is an adopted child — one whose spawn item this router has
        not dequeued yet gets the bare ``agent`` (the server adopts on
        ``thread/started``, ahead of the consumer)."""
        if thread_id == self._parent:
            return None
        child = self._by_thread.get(thread_id)
        name = child.label if child is not None else self._stash.get(thread_id)
        return f"agent {name}" if name else "agent"

    # --- entry points -----------------------------------------------------------
    def route(self, method: str, params: dict) -> list[object] | None:
        """Route one raw notification off the shared queue. Returns None when
        it is the parent's own (the caller translates it as before); else the
        routed items for a child's traffic (possibly empty: consumed)."""
        tid = thread_id_for(method, params)
        if tid is None or tid == self._parent:
            return None
        child = self._by_thread.get(tid)
        if child is None:
            if method == "thread/started":
                self._stash_thread(tid, params)
            else:
                logger.debug("codex collab: %s for unmapped thread %s dropped", method, tid)
            return []
        if method == "thread/started":
            self._note_thread(child, params.get("thread") or {})
            return []
        out: list[object] = []
        for item in child.translator.translate(method, params):
            out.extend(self._child_item(child, item))
        return out

    def route_item(self, item: object) -> list[object]:
        """A translated PARENT-stream item → what the caller yields: the item
        itself, or what a collab item turns into."""
        if isinstance(item, (CollabCall, CollabDone, AgentPing)):
            return self._collab(item, None)
        return [item]

    def seal_open(self) -> list[LedgerOnly]:
        """The parent turn ended: close every still-open top-level card in
        the ledger only (see module docstring)."""
        out: list[LedgerOnly] = []
        for child in self._by_stream.values():
            if child.container is None and child.ledger_open:
                child.ledger_open = False
                out.append(LedgerOnly(ActivityEnd(child.stream_id, DETACHED_RESULT, False)))
        return out

    def close_open(self, result: str = CLOSED_RESULT) -> list[object]:
        """The thread is going away (a spawn's one turn ended; its children
        die with it): settle every still-open card for real — a bare
        ``ActivityEnd`` for a top-level child, ``Routed`` to its container
        for a nested one — newest first, so a grandchild closes before the
        child whose stream it lives on."""
        out: list[object] = []
        for child in reversed(list(self._by_stream.values())):
            if not child.settled:
                out.extend(self._settle(child, result, False))
        return out

    # --- child traffic ----------------------------------------------------------
    def _child_item(self, child: _Child, item: object) -> list[object]:
        if isinstance(item, (CollabCall, CollabDone, AgentPing)):
            return self._collab(item, child.stream_id)
        if isinstance(item, UsageUpdate):
            self._accumulate_usage(child, item)
            return self._emit(child, item)
        if isinstance(item, TurnDone):
            if item.status == "failed":
                return self._emit(child, Notice(f"agent turn failed: {item.error or 'error'}"))
            return []  # a child turn ending is not the agent finishing
        if isinstance(item, TurnFailure):
            return self._emit(child, Notice(item.message))
        if isinstance(item, TextDelta):
            self._note_text(child, item)
        return self._emit(child, item)

    def _emit(self, child: _Child, item: object) -> list[object]:
        """Bind ``item`` to the child's stream, re-opening the ledger entry
        first when this is the child's first traffic of a later turn."""
        out: list[object] = list(self._touch(child))
        model = None
        if not child.model_sent:
            child.model_sent = True
            model = f"{BACKEND}:{child.model or 'default'}"
        out.append(Routed(child.stream_id, item, usage=child.usage, model=model))
        return out

    def _touch(self, child: _Child) -> list[LedgerOnly]:
        if child.container is not None or child.ledger_open or child.settled:
            return []
        child.ledger_open = True
        args = {**child.args, "resumed": True}
        return [LedgerOnly(ActivityStart(child.stream_id, SPAWN_TOOL, args))]

    def _note_text(self, child: _Child, item: TextDelta) -> None:
        if child.last_text_id != item.item_id:
            child.last_text_id = item.item_id
            child.last_text = []
        child.last_text.append(item.delta)

    def _accumulate_usage(self, child: _Child, item: UsageUpdate) -> None:
        """The child's cumulative usage as a ``RunUsage``. A spawned thread
        starts at zero, but a ``resumeAgent`` can reopen one with history:
        seed the baseline from the first update's ``total − last`` exactly
        like ``turn._seeded_baseline`` does for a resumed parent."""
        if child.baseline is None:
            child.baseline = delta_since(item.total, item.last) if item.last else {}
        delta = delta_since(item.total, child.baseline)
        child.requests += 1
        child.usage = RunUsage(
            requests=child.requests,
            input_tokens=delta["inputTokens"],
            output_tokens=delta["outputTokens"],
            cache_read_tokens=delta["cachedInputTokens"],
        )

    # --- collab items (parent or child stream) ------------------------------------
    def _collab(self, item: object, container: str | None) -> list[object]:
        if isinstance(item, CollabCall):
            return self._call(item, container)
        if isinstance(item, CollabDone):
            return self._done(item)
        assert isinstance(item, AgentPing)
        return self._ping(item)

    def _call(self, item: CollabCall, container: str | None) -> list[object]:
        if item.tool == SPAWN_AGENT:
            return self._spawn(item, container)
        if item.tool not in _FOLLOW_UPS:
            return []  # listAgents and unknown tools: nothing to show
        out: list[object] = []
        text = f"{item.tool}: {item.prompt}" if item.prompt else item.tool
        for child in self._receivers(item.receivers):
            if child.settled:
                child.settled = False  # the parent is putting it back to work
            out.extend(self._emit(child, Notice(text)))
        return out

    def _spawn(self, item: CollabCall, container: str | None) -> list[object]:
        args = {
            "type": "codex-agent",
            "task": item.prompt or "",
            "description": "",
            "model": item.model,
            "backend": BACKEND,
            "thread_id": item.receivers[0] if item.receivers else None,
        }
        child = _Child(stream_id=item.item_id, container=container, args=args, model=item.model)
        self._by_stream[item.item_id] = child
        child.ledger_open = container is None
        self._bind(child, item.receivers)
        return self._on_container(container, ActivityStart(item.item_id, SPAWN_TOOL, args))

    def _done(self, item: CollabDone) -> list[object]:
        out: list[object] = []
        if item.tool == SPAWN_AGENT:
            child = self._by_stream.get(item.item_id)
            if child is not None:
                self._bind(child, item.receivers)
                if item.status == "failed" and child.thread_id is None:
                    out.extend(self._settle(child, item.result or "spawn failed", True))
        elif item.is_error:
            text = f"{item.tool} failed: {item.result}" if item.result else f"{item.tool} failed"
            for child in self._receivers(item.receivers):
                out.extend(self._emit(child, Notice(text)))
        for tid, state in item.states.items():
            out.extend(self._fold_state(str(tid), state, item.result))
        return out

    def _fold_state(self, tid: str, state: object, fallback: str) -> list[object]:
        child = self._by_thread.get(tid)
        status = str(state.get("status") or "") if isinstance(state, dict) else ""
        if child is None or status not in _TERMINAL:
            return []
        out: list[object] = []
        if not child.settled:
            message = state.get("message") if isinstance(state, dict) else None
            content = "".join(child.last_text) or (str(message) if message else "") or fallback
            out.extend(self._settle(child, content, status in _FAILED))
        if status in _GONE:
            self._forget(child)
        return out

    def _ping(self, item: AgentPing) -> list[object]:
        child = self._by_thread.get(item.thread_id)
        if child is None:
            return []
        if item.path and child.label is None:
            child.label = item.path.rsplit("/", 1)[-1] or item.path
        out = self._emit(child, Notice(f"agent {item.path or child.label} {item.kind}"))
        if item.kind in _PING_SETTLES and not child.settled:
            content = "".join(child.last_text) or f"agent {item.kind}"
            out.extend(self._settle(child, content, item.kind == "interrupted"))
        return out

    # --- bookkeeping ------------------------------------------------------------
    def _receivers(self, receivers: tuple[str, ...]) -> list[_Child]:
        return [c for tid in receivers if (c := self._by_thread.get(tid)) is not None]

    def _bind(self, child: _Child, receivers: tuple[str, ...]) -> None:
        """Map (and adopt) the spawned thread once its id is known — on the
        ``item/started`` when Codex already reports it there, else on the
        ``item/completed``."""
        if child.thread_id is not None or not receivers:
            return
        tid = receivers[0]
        child.thread_id = tid
        child.args["thread_id"] = tid
        self._by_thread[tid] = child
        if tid in self._stash:
            child.label = self._stash.pop(tid)
        self._adopt(tid)
        for extra in receivers[1:]:
            logger.debug("codex collab: extra receiver %s of %s not tracked", extra, tid)

    def _settle(self, child: _Child, content: str, is_error: bool) -> list[object]:
        child.settled = True
        child.ledger_open = False
        return self._on_container(child.container, ActivityEnd(child.stream_id, content, is_error))

    def _forget(self, child: _Child) -> None:
        if child.thread_id is not None:
            self._by_thread.pop(child.thread_id, None)
            self._release(child.thread_id)

    def _on_container(self, container: str | None, item: object) -> list[object]:
        """Deliver a synthesized spawn card item to the stream the spawn
        happened in: the parent's (a bare item) or a child's (routed)."""
        if container is None:
            return [item]
        holder = self._by_stream.get(container)
        if holder is None:
            return []
        return self._emit(holder, item)

    def _stash_thread(self, tid: str, params: dict) -> None:
        thread = params.get("thread") or {}
        if str(thread.get("parentThreadId") or "") == self._parent:
            self._stash[tid] = _thread_label(thread)

    def _note_thread(self, child: _Child, thread: dict) -> None:
        child.label = _thread_label(thread) or child.label


def router_for(server: CodexServer, handle: ThreadHandle) -> CollabRouter:
    """A router for ``handle`` whose adopt/release hooks register the children
    on ``server`` under it (so dropping the parent drops them too)."""
    return CollabRouter(
        handle.thread_id,
        adopt=lambda cid: server.adopt_thread(handle, cid),
        release=server.release_thread,
    )


@dataclass(frozen=True)
class ChildSinks:
    """Where a child's routed traffic goes — the sub-agents screen's four
    entry points (``UiCallbacks.on_subagent_*``), each optional (headless)."""

    on_event: Callable[[str, object, object], Awaitable[None]] | None = None
    on_model: Callable[[str, str], Awaitable[None]] | None = None
    on_notice: Callable[[str, str], Awaitable[None]] | None = None
    on_usage: Callable[[str, object], Awaitable[None]] | None = None


class ChildStreams:
    """Delivers ``Routed`` items: one ``ItemTranscript`` per card stream folds
    the child's items into the pydantic-ai events the screen renders (and a
    message list a spawn can persist as the child's sidecar transcript);
    notices, usage and the model label go to their own sinks."""

    def __init__(self, sinks: ChildSinks) -> None:
        self._sinks = sinks
        self._transcripts: dict[str, ItemTranscript] = {}

    @property
    def transcripts(self) -> dict[str, list]:
        """Each child's messages so far, keyed by its card's stream id."""
        return {sid: tx.messages for sid, tx in self._transcripts.items()}

    async def deliver(self, routed: Routed) -> None:
        sid, item, sinks = routed.stream_id, routed.item, self._sinks
        if routed.model and sinks.on_model is not None:
            await sinks.on_model(sid, routed.model)
        if isinstance(item, Notice):
            if sinks.on_notice is not None:
                await sinks.on_notice(sid, item.message)
            return
        if isinstance(item, UsageUpdate):
            if sinks.on_usage is not None and routed.usage is not None:
                await sinks.on_usage(sid, routed.usage)
            return
        tx = self._transcripts.setdefault(sid, ItemTranscript())
        for event in tx.feed(item):
            if sinks.on_event is not None:
                await sinks.on_event(sid, event, routed.usage)


def _thread_label(thread: dict) -> str:
    return str(thread.get("agentNickname") or thread.get("agentRole") or "")
