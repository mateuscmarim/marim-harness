"""Fire lifecycle hooks for one harness turn.

Centralizes the ``if deps.hooks is None: skip; else dispatch(event, payload)``
boilerplate and the payload assembly from the live session, so callers fire a
named hook in one line instead of repeating the guard + ``base_payload`` dance.

All hooks are observe-only except ``session_start`` and ``user_prompt_submit``,
which return whatever context the hook injected (``None`` when no hook ran).
"""

import logging
from typing import Optional

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    RetryPromptPart,
)

from . import events as hook_events
from .runner import base_payload

logger = logging.getLogger(__name__)


class TurnHooks:
    """Hook dispatcher bound to a harness's ``deps`` and ``session``. Holds no
    state of its own — payloads are read fresh from the live session each call,
    so a session switch or rename is reflected without rewiring."""

    def __init__(self, deps, session):
        self.deps = deps
        self.session = session

    def _payload(self, event: str, **extra) -> dict:
        """A hook payload with the common fields drawn from the live session,
        plus any event-specific extras."""
        store = self.session.store
        return base_payload(
            event,
            session_id=store.session_id if store is not None else "",
            cwd=str(self.deps.workspace_root),
            transcript_path=str(store.path) if store is not None else "",
            **extra,
        )

    async def _dispatch(self, event: str, **extra) -> Optional[str]:
        """Fire ``event`` if hooks are configured, returning any injected
        context. A no-op (returns ``None``) when no hooks are wired."""
        if self.deps.hooks is None:
            return None
        return await self.deps.hooks.dispatch(event, self._payload(event, **extra))

    async def session_start(self, source: str) -> Optional[str]:
        """SessionStart (``source`` is ``startup``/``resume``/``clear``); returns
        any context the hook wants prepended to the next turn's prompt."""
        return await self._dispatch(hook_events.SESSION_START, source=source)

    async def session_end(self, reason: str = "exit") -> None:
        """SessionEnd on teardown. Observe-only."""
        await self._dispatch(hook_events.SESSION_END, reason=reason)

    async def user_prompt_submit(self, prompt: str) -> Optional[str]:
        """UserPromptSubmit; returns any context to prepend to this turn."""
        return await self._dispatch(hook_events.USER_PROMPT_SUBMIT, prompt=prompt)

    async def stop(self) -> None:
        """Stop, fired once the turn produces its final text. Observe-only."""
        await self._dispatch(hook_events.STOP)

    async def subagent_start(self, subagent_type: str, task: str) -> None:
        """SubagentStart for a spawned sub-agent. Observe-only."""
        await self._dispatch(
            hook_events.SUBAGENT_START, subagent_type=subagent_type, task=task
        )

    async def subagent_stop(self, subagent_type: str, task: str, result: str) -> None:
        """SubagentStop once a spawned sub-agent returns. Observe-only."""
        await self._dispatch(
            hook_events.SUBAGENT_STOP,
            subagent_type=subagent_type,
            task=task,
            result=result,
        )

    async def post_tool_use_failure(self, tool_name: str, tool_input: dict,
                                    error: str) -> None:
        """PostToolUseFailure: a tool call errored or was retried. Observe-only."""
        await self._dispatch(
            hook_events.POST_TOOL_USE_FAILURE,
            tool_name=tool_name,
            tool_input=tool_input,
            error=error,
        )

    async def tool_event(self, event, call_inputs: Optional[dict] = None) -> None:
        """Map a streamed tool event to a Pre/PostToolUse hook (observe-only).

        ``call_inputs`` is a per-turn dict (tool_call_id → tool_input) used to
        correlate a PostToolUse result with the args from its matching call, so
        that CC plugin scripts receive ``tool_input`` on both event types.
        """
        if self.deps.hooks is None:
            return
        if isinstance(event, FunctionToolCallEvent):
            try:
                tool_input = event.part.args_as_dict()
            except Exception as exc:
                logger.debug("failed to parse tool args: %s", exc)
                tool_input = {}
            # Stash input so the paired PostToolUse event can include it.
            if call_inputs is not None:
                call_inputs[event.part.tool_call_id] = tool_input
            await self.deps.hooks.dispatch(
                hook_events.PRE_TOOL_USE,
                self._payload(
                    hook_events.PRE_TOOL_USE,
                    tool_name=event.part.tool_name,
                    tool_input=tool_input,
                ),
            )
        elif isinstance(event, FunctionToolResultEvent):
            # Look up the stashed input by tool_call_id; fall back gracefully.
            tool_input = ({} if call_inputs is None
                          else call_inputs.get(event.tool_call_id, {}))
            part = event.part
            if isinstance(part, RetryPromptPart):
                # A failed/retried call: fire PostToolUseFailure instead of
                # PostToolUse so the two are distinct (matches Claude Code).
                await self.post_tool_use_failure(
                    tool_name=getattr(part, "tool_name", "") or "",
                    tool_input=tool_input,
                    error=part.model_response(),
                )
            else:
                await self.deps.hooks.dispatch(
                    hook_events.POST_TOOL_USE,
                    self._payload(
                        hook_events.POST_TOOL_USE,
                        tool_name=getattr(event.part, "tool_name", ""),
                        tool_input=tool_input,
                        tool_response=str(getattr(event.part, "content", "")),
                    ),
                )
