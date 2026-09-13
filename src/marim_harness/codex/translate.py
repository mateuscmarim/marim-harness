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


_TOOL_NAMES = {
    "commandExecution": "bash",
    "fileChange": "apply_patch",
    "webSearch": "web_search",
    "collabAgentToolCall": "codex_agent",
    "subAgentActivity": "codex_agent",
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


def _agent_activity_args(item: dict) -> dict:
    return {
        k: item.get(k)
        for k in ("prompt", "model", "receiverThreadIds", "agentPath", "kind")
        if item.get(k) is not None
    }


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
    "collabAgentToolCall": _agent_activity_args,
    "subAgentActivity": _agent_activity_args,
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


class ItemTranslator:
    def __init__(self) -> None:
        self._streamed: set[str] = set()
        self._output: dict[str, list[str]] = {}

    def translate(self, method: str, params: dict) -> list[object]:
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
        item = params.get("item") or {}
        kind = item.get("type")
        item_id = str(item.get("id"))
        if kind == "fileChange":
            return [ActivityStart(cid, "apply_patch", args) for cid, args in _changes(item)]
        if kind == "plan":
            text = str(item.get("text", ""))
            return [
                ActivityStart(item_id, "update_plan", {"text": text}),
                ActivityEnd(item_id, text, False),
            ]
        if kind == "contextCompaction":
            return [Notice("Codex compacted its context")]
        name = tool_name_for(item)
        if name is None:
            return []
        return [ActivityStart(item_id, name, args_for(item))]

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
_COMPLETED_BY_KIND: dict[str, Callable[[ItemTranslator, dict, str], list[object]]] = {
    "agentMessage": ItemTranslator._completed_agent_message,
    "reasoning": ItemTranslator._completed_reasoning,
    "commandExecution": ItemTranslator._completed_command_execution,
    "fileChange": ItemTranslator._completed_file_change,
    "mcpToolCall": ItemTranslator._completed_result,
    "webSearch": ItemTranslator._completed_result,
    "collabAgentToolCall": ItemTranslator._completed_result,
    "subAgentActivity": ItemTranslator._completed_result,
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
