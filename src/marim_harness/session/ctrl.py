import logging
import time
from collections.abc import Callable

from pydantic_ai.messages import ModelMessage
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
        store: SessionStore | None,
        manager: SessionManager | None,
        deps: Deps,
        max_context_tokens: int,
        keep_last_messages: int,
        summarizer: Summarizer | None = None,
        titler: Titler | None = None,
    ) -> None:
        self.store = store
        self.manager = manager
        self.deps = deps
        self.max_context_tokens = max_context_tokens
        self.keep_last_messages = keep_last_messages
        self.summarizer = summarizer
        self.titler = titler
        self.history_version: int = 0
        self._last_persisted_version: int = -1
        # ``history`` is a property; the underlying list lives in ``_history``.
        # The setter bumps ``history_version`` so the persist cache can detect
        # no-op writes — set both fields before the first assignment below.
        # No annotation here: it would redeclare the property and shadow it.
        self.history = []
        self.usage: RunUsage = RunUsage()
        self.duration_seconds: float = 0.0
        self._segment_start: float = 0.0
        self.on_compact: Callable[[int, int], None] | None = None
        self.on_compact_start: Callable[[], None] | None = None
        self.on_rename: Callable[[str, str], None] | None = None

    @property
    def session_name(self) -> str | None:
        return self.store.name if self.store is not None else None

    @property
    def total_tokens(self) -> int:
        return self.usage.total_tokens

    # Attribute setter that bumps the version on every history replacement —
    # the persist cache relies on this, so direct ``self.history = x`` (which
    # the property funnel routes through) invalidates the cache too. Sites
    # that prefer an explicit method can call ``set_history`` instead.
    @property
    def history(self) -> list[ModelMessage]:
        return self._history

    @history.setter
    def history(self, value: list[ModelMessage]) -> None:
        self._history = value
        self.history_version += 1

    def set_history(self, history: list[ModelMessage]) -> None:
        """Replace ``self.history`` and bump the version. Equivalent to
        ``self.history = history`` but reads as a method call at the call
        site; the property setter enforces cache invalidation either way."""
        self.history = history

    def sessions(self) -> list[SessionInfo]:
        if self.manager is None:
            return []
        return self.manager.list()

    def persist(self, *, force: bool = False) -> None:
        if self.store is not None:
            # Skip the (encode -> decode -> write) round-trip when the history
            # version hasn't changed since the last persist — the on-disk file
            # is already current. Usage-only changes are rare in practice and
            # not worth a dedicated cache key. ``force`` is for metadata-only
            # mutations (rename, auto_named flip) that don't touch ``history``.
            if not force and self.history_version == self._last_persisted_version:
                return
            elapsed = (time.monotonic() - self._segment_start) if self._segment_start else 0.0
            self.store.save(
                self.history, self.usage, self.deps.tasks.to_payload(),
                duration_seconds=self.duration_seconds + elapsed,
            )
            self._last_persisted_version = self.history_version

    def set_model(self, model_id: str) -> None:
        if self.store is not None:
            self.store.model = model_id
            self.persist(force=True)

    def resume(self) -> int:
        if self.store is None:
            return 0
        self.history, self.usage, tasks, prev_duration = self.store.load()
        self.deps.tasks.load(tasks)
        self.duration_seconds = prev_duration or 0.0
        self._segment_start = time.monotonic()
        return len(self.history)

    def ensure_segment_started(self) -> None:
        """Start the active-time segment clock if it isn't already running.
        ``resume``/``new_session`` set it, but a fresh, never-resumed session
        leaves it at 0.0; the interactive app calls this on mount so idle time
        accrues from the first paint rather than the first turn."""
        if self._segment_start == 0.0:
            self._segment_start = time.monotonic()

    def finalize_active_time(self) -> None:
        """Fold the open active-time segment into ``duration_seconds`` and stop
        the clock. Called once at shutdown so the running total is complete.

        Closing the segment (``_segment_start = 0``) is what makes this correct:
        a following ``persist`` then adds nothing (its own ``elapsed`` is 0), so
        the final segment is counted exactly once — not dropped by a
        cache-skipped ``persist`` (the increment would be lost), nor double-added
        by ``persist`` recomputing ``elapsed`` from a still-open segment."""
        if self._segment_start:
            self.duration_seconds += time.monotonic() - self._segment_start
            self._segment_start = 0.0

    def reset(self) -> None:
        self.history = []
        self.usage = RunUsage()
        self.duration_seconds = 0.0
        self._segment_start = 0.0
        self.deps.tasks.clear()
        if self.store is not None:
            self.store.clear()

    def new_session(self, name: str | None = None, model_id: str | None = None) -> None:
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

    async def maybe_compact(self, *, force: bool = False) -> bool:
        """Compact the history when over budget (or unconditionally when ``force``,
        used after a provider context-overflow error where the token estimate
        undershot). Returns True if it actually shrank the history."""
        before = len(self.history)
        # This predicate mirrors compact_history's own decision exactly, so the
        # PreCompact hook and the on_compact_start indicator fire iff a compaction
        # will actually happen. A forced compaction is always "going".
        going = force or will_compact(
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
        start_cb = self.on_compact_start if going else None
        indicator_shown = start_cb is not None
        if start_cb is not None:
            start_cb()
        if self.summarizer is not None:
            new_history, did = await compact_history_with_summary(
                self.history, self.max_context_tokens, self.summarizer,
                self.keep_last_messages, force=force,
            )
        else:
            new_history, did = compact_history(
                self.history, self.max_context_tokens, self.keep_last_messages,
                force=force,
            )
        if did:
            self.history = new_history
            # Persist the compacted history now. The post-turn compaction runs
            # after the turn's own persist, so without this the smaller history
            # lives only in memory until the next turn — a process death between
            # turns would lose it and leave the rollback baseline diverged from
            # disk. The setter bumped the version, so a plain persist() writes.
            self.persist()
        # on_compact both reports the result AND clears the "compacting…" notice,
        # so it must fire whenever the notice was shown — not only when history
        # shrank. A forced compaction (post-overflow) can run without shrinking;
        # before == len(history) then signals the UI to just drop the notice
        # instead of leaving a stuck spinner.
        if self.on_compact is not None and (did or indicator_shown):
            self.on_compact(before, len(self.history))
        return did

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
        # Metadata-only change — history didn't move, so the version didn't
        # bump. Force the persist so the rename survives a restart.
        self.persist(force=True)
        if self.on_rename is not None:
            self.on_rename(old, title)

    async def rename(self, name: str | None = None) -> str | None:
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
        self.persist(force=True)
        return new
