"""Reactive status bar — owns all display state that was scattered across
HarnessApp and StatusPresenter. Setting any reactive triggers an automatic
re-render, eliminating the manual refresh_status() call sites."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from textual.content import Content
from textual.reactive import reactive
from textual.widgets import Static

from ....compaction import estimate_tokens
from ....usage import resolve_cost
from .format import _SPINNER, format_cost, format_duration, format_token_split, human_tokens

if TYPE_CHECKING:
    from ..app import HarnessApp


def osc_title(text: str) -> str:
    """OSC 0 escape that sets the terminal's tab AND window title."""
    return f"\033]0;{text}\007"


class StatusBar(Static):
    """A reactive status bar that auto-renders on state changes."""

    busy: reactive[bool] = reactive(False, init=False)
    live_run_tokens: reactive[int] = reactive(0, init=False)
    last_ttft: reactive[float | None] = reactive(None, init=False)
    model_name: reactive[str] = reactive("", init=False)
    mode: reactive[str] = reactive("", init=False)

    def __init__(self) -> None:
        super().__init__(id="status-bar")
        self.spin = 0
        self.session_start = time.monotonic()
        self.turn_start = time.monotonic()
        # Memoized context-size estimate (see _context_tokens). -1 forces a first
        # compute; 0 is a legitimate cached value for an empty history.
        self._ctx_tokens_key = -1
        self._ctx_tokens = 0
        # Memoized committed-cost estimate (see _session_cost). A sentinel key no
        # real (total, model) pair can equal forces the first compute.
        self._cost_key: object = None
        self._cost: float | None = None

    def _context_tokens(self) -> int:
        """The context-size estimate for the status bar, memoized on history length.

        estimate_tokens() serializes every part of every message (O(total bytes)),
        but the status bar repaints once a second while idle and ~12.5x/s while a
        turn streams — recomputing it each time re-stringifies the whole transcript
        for a number that only moves when a message is committed."""
        app: HarnessApp = self.app  # type: ignore[assignment]
        history = app.harness.session.history
        key = len(history)
        if key != self._ctx_tokens_key:
            self._ctx_tokens_key = key
            self._ctx_tokens = estimate_tokens(history)
        return self._ctx_tokens

    def _session_cost(self) -> float | None:
        """The committed session cost for the status bar, memoized on (token total,
        model). resolve_cost → estimate_cost is a genai-prices table lookup; the
        committed total moves only on commit, so it's an exact change key."""
        app: HarnessApp = self.app  # type: ignore[assignment]
        usage = app.harness.session.usage
        model_id = app.harness.model_id
        key = (usage.total_tokens, model_id)
        if key != self._cost_key:
            self._cost_key = key
            self._cost, _ = resolve_cost(usage, model_id)
        return self._cost

    def render(self) -> Content:
        app: HarnessApp = self.app  # type: ignore[assignment]
        cfg = getattr(app.harness, "model_label", "model")
        used = self._context_tokens()
        # Denominate against the resolved threshold (min(budget, 0.8×window)), not
        # the raw budget: 100% keeps meaning "compaction imminent" even when a
        # small discovered window, not the budget, is the binding limit.
        max_ctx = getattr(app.harness.session, "compact_threshold", 0) or 0
        pct = round(used / max_ctx * 100) if max_ctx else 0
        ctx_text = f"ctx {human_tokens(used)}/{human_tokens(max_ctx)} ({pct}%)"
        ctx_style = "red" if pct >= 90 else "yellow" if pct >= 75 else ""
        tokens_text = format_token_split(app.harness.session.usage)
        if self.live_run_tokens:
            tokens_text += f" +{human_tokens(self.live_run_tokens)}"
        cost = self._session_cost()
        if cost is not None:
            tokens_text += f" · {format_cost(cost)}"
        session_text = f"session {format_duration(time.monotonic() - self.session_start)}"
        fields = [
            Content(self.mode),
            Content(self.model_name or cfg),
            Content.assemble((ctx_text, ctx_style)) if ctx_style else Content(ctx_text),
            Content(tokens_text),
            Content(session_text),
        ]
        # Time-to-first-token of the latest model request. Lingers while idle
        # (it describes the last request, still true).
        if self.last_ttft is not None:
            fields.append(Content(f"ttft {self.last_ttft:.1f}s"))
        if self.busy:
            elapsed = format_duration(time.monotonic() - self.turn_start)
            fields.append(Content(f"working… {elapsed}"))
        return Content.from_markup(" [dim]·[/] ").join(fields)

    def refresh_status(self) -> None:
        """Force a repaint reflecting live harness/session state (ctx, tokens,
        cost, session duration) that isn't tracked by a dedicated reactive, and
        pull the renderer's in-flight live-token/ttft tallies into the matching
        reactives so a caller that only touches ``app.stream`` still shows up."""
        app: HarnessApp = self.app  # type: ignore[assignment]
        self.mode = app.harness.deps.workspace.mode.value
        self.model_name = app.harness.model_label
        stream = getattr(app, "stream", None)
        if stream is not None:
            self.live_run_tokens = stream.live_run_tokens
            self.last_ttft = stream.last_ttft
        self.refresh()

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        if busy:
            self.spin = 0  # start the working animation at the first frame
            self.turn_start = time.monotonic()
        else:
            # The finished run is now folded into session usage by run_turn; drop
            # the in-flight tally so it isn't added on top a second time.
            self.app.stream.reset_live_tokens()  # type: ignore[union-attr]
            self.live_run_tokens = 0
        self.refresh_title()  # spinner ↔ static ●

    def refresh_title(self) -> None:
        """Set the in-app Header (via App.title) AND the real terminal tab/window
        title (via an OSC sequence Textual doesn't emit on its own) to an
        idle/working mark + the session name."""
        app: HarnessApp = self.app  # type: ignore[assignment]
        mark = _SPINNER[self.spin] if self.busy else "●"
        name = app.harness.session.session_name or "marim-harness"
        app.title = f"{mark} {name}"  # in-app Header
        if app._driver is not None:  # the actual terminal tab
            # Best-effort: refresh_title runs from set_busy, which fires in
            # _run_turn's finally block. If the driver is mid-teardown (e.g.
            # /exit fired mid-turn) write/flush can raise BrokenPipeError —
            # letting it escape would skip _after_turn() and stall the queue /
            # autonomous-wake chain. Swallow it, mirroring on_unmount.
            try:
                app._driver.write(osc_title(f"{mark} {name}"))
                app._driver.flush()
            except Exception:
                pass

    def tick_spinner(self) -> None:
        """Advance the working-indicator animation while a turn runs. No-op when
        idle, so the title/tab aren't rewritten and the static ● stays put."""
        if not self.busy:
            return
        self.spin = (self.spin + 1) % len(_SPINNER)
        self.refresh_title()
