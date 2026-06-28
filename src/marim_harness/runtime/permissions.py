import json
from collections.abc import Awaitable, Callable
from enum import Enum

from pydantic_ai import DeferredToolRequests, DeferredToolResults, ToolDenied
from pydantic_ai.tools import DeferredToolApprovalResult

from ..read_only_commands import is_read_only


class Mode(str, Enum):
    ask = "ask"
    auto = "auto"
    plan = "plan"

    def cycle(self) -> "Mode":
        order = [Mode.ask, Mode.auto, Mode.plan]
        return order[(order.index(self) + 1) % len(order)]


def _bash_command(call: object) -> str:
    """Best-effort extract the ``command`` arg from an approval call. Tool args
    arrive as a dict or, from some providers, a JSON string."""
    args = getattr(call, "args", None)
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            return ""
    if isinstance(args, dict):
        return str(args.get("command", ""))
    return ""


def _plan_decision(call: object) -> "bool | ToolDenied":
    """Plan mode is read-only. Deny mutations, but let a read-only ``bash``
    command through so the agent can research before presenting a plan
    (see read_only_commands.is_read_only — best-effort, not a sandbox)."""
    if getattr(call, "tool_name", None) == "bash" and is_read_only(_bash_command(call)):
        return True
    if getattr(call, "tool_name", None) == "bash":
        return ToolDenied("plan mode: read-only commands only")
    return ToolDenied("read-only plan mode")


async def resolve_approvals(
    requests: DeferredToolRequests,
    mode: Mode,
    request_approval: Callable[[object], Awaitable[DeferredToolApprovalResult | bool]] | None,
) -> DeferredToolResults:
    """Turn pending tool-approval requests into results based on the current mode.

    auto -> approve all. plan -> deny all (read-only). ask -> delegate to callback,
    which returns True (approve) or a ToolDenied (reject). In ask mode with no
    callback wired (e.g. a non-interactive run), deny rather than crash — nothing
    can grant approval, so the safe answer is to refuse.
    """
    results = DeferredToolResults()
    for call in requests.approvals:
        if mode is Mode.auto:
            results.approvals[call.tool_call_id] = True
        elif mode is Mode.plan:
            results.approvals[call.tool_call_id] = _plan_decision(call)
        elif request_approval is None:
            results.approvals[call.tool_call_id] = ToolDenied(
                "no approver available; denied"
            )
        else:  # Mode.ask
            results.approvals[call.tool_call_id] = await request_approval(call)
    return results
