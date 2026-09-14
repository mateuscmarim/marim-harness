"""Public Claude stream-json lifecycle fields (2.1.270), parsed defensively.

No control request or private emission flag is required. The processor observes
state; result/closure ownership remains in ClaudeProcess.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from uuid import uuid4

from ..config.lifecycle import BackendNotice, nonnegative_int


@dataclass(frozen=True)
class BackendTask:
    task_id: str
    description: str
    status: str
    backend: str = "claude-cli"


@dataclass(frozen=True)
class LifecycleChunk:
    obj: dict


def _text(obj: dict, key: str) -> str:
    value = obj.get(key)
    return value if isinstance(value, str) else ""


def inventory_from(obj: dict) -> dict:
    if not any(
        key in obj for key in ("tools", "slash_commands", "agents", "mcp_servers", "permissionMode")
    ):
        return {}
    result: dict = {"backend": "claude-cli"}
    for key in ("tools", "slash_commands", "agents"):
        values = obj.get(key)
        result[key] = [s for s in values if isinstance(s, str)] if isinstance(values, list) else []
    servers = obj.get("mcp_servers")
    result["mcp_servers"] = (
        [
            {"name": _text(s, "name"), "status": _text(s, "status")}
            for s in servers
            if isinstance(s, dict) and _text(s, "name")
        ]
        if isinstance(servers, list)
        else []
    )
    result["permission_mode"] = _text(obj, "permissionMode")
    return result


def _notice(obj: dict) -> BackendNotice | None:
    kind = _text(obj, "subtype")
    data: dict = {}
    message = ""
    if kind == "compact_boundary":
        metadata = obj.get("compact_metadata")
        if not isinstance(metadata, dict):
            return None
        data = {
            k: v
            for k in ("pre_tokens", "post_tokens")
            if (v := nonnegative_int(metadata.get(k))) is not None
        }
        trigger = _text(metadata, "trigger")
        if trigger:
            data["trigger"] = trigger
        message, kind = "Claude compacted its context", "compaction"
    elif kind == "model_refusal_fallback":
        original, fallback = _text(obj, "original_model"), _text(obj, "fallback_model")
        message = (
            f"Claude model fallback: {original} → {fallback}"
            if original and fallback
            else _text(obj, "content")
        )
        data = {
            "original_model": original,
            "fallback_model": fallback,
            "scope": _text(obj, "scope"),
        }
    elif kind in ("notification", "permission_denied"):
        message = _text(obj, "text" if kind == "notification" else "message")
    if not message.strip():
        return None
    identity = f"claude:{_text(obj, 'session_id')}:{_text(obj, 'uuid') or uuid4()}"
    return BackendNotice(
        message, "claude-cli", kind, "info" if kind == "compaction" else "warning", identity, data
    )


class ClaudeLifecycle:
    """One process's optional observations, independently of turn completion."""

    def __init__(self) -> None:
        self.inventory: dict = {}
        self.thinking_tokens: int | None = None
        self.backend_state: str | None = None
        self.vcs_revision = 0
        self.result_details: dict = {}
        self.tasks: dict[str, BackendTask] = {}
        self._seen: deque[str] = deque(maxlen=2000)

    def begin_turn(self) -> None:
        self.thinking_tokens = None
        self.result_details = {}

    def consume(self, obj: dict) -> list:
        kind = obj.get("subtype")
        if kind in ("task_started", "task_progress", "task_notification"):
            return self._task(obj)
        notice = _notice(obj)
        if notice is not None:
            if notice.id in self._seen:
                return []
            self._seen.append(notice.id)
            return [notice]
        self._observe(obj)
        return []

    def _observe(self, obj: dict) -> None:
        kind = obj.get("subtype")
        if kind == "init":
            self.inventory = inventory_from(obj)
        elif kind == "thinking_tokens":
            tokens = nonnegative_int(obj.get("estimated_tokens"))
            if tokens is not None:
                self.thinking_tokens = tokens
        elif kind == "session_state_changed":
            state = obj.get("state")
            if state in ("idle", "running", "requires_action"):
                self.backend_state = state
        elif kind == "vcs_state_changed" and _text(obj, "kind"):
            self.vcs_revision += 1

    def _task(self, obj: dict) -> list[BackendTask]:
        task_id = _text(obj, "task_id")
        if not task_id or obj.get("owned_by_subagent"):
            return []
        kind = obj.get("subtype")
        known = self.tasks.get(task_id)
        if kind == "task_started":
            if obj.get("task_type") != "local_bash" or known is not None:
                return []
            known = BackendTask(
                f"claude:{_text(obj, 'session_id')}:{task_id}",
                _text(obj, "description") or "Background shell",
                "running",
            )
        elif known is None or known.status != "running":
            return []
        elif kind == "task_progress":
            known = replace(known, description=_text(obj, "description") or known.description)
        elif kind == "task_notification":
            status = obj.get("status")
            if status not in ("completed", "failed", "stopped"):
                return []
            known = replace(
                known,
                status="interrupted" if status == "stopped" else status,
                description=_text(obj, "summary") or known.description,
            )
        self.tasks[task_id] = known
        return [known]

    def close(self) -> list[BackendTask]:
        closed = []
        for key, task in self.tasks.items():
            if task.status == "running":
                settled = replace(task, status="interrupted")
                self.tasks[key] = settled
                closed.append(settled)
        return closed

    def result(self, obj: dict) -> None:
        self.result_details = {
            k: v
            for k in ("num_turns", "duration_api_ms")
            if (v := nonnegative_int(obj.get(k))) is not None
        }
        reason = _text(obj, "stop_reason")
        if reason:
            self.result_details["stop_reason"] = reason
        denials = obj.get("permission_denials")
        if isinstance(denials, list):
            self.result_details["permission_denials"] = [
                {"tool_name": _text(d, "tool_name"), "tool_use_id": _text(d, "tool_use_id")}
                for d in denials
                if isinstance(d, dict) and _text(d, "tool_name")
            ]
