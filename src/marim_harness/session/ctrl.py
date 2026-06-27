from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, SupportsIndex

if TYPE_CHECKING:
    from pydantic_ai.models import Model

from pydantic_ai.messages import ModelMessage
from pydantic_ai.usage import RunUsage

from ..compaction import (
    Summarizer,
    Titler,
    _plan_tail_start,
    compact_history,
    compact_history_with_summary,
    make_summarizer,
    make_titler,
)
from ..hooks import events as hook_events
from ..hooks.runner import base_payload
from ..runtime.deps import Deps
from .store import SessionInfo, SessionManager, SessionStore

logger = logging.getLogger(__name__)


class _VersionedHistory(list):
    """A ``list`` that bumps its owner controller's ``history_version`` on every
    in-place mutation.

    The persist cache (`SessionController.persist`) skips the disk write when
    ``history_version`` is unchanged. The property setter bumps the version on
    *replacement* (``self.history = x``), but an in-place mutation
    (``history.append(...)``, ``history += [...]``, ``history[i] = ...``) would
    otherwise change the list without bumping the version — and the next
    ``persist()`` would silently drop it. Routing every mutator through
    ``_bump`` closes that trap so ``.append()`` is safe rather than lossy."""

    def __init__(self, iterable, owner: SessionController) -> None:
        super().__init__(iterable)
        self._owner = owner

    def _bump(self) -> None:
        self._owner.history_version += 1

    def append(self, item) -> None:
        super().append(item)
        self._bump()

    def extend(self, items) -> None:
        super().extend(items)
        self._bump()

    def insert(self, index, item) -> None:
        super().insert(index, item)
        self._bump()

    def pop(self, index: SupportsIndex = -1):
        item = super().pop(index)
        self._bump()
        return item

    def remove(self, item) -> None:
        super().remove(item)
        self._bump()

    def clear(self) -> None:
        super().clear()
        self._bump()

    def sort(self, *args, **kwargs) -> None:
        super().sort(*args, **kwargs)
        self._bump()

    def reverse(self) -> None:
        super().reverse()
        self._bump()

    def __setitem__(self, index, value) -> None:
        super().__setitem__(index, value)
        self._bump()

    def __delitem__(self, index) -> None:
        super().__delitem__(index)
        self._bump()

    def __iadd__(self, other):
        super().__iadd__(other)
        self._bump()
        return self

    def __imul__(self, n):
        super().__imul__(n)
        self._bump()
        return self


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
        # Wrap in a version-tracking proxy so in-place mutations
        # (append/+=/[i]=) bump the version too — see _VersionedHistory.
        self._history = _VersionedHistory(value, self)
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

    @property
    def saved_model_id(self) -> str | None:
        """The model id persisted with this session, or None if unavailable."""
        return self.store.model if self.store is not None else None

    def set_model(self, model_id: str) -> None:
        if self.store is not None:
            self.store.model = model_id
            self.persist(force=True)

    def update_model(self, model: Model) -> None:
        """Rebuild aux agents (summarizer/titler) for a new model. Only
        replaces those that were originally configured — a None stays None."""
        if self.summarizer is not None:
            self.summarizer = make_summarizer(model)
        if self.titler is not None:
            self.titler = make_titler(model)

    def _load_active_store(self) -> int:
        """Load history/usage/tasks/duration from ``self.store`` (assumed set) into
        this controller and (re)start the active-time clock. Shared by ``resume``
        and ``switch_session`` so the load sequence can't drift between them.
        Returns the loaded message count."""
        assert self.store is not None  # callers guard; narrows for the type checker
        self.history, self.usage, tasks, prev_duration = self.store.load()
        self.deps.tasks.load(tasks)
        self.duration_seconds = prev_duration or 0.0
        self._segment_start = time.monotonic()
        return len(self.history)

    def resume(self) -> int:
        if self.store is None:
            return 0
        return self._load_active_store()

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
        return self._load_active_store()

    async def maybe_compact(self, *, force: bool = False) -> bool:
        """Compact the history when over budget (or unconditionally when ``force``,
        used after a provider context-overflow error where the token estimate
        undershot). Returns True if it actually shrank the history."""
        before = len(self.history)
        # Plan the tail boundary exactly once. It both decides whether a
        # compaction will happen (so the PreCompact hook / on_compact_start
        # indicator fire iff one actually will) AND is handed to the compaction
        # call below, so estimate_tokens runs over the whole history once per
        # maybe_compact instead of twice when a compaction fires. The PreCompact
        # hook between here and the compaction is observe-only (it can't mutate
        # the history), so the precomputed boundary stays valid. A forced
        # compaction is always "going"; _plan_tail_start still returns None when
        # there is nothing meaningful to drop.
        tail_start = _plan_tail_start(
            self.history, self.max_context_tokens, self.keep_last_messages,
            force=force,
        )
        going = force or tail_start is not None
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
        indicator_shown = going and self.on_compact_start is not None
        if going and self.on_compact_start is not None:
            self.on_compact_start()
        if self.summarizer is not None:
            new_history, compacted = await compact_history_with_summary(
                self.history, self.max_context_tokens, self.summarizer,
                self.keep_last_messages, force=force, tail_start=tail_start,
            )
        else:
            new_history, compacted = compact_history(
                self.history, self.max_context_tokens, self.keep_last_messages,
                force=force, tail_start=tail_start,
            )
        if compacted:
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
        if self.on_compact is not None and (compacted or indicator_shown):
            self.on_compact(before, len(self.history))
        return compacted

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
