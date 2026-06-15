from enum import Enum
from typing import Awaitable, Callable

from pydantic_ai import DeferredToolRequests, DeferredToolResults, ToolDenied


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
    request_approval: Callable[[object], Awaitable[object]],
) -> DeferredToolResults:
    """Turn pending tool-approval requests into results based on the current mode.

    auto -> approve all. plan -> deny all (read-only). ask -> delegate to callback,
    which returns True (approve) or a ToolDenied (reject).
    """
    results = DeferredToolResults()
    for call in requests.approvals:
        if mode is Mode.auto:
            results.approvals[call.tool_call_id] = True
        elif mode is Mode.plan:
            results.approvals[call.tool_call_id] = ToolDenied("read-only plan mode")
        else:  # Mode.ask
            results.approvals[call.tool_call_id] = await request_approval(call)
    return results
