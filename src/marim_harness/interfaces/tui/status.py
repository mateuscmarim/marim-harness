"""The status bar, terminal title, working-spinner, and session/turn timers —
extracted from HarnessApp. Holds its own busy/spin/timer state; reaches the app
and sibling collaborators through `self.app`."""

import time
from typing import TYPE_CHECKING

from textual.content import Content
from textual.css.query import NoMatches
from textual.widgets import Static

from ...compaction import estimate_tokens
from ...usage import resolve_cost
from .widgets import format_cost, format_token_split, human_tokens
from .widgets.format import (  # re-exported  # noqa: F401
    _SPINNER,
    _SPINNER_TICK_INTERVAL,
    format_duration,
)

if TYPE_CHECKING:
    from .app import HarnessApp

_CLOCK_TICK_INTERVAL = 1.0


def osc_title(text: str) -> str:
    """OSC 0 escape that sets the terminal's tab AND window title."""
    return f"\033]0;{text}\007"


class StatusPresenter:
    def __init__(self, app: "HarnessApp") -> None:
        self.app = app
        self.busy = False
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
        for a number that only moves when a message is committed. The committed
        history grows message-by-message (a streamed reply is one ModelResponse
        appended at turn end, not parts mutated in place), so len(history) is an
        exact change key: stable between commits, bumped on each new message."""
        history = self.app.harness.session.history
        key = len(history)
        if key != self._ctx_tokens_key:
            self._ctx_tokens_key = key
            self._ctx_tokens = estimate_tokens(history)
        return self._ctx_tokens

    def _session_cost(self) -> float | None:
        """The committed session cost for the status bar, memoized on (token total,
        model). resolve_cost → estimate_cost is a genai-prices table lookup, but the
        status bar repaints once a second while idle and ~12.5x/s while a turn
        streams — and ``session.usage`` only changes when a turn commits (the
        in-flight tally rides on ``live_run_tokens``, not here). Re-pricing every
        frame re-runs the lookup for an identical number; the committed total moves
        only on commit, so it's an exact change key — same rationale as
        _context_tokens above. The model is part of the key so a /model switch
        re-prices the same totals at the new rate."""
        usage = self.app.harness.session.usage
        model_id = self.app.harness.model_id
        key = (usage.total_tokens, model_id)
        if key != self._cost_key:
            self._cost_key = key
            self._cost, _ = resolve_cost(usage, model_id)
        return self._cost

    def status_text(self) -> Content:
        cfg = getattr(self.app.harness, "model_label", "model")
        used = self._context_tokens()
        # Denominate against the resolved threshold (min(budget, 0.8×window)),
        # not the raw budget: 100% keeps meaning "compaction imminent" even
        # when a small discovered window, not the budget, is the binding limit.
        max_ctx = getattr(self.app.harness.session, "compact_threshold", 0) or 0
        pct = round(used / max_ctx * 100) if max_ctx else 0
        ctx_text = f"ctx {human_tokens(used)}/{human_tokens(max_ctx)} ({pct}%)"
        ctx_style = "red" if pct >= 90 else "yellow" if pct >= 75 else ""
        # The committed in/cached/out split, then the current run's in-flight
        # tokens as a live +N delta (they aren't split until the turn commits),
        # then spend — billed when the provider reports it, else estimated.
        tokens_text = format_token_split(self.app.harness.session.usage)
        if self.app.stream.live_run_tokens:
            tokens_text += f" +{human_tokens(self.app.stream.live_run_tokens)}"
        cost = self._session_cost()
        if cost is not None:
            tokens_text += f" · {format_cost(cost)}"
        mode = self.app.harness.deps.workspace.mode.value
        # The session name now lives in the terminal title (see refresh_title);
        # the status-bar head is just the permission mode.
        session_text = f"session {format_duration(time.monotonic() - self.session_start)}"
        fields = [
            Content(mode),
            Content(cfg),
            Content.assemble((ctx_text, ctx_style)) if ctx_style else Content(ctx_text),
            Content(tokens_text),
            Content(session_text),
        ]
        # Time-to-first-token of the latest model request — how snappy the
        # provider feels right now. Lingers while idle (it describes the last
        # request, still true); cleared only on a session reset.
        ttft = self.app.stream.last_ttft
        if ttft is not None:
            fields.append(Content(f"ttft {ttft:.1f}s"))
        if self.busy:
            elapsed = format_duration(time.monotonic() - self.turn_start)
            fields.append(Content(f"working… {elapsed}"))
        return Content.from_markup(" [dim]·[/] ").join(fields)

    def refresh_status(self) -> None:
        try:
            bar = self.app.query_one("#status-bar", Static)
        except NoMatches:
            # The status bar is gone — the app is tearing down (e.g. /exit fired
            # mid-turn) and a worker's finally block is still firing. Nothing to
            # update; quietly skip.
            return
        bar.update(self.status_text())

    def refresh_title(self) -> None:
        """Set the in-app Header (via App.title) AND the real terminal tab/window
        title (via an OSC sequence Textual doesn't emit on its own) to an
        idle/working mark + the session name. The title is a plain string, not
        markup-parsed, so a model-generated name needs no escaping."""
        mark = _SPINNER[self.spin] if self.busy else "●"
        name = self.app.harness.session.session_name or "marim-harness"
        self.app.title = f"{mark} {name}"  # in-app Header
        if self.app._driver is not None:  # the actual terminal tab
            # Best-effort: refresh_title runs from set_busy, which fires in
            # _run_turn's finally block. If the driver is mid-teardown (e.g.
            # /exit fired mid-turn) write/flush can raise BrokenPipeError —
            # letting it escape would skip _after_turn() and stall the queue /
            # autonomous-wake chain. Swallow it, mirroring on_unmount.
            try:
                self.app._driver.write(osc_title(f"{mark} {name}"))
                self.app._driver.flush()
            except Exception:
                pass

    def tick_spinner(self) -> None:
        """Advance the working-indicator animation while a turn runs. No-op when
        idle, so the title/tab aren't rewritten and the static ○ stays put."""
        if not self.busy:
            return
        self.spin = (self.spin + 1) % len(_SPINNER)
        self.refresh_title()

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        if busy:
            self.spin = 0  # start the working animation at the first frame
        else:
            # The finished run is now folded into session usage by run_turn; drop
            # the in-flight tally so it isn't added on top a second time.
            self.app.stream.reset_live_tokens()
        self.refresh_title()  # spinner ↔ static ○
        self.refresh_status()
