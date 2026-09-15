"""Per-script identity shared by concurrent upstream host calls."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..runtime.deps import Deps


@dataclass
class WorkflowInvocation:
    tool_call_id: str
    deps: Deps
    aborted: bool = False
    seq: int = 0


current_workflow: ContextVar[WorkflowInvocation | None] = ContextVar(
    "current_workflow", default=None
)
