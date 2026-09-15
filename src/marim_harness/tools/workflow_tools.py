"""Lazy workflow registration, without importing the optional Harness/Monty extra.

Each run gets a fresh upstream toolset/budget from the session's integration.
The provider wraps this toolset in approval, including the unavailable fallback.
"""

from pydantic_ai import RunContext
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

from ..runtime.deps import Deps

_UNAVAILABLE = (
    "Workflows are unavailable in this session. Install marim-harness[workflows] "
    "and enable MARIM_WORKFLOWS, or use spawn_agent instead."
)


async def run_workflow(code: str) -> str:
    """Run a Python workflow; install marim-harness[workflows] to enable it."""
    return _UNAVAILABLE


async def workflow_toolset(ctx: RunContext[Deps]) -> AbstractToolset[Deps]:
    # DynamicToolset factories run after the ordinary for_run traversal. Resolve
    # the upstream per-run clone explicitly, otherwise a nested lazy toolset
    # remains on its unavailable fallback for the entire run.
    integration = ctx.deps.services.workflows
    if integration is None:
        toolset: AbstractToolset[Deps] = FunctionToolset(tools=[run_workflow])
    else:
        toolset = await integration.toolset().for_run(ctx)
    return toolset.approval_required()
