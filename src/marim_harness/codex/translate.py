"""Pure translation of app-server notifications into neutral events.

The model layer (config/codex_cli_model.py) turns ``TextDelta``/``ThinkingDelta``
into pydantic-ai stream events and ``ActivityStart``/``ActivityEnd`` into
display-only tool cards. Tool names are harness names (``bash``,
``apply_patch``, ``web_search``) so the TUI renders them like native calls.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass


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
    total: dict


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


def args_for(item: dict) -> dict:
    kind = item.get("type")
    if kind == "commandExecution":
        return {"command": _command_text(item.get("command")), "cwd": item.get("cwd")}
    if kind == "mcpToolCall":
        args = item.get("arguments")
        return dict(args) if isinstance(args, dict) else {"arguments": args}
    if kind == "webSearch":
        return {"query": item.get("query", "")}
    if kind in ("collabAgentToolCall", "subAgentActivity"):
        return {
            k: item.get(k)
            for k in ("prompt", "model", "receiverThreadIds", "agentPath", "kind")
            if item.get(k) is not None
        }
    if kind == "plan":
        return {"text": item.get("text", "")}
    return {}


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
        item = params.get("item") or {}
        kind = item.get("type")
        item_id = str(item.get("id"))
        if kind == "agentMessage":
            return (
                [] if item_id in self._streamed else [TextDelta(item_id, str(item.get("text", "")))]
            )
        if kind == "reasoning":
            if item_id in self._streamed:
                return []
            text = "\n".join(item.get("summary") or item.get("content") or [])
            return [ThinkingDelta(item_id, text)] if text else []
        if kind == "commandExecution":
            return [self._command_end(item, item_id)]
        if kind == "fileChange":
            failed = item.get("status") != "completed"
            return [ActivityEnd(cid, str(args["diff"]), failed) for cid, args in _changes(item)]
        if kind in ("mcpToolCall", "webSearch", "collabAgentToolCall", "subAgentActivity"):
            text, is_error = _result_text(item)
            return [ActivityEnd(item_id, text, is_error)]
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
        total = (params.get("tokenUsage") or {}).get("total") or {}
        return [UsageUpdate(dict(total))]

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
