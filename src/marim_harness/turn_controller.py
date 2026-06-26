from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .deps import Deps, HarnessAgent
    from .hooks.dispatch import TurnHooks
    from .mcp import McpManager
    from .session import SessionController
    from .session.checkpoints import CheckpointManager


class TurnController:
    """Drives one user turn to completion. Extracted from Harness; logic moves in Task 2."""

    def __init__(
        self,
        agent: HarnessAgent,
        session: SessionController,
        checkpoints: CheckpointManager,
        hooks: TurnHooks,
        mcp: McpManager,
        deps: Deps,
    ) -> None:
        self.agent = agent
        self.session = session
        self.checkpoints = checkpoints
        self.hooks = hooks
        self.mcp = mcp
        self.deps = deps

        self._pending_error_note: str | None = None
        self._pending_hook_context: str | None = None
        self._pending_jobs_digest: str | None = None
        self._consumed_this_turn: tuple[str | None, str | None] = (None, None)
        self._active_run_ctx: Any = None
        self._steer_buffer: list[tuple[str, list[tuple[bytes, str]] | None]] = []

    async def run_turn(
        self,
        prompt: str,
        event_stream_handler: Any = None,
        attachments: list[tuple[bytes, str]] | None = None,
    ) -> str:
        raise NotImplementedError

    async def _run_with_approval(self, *args: Any, **kwargs: Any) -> str:
        raise NotImplementedError
