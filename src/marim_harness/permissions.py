from enum import Enum
from typing import Awaitable, Callable, Optional

from pydantic_ai import DeferredToolRequests, DeferredToolResults, ToolDenied
from pydantic_ai.tools import DeferredToolApprovalResult


class Mode(str, Enum):
    ask = "ask"
    auto = "auto"
    plan = "plan"

    def cycle(self) -> "Mode":
        order = [Mode.ask, Mode.auto, Mode.plan]
        return order[(order.index(self) + 1) % len(order)]


async def resolve_approvals(
    requests: DeferredToolRequests,
    mode: Mode,
    request_approval: Optional[Callable[[object], Awaitable[DeferredToolApprovalResult | bool]]],
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
            results.approvals[call.tool_call_id] = ToolDenied("read-only plan mode")
        elif request_approval is None:
            results.approvals[call.tool_call_id] = ToolDenied(
                "no approver available; denied"
            )
        else:  # Mode.ask
            results.approvals[call.tool_call_id] = await request_approval(call)
    return results
