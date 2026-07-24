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
    BREAKER_NOTICE,
    CompactionBreaker,
    Summarizer,
    Titler,
    _measured_or_estimated,
    _plan_tail_start,
    compact_history,
    compact_history_with_summary,
    estimate_tokens,
    make_summarizer,
    make_titler,
    mask_stale_observations,
    revalidate_elided_pointers,
)
from ..hooks import events as hook_events
from ..hooks.runner import HookVerdict, base_payload
from ..runtime.deps import Deps
from ..workspace.scratchpad import persist_elided
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
        # Fired right BEFORE the compacted history is persisted, but only when a
        # compaction stage RESTRUCTURED the history (its message count changed, so
        # absolute message indices moved). The TurnController wires this to
        # CheckpointManager.invalidate_after_compaction so the checkpoint sidecar
        # is rewritten before the shorter history hits disk — a crash between the
        # two writes then can't leave checkpoints indexing a history that no longer
        # exists. A mask-only (micro) compaction leaves the count unchanged and
        # every index valid, so it does NOT fire (invalidating would needlessly
        # destroy the user's rewind history). Left None by default (embedders /
        # tests without checkpoints), and inert until the controller wires it.
        self.on_history_restructured: Callable[[], None] | None = None
        self.on_rename: Callable[[str, str], None] | None = None
        # The in-flight background autoname, if any (see schedule_autoname).
        # Doubles as the strong reference that keeps the task alive.
        self._autoname_task: asyncio.Task[None] | None = None
        # Rapid-refill breaker for auto-compaction plus its one-shot notice
        # flag. In-memory only: a resumed session re-measures, so persisting
        # breaker state would carry stale thrash verdicts across restarts.
        self.breaker = CompactionBreaker()
        self._breaker_noticed = False
        self.on_notice: Callable[[str], None] | None = None

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
                # Snapshot tasks and jobs at the SAME point as the history, into
                # locals, rather than reading them live as save() arguments. The
                # three must describe one generation: an abandoned/orphaned writer
                # (see above) that read tasks/jobs live at save time could pair the
                # history it froze here with tasks/jobs a LATER turn has since
                # mutated, writing an internally inconsistent snapshot (history
                # missing work that its task/job list already reflects). Capturing
                # them adjacently keeps the trio consistent to the same degree the
                # history snapshot already is.
                tasks_snapshot = self.deps.tasks.to_payload()
                jobs_snapshot = self.deps.jobs.export_settled()
                self.store.save(
                    history_snapshot, self.usage, tasks_snapshot,
                    duration_seconds=self.duration_seconds + elapsed,
                    jobs=jobs_snapshot,
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

    @property
    def saved_advisor_id(self) -> str | None:
        """The advisor persisted with this session — a provider:slug, the
        "off" sentinel, or None (unset) — or None if no store."""
        return self.store.advisor_model if self.store is not None else None

    def set_advisor(self, value: str) -> None:
        """Persist the session's advisor choice (a provider:slug or the "off"
        sentinel). Same metadata-only patch rules as ``set_model``: a switch
        can land mid-turn when in-memory history must never reach disk, so
        patch the header when a file exists, else force one clean persist."""
        if self.store is not None:
            self.store.advisor_model = value
            if self.store.path.exists():
                self.store.save_meta()
            else:
                self.persist(force=True)

    @property
    def saved_thinking_id(self) -> str | None:
        """The thinking level persisted with this session — a level name
        (including "off"), or None (unset) — or None if no store."""
        return self.store.thinking if self.store is not None else None

    def set_thinking(self, value: str) -> None:
        """Persist the session's thinking choice (a member of
        thinking.THINKING_LEVELS). Same metadata-only patch rules as
        ``set_advisor``: a switch can land mid-turn when in-memory history must
        never reach disk, so patch the header when a file exists, else force one
        clean persist."""
        if self.store is not None:
            self.store.thinking = value
            if self.store.path.exists():
                self.store.save_meta()
            else:
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
        # A session can outlive its /tmp scratchpad (reboot, systemd-tmpfiles
        # aging), leaving elided-pointer placeholders in the persisted history
        # that promise a read_file the model can no longer perform. Revalidate
        # them HERE, at the load seam every resume/switch passes through, and
        # nowhere else: this is a cache-cold moment — no provider has a warm
        # prompt cache for a history this process hasn't sent yet — so the
        # rewrite costs nothing, whereas a per-turn check would rewrite
        # mid-session and bust the prompt-cache tail on every dangling hit.
        # Dangling pointers degrade to the plain masked placeholder ("re-run
        # the tool"). No forced write: revalidate returns the same list when
        # nothing dangles, and when it does rewrite, the history setter below
        # bumps history_version, so the healed history rides the next normal
        # persist to disk.
        history, n_dangling = revalidate_elided_pointers(history)
        if n_dangling:
            logger.debug(
                "session load: degraded %d dangling elided pointer(s) to the "
                "plain placeholder (scratchpad file gone)", n_dangling,
            )
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
        self.breaker.reset()
        self._breaker_noticed = False
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
            self._purge_session_sidecars(self.store)
        # /clear (TUI reset_conversation -> Harness.reset -> here) does not go
        # through _load_active_store, so it must reset the breaker itself — a
        # cleared conversation must always start with a closed breaker.
        self.breaker.reset()
        self._breaker_noticed = False

    def _purge_session_sidecars(self, store: SessionStore) -> None:
        """Remove the per-id sidecars that ``store.clear()`` leaves behind on a
        /clear: the sub-agent transcript dir, the image cache dir, and the
        scratchpad dir — all keyed by the (still-live) session id.

        Without this, a transcript sidecar still stamped ``status="running"`` (a
        spawn interrupted before /clear) resurrects as a phantom "interrupted
        spawn" card when the now-empty session is resumed, and stale cached images
        linger. This mirrors ``SessionManager.delete``'s cleanup but keeps the
        session id and its JSON path alive (a following turn re-saves it) — it does
        NOT touch checkpoint refs, which the harness clears on its own /clear path.
        Best-effort: a missing artifact never blocks the clear."""
        import shutil

        from ..images import image_cache_root
        from ..workspace.scratchpad import scratchpad_root
        from .transcripts import TranscriptStore

        sid = store.session_id
        TranscriptStore(store.path, sid).delete_all()
        shutil.rmtree(image_cache_root() / sid, ignore_errors=True)
        shutil.rmtree(
            scratchpad_root(self.deps.workspace.root, sid).parent,
            ignore_errors=True,
        )

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
        self.breaker.reset()
        self._breaker_noticed = False

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

    def _elided_persist(self) -> Callable[[str, str], str | None] | None:
        """The persist callback for mask_stale_observations, or None when the
        scratchpad is unavailable — masking then degrades to plain placeholders."""
        get = getattr(self.deps, "get_scratchpad", None)
        if get is None:
            return None
        pad = get()
        if pad is None:
            return None

        def persist(content: str, tool_name: str) -> str | None:
            path = persist_elided(pad, content, tool_name)
            return str(path) if path is not None else None

        return persist

    async def _dispatch_pre_compact(
        self, trigger: str, instructions: str | None
    ) -> HookVerdict:
        if self.deps.hooks is None:
            return HookVerdict()
        return await self.deps.hooks.dispatch_verdict(
            hook_events.PRE_COMPACT,
            base_payload(
                hook_events.PRE_COMPACT,
                session_id=self.store.session_id if self.store is not None else "",
                cwd=str(self.deps.workspace.root),
                transcript_path=str(self.store.path) if self.store is not None else "",
                trigger=trigger,
                custom_instructions=instructions or "",
            ),
        )

    async def _dispatch_post_compact(
        self, trigger: str, pre_tokens: int, post_tokens: int, stage: str
    ) -> None:
        """Fire the PostCompact hook with before/after token counts. Note the two
        counts are measured differently: ``pre_compact_tokens`` may be the
        provider-measured last-request count (when a real measurement was
        available), while ``post_compact_tokens`` is always the char/4 estimate of
        the freshly compacted history — so a small pre/post delta can reflect the
        estimator, not only the actual reduction."""
        if self.deps.hooks is None:
            return
        await self.deps.hooks.dispatch(
            hook_events.POST_COMPACT,
            base_payload(
                hook_events.POST_COMPACT,
                session_id=self.store.session_id if self.store is not None else "",
                cwd=str(self.deps.workspace.root),
                transcript_path=str(self.store.path) if self.store is not None else "",
                trigger=trigger,
                pre_compact_tokens=pre_tokens,
                post_compact_tokens=post_tokens,
                stage=stage,
            ),
        )

    def _breaker_should_skip(self, over: bool, manual: bool, force: bool) -> bool:
        """True if auto-compaction should be skipped because the breaker is
        open (thrashing: compacting again would just refill). Manual and
        forced compaction always bypass the breaker. Fires the one-shot
        notice (only once per open spell) as a side effect."""
        if not (over and not (manual or force) and self.breaker.open):
            return False
        if not self._breaker_noticed and self.on_notice is not None:
            self._breaker_noticed = True
            self.on_notice(BREAKER_NOTICE)
        return True

    async def _verdict_blocks(
        self, trigger: str, instructions: str | None, *, manual: bool
    ) -> bool:
        """Dispatch PreCompact and return True if ``maybe_compact`` should
        abort. A block verdict is honored ONLY for a manual /compact — a hook
        must never be able to wedge a session into the hard context limit, so
        on auto/force it's merely logged and compaction proceeds."""
        verdict = await self._dispatch_pre_compact(trigger, instructions)
        if not verdict.blocked:
            return False
        if manual:
            if self.on_notice is not None:
                reason = f": {verdict.reason}" if verdict.reason else ""
                self.on_notice(f"Compaction blocked by PreCompact hook{reason}")
            return True
        logger.info("PreCompact block ignored (trigger=%s): %s", trigger, verdict.reason)
        return False

    async def _prepare_compact(self, *, manual: bool) -> None:
        """Breaker bookkeeping and limits refresh that must happen before the
        size gate is evaluated: a manual /compact always resets the breaker
        (a deliberate, user-initiated compaction is never "thrashing"),
        while an auto/force attempt feeds the rapid-refill window so a
        subsequent open-breaker check sees this attempt. Limits are
        re-resolved so ``compact_threshold`` reflects the current model."""
        if manual:
            self.breaker.reset()
            self._breaker_noticed = False
        else:
            # Also fires on the mid-turn force-recovery invocation (force=True,
            # not manual) — conservative: an extra note_turn only makes the
            # rapid-refill breaker trip a little later, never suppresses it.
            self.breaker.note_turn()
        # Warm window discovery before gating: this is an async site, and the
        # resolver caches, so all later sync reads (the gauge, the property
        # above) see the discovered window. Never raises — discovery is
        # best-effort by contract.
        if self.limits is not None:
            model_id = self.get_model_id() if self.get_model_id else None
            await self.limits.resolve(model_id)

    def _stage_mask(self, *, force: bool, manual: bool) -> bool:
        """STAGE 1 — microcompact: elide stale tool observations (persisting
        payloads to the scratchpad when available). Runs before the
        summarizer so that when old tool output IS the bloat, we get under
        threshold without a model call. Cache-safe: this only ever runs when
        the gate has tripped, i.e. when a history rewrite (and its cache
        miss) was about to happen anyway. Force/manual run it regardless of
        the routine-hygiene toggle — force is recovery of last resort, and a
        manual /compact asks for maximum reduction. Mutates ``self.history``
        and returns whether it actually shrank."""
        if not (self.mask_observations or force or manual):
            return False
        masked_history, n_masked = mask_stale_observations(
            self.history,
            self.mask_keep_recent,
            min_chars=self.mask_min_chars,
            persist=self._elided_persist(),
        )
        if not n_masked:
            return False
        self.history = masked_history
        return True

    async def _stage_summarize(
        self,
        threshold: int,
        *,
        manual: bool,
        force: bool,
        has_masked: bool,
        instructions: str | None,
    ) -> bool:
        """STAGE 2 — summarize-compact, only if still over (manual/force always
        proceed: the user or the overflow retry asked for a real compaction).
        After a stage-1 mask the provider's measured count is stale (the
        history just shrank under it), so the tail planner runs on the
        estimate alone in that case — but when stage 1 didn't touch the
        history (masking off/ineffective, ``has_masked`` False),
        last_input_tokens is still fresh and must keep gating here exactly as
        it did the entry check in ``maybe_compact``; otherwise a measured-only
        overflow (dense content the char/4 estimate undershoots) would trip
        the initial gate, fire PreCompact, then silently do nothing. Mutates
        ``self.history`` and returns whether it actually shrank."""
        measured = None if has_masked else self.last_input_tokens
        still_over = _measured_or_estimated(self.history, measured) > threshold
        if not (manual or force or still_over):
            return False
        tail_start = _plan_tail_start(
            self.history, threshold, self.keep_last_messages,
            force=force or manual, measured_tokens=measured,
        )
        if tail_start is None:
            return False
        if self.summarizer is not None:
            new_history, did = await compact_history_with_summary(
                self.history, threshold, self.summarizer,
                self.keep_last_messages, force=force or manual,
                tail_start=tail_start, instructions=instructions,
            )
        else:
            new_history, did = compact_history(
                self.history, threshold, self.keep_last_messages,
                force=force or manual, tail_start=tail_start,
            )
        if did:
            self.history = new_history
        return did

    async def maybe_compact(
        self,
        *,
        force: bool = False,
        trigger: str = "auto",
        instructions: str | None = None,
    ) -> bool:
        """Run the staged reduction pipeline: mask stale tool observations
        first, then summarize-compact only if the history is still over
        threshold. ``force`` is the post-overflow path (the estimate is known
        to have undershot); ``trigger="manual"`` is the /compact command —
        it bypasses the size gate and the breaker, and is the only trigger a
        PreCompact hook can block. Returns True if the history shrank."""
        before = len(self.history)
        manual = trigger == "manual"
        await self._prepare_compact(manual=manual)
        threshold = self.compact_threshold
        pre_tokens = _measured_or_estimated(self.history, self.last_input_tokens)
        over = pre_tokens > threshold
        if not (over or force or manual):
            return False
        if self._breaker_should_skip(over, manual, force):
            return False
        if await self._verdict_blocks(trigger, instructions, manual=manual):
            return False
        # Fire PreCompact *before* the compaction work, while the transcript is
        # still full — matching Claude Code, where the hook can snapshot the
        # conversation before it's summarized/collapsed.
        indicator_shown = self.on_compact_start is not None
        if self.on_compact_start is not None:
            self.on_compact_start()
        stages: list[str] = []
        if self._stage_mask(force=force, manual=manual):
            stages.append("micro")
        if await self._stage_summarize(
            threshold, manual=manual, force=force,
            has_masked="micro" in stages, instructions=instructions,
        ):
            stages.append("summary")
        compacted = bool(stages)
        if compacted:
            # The measurement that triggered this compaction described the old,
            # larger history; carried forward it would gate the NEXT
            # maybe_compact on max(estimate, stale_measured) and re-compact a
            # history that now comfortably fits (detail loss and a busted
            # prompt cache for nothing). Drop it — the estimate governs until
            # the next real request reports usage.
            self.last_input_tokens = None
            # Invalidate checkpoints BEFORE persisting when the history was
            # restructured (message count changed → absolute indices moved). This
            # ordering is the crash-safety guarantee: if the process dies between
            # the two writes, the checkpoint sidecar is already gone/consistent
            # rather than left pointing into a history that the persist below is
            # about to shorten. A mask-only compaction preserves the count, so its
            # indices stay valid and we must NOT invalidate (that would throw away
            # the user's rewind points for nothing). The controller owns the
            # CheckpointManager and wires on_history_restructured.
            if len(self.history) != before and self.on_history_restructured is not None:
                self.on_history_restructured()
            # Persist the compacted history now: the post-turn compaction runs
            # after the turn's own persist, so without this the smaller history
            # lives only in memory until the next turn — a process death
            # between turns would lose it and leave the rollback baseline
            # diverged from disk. The setter bumped the version, so a plain
            # persist() writes.
            self.persist()
            self.breaker.note_compact()
            await self._dispatch_post_compact(
                trigger, pre_tokens, estimate_tokens(self.history), "+".join(stages)
            )
        # on_compact both reports the result AND clears the "compacting…"
        # notice, so it must fire whenever the notice was shown — not only
        # when history shrank.
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
            logger.warning("autoname titler failed: %s", exc, exc_info=True)
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
