from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, SupportsIndex

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from ..config.context_limits import ContextLimits

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
    mask_stale_observations,
)
from ..hooks import events as hook_events
from ..hooks.runner import base_payload
from ..runtime.deps import Deps
from .store import SessionInfo, SessionManager, SessionStore

logger = logging.getLogger(__name__)


def aux_model_for(model: Model, *, cwd: str) -> Model:
    """The model the aux agents (summarizer/titler) should run on.

    A ``ClaudeCliModel`` carries the live ``session_id``, so an aux agent sharing
    it would resume — and reply into — the user's real Claude session (dropping
    its own instructions). Such a model is swapped for a stateless, read-only
    ``ephemeral_clone`` that never resumes or stores a session; every other
    provider reuses the one model unchanged.

    This is the SINGLE source of that decision: both bootstrap (initial build)
    and ``update_model`` (runtime ``/model`` switch) route the model through here,
    so the clone can never be dropped on one path but kept on the other."""
    from ..config.claude_cli_model import ClaudeCliModel

    if isinstance(model, ClaudeCliModel):
        return model.ephemeral_clone(cwd=cwd)
    return model


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
        mask_observations: bool = False,
        mask_keep_recent: int = 4,
        mask_min_chars: int = 200,
        limits: ContextLimits | None = None,
        get_model_id: Callable[[], str | None] | None = None,
    ) -> None:
        self.store = store
        self.manager = manager
        self.deps = deps
        self.max_context_tokens = max_context_tokens
        self.keep_last_messages = keep_last_messages
        self.summarizer = summarizer
        self.titler = titler
        # When set, compaction also elides older tool-observation payloads in the
        # retained tail (see mask_stale_observations). Off by default so the
        # behaviour is opt-in for non-TUI/embedding callers; the harness wires the
        # user-facing toggle through HarnessConfig.
        self.mask_observations = mask_observations
        self.mask_keep_recent = mask_keep_recent
        self.mask_min_chars = mask_min_chars
        # The window/budget resolver, when the harness wires one (headless and
        # TUI both do via build_collaborators; embedders may leave it None and
        # keep the fixed max_context_tokens gate). get_model_id reads the LIVE
        # model so a /model switch re-keys the threshold without rewiring.
        self.limits = limits
        self.get_model_id = get_model_id
        self.history_version: int = 0
        self._last_persisted_version: int = -1
        # Serializes concurrent persist() writers — see persist() for why an
        # unserialized abandoned writer can clobber a newer write.
        self._persist_lock = threading.Lock()
        # ``history`` is a property; the underlying list lives in ``_history``.
        # The setter bumps ``history_version`` so the persist cache can detect
        # no-op writes — set both fields before the first assignment below.
        # No annotation here: it would redeclare the property and shadow it.
        self.history = []
        self.usage: RunUsage = RunUsage()
        # The provider-reported input-token count of the most recent request, i.e.
        # the ACTUAL size of the context last sent — the authoritative signal for
        # the compaction trigger. ``usage`` (above) is a session-cumulative total
        # and must NOT be used for this; this field is the single last request's
        # value, set by the turn runner after each run. None until the first run
        # reports usage, in which case the gate falls back to the char/4 estimate.
        self.last_input_tokens: int | None = None
        self.duration_seconds: float = 0.0
        self._segment_start: float = 0.0
        self.on_compact: Callable[[int, int], None] | None = None
        self.on_compact_start: Callable[[], None] | None = None
        self.on_rename: Callable[[str, str], None] | None = None
        # The in-flight background autoname, if any (see schedule_autoname).
        # Doubles as the strong reference that keeps the task alive.
        self._autoname_task: asyncio.Task[None] | None = None

    @property
    def session_name(self) -> str | None:
        return self.store.name if self.store is not None else None

    @property
    def total_tokens(self) -> int:
        return self.usage.total_tokens

    @property
    def compact_threshold(self) -> int:
        """The compaction trigger: the resolver's threshold for the current
        model when one is wired (already min(budget, 0.8 × window)), else the
        legacy fixed budget. Sync and I/O-free — the status gauge reads this
        every frame; maybe_compact warms discovery before comparing."""
        if self.limits is not None:
            model_id = self.get_model_id() if self.get_model_id else None
            return self.limits.threshold(model_id)
        return self.max_context_tokens

    @property
    def known_window(self) -> int | None:
        """The KNOWN served context window for the current model (override or
        discovered), or None. Distinct from ``compact_threshold`` — this is the
        raw window the overflow-contention classifier needs, not the derived
        min(budget, 0.8 × window) trigger."""
        if self.limits is None:
            return None
        model_id = self.get_model_id() if self.get_model_id else None
        return self.limits.window_for(model_id)

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
            # Serialize the writers. persist() runs from worker threads
            # (asyncio.to_thread), and the Ctrl-C flush path *abandons* its
            # worker after a short deadline without stopping it — on a stalled
            # disk that orphan resumes later and, unserialized, would land its
            # stale snapshot over a newer persist. The lock forces the orphan
            # to either finish before the newer write (which then overwrites
            # it) or re-check the cache below and no-op. Capture the version
            # *inside* the lock and stamp that captured value after the save:
            # stamping the then-current self.history_version instead would mark
            # history the orphan never wrote as persisted, cache-skipping the
            # write that should heal the disk.
            with self._persist_lock:
                version = self.history_version
                if not force and version == self._last_persisted_version:
                    return
                elapsed = (
                    (time.monotonic() - self._segment_start) if self._segment_start else 0.0
                )
                # Snapshot the history list (not deep-copy the messages —
                # just freeze the list's own length) right before it's handed
                # to the serializer. `_persist_lock` above only serializes
                # persist-vs-persist, not persist-vs-*mutation* of the same
                # `_VersionedHistory` object — see the orphaned-worker comment
                # above for how an unserialized persist can outlive its turn.
                # If a later turn appends to `self.history` while the orphan
                # is still iterating it, dump_python can raise "list changed
                # size during iteration" or write a torn snapshot. `list(...)`
                # gives the orphan its own fixed-length copy that later
                # appends can't touch, regardless of what `self._history`
                # points to afterward. A SHALLOW copy suffices: the
                # ModelMessage objects inside are still shared, not
                # deep-copied, but that's fine because turn code only ever
                # appends new messages, never mutates ones already in the
                # list.
                history_snapshot = list(self.history)
                self.store.save(
                    history_snapshot, self.usage, self.deps.tasks.to_payload(),
                    duration_seconds=self.duration_seconds + elapsed,
                    jobs=self.deps.jobs.export_settled(),
                )
                self._last_persisted_version = version

    @property
    def saved_model_id(self) -> str | None:
        """The model id persisted with this session, or None if unavailable."""
        return self.store.model if self.store is not None else None

    def set_model(self, model_id: str) -> None:
        if self.store is not None:
            self.store.model = model_id
            if self.store.path.exists():
                # Metadata-only on-disk patch, NOT a full persist: a model
                # switch can land mid-turn, when the in-memory history may end
                # in unanswered tool calls that must never reach disk — the
                # same dirty-history rule rename/autoname follow.
                self.store.save_meta()
            else:
                # No session file yet (nothing persisted, so no clean baseline
                # to protect): save_meta would silently no-op and the choice
                # would be lost on an immediate session switch. Create the file.
                self.persist(force=True)

    def update_model(self, model: Model) -> None:
        """Rebuild aux agents (summarizer/titler) for a new model. Only
        replaces those that were originally configured — a None stays None.

        The aux agents are built on ``aux_model_for(model)``, never the raw
        model: switching TO a claude-cli model must NOT hand the session-carrying
        instance to the summarizer/titler (see ``aux_model_for``), the same guard
        bootstrap applies at initial build."""
        aux = aux_model_for(model, cwd=str(self.deps.workspace.root))
        if self.summarizer is not None:
            self.summarizer = make_summarizer(aux)
        if self.titler is not None:
            self.titler = make_titler(aux)

    def _load_active_store(self, store: SessionStore) -> int:
        """Load history/usage/tasks/duration from ``store`` into this controller,
        rebind ``self.store`` to it, and (re)start the active-time clock. Shared by
        ``resume`` and ``switch_session`` so the load sequence can't drift between
        them. Returns the loaded message count.

        ``store.load()`` runs FIRST, into locals, and is the only step that can
        raise (a corrupt/version-skewed file is a designed ``SessionLoadError``
        path). ``self.store`` and the in-memory history/usage are mutated only
        AFTER it succeeds — so a failed switch leaves the controller wholly on the
        previous session. Rebinding ``self.store`` before the load (the old bug)
        left store=target but history=previous, and the next ``persist()`` wrote
        the previous session's history over the target's file."""
        history, usage, tasks, prev_duration, jobs = store.load()  # may raise
        self.store = store
        self.history = history
        self.usage = usage
        # Per-request context size isn't persisted, and it belongs to the process
        # that made the request — a resumed/switched session hasn't sent one yet,
        # so drop any carried-over value and let the estimate gate until the first
        # run of this session reports usage.
        self.last_input_tokens = None
        self.deps.tasks.load(tasks)
        self.deps.jobs.import_history(jobs)
        self.duration_seconds = prev_duration or 0.0
        self._segment_start = time.monotonic()
        return len(self.history)

    def resume(self) -> int:
        if self.store is None:
            return 0
        return self._load_active_store(self.store)

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
        self.cancel_autoname()
        self.history = []
        self.usage = RunUsage()
        self.last_input_tokens = None
        self.duration_seconds = 0.0
        self._segment_start = 0.0
        self.deps.tasks.clear()
        if self.store is not None:
            self.store.clear()

    def new_session(self, name: str | None = None, model_id: str | None = None) -> None:
        if self.manager is None:
            self.reset()
            return
        self.cancel_autoname()
        self.store = self.manager.create(name)
        if model_id is not None:
            self.store.model = model_id
        self.history = []
        self.usage = RunUsage()
        self.last_input_tokens = None
        self.duration_seconds = 0.0
        self._segment_start = time.monotonic()
        self.deps.tasks.clear()

    def switch_session(self, session_id: str) -> int:
        if self.manager is None:
            return 0
        # Load the target BEFORE touching any controller state: _load_active_store
        # rebinds self.store only after store.load() succeeds, so a corrupt target
        # (SessionLoadError) propagates with the controller still coherently on the
        # current session — nothing to clobber the target's file later. The old
        # session's in-flight autoname is dropped only once the switch commits, so
        # a failed switch doesn't needlessly cancel titling we're staying with.
        result = self._load_active_store(self.manager.store(session_id))
        self.cancel_autoname()
        return result

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
        #
        # Warm window discovery before gating: this is an async site, and the
        # resolver caches, so all later sync reads (the gauge, the property
        # above) see the discovered window. Never raises — discovery is
        # best-effort by contract.
        if self.limits is not None:
            model_id = self.get_model_id() if self.get_model_id else None
            await self.limits.resolve(model_id)
        threshold = self.compact_threshold
        # Gate on the larger of the char/4 estimate and the provider's real
        # last-request input-token count (last_input_tokens): the estimate
        # undershoots dense code/JSON by ~25%, so on its own it lets a session
        # sail past the true window. When no request has reported usage yet the
        # helper falls back to the estimate alone (legacy behavior).
        tail_start = _plan_tail_start(
            self.history, threshold, self.keep_last_messages,
            force=force, measured_tokens=self.last_input_tokens,
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
                    cwd=str(self.deps.workspace.root),
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
                self.history, threshold, self.summarizer,
                self.keep_last_messages, force=force, tail_start=tail_start,
            )
        else:
            new_history, compacted = compact_history(
                self.history, threshold, self.keep_last_messages,
                force=force, tail_start=tail_start,
            )
        # Mask stale tool observations only when a compaction actually fired: the
        # rewrite has already invalidated the cached message tail, so eliding the
        # bulky payloads here is free of any extra cache miss (a per-turn mask
        # would bust that cache every turn instead). Skipped when nothing compacted
        # so a warm cache stays warm.
        if compacted and self.mask_observations:
            new_history, _ = mask_stale_observations(
                new_history,
                self.mask_keep_recent,
                min_chars=self.mask_min_chars,
            )
        if compacted:
            self.history = new_history
            # Persist the compacted history now. The post-turn compaction runs
            # after the turn's own persist, so without this the smaller history
            # lives only in memory until the next turn — a process death between
            # turns would lose it and leave the rollback baseline diverged from
            # disk. The setter bumped the version, so a plain persist() writes.
            self.persist()
        elif force:
            # Forced (post-overflow) compaction found no droppable prefix: the
            # overflow lives inside a single enormous turn, which the tail
            # planner can't split. Without a fallback the retry fails
            # identically and the session wedges until a manual /clear — so
            # mask stale tool observations instead, the one lever that shrinks
            # a turn in place. Runs regardless of the mask_observations toggle:
            # that flag governs routine compaction hygiene, while this is a
            # recovery of last resort (and the cache concern is moot — the
            # request just failed, there is no warm tail to protect).
            masked_history, masked = mask_stale_observations(
                self.history,
                self.mask_keep_recent,
                min_chars=self.mask_min_chars,
            )
            if masked:
                self.history = masked_history
                self.persist()
                compacted = True
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
        # Capture the store and snapshot the history up front: this usually runs
        # as a background task (schedule_autoname), so by the time the titler's
        # LLM round-trip returns, the user may have switched sessions (self.store
        # rebound) and the next turn may be rewriting self.history under us.
        store = self.store
        history = list(self.history)
        old = store.name
        try:
            title = await self.titler(history)
        except Exception as exc:
            logger.warning("autoname titler failed: %s", exc)
            return
        if not title:
            return
        # Re-check after the await. A session switch rebound self.store — this
        # title belongs to the *old* transcript, so applying it now would name
        # the wrong session. And an explicit rename() flipped auto_named — the
        # user's chosen name must win over the generated one.
        if self.store is not store or not store.auto_named:
            return
        store.name = title
        store.auto_named = False
        # Metadata-only on-disk patch, deliberately NOT a full persist: running
        # in the background means this can land mid-turn, when the in-memory
        # history may end in unanswered tool calls that must never reach disk
        # (the approval-round invariant in TurnController._run_with_approval).
        # save_meta rewrites just the name/auto header, never the messages.
        store.save_meta()
        if self.on_rename is not None:
            self.on_rename(old, title)

    def schedule_autoname(self) -> None:
        """Run ``maybe_autoname`` as a background task.

        The titler is a full LLM round-trip whose result is cosmetic metadata —
        nothing in the turn depends on it — so the turn (and the TUI's busy
        spinner / queued-prompt drain behind it) must not block on it. At most
        one task is in flight; ``auto_named`` only flips off on success, so a
        skipped schedule simply retries at the next turn's end. Headless callers
        settle the task before teardown via ``wait_autoname``."""
        if self._autoname_task is not None and not self._autoname_task.done():
            return
        if (
            self.titler is None
            or self.store is None
            or not self.store.auto_named
            or not self.history
        ):
            return
        task = asyncio.get_running_loop().create_task(self.maybe_autoname())
        # _autoname_task is the strong reference keeping the task from being
        # GC'd mid-flight; the callback clears it and surfaces failures —
        # maybe_autoname already swallows titler errors, so anything escaping
        # is a real bug that must not vanish into a never-awaited task.
        task.add_done_callback(self._on_autoname_done)
        self._autoname_task = task

    def _on_autoname_done(self, task: asyncio.Task[None]) -> None:
        if self._autoname_task is task:
            self._autoname_task = None
        if not task.cancelled() and task.exception() is not None:
            logger.warning("background autoname failed", exc_info=task.exception())

    async def wait_autoname(self) -> None:
        """Block until any in-flight background autoname settles. Headless runs
        call this before teardown: the process exits right after the turn, and
        an unawaited task would silently never title the session. asyncio.wait
        never re-raises, so a failed or cancelled task can't break the caller."""
        if self._autoname_task is not None:
            await asyncio.wait([self._autoname_task])

    def cancel_autoname(self) -> None:
        """Drop any in-flight background autoname. Called when the session being
        titled stops being current (new/switch/reset) and on TUI exit, where
        quit must stay snappy rather than wait out a titler call. ``auto_named``
        stays True, so the next resume's first turn retries."""
        if self._autoname_task is not None:
            self._autoname_task.cancel()
            self._autoname_task = None

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
        # Metadata-only on-disk patch, like the autoname apply above — and for
        # the same reason: the TUI dispatches slash commands even mid-turn, so
        # /name during an approval wait would otherwise full-persist a history
        # ending in unanswered tool calls (the invariant in
        # TurnController._run_with_approval). When the session file doesn't
        # exist yet this is a no-op and the name lands with the next full
        # persist (turn end / app exit / headless teardown).
        self.store.save_meta()
        return new
