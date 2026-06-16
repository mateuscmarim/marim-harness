from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .permissions import Mode
from .tasks import TaskList

ApprovalFn = Callable[[object], Awaitable[object]]
# (type, task, stream_id) -> the sub-agent's final report. Wired by the Harness.
SubAgentRunner = Callable[[str, str, str], Awaitable[str]]
# (stream_id, event) -> None. Forwards a sub-agent's run events to the UI so it
# can stream them nested under the spawn. Wired by the TUI; None when headless.
SubAgentEventCb = Callable[[str, object], Awaitable[None]]


@dataclass
class Deps:
    workspace_root: Path
    mode: Mode = Mode.ask
    request_approval: Optional[ApprovalFn] = None
    # The session's live checklist. Tools mutate it via ctx.deps; the Harness
    # persists it and the TUI renders it. Every Deps gets its own.
    tasks: TaskList = field(default_factory=TaskList)
    # Lets the spawn_agent tool launch a sub-agent and stream its events.
    run_subagent: Optional[SubAgentRunner] = None
    on_subagent_event: Optional[SubAgentEventCb] = None
