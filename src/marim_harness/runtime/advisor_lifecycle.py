"""Cancellation and accounting integration around upstream tool execution.

The upstream capability owns consultation. This public hook only keeps pending
cleanup inside Marim's session ownership lifetime, including repeated interrupts.
"""

import asyncio

import anyio
from pydantic_ai.capabilities import AbstractCapability

from .backend_jobs import drain_task


class AdvisorLifecycle(AbstractCapability):
    async def wrap_tool_execute(self, ctx, *, call, tool_def, args, handler):
        if call.tool_name != "advisor":
            return await handler(args)
        # Provider-billed detail fields may cover only the executor. Preserve
        # that fact through additive RunUsage/session persistence.
        ctx.usage.details["advisor_mixed_cost"] = 1
        task = asyncio.ensure_future(handler(args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            with anyio.CancelScope(shield=True):
                await drain_task(asyncio.create_task(_settle(task)))
            raise


async def _settle(task):
    await asyncio.gather(task, return_exceptions=True)
