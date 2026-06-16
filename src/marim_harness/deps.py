from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .permissions import Mode
from .tasks import TaskList

ApprovalFn = Callable[[object], Awaitable[object]]


@dataclass
class Deps:
    workspace_root: Path
    mode: Mode = Mode.ask
    request_approval: Optional[ApprovalFn] = None
    # The session's live checklist. Tools mutate it via ctx.deps; the Harness
    # persists it and the TUI renders it. Every Deps gets its own.
    tasks: TaskList = field(default_factory=TaskList)
