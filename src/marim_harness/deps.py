from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .permissions import Mode

ApprovalFn = Callable[[object], Awaitable[object]]


@dataclass
class Deps:
    workspace_root: Path
    mode: Mode = Mode.ask
    request_approval: Optional[ApprovalFn] = None
