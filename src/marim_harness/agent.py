from typing import Awaitable, Callable, Optional

from pydantic_ai import Agent, DeferredToolRequests, RunContext
from pydantic_ai.usage import RunUsage

from .compaction import (
    Summarizer,
    compact_history,
    compact_history_with_summary,
    render_transcript,
)
from .deps import Deps
from .instructions import load_project_instructions
from .memory import global_scope, load_index, project_scope
from .permissions import resolve_approvals
from .session import SessionInfo, SessionManager, SessionStore
from .skills import discover_skills, skills_index_text
from .tools.provider import ToolProvider

_SUMMARY_INSTRUCTIONS = (
    "You compress a coding-session transcript into a dense summary so the agent "
    "can keep working with less context. Preserve: the user's goals and "
    "constraints, decisions made, files read or edited and what changed, command "
    "results, and any unresolved problems or next steps. Drop pleasantries and "
    "redundant detail. Write terse notes, not prose."
)


def make_summarizer(model) -> Summarizer:
    """Build a summarizer backed by a dedicated, tool-free agent on ``model``."""
    summary_agent = Agent(model, instructions=_SUMMARY_INSTRUCTIONS)

    async def summarize(messages: list) -> str:
        result = await summary_agent.run(render_transcript(messages))
        return result.output

    return summarize


Titler = Callable[[list], Awaitable[str]]

_TITLE_INSTRUCTIONS = (
    "You write a short, specific title for a coding session from its transcript. "
    "Reply with the title only — no quotes, no trailing punctuation, at most six "
    "words. Name the concrete task, e.g. 'Fix the parser off-by-one' or 'Add "
    "session auto-naming'."
)

_MAX_TITLE_CHARS = 50


def clean_title(raw: str) -> str:
    """Reduce a model's reply to a single tidy title line, with a safe fallback."""
    lines = [line.strip() for line in (raw or "").splitlines()]
    text = next((line for line in lines if line), "")
    if text.lower().startswith("title:"):
        text = text[len("title:"):].strip()
    text = text.strip("\"'`").strip().rstrip(".!?,;:").strip()
    if len(text) > _MAX_TITLE_CHARS:
        text = text[:_MAX_TITLE_CHARS].rstrip() + "…"
    return text or "Untitled session"


def make_titler(model) -> Titler:
    """Build a titler backed by a dedicated, tool-free agent on ``model``."""
    title_agent = Agent(model, instructions=_TITLE_INSTRUCTIONS)

    async def title(messages: list) -> str:
        result = await title_agent.run(render_transcript(messages))
        return clean_title(result.output)

    return title


_PROACTIVE_MEMORY_POLICY = (
    "Proactive memory is ON. Beyond explicit requests, save durable facts that "
    "will help in future sessions with the remember tool: the user's stable "
    "preferences and identity, feedback they give on how you should work, and "
    "project conventions or decisions not derivable from the code or git "
    "history. Convert relative dates to absolute. Do NOT save anything "
    "recoverable from the code, files, or git; one-off conversational details; "
    "or secrets. Prefer updating an existing memory over adding a duplicate."
)

_ON_REQUEST_MEMORY_POLICY = (
    "Save to memory only when the user explicitly asks you to (for example, "
    "\"remember that …\" or the /remember command). Do not save memories "
    "proactively or on your own initiative, even if the user mentions a "
    "preference or fact in passing."
)


class Harness:
    """Owns the Pydantic AI agent and drives one user turn to completion,
    resolving deferred tool approvals by the current mode."""

    def __init__(self, model, provider: ToolProvider, deps: Deps, instructions: str,
                 model_label: str = "model", store: Optional[SessionStore] = None,
                 manager: Optional[SessionManager] = None,
                 max_context_tokens: int = 100_000, keep_last_messages: int = 20,
                 summarizer: Optional[Summarizer] = None,
                 titler: Optional[Titler] = None, model_source=None,
                 model_id: Optional[str] = None, proactive_memory: bool = False):
        self.proactive_memory = proactive_memory
        self.agent = Agent(
            model,
            deps_type=Deps,
            instructions=instructions,
            output_type=[str, DeferredToolRequests],
        )
        provider.register(self.agent)

        @self.agent.instructions
        def _project_instructions(ctx: RunContext[Deps]) -> str:
            """Layer the workspace's AGENTS.md on top of the base prompt, re-read
            each turn so edits take effect without a restart."""
            text = load_project_instructions(ctx.deps.workspace_root)
            if not text:
                return ""
            return f"Project-specific instructions from AGENTS.md:\n\n{text}"

        @self.agent.instructions
        def _memory_indexes(ctx: RunContext[Deps]) -> str:
            """Inject the global and project memory indexes, re-read each turn.
            Each line names a memory the model can expand with the recall tool;
            new facts are saved with the remember tool."""
            parts = []
            g = load_index(global_scope())
            if g:
                parts.append(f"# User memory (global)\n\n{g}")
            p = load_index(project_scope(ctx.deps.workspace_root))
            if p:
                parts.append(f"# Project memory\n\n{p}")
            if not parts:
                return ""
            return (
                "Persistent memory indexes below. Each line is a one-line hook; "
                "read the full fact with the recall tool (by the entry's title or "
                "slug, with the matching scope). Save new durable facts with the "
                "remember tool.\n\n" + "\n\n".join(parts)
            )

        @self.agent.instructions
        def _skill_index(ctx: RunContext[Deps]) -> str:
            """Inject the discovery index of available skills, re-read each turn.
            Each line is a packaged workflow the model can pull in with the
            activate_skill tool; manual-only skills are omitted by the formatter."""
            text = skills_index_text(discover_skills(ctx.deps.workspace_root))
            if not text:
                return ""
            return (
                "Available skills below — each is a packaged workflow. When a "
                "task matches one's description, load its full instructions with "
                "the activate_skill tool (by name) and follow them.\n\n" + text
            )

        @self.agent.instructions
        def _memory_policy(ctx: RunContext[Deps]) -> str:
            """Standing memory policy. The remember tool is always available, so
            the default must actively restrain proactive saves; the toggle flips
            that restraint into encouragement."""
            if self.proactive_memory:
                return _PROACTIVE_MEMORY_POLICY
            return _ON_REQUEST_MEMORY_POLICY

        self.deps = deps
        self.history: list = []
        self.model_label = model_label
        # The model object used for each turn (swappable at runtime), the source
        # that builds new ones, and the id of the active model.
        self.current_model = model
        self.model_source = model_source
        self.model_id = model_id
        self.usage = RunUsage()
        self.store = store
        self.manager = manager
        self.max_context_tokens = max_context_tokens
        self.keep_last_messages = keep_last_messages
        self.summarizer = summarizer
        self.titler = titler
        # Called with (messages_before, messages_after) when history is compacted.
        self.on_compact: Optional[Callable[[int, int], None]] = None
        # Called with (old_name, new_name) when a session is auto-titled.
        self.on_rename: Optional[Callable[[str, str], None]] = None

    @property
    def total_tokens(self) -> int:
        """Cumulative input + output tokens across the whole session."""
        return self.usage.total_tokens

    def set_model(self, model_id: str, *, persist: bool = True) -> None:
        """Switch the active model at runtime. Rebuilds the per-turn model and
        any configured aux agents (summarizer/titler) on the new model, updates
        the label, and records the choice on the session. No-op without a source.
        """
        if self.model_source is None:
            return
        model = self.model_source.build(model_id)
        self.current_model = model
        self.model_id = model_id
        self.model_label = self.model_source.label(model_id)
        if self.summarizer is not None:
            self.summarizer = make_summarizer(model)
        if self.titler is not None:
            self.titler = make_titler(model)
        if self.store is not None:
            self.store.model = model_id
            if persist:
                self._persist()

    def _apply_saved_model(self) -> None:
        """Re-point at a session's saved model after loading it, if one differs
        from what's already active."""
        if (
            self.store is not None
            and self.store.model
            and self.model_source is not None
            and self.store.model != self.model_id
        ):
            self.set_model(self.store.model, persist=False)

    def resume(self) -> int:
        """Load a previously saved conversation for this workspace into history.
        Returns the number of messages restored (0 if none / no store)."""
        if self.store is None:
            return 0
        self.history, self.usage = self.store.load()
        self._apply_saved_model()
        return len(self.history)

    def reset(self) -> None:
        """Drop the conversation: clear history, token counters, and any saved
        session for this workspace. Used by the /clear command."""
        self.history = []
        self.usage = RunUsage()
        if self.store is not None:
            self.store.clear()

    @property
    def session_name(self) -> Optional[str]:
        """Display name of the active session, if any."""
        return self.store.name if self.store is not None else None

    def sessions(self) -> list[SessionInfo]:
        """All saved sessions for this workspace, newest first."""
        if self.manager is None:
            return []
        return self.manager.list()

    def new_session(self, name: Optional[str] = None) -> None:
        """Start a fresh session (new store), leaving existing ones untouched."""
        if self.manager is None:
            self.reset()
            return
        self.store = self.manager.create(name)
        self.store.model = self.model_id  # keep the current model on the new session
        self.history = []
        self.usage = RunUsage()

    def switch_session(self, session_id: str) -> int:
        """Switch to an existing session, loading its history. Returns the number
        of messages restored."""
        if self.manager is None:
            return 0
        self.store = self.manager.store(session_id)
        self.history, self.usage = self.store.load()
        self._apply_saved_model()
        return len(self.history)

    def _persist(self) -> None:
        if self.store is not None:
            self.store.save(self.history, self.usage)

    async def _maybe_autoname(self) -> None:
        """After a turn, give an unnamed session an LLM-generated title (once).
        Silent on failure — a titling hiccup must never break the turn."""
        if (
            self.titler is None
            or self.store is None
            or not self.store.auto_named
            or not self.history
        ):
            return
        old = self.store.name
        try:
            title = await self.titler(self.history)
        except Exception:
            return
        if not title:
            return
        self.store.name = title
        self.store.auto_named = False
        self._persist()
        if self.on_rename is not None:
            self.on_rename(old, title)

    async def rename_session(self, name: Optional[str] = None) -> Optional[str]:
        """Rename the active session. With ``name``, set it verbatim; without,
        generate one from the conversation via the titler. Returns the new name,
        or None if it couldn't be done (no store, no titler, empty conversation).
        """
        if self.store is None:
            return None
        if name:
            new = name.strip()
        elif self.titler is not None and self.history:
            try:
                new = await self.titler(self.history)
            except Exception:
                return None
        else:
            return None
        if not new:
            return None
        self.store.name = new
        self.store.auto_named = False
        self._persist()
        return new

    async def _maybe_compact(self) -> None:
        """Compact history if it has grown past the token budget, keeping the task
        anchor and a recent tail. Summarizes the dropped middle when a summarizer
        is configured (falling back to truncation); fires on_compact when it trims.
        """
        before = len(self.history)
        if self.summarizer is not None:
            new_history, did = await compact_history_with_summary(
                self.history, self.max_context_tokens, self.summarizer,
                self.keep_last_messages,
            )
        else:
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
        await self._maybe_compact()
        user_prompt: Optional[str] = prompt
        deferred_results = None
        while True:
            result = await self.agent.run(
                user_prompt,
                model=self.current_model,
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
            output = result.output
            await self._maybe_autoname()
            return output
