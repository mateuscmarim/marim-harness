"""Marim's storage selection and media exemption for upstream output reduction.

Measurement, serialization, previews, fallback clipping and retrieval all belong
to Pydantic AI Harness. Its binary spilling is unsuitable for image tools: their
bytes must reach the model. Mixed media is therefore exempt from text reduction.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_ai.capabilities import DynamicCapability
from pydantic_ai.messages import ToolCallPart, ToolReturn
from pydantic_ai.tools import RunContext, ToolDefinition
from pydantic_ai_harness.tool_output_limits import (
    LocalFileStore,
    ToolOutputLimits,
    indented_json,
)

from ..binary_safe import has_binary_content
from .deps import Deps


class MediaSafeOutputLimits(ToolOutputLimits[Deps]):
    """Keep supported media-bearing results intact; use upstream for everything else."""

    async def after_tool_execute(
        self,
        ctx: RunContext[Deps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        result: Any,
    ) -> Any:
        values = (
            (result.return_value, result.content) if isinstance(result, ToolReturn) else (result,)
        )
        if any(has_binary_content(value) for value in values):
            return result
        return await super().after_tool_execute(
            ctx, call=call, tool_def=tool_def, args=args, result=result
        )


@dataclass(frozen=True)
class OutputStorage:
    """The owning session's destinations, captured before asynchronous work starts."""

    root: Path
    scratchpad: Path | None

    @classmethod
    def capture(cls, deps: Deps) -> OutputStorage:
        services = deps.services
        scratch = services.get_scratchpad() if services.get_scratchpad else None
        session_id = services.get_session_id() if services.get_session_id else None
        root = (
            scratch / "tool-results"
            if scratch is not None
            else deps.workspace.root
            / ".marim/output/upstream"
            / (session_id or deps.output_session_key)
        )
        return cls(root, scratch)

    def capability(self) -> MediaSafeOutputLimits:
        return MediaSafeOutputLimits(store=LocalFileStore(self.root), serializer=indented_json)


def session_output_limits() -> DynamicCapability[Deps]:
    """Capture once per run, with a stable fallback key for a sessionless harness.

    DynamicCapability keeps the retrieval tool and execution hook on the same
    resolved instance. A live getter inside either would redirect late results
    when a detached job overlaps a session switch.
    """

    def for_run(ctx: RunContext[Deps]) -> MediaSafeOutputLimits:
        return OutputStorage.capture(ctx.deps).capability()

    return DynamicCapability(for_run, id="marim_output_limits")
