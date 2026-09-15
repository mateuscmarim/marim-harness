"""Marim integration around pydantic-ai-harness' public compaction strategies."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Generic, TypeVar

from pydantic_ai import RunContext
from pydantic_ai.exceptions import (
    FallbackExceptionGroup,
    ModelAPIError,
    UnexpectedModelBehavior,
)
from pydantic_ai.messages import ModelMessage, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_ai_harness.compaction import (
    ClearToolResults,
    CompactionStrategy,
    FallbackCompaction,
    SlidingWindowCompaction,
    SupportsFocus,
    TieredCompaction,
    compact_now,
)

from ..tools.names import GATED_TOOLS

logger = logging.getLogger(__name__)
AgentDepsT = TypeVar("AgentDepsT")

# These tools change session or external process state. Their result placeholder must
# never invite the model to repeat the action merely to recover cleared information.
KNOWN_MUTATING_TOOLS = GATED_TOOLS | frozenset(
    {
        "remember",
        "forget",
        "update_tasks",
        "present_plan",
        "cancel_job",
        "job",
        "spawn_agent",
        "run_workflow",
    }
)


@dataclass
class SafeClearToolResults(ClearToolResults[AgentDepsT], Generic[AgentDepsT]):
    """Clear through upstream while avoiding its ambiguous duplicate-ID behavior."""

    async def compact(
        self,
        messages: list[ModelMessage],
        ctx: RunContext[AgentDepsT],
    ) -> list[ModelMessage]:
        names_by_id: dict[str, set[str]] = {}
        counts: dict[str, int] = {}
        for message in messages:
            for part in message.parts:
                if isinstance(part, ToolCallPart) and part.tool_call_id is not None:
                    counts[part.tool_call_id] = counts.get(part.tool_call_id, 0) + 1
                    names_by_id.setdefault(part.tool_call_id, set()).add(part.tool_name)

        duplicate_names = {
            name for call_id, count in counts.items() if count > 1 for name in names_by_id[call_id]
        }
        if duplicate_names:
            logger.warning(
                "Skipping tool-result clearing for %d tool name(s) with duplicate tool call IDs",
                len(duplicate_names),
            )

        delegate = replace(self, exclude_tools=self.exclude_tools | frozenset(duplicate_names))
        upstream = super(SafeClearToolResults, delegate)
        return await upstream.compact(messages, ctx)


def safe_tool_result_clearer(
    *,
    keep_pairs: int,
    max_tokens: int,
    exclude_tools: frozenset[str] = frozenset(),
) -> SafeClearToolResults[object]:
    """Build the conservative clearing strategy shared by main and sub-agent histories."""
    return SafeClearToolResults(
        keep_pairs=keep_pairs,
        max_tokens=max_tokens,
        clear_tool_inputs=False,
        exclude_tools=KNOWN_MUTATING_TOOLS | exclude_tools,
    )


@dataclass(frozen=True)
class ReductionOptions:
    """Strategy selection and retention policy for one history-reduction attempt."""

    summary: CompactionStrategy[None] | None
    target_tokens: int
    keep_messages: int
    keep_pairs: int
    clear: bool
    force: bool
    focus: str | None


@dataclass(frozen=True)
class Reduction:
    messages: list[ModelMessage]
    stages: tuple[str, ...]
    restructured: bool


@dataclass
class _ReportStage(Generic[AgentDepsT]):
    name: str
    strategy: CompactionStrategy[AgentDepsT]
    changed: list[str]

    async def compact(
        self,
        messages: list[ModelMessage],
        ctx: RunContext[AgentDepsT],
    ) -> list[ModelMessage]:
        result = await self.strategy.compact(messages, ctx)
        if result != messages:
            self.changed.append(self.name)
        return result


def _focus(strategy: CompactionStrategy[None], focus: str | None) -> CompactionStrategy[None]:
    if focus is not None and isinstance(strategy, SupportsFocus):
        return strategy.with_focus(focus)
    return strategy


async def _compact(
    strategy: CompactionStrategy[None],
    messages: list[ModelMessage],
    *,
    model: Model,
    usage: RunUsage,
    usage_limits: UsageLimits | None,
) -> list[ModelMessage]:
    if usage_limits is None:
        return await compact_now(strategy, messages, model=model, usage=usage)
    ctx = RunContext(
        deps=None,
        model=model,
        usage=usage,
        usage_limits=usage_limits,
        messages=messages,
    )
    return await strategy.compact(messages, ctx)


async def reduce_history(
    messages: list[ModelMessage],
    options: ReductionOptions,
    *,
    model: Model,
    usage: RunUsage,
    usage_limits: UsageLimits | None = None,
) -> Reduction:
    """Apply upstream strategies; the caller owns committing history and usage."""
    if not messages:
        return Reduction(messages=messages, stages=(), restructured=False)

    changed: list[str] = []
    tiers: list[CompactionStrategy[None]] = []
    if options.clear:
        clearer = safe_tool_result_clearer(keep_pairs=options.keep_pairs, max_tokens=1)
        tiers.append(_ReportStage("clear", clearer, changed))

    trim = _ReportStage(
        "trim",
        SlidingWindowCompaction(max_tokens=1, keep_messages=options.keep_messages),
        changed,
    )
    final: CompactionStrategy[None] = trim
    if options.summary is not None:
        focused_summary = _focus(options.summary, options.focus)
        final = FallbackCompaction(
            fallback_chain=[_ReportStage("summary", focused_summary, changed), trim],
            fallback_on=(ModelAPIError, FallbackExceptionGroup, UnexpectedModelBehavior),
        )
    tiers.append(final)

    logger.debug(
        "Reducing history: messages=%d force=%s clear=%s target_tokens=%d",
        len(messages),
        options.force,
        options.clear,
        options.target_tokens,
    )
    result = list(messages)
    if options.force:
        for strategy in tiers:
            result = await _compact(
                strategy,
                result,
                model=model,
                usage=usage,
                usage_limits=usage_limits,
            )
    else:
        strategy = TieredCompaction(target_tokens=options.target_tokens, tiers=tiers)
        result = await _compact(
            strategy,
            result,
            model=model,
            usage=usage,
            usage_limits=usage_limits,
        )

    stages = tuple(changed)
    restructured = any(stage in {"summary", "trim"} for stage in stages)
    logger.debug(
        "History reduction completed: changed=%s stages=%s messages=%d",
        result != messages,
        stages,
        len(result),
    )
    return Reduction(messages=result, stages=stages, restructured=restructured)


__all__ = [
    "KNOWN_MUTATING_TOOLS",
    "Reduction",
    "ReductionOptions",
    "SafeClearToolResults",
    "reduce_history",
    "safe_tool_result_clearer",
]
