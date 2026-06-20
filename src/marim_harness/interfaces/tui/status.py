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

if TYPE_CHECKING:
    from .app import HarnessApp

_CLOCK_TICK_INTERVAL = 1.0
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPINNER_TICK_INTERVAL = 0.1


def osc_title(text: str) -> str:
    """OSC 0 escape that sets the terminal's tab AND window title."""
    return f"\033]0;{text}\007"


def format_duration(seconds: float, *, precise: bool = False) -> str:
    """Human-readable elapsed time. ``precise`` (for the per-turn stamp) keeps a
    decimal under a minute (``12.4s``); otherwise whole units (``12s``, ``3m``,
    ``1h 5m``)."""
    if seconds < 60:
        return f"{seconds:.1f}s" if precise else f"{int(seconds)}s"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60}m"


class StatusPresenter:
    def __init__(self, app: "HarnessApp") -> None:
        self.app = app
        self.busy = False
        self.spin = 0
        self.session_start = time.monotonic()
        self.turn_start = time.monotonic()

    def status_text(self) -> Content:
        cfg = getattr(self.app.harness, "model_label", "model")
        used = estimate_tokens(self.app.harness.session.history)
        max_ctx = getattr(self.app.harness.session, "max_context_tokens", 0) or 0
        pct = round(used / max_ctx * 100) if max_ctx else 0
        ctx_text = f"ctx {human_tokens(used)}/{human_tokens(max_ctx)} ({pct}%)"
        ctx_style = "red" if pct >= 90 else "yellow" if pct >= 75 else ""
        # The committed in/cached/out split, then the current run's in-flight
        # tokens as a live +N delta (they aren't split until the turn commits),
        # then spend — billed when the provider reports it, else estimated.
        tokens_text = format_token_split(self.app.harness.session.usage)
        if self.app._live_run_tokens:
            tokens_text += f" +{human_tokens(self.app._live_run_tokens)}"
        cost, _ = resolve_cost(self.app.harness.session.usage, self.app.harness.model_id)
        if cost is not None:
            tokens_text += f" · {format_cost(cost)}"
        mode = self.app.harness.deps.mode.value
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
        mark = _SPINNER[self.spin] if self.busy else "○"
        name = self.app.harness.session.session_name or "marim-harness"
        self.app.title = f"{mark} {name}"  # in-app Header
        if self.app._driver is not None:  # the actual terminal tab
            self.app._driver.write(osc_title(f"{mark} {name}"))
            self.app._driver.flush()

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
            self.app._live_run_tokens = 0
        self.refresh_title()  # spinner ↔ static ○
        self.refresh_status()
