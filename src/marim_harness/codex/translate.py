"""Pure translation of app-server notifications into neutral events.

The model layer (config/codex_cli_model.py) turns ``TextDelta``/``ThinkingDelta``
into pydantic-ai stream events and ``ActivityStart``/``ActivityEnd`` into
display-only tool cards. Tool names are harness names (``bash``,
``apply_patch``, ``web_search``) so the TUI renders them like native calls.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import uuid4

from ..config.lifecycle import BackendNotice


@dataclass(frozen=True)
class TextDelta:
    item_id: str
    delta: str


@dataclass(frozen=True)
class ThinkingDelta:
    item_id: str
    delta: str


@dataclass(frozen=True)
class ActivityStart:
    item_id: str
    tool_name: str
    args: dict


@dataclass(frozen=True)
class ActivityEnd:
    item_id: str
    content: str
    is_error: bool


@dataclass(frozen=True)
class UsageUpdate:
    """``thread/tokenUsage/updated``: ``total`` is the thread's cumulative
    usage, ``last`` the most recent model response's own usage. Both are
    required on the wire; ``last`` is what lets a resumed thread seed its
    usage baseline (see ``turn.finish_turn``) and what the context report
    reads its prompt size from. ``model_context_window`` is the model's
    window when the server knows it (nullable on the wire)."""

    total: dict
    last: dict = field(default_factory=dict)
    model_context_window: int | None = None


@dataclass(frozen=True)
class TurnDone:
    status: str
    error: str | None


@dataclass(frozen=True)
class TurnFailure:
    message: str
    will_retry: bool


@dataclass(frozen=True)
class Notice:
    message: str
    kind: str = "warning"
    severity: str = "warning"
    id: str = field(default_factory=lambda: str(uuid4()), compare=False)
    data: dict = field(default_factory=dict)
    transient: bool = field(default=False, compare=False)

    def normalized(self) -> BackendNotice:
        return BackendNotice(
            self.message, "codex-cli", self.kind, self.severity, self.id, self.data
        )


# --- collab (Codex-side sub-agents) ------------------------------------------
# Codex's collab tools (``spawnAgent``, ``sendInput``, ``wait``, ``closeAgent``,
# ...) show up on the PARENT thread as ``collabAgentToolCall`` items, and the
# spawned agents' lifecycle as ``subAgentActivity`` pings. They are not tool
# cards: ``codex/collab.py`` turns them into ``spawn_agent`` cards, notices
# and child-thread adoption. The translator only names the wire shapes.
# Every field is optional on the way in (version-tolerant): a missing
# ``agentsStates`` is ``{}``, an unknown ``tool`` passes through as its string.


@dataclass(frozen=True)
class CollabCall:
    """``collabAgentToolCall`` ``item/started``."""

    item_id: str
    tool: str  # spawnAgent | sendInput | resumeAgent | wait | closeAgent | ...
    receivers: tuple[str, ...]  # receiverThreadIds (a spawn: the new thread)
    prompt: str | None
    model: str | None
    effort: str | None
    states: dict  # agentsStates: {thread id: {"status", "message"?}}


@dataclass(frozen=True)
class CollabDone:
    """``collabAgentToolCall`` ``item/completed``."""

    item_id: str
    tool: str
    receivers: tuple[str, ...]
    status: str  # completed | failed | interrupted
    states: dict
    result: str
    is_error: bool


@dataclass(frozen=True)
class AgentPing:
    """``subAgentActivity``: a lifecycle ping for one spawned agent."""

    item_id: str
    thread_id: str  # agentThreadId
    path: str  # agentPath, e.g. "/root/reviewer"
    kind: str  # started | interacted | interrupted | completed


_TOOL_NAMES = {
    "commandExecution": "bash",
    "fileChange": "apply_patch",
    "webSearch": "web_search",
    "plan": "update_plan",
}


def _command_text(command: object) -> str:
    if isinstance(command, list):
        return " ".join(shlex.quote(str(c)) for c in command)
    return str(command or "")


def tool_name_for(item: dict) -> str | None:
    kind = item.get("type")
    if kind == "mcpToolCall":
        return f"{item.get('server', 'mcp')}.{item.get('tool', 'tool')}"
    return _TOOL_NAMES.get(str(kind))


def _command_execution_args(item: dict) -> dict:
    return {"command": _command_text(item.get("command")), "cwd": item.get("cwd")}


def _mcp_tool_call_args(item: dict) -> dict:
    args = item.get("arguments")
    return dict(args) if isinstance(args, dict) else {"arguments": args}


def _web_search_args(item: dict) -> dict:
    return {"query": item.get("query", "")}


def _plan_args(item: dict) -> dict:
    return {"text": item.get("text", "")}


def _changes(item: dict) -> list[tuple[str, dict]]:
    out = []
    for n, change in enumerate(item.get("changes") or []):
        kind = change.get("kind") or {}
        args = {
            "path": change.get("path"),
            "kind": kind.get("type"),
            "diff": change.get("diff", ""),
        }
        out.append((f"{item.get('id')}:{n}", args))
    return out


def _file_change_args(item: dict) -> dict:
    """A single args dict for a fileChange approval prompt or activity card:
    the changed path(s) plus kind and diff, so an approval panel never renders
    a fileChange blind. One change flattens to {"path", "kind", "diff"}; more
    than one is {"paths": [...], "changes": [...]} so the panel can still show
    every path even though a single ToolCallPart carries one args dict."""
    changes = [args for _cid, args in _changes(item)]
    if not changes:
        return {}
    if len(changes) == 1:
        return changes[0]
    return {"paths": [c["path"] for c in changes], "changes": changes}


# Dispatch table for `args_for`, keyed by ThreadItem `type` — a dict beats an
# if/elif chain past ruff's PLR0911 (too many returns) ceiling, and reads as
# "one handler per item kind" rather than a wall of comparisons.
_ARGS_BY_KIND: dict[str, Callable[[dict], dict]] = {
    "commandExecution": _command_execution_args,
    "mcpToolCall": _mcp_tool_call_args,
    "webSearch": _web_search_args,
    "plan": _plan_args,
    "fileChange": _file_change_args,
}


def args_for(item: dict) -> dict:
    handler = _ARGS_BY_KIND.get(str(item.get("type")))
    return handler(item) if handler is not None else {}


def _result_text(item: dict) -> tuple[str, bool]:
    error = item.get("error")
    if error:
        msg = error.get("message") if isinstance(error, dict) else str(error)
        return str(msg), True
    result = item.get("result")
    if result is None:
        return "", item.get("status") == "failed"
    text = result if isinstance(result, str) else json.dumps(result)
    return text, item.get("status") == "failed"


def _receivers(item: dict) -> tuple[str, ...]:
    raw = item.get("receiverThreadIds")
    return tuple(str(r) for r in raw) if isinstance(raw, list) else ()


def _states(item: dict) -> dict:
    states = item.get("agentsStates")
    return dict(states) if isinstance(states, dict) else {}


def _opt_str(item: dict, key: str) -> str | None:
    value = item.get(key)
    return str(value) if value is not None else None


def collab_call(item: dict) -> CollabCall:
    return CollabCall(
        item_id=str(item.get("id")),
        tool=str(item.get("tool") or ""),
        receivers=_receivers(item),
        prompt=_opt_str(item, "prompt"),
        model=_opt_str(item, "model"),
        effort=_opt_str(item, "reasoningEffort"),
        states=_states(item),
    )


def collab_done(item: dict) -> CollabDone:
    text, is_error = _result_text(item)
    return CollabDone(
        item_id=str(item.get("id")),
        tool=str(item.get("tool") or ""),
        receivers=_receivers(item),
        status=str(item.get("status") or "completed"),
        states=_states(item),
        result=text,
        is_error=is_error,
    )


def agent_ping(item: dict) -> AgentPing:
    return AgentPing(
        item_id=str(item.get("id")),
        thread_id=str(item.get("agentThreadId") or ""),
        path=str(item.get("agentPath") or ""),
        kind=str(item.get("kind") or ""),
    )


class ItemTranslator:
    def __init__(self) -> None:
        self._streamed: set[str] = set()
        self._output: dict[str, list[str]] = {}
        from .lifecycle import CodexLifecycle

        self.lifecycle = CodexLifecycle()

    def translate(self, method: str, params: dict) -> list[object]:
        if not isinstance(params, dict):
            return []
        if method in ("item/started", "item/completed") and not isinstance(
            params.get("item"), dict
        ):
            return []
        lifecycle = self.lifecycle.translate(method, params)
        if lifecycle is not None:
            return lifecycle
        handler = _METHODS.get(method)
        return handler(self, params) if handler is not None else []

    # --- deltas -------------------------------------------------------------
    def _text_delta(self, params: dict) -> list[object]:
        item_id = str(params.get("itemId"))
        self._streamed.add(item_id)
        return [TextDelta(item_id, str(params.get("delta", "")))]

    def _thinking_delta(self, params: dict) -> list[object]:
        item_id = str(params.get("itemId"))
        self._streamed.add(item_id)
        return [ThinkingDelta(item_id, str(params.get("delta", "")))]

    def _output_delta(self, params: dict) -> list[object]:
        self._output.setdefault(str(params.get("itemId")), []).append(str(params.get("delta", "")))
        return []

    # --- items --------------------------------------------------------------
    def _started(self, params: dict) -> list[object]:
        # The item kinds with a shape of their own dispatch via
        # `_STARTED_BY_KIND` (below the class, like `_COMPLETED_BY_KIND`);
        # everything else is a plain tool card or nothing.
        item = params.get("item") or {}
        special = _STARTED_BY_KIND.get(str(item.get("type")))
        if special is not None:
            return special(item)
        name = tool_name_for(item)
        if name is None:
            return []
        return [ActivityStart(str(item.get("id")), name, args_for(item))]

    def _completed(self, params: dict) -> list[object]:
        # Dispatch by item `type` via `_COMPLETED_BY_KIND` (defined below the
        # class, mirroring `_METHODS`) rather than an if/elif chain past
        # ruff's PLR0911 (too many returns) ceiling.
        item = params.get("item") or {}
        kind = item.get("type")
        item_id = str(item.get("id"))
        handler = _COMPLETED_BY_KIND.get(str(kind))
        return handler(self, item, item_id) if handler is not None else []

    def _completed_agent_message(self, item: dict, item_id: str) -> list[object]:
        if item_id in self._streamed:
            return []
        return [TextDelta(item_id, str(item.get("text", "")))]

    def _completed_reasoning(self, item: dict, item_id: str) -> list[object]:
        if item_id in self._streamed:
            return []
        text = "\n".join(item.get("summary") or item.get("content") or [])
        return [ThinkingDelta(item_id, text)] if text else []

    def _completed_command_execution(self, item: dict, item_id: str) -> list[object]:
        return [self._command_end(item, item_id)]

    def _completed_file_change(self, item: dict, item_id: str) -> list[object]:
        failed = item.get("status") != "completed"
        return [ActivityEnd(cid, str(args["diff"]), failed) for cid, args in _changes(item)]

    def _completed_result(self, item: dict, item_id: str) -> list[object]:
        text, is_error = _result_text(item)
        return [ActivityEnd(item_id, text, is_error)]

    def _completed_collab(self, item: dict, item_id: str) -> list[object]:
        return [collab_done(item)]

    def _completed_agent_ping(self, item: dict, item_id: str) -> list[object]:
        # A ping is complete on arrival: `item/completed` repeats what
        # `item/started` said (the router keys on the started one).
        return []

    def _command_end(self, item: dict, item_id: str) -> ActivityEnd:
        buffered = "".join(self._output.pop(item_id, []))
        content = buffered or str(item.get("aggregatedOutput") or "")
        status = item.get("status")
        exit_code = item.get("exitCode")
        if status != "completed" and exit_code not in (None, 0):
            content = f"{content}\n[exit {exit_code}]".strip()
        return ActivityEnd(item_id, content, status != "completed")

    # --- turn level ---------------------------------------------------------
    def _usage(self, params: dict) -> list[object]:
        usage = params.get("tokenUsage") or {}
        window = usage.get("modelContextWindow")
        return [
            UsageUpdate(
                dict(usage.get("total") or {}),
                dict(usage.get("last") or {}),
                window if isinstance(window, int) and window > 0 else None,
            )
        ]

    def _turn_completed(self, params: dict) -> list[object]:
        turn = params.get("turn") or {}
        error = turn.get("error") or {}
        return [
            TurnDone(str(turn.get("status", "completed")), error.get("message") if error else None)
        ]

    def _error(self, params: dict) -> list[object]:
        error = params.get("error") or {}
        return [
            TurnFailure(str(error.get("message", "codex error")), bool(params.get("willRetry")))
        ]

    def _warning(self, params: dict) -> list[object]:
        return [Notice(str(params.get("message", "")))]


# Dispatch table for `_completed`, keyed by ThreadItem `type` — same rationale
# as `_ARGS_BY_KIND`: a dict beats an if/elif/return chain past ruff's
# PLR0911 ceiling. Handlers are unbound methods, called as `handler(self, ...)`
# like `_METHODS` below.
def _started_plan(item: dict) -> list[object]:
    item_id, text = str(item.get("id")), str(item.get("text", ""))
    start = ActivityStart(item_id, "update_plan", {"text": text})
    return [start, ActivityEnd(item_id, text, False)]


_STARTED_BY_KIND: dict[str, Callable[[dict], list[object]]] = {
    "fileChange": lambda item: [
        ActivityStart(cid, "apply_patch", args) for cid, args in _changes(item)
    ],
    "plan": _started_plan,
    "collabAgentToolCall": lambda item: [collab_call(item)],
    "subAgentActivity": lambda item: [agent_ping(item)],
}

_COMPLETED_BY_KIND: dict[str, Callable[[ItemTranslator, dict, str], list[object]]] = {
    "agentMessage": ItemTranslator._completed_agent_message,
    "reasoning": ItemTranslator._completed_reasoning,
    "commandExecution": ItemTranslator._completed_command_execution,
    "fileChange": ItemTranslator._completed_file_change,
    "mcpToolCall": ItemTranslator._completed_result,
    "webSearch": ItemTranslator._completed_result,
    "collabAgentToolCall": ItemTranslator._completed_collab,
    "subAgentActivity": ItemTranslator._completed_agent_ping,
}


_METHODS = {
    "item/agentMessage/delta": ItemTranslator._text_delta,
    "item/reasoning/textDelta": ItemTranslator._thinking_delta,
    "item/reasoning/summaryTextDelta": ItemTranslator._thinking_delta,
    "item/commandExecution/outputDelta": ItemTranslator._output_delta,
    "item/started": ItemTranslator._started,
    "item/completed": ItemTranslator._completed,
    "thread/tokenUsage/updated": ItemTranslator._usage,
    "turn/completed": ItemTranslator._turn_completed,
    "error": ItemTranslator._error,
    "warning": ItemTranslator._warning,
}
