from typing import Callable, Optional

from pydantic_ai.usage import RunUsage

from ..compaction import (
    Summarizer,
    Titler,
    compact_history,
    compact_history_with_summary,
)
from ..deps import Deps
from ..hooks import events as hook_events
from ..hooks.runner import base_payload
from .store import SessionInfo, SessionManager, SessionStore


class SessionController:
    """Owns session lifecycle: history, usage, persistence, compaction, naming."""

    def __init__(
        self,
        store: Optional[SessionStore],
        manager: Optional[SessionManager],
        deps: Deps,
        max_context_tokens: int,
        keep_last_messages: int,
        summarizer: Optional[Summarizer] = None,
        titler: Optional[Titler] = None,
    ) -> None:
        self.store = store
        self.manager = manager
        self.deps = deps
        self.max_context_tokens = max_context_tokens
        self.keep_last_messages = keep_last_messages
        self.summarizer = summarizer
        self.titler = titler
        self.history: list = []
        self.usage: RunUsage = RunUsage()
        self.on_compact: Optional[Callable[[int, int], None]] = None
        self.on_rename: Optional[Callable[[str, str], None]] = None

    @property
    def session_name(self) -> Optional[str]:
        return self.store.name if self.store is not None else None

    @property
    def total_tokens(self) -> int:
        return self.usage.total_tokens

    def sessions(self) -> list[SessionInfo]:
        if self.manager is None:
            return []
        return self.manager.list()

    def persist(self) -> None:
        if self.store is not None:
            self.store.save(self.history, self.usage, self.deps.tasks.to_payload())

    def set_model(self, model_id: str) -> None:
        if self.store is not None:
            self.store.model = model_id
            self.persist()

    def resume(self) -> int:
        if self.store is None:
            return 0
        self.history, self.usage, tasks = self.store.load()
        self.deps.tasks.load(tasks)
        return len(self.history)

    def reset(self) -> None:
        self.history = []
        self.usage = RunUsage()
        self.deps.tasks.clear()
        if self.store is not None:
            self.store.clear()

    def new_session(self, name: Optional[str] = None, model_id: Optional[str] = None) -> None:
        if self.manager is None:
            self.reset()
            return
        self.store = self.manager.create(name)
        if model_id is not None:
            self.store.model = model_id
        self.history = []
        self.usage = RunUsage()
        self.deps.tasks.clear()

    def switch_session(self, session_id: str) -> int:
        if self.manager is None:
            return 0
        self.store = self.manager.store(session_id)
        self.history, self.usage, tasks = self.store.load()
        self.deps.tasks.load(tasks)
        return len(self.history)

    async def maybe_compact(self) -> None:
        before = len(self.history)
        if self.summarizer is not None:
            new_history, did = await compact_history_with_summary(
                self.history, self.max_context_tokens, self.summarizer,
                self.keep_last_messages,
            )
        else:
            new_history, did = compact_history(
                self.history, self.max_context_tokens, self.keep_last_messages,
            )
        if did:
            self.history = new_history
            if self.deps.hooks is not None:
                await self.deps.hooks.dispatch(
                    hook_events.PRE_COMPACT,
                    base_payload(
                        hook_events.PRE_COMPACT,
                        session_id=self.store.session_id if self.store is not None else "",
                        cwd=str(self.deps.workspace_root),
                        transcript_path=str(self.store.path) if self.store is not None else "",
                        trigger="auto",
                        custom_instructions="",
                    ),
                )
            if self.on_compact is not None:
                self.on_compact(before, len(self.history))

    async def maybe_autoname(self) -> None:
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
        self.persist()
        if self.on_rename is not None:
            self.on_rename(old, title)

    async def rename(self, name: Optional[str] = None) -> Optional[str]:
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
        self.persist()
        return new
