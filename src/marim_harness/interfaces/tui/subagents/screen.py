"""Full-bleed sub-agents screen controller (ctrl+x).

Owns the open/navigate/close lifecycle of the sub-agents view and the
coalescing protocol that keeps a fan-out from pinning a core. Extracted from
:class:`HarnessApp` so the dense invariants — cursor-as-source-of-truth
selection sync, dirty-flag-on-the-flush-tick repaint coalescing, and lazy
transcript replay — live in one cohesive, independently-testable place.

It holds a back-reference to the ``HarnessApp`` because every operation drives
the app's mounted widgets (``query_one``), workers (``run_worker``), and the
``StreamRenderer``'s live card list (``app.stream.subagents``). The view itself
is a widget mounted in the main screen and shown/hidden via ``display`` — not a
pushed Textual ``Screen`` — so the renderer's panes stay mounted whether or not
the screen is open, and opening mid-run shows an already-current transcript.
"""

from typing import TYPE_CHECKING

from textual.containers import VerticalScroll
from textual.css.query import NoMatches

from ..widgets import NoticeMessage, PromptInput
from .stats import tree_order
from .view import SubAgentsView

if TYPE_CHECKING:
    from ..app import HarnessApp


class SubAgentsScreen:
    """Drives the ctrl+x sub-agents screen on behalf of ``HarnessApp``.

    Despite the name, this is a plain controller, not a Textual ``Screen``, and
    is never pushed onto the app's screen stack. ``SubAgentsView`` — the actual
    widget — is mounted directly in ``HarnessApp.compose`` and shown/hidden via
    ``display``, so the renderer's transcript panes stay mounted (and keep
    streaming) whether or not the view is on screen.

    This used to subclass ``Screen`` for its ``reactive`` descriptors and its
    ``app`` property. Neither earned the base class: none of the three fields
    below has a watcher, so ``reactive`` bought nothing but a repaint scheduled
    against a node that was never in the DOM, and ``app`` only resolved the
    running app off a context var — which the explicit back-reference below now
    does without the inheritance or the implicit global.
    """

    def __init__(self, app: "HarnessApp") -> None:
        self._app = app
        # Whether the view is on-screen, and which spawned sub-agent (index into
        # app.stream.subagents) is selected.
        self.open = False
        self.index = 0
        # Set by a streamed sub-agent event to ask for a list/summary repaint;
        # the flush tick drains it once per frame via drain_repaint(). Plain
        # state on purpose: an auto-repainting watcher would repaint inline on
        # every event instead of coalescing to the flush tick, reintroducing the
        # fan-out-pins-a-core cost mark_dirty()/drain_repaint() exist to avoid.
        self.dirty = False

    def _ordered(self) -> list:
        """Sub-agents in the list's display (depth-first) order — the same order
        SubAgentList.refresh_rows renders — so a DataTable cursor row maps to the
        correct agent."""
        return [tr.agent for tr in tree_order(self._app.stream.subagents)]

    def toggle(self) -> None:
        """Ctrl+X: open the full-bleed sub-agents screen (or close it if open)."""
        if self.open:
            self.close()
        else:
            self.open_at(None)

    def open_at(self, stream_id: str | None) -> None:
        """Open the screen, selecting ``stream_id`` (or the most recent spawn when
        None — the one you most likely just watched)."""
        app = self._app
        subs = app.stream.subagents
        if not subs:
            app.query_one("#log", VerticalScroll).mount(
                NoticeMessage("No sub-agents spawned yet — nothing to view.")
            )
            return
        ordered = self._ordered()
        index = len(ordered) - 1
        if stream_id is not None:
            index = next(
                (i for i, w in enumerate(ordered) if w.stream_id == stream_id), index
            )
        self.open = True
        self.index = index
        view = app.query_one(SubAgentsView)
        app.query_one("#log", VerticalScroll).display = False
        view.display = True
        self._apply_view()
        view.list.focus()

    def close(self) -> None:
        app = self._app
        self.open = False
        app.query_one(SubAgentsView).display = False
        app.query_one("#log", VerticalScroll).display = True
        app.query_one(PromptInput).focus()

    def resume_selected(self) -> None:
        """The `r` key: resume the selected interrupted spawn as a background
        job. A no-op on any other status."""
        ordered = self._ordered()
        if not ordered or not (0 <= self.index < len(ordered)):
            return
        card = ordered[self.index]
        if card.status != "interrupted":
            return
        # Own group + exit_on_error=False, same rationale as the transcript
        # loader below: the default group belongs to the exclusive turn worker.
        self._app.run_worker(
            self._resume(card), group="subagent-resume", exit_on_error=False
        )

    async def _resume(self, card) -> None:
        resume = self._app.harness.deps.services.resume_subagent
        if resume is None:
            return
        job_id, message = await resume(card.stream_id)
        if job_id is None:
            # Refused: surface the reason in the card's pane; the card stays
            # interrupted so the user can retry after fixing the cause.
            if card.pane is not None:
                card.pane.append_error(message)
        else:
            self._app.stream.adopt_resumed_card(card, job_id)
        self._repaint_list()

    def _repaint_list(self, select: int | None = None) -> None:
        """Repaint the list/summary scalars and show the selected agent's pane.
        Closes the screen if the list emptied. Does NOT flush transcripts — the
        flush tick owns that — so this is safe to call from within the tick without
        re-entering it.

        ``select`` forces the cursor (open/navigate); None preserves it (a live
        stats repaint). The DataTable cursor is the source of truth for the
        selection: we sync ``index`` FROM it after the repaint, so a per-frame
        repaint during a fan-out follows the user's cursor instead of snapping it
        back to a stale stored index (the lag between a key press moving the cursor
        and its async RowHighlighted updating ``index``)."""
        app = self._app
        subs = app.stream.subagents
        if not subs:
            self.close()
            return
        view = app.query_one(SubAgentsView)
        view.repaint(subs, self.cost, selected=select)
        try:
            cursor_row = view.list.cursor_row
        except NoMatches:
            # A flush tick can land between this view being created and its compose
            # children mounting, so the list isn't queryable yet. view.repaint above
            # already skipped for the same reason; skip the cursor sync too rather
            # than crash the live flush path — the next tick repaints once the list
            # exists (mirrors SubAgentsView.repaint's own NoMatches guard).
            return
        ordered = self._ordered()
        self.index = max(0, min(cursor_row, len(ordered) - 1))
        current = ordered[self.index]
        if current.pane is not None:
            view.host.show(current.stream_id)
            self._lazy_load(current)

    def _lazy_load(self, card) -> None:
        """Lazy-load the persisted transcript the first time ``card``'s pane is
        shown. Both selection paths must call this — the repaint (open / flush
        tick) AND the RowHighlighted cursor move: in a resumed idle session
        nothing streams, so the flush tick never repaints and a cursor move is
        the only chance a non-initially-selected pane gets to replay.

        The replay awaits a widget mount per message, but the callers are sync
        (and the repaint fires on every live flush tick) — so flip the guard now
        and hand the actual replay to a one-shot worker, which keeps later
        calls from relaunching it."""
        if card.pane.transcript_loaded:
            return
        card.pane.transcript_loaded = True
        # group="subagent-transcripts": the turn worker runs
        # exclusive=True in the DEFAULT group, and Textual cancels every
        # worker sharing a group when an exclusive worker joins it — a
        # turn starting mid-replay would truncate the transcript with
        # the loaded-guard already set, leaving no retry (the same sweep
        # hazard the shell-passthrough worker in app.py documents).
        # exit_on_error=False: a replay of arbitrary persisted data must
        # degrade to a broken pane, never take down the whole session.
        self._app.run_worker(
            self._load_transcript(card.pane, card.stream_id),
            group="subagent-transcripts",
            exit_on_error=False,
        )

    async def _load_transcript(self, pane, stream_id: str) -> None:
        """Replay a resumed sub-agent's persisted transcript into ``pane``.

        Runs as a worker off the sync repaint path (``_repaint_list`` already set
        ``pane.transcript_loaded``). A missing store or sidecar just renders a
        fallback note — the guard is already set, so it isn't retried."""
        store = self._app.harness.session.store
        if store is None:
            return
        from ....session import TranscriptStore
        msgs = TranscriptStore(store.path, store.session_id).read(stream_id)
        if msgs is not None:
            await self._app.session.replay_messages_into(pane, msgs, parent_id=stream_id)
            # A nested background spawn buried in this transcript replays as a
            # fresh *pending* card — but the main-pass finish_replayed_cards already
            # ran (before this lazy pane load created the card), so nothing would
            # ever settle it and it would dangle pending forever. Re-run the settle
            # join now to fill the newly created child card from jobs history /
            # sidecar meta. Safe to re-run: the join only touches cards whose status
            # is still "pending" (plus the repair-stub-gated running-sidecar elif),
            # so every card the first pass already finished is skipped — idempotent.
            await self._app.session.finish_replayed_cards()
        else:
            from textual.content import Content
            from textual.widgets import Static
            await pane.add(
                Static(Content("transcript unavailable for this resumed sub-agent"))
            )

    def _apply_view(self) -> None:
        """Open/navigate path: repaint the list AND flush the now-selected
        transcript immediately (its stream is skipped while it isn't the host's
        current pane, so it needs a one-off render on selection). Driven by user
        actions (open, cursor move), which are infrequent — unlike the live
        streaming path, which coalesces via mark_dirty."""
        self._repaint_list(select=self.index)
        if self.open:  # still open (not closed by an emptied list)
            self._app.stream.flush_streams()

    def mark_dirty(self) -> None:
        """Mark the open screen for a repaint on the next flush tick. Called from
        the renderer on every streamed sub-agent event; repainting inline per event
        — a full DataTable rebuild plus a transcript flush — pins a core during a
        fan-out, so the actual repaint is coalesced to the ~12.5Hz flush tick
        (drain_repaint). A no-op when closed, so streaming pays nothing for a hidden
        screen."""
        if self.open:
            self.dirty = True

    def drain_repaint(self) -> None:
        """Repaint the open screen's list once if a streamed event marked it dirty
        since the last frame. Called from the flush tick so per-event repaint
        requests collapse into one repaint per frame. No transcript flush here: the
        tick already flushed the visible pane before draining."""
        if self.open and self.dirty:
            self.dirty = False
            self._repaint_list()

    def cost(self, widget) -> float:
        """The dollar cost of one sub-agent for the summary roll-up — the numeric
        cost the renderer already computed via resolve_cost and stored on the card
        (cost_value). 0.0 until metered. No re-costing here: resolve_cost needs the
        full RunUsage split, which the card doesn't keep."""
        return widget.cost_value or 0.0

    def on_row_highlighted(self, event) -> None:
        """Moving the list cursor selects that agent's transcript."""
        if self.open and event.cursor_row is not None:
            ordered = self._ordered()
            if not ordered:
                return
            # Clamp the highlighted row into the ordered list, mirroring
            # _repaint_list — a defensive bound so a stale cursor row never
            # indexes past the tree-ordered agents.
            self.index = max(0, min(event.cursor_row, len(ordered) - 1))
            current = ordered[self.index]
            if current.pane is not None:
                self._app.query_one(SubAgentsView).host.show(current.stream_id)
                self._lazy_load(current)
            self._app.stream.flush_streams()
