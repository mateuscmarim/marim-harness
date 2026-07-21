"""The advisor tool: consult a separately-configured, typically stronger model
for strategic guidance mid-task.

Main-agent only — deliberately NOT in provider._SUBAGENT_FNS (sub-agents have
model tiering instead; see the design spec's scope section). Registered plain
(ungated): configuring an advisor is consent to send the transcript to that
provider, the same consent running the main model implies.

The ``prepare`` hook is the live toggle: it omits the tool from any run where
``services.advise`` is None, so an unconfigured install never advertises it
and ``/advisor off`` takes effect on the next model request with no agent
rebuild.
"""

from pydantic_ai import RunContext
from pydantic_ai.tools import ToolDefinition

from ..runtime.deps import Deps


async def prepare_advisor(
    ctx: RunContext[Deps], tool_def: ToolDefinition
) -> ToolDefinition | None:
    """Omit the advisor tool from the run schema when no advisor is
    configured. Reads the live seam per request, so toggling the advisor
    mid-session applies on the next request."""
    if ctx.deps.services.advise is None:
        return None
    return tool_def


async def advisor(ctx: RunContext[Deps]) -> str:
    """Consult your advisor: a stronger reviewer model that sees this entire
    conversation — the task, your reasoning, and every tool call and result —
    and returns strategic guidance.

    Call it before starting substantive work on a non-trivial task, when you
    are stuck or about to make a risky change, and before declaring a complex
    task done. It takes no arguments; the transcript is forwarded
    automatically. The advice is guidance to weigh against your own evidence,
    not an instruction to follow blindly.
    """
    advise = ctx.deps.services.advise
    if advise is None:
        # The prepare hook normally hides the tool in this state; this is the
        # race window where the advisor was turned off after the request that
        # advertised the tool was already in flight.
        return "No advisor is configured. Continue without advice."
    cap = ctx.deps.advisor_max_uses
    if cap is not None and ctx.deps.advisor_uses >= cap:
        return (
            f"Advisor call limit reached for this turn ({cap}). "
            "Continue without further advice."
        )
    ctx.deps.advisor_uses += 1
    return await advise(list(ctx.messages))
