import logging
import time
from typing import Callable, Optional

from pydantic_ai.usage import RunUsage

from ..compaction import (
    Summarizer,
    Titler,
    compact_history,
    compact_history_with_summary,
    will_compact,
)
from ..deps import Deps
from ..hooks import events as hook_events
from ..hooks.runner import base_payload
from .store import SessionInfo, SessionManager, SessionStore

logger = logging.getLogger(__name__)


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
        self.duration_seconds: float = 0.0
        self._segment_start: float = 0.0
        self.on_compact: Optional[Callable[[int, int], None]] = None
        self.on_compact_start: Optional[Callable[[], None]] = None
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
            elapsed = (time.monotonic() - self._segment_start) if self._segment_start else 0.0
            self.store.save(
                self.history, self.usage, self.deps.tasks.to_payload(),
                duration_seconds=self.duration_seconds + elapsed,
            )

    def set_model(self, model_id: str) -> None:
        if self.store is not None:
            self.store.model = model_id
            self.persist()

    def resume(self) -> int:
        if self.store is None:
            return 0
        self.history, self.usage, tasks, prev_duration = self.store.load()
        self.deps.tasks.load(tasks)
        self.duration_seconds = prev_duration or 0.0
        self._segment_start = time.monotonic()
        return len(self.history)

    def reset(self) -> None:
        self.history = []
        self.usage = RunUsage()
        self.duration_seconds = 0.0
        self._segment_start = 0.0
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
        self.duration_seconds = 0.0
        self._segment_start = time.monotonic()
        self.deps.tasks.clear()

    def switch_session(self, session_id: str) -> int:
        if self.manager is None:
            return 0
        self.store = self.manager.store(session_id)
        self.history, self.usage, tasks, prev_duration = self.store.load()
        self.deps.tasks.load(tasks)
        self.duration_seconds = prev_duration or 0.0
        self._segment_start = time.monotonic()
        return len(self.history)

    async def maybe_compact(self) -> None:
        before = len(self.history)
        # This predicate mirrors compact_history's own decision exactly, so the
        # PreCompact hook and the on_compact_start indicator fire iff a compaction
        # will actually happen.
        going = will_compact(
            self.history, self.max_context_tokens, self.keep_last_messages
        )
        # Fire PreCompact *before* the compaction work, while the transcript is
        # still full — matching Claude Code, where the hook can snapshot the
        # conversation before it's summarized/collapsed.
        if going and self.deps.hooks is not None:
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
        # Surface a live "compacting…" indicator before the (possibly slow,
        # summarizer-driven) work begins; the on_compact finish callback clears it.
        if going and self.on_compact_start is not None:
            self.on_compact_start()
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
        except Exception as exc:
            logger.warning("autoname titler failed: %s", exc)
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
            except Exception as exc:
                logger.warning("session rename titler failed: %s", exc)
                return None
        else:
            return None
        if not new:
            return None
        self.store.name = new
        self.store.auto_named = False
        self.persist()
        return new
