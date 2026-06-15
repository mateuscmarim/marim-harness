from typing import Callable, Optional

from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.usage import RunUsage

from .compaction import compact_history
from .deps import Deps
from .permissions import resolve_approvals
from .session import SessionStore
from .tools.provider import ToolProvider


class Harness:
    """Owns the Pydantic AI agent and drives one user turn to completion,
    resolving deferred tool approvals by the current mode."""

    def __init__(self, model, provider: ToolProvider, deps: Deps, instructions: str,
                 model_label: str = "model", store: Optional[SessionStore] = None,
                 max_context_tokens: int = 100_000, keep_last_messages: int = 20):
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
        self.usage = RunUsage()
        self.store = store
        self.max_context_tokens = max_context_tokens
        self.keep_last_messages = keep_last_messages
        # Called with (messages_before, messages_after) when history is compacted.
        self.on_compact: Optional[Callable[[int, int], None]] = None

    @property
    def total_tokens(self) -> int:
        """Cumulative input + output tokens across the whole session."""
        return self.usage.total_tokens

    def resume(self) -> int:
        """Load a previously saved conversation for this workspace into history.
        Returns the number of messages restored (0 if none / no store)."""
        if self.store is None:
            return 0
        self.history, self.usage = self.store.load()
        return len(self.history)

    def _persist(self) -> None:
        if self.store is not None:
            self.store.save(self.history, self.usage)

    def _maybe_compact(self) -> None:
        """Truncate history if it has grown past the token budget, keeping the
        task anchor and a recent tail. Fires on_compact when it trims."""
        before = len(self.history)
        new_history, did = compact_history(
            self.history, self.max_context_tokens, self.keep_last_messages
        )
        if did:
            self.history = new_history
            if self.on_compact is not None:
                self.on_compact(before, len(self.history))

    async def run_turn(self, prompt: str, event_stream_handler=None) -> str:
        """Run the agent until it produces a final text answer, looping through
        any approval rounds. Returns the final text output."""
        self._maybe_compact()
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
            self.usage += result.usage
            self._persist()
            if isinstance(result.output, DeferredToolRequests):
                deferred_results = await resolve_approvals(
                    result.output, self.deps.mode, self.deps.request_approval
                )
                user_prompt = None  # continuation is driven by deferred_results
                continue
            return result.output
