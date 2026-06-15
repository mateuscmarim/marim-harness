from typing import Optional

from pydantic_ai import Agent, DeferredToolRequests

from .deps import Deps
from .permissions import resolve_approvals
from .tools.provider import ToolProvider


class Harness:
    """Owns the Pydantic AI agent and drives one user turn to completion,
    resolving deferred tool approvals by the current mode."""

    def __init__(self, model, provider: ToolProvider, deps: Deps, instructions: str,
                 model_label: str = "model"):
        self.agent = Agent(
            model,
            deps_type=Deps,
            instructions=instructions,
            output_type=[str, DeferredToolRequests],
        )
        provider.register(self.agent)
        self.deps = deps
        self.history: list = []
        self.model_label = model_label

    async def run_turn(self, prompt: str, event_stream_handler=None) -> str:
        """Run the agent until it produces a final text answer, looping through
        any approval rounds. Returns the final text output."""
        user_prompt: Optional[str] = prompt
        deferred_results = None
        while True:
            result = await self.agent.run(
                user_prompt,
                message_history=self.history,
                deps=self.deps,
                deferred_tool_results=deferred_results,
                event_stream_handler=event_stream_handler,
            )
            self.history = result.all_messages()
            if isinstance(result.output, DeferredToolRequests):
                deferred_results = await resolve_approvals(
                    result.output, self.deps.mode, self.deps.request_approval
                )
                user_prompt = None  # continuation is driven by deferred_results
                continue
            return result.output
