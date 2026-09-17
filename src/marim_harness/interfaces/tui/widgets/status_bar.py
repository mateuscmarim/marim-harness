"""Reactive status bar — owns the display state that was scattered across
HarnessApp and the retired StatusPresenter. Setting any of the reactives below
(busy, mode, model_name, live_run_tokens, last_ttft) re-renders on its own, so
those five no longer need a manual repaint call.

The rest of the bar — context size, token split, cost, session duration — is
still read straight off live harness/session state in ``render``, so it has no
reactive to change and something must still poke the widget: hence the surviving
``refresh_status`` below and the idle ``_CLOCK_TICK_INTERVAL`` repaint."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from textual.content import Content
from textual.reactive import reactive
from textual.widgets import Static

from ....usage import resolve_cost
from .format import _SPINNER, format_cost, format_duration, format_token_split, human_tokens

if TYPE_CHECKING:
    from ..app import HarnessApp


def osc_title(text: str) -> str:
    """OSC 0 escape that sets the terminal's tab AND window title."""
    return f"\033]0;{text}\007"


def backend_telemetry_text(telemetry: dict) -> str:
    """An observation label separate from billed usage and host busy state."""
    fields = []
    estimate = telemetry.get("thinking_tokens")
    if isinstance(estimate, int) and not isinstance(estimate, bool) and estimate >= 0:
        fields.append(f"thinking ~{human_tokens(estimate)}")
    state = telemetry.get("state")
    if state in ("idle", "running", "requires_action"):
        fields.append(f"backend {state.replace('_', ' ')}")
    return " · ".join(fields)


class StatusBar(Static):
    """A reactive status bar that auto-renders on state changes."""

    busy: reactive[bool] = reactive(False, init=False)
    live_run_tokens: reactive[int] = reactive(0, init=False)
    last_ttft: reactive[float | None] = reactive(None, init=False)
    model_name: reactive[str] = reactive("", init=False)
    mode: reactive[str] = reactive("", init=False)
    # Where the session lives when it is not this process ("daemon",
    # "daemon · reconnecting…", "daemon · lost"); empty for a local session.
    link_label: reactive[str] = reactive("", init=False)

    def __init__(self) -> None:
        super().__init__(id="status-bar")
        self.spin = 0
        self.session_start = time.monotonic()
        self.turn_start = time.monotonic()
        # Memoized committed-cost estimate (see _session_cost). A sentinel key no
        # real (total, model) pair can equal forces the first compute.
        self._cost_key: object = None
        self._cost: float | None = None

    def _context_tokens(self) -> int:
        """The context-size estimate for the status bar. The link's read
        model memoizes it (the local one on history length — estimate_token_count
        re-stringifies the whole transcript, and the bar repaints ~12.5x/s
        while a turn streams; the remote one is a field of ``GET session``)."""
        app: HarnessApp = self.app  # type: ignore[assignment]
        return app.link.info.history_tokens

    def _context_gauge(self) -> tuple[int, int]:
        """``(used, capacity)`` for the ctx field. A CLI backend's own report
        wins (the real prompt size against the model's window — marim's
        history is only a mirror of that context); otherwise the estimate,
        denominated against the resolved threshold (min(budget, 0.8×window)),
        not the raw budget, so 100% keeps meaning "compaction imminent" even
        when a small discovered window, not the budget, is the binding limit."""
        app: HarnessApp = self.app  # type: ignore[assignment]
        report = app.link.info.context_report
        if report is not None:
            return report.used, report.window or 0
        return self._context_tokens(), app.link.info.compact_threshold or 0

    def _session_cost(self) -> float | None:
        """The committed session cost for the status bar, memoized on (token total,
        model). resolve_cost → estimate_cost is a genai-prices table lookup; the
        committed total moves only on commit, so it's an exact change key."""
        app: HarnessApp = self.app  # type: ignore[assignment]
        usage = app.link.info.usage
        model_id = app.link.info.model_id
        key = (usage.total_tokens, model_id)
        if key != self._cost_key:
            self._cost_key = key
            self._cost, _ = resolve_cost(usage, model_id)
        return self._cost

    def _quota_text(self) -> str:
        """The current model's quota hint (``quota 37% (5h) · 12% (1w)``), or
        empty for providers that have none."""
        app: HarnessApp = self.app  # type: ignore[assignment]
        hint = app.link.info.quota_hint
        if hint is None:
            return ""
        # The local read model hands over the live QuotaHint; the remote one
        # the daemon's already-rendered string.
        return hint.render() if hasattr(hint, "render") else str(hint)

    def render(self) -> Content:
        app: HarnessApp = self.app  # type: ignore[assignment]
        cfg = app.link.info.model_label or "model"
        used, max_ctx = self._context_gauge()
        pct = round(used / max_ctx * 100) if max_ctx else 0
        ctx_text = f"ctx {human_tokens(used)}/{human_tokens(max_ctx)} ({pct}%)"
        ctx_style = "red" if pct >= 90 else "yellow" if pct >= 75 else ""
        tokens_text = format_token_split(app.link.info.usage)
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
        # Subscription quota (the CLI backends): the model keeps its latest
        # reading (codex `account/rateLimits/read`, claude `get_usage`),
        # refreshed once per turn; read straight off the live model like the
        # other harness-state fields.
        quota = self._quota_text()
        if quota:
            fields.append(Content(quota))
        telemetry = backend_telemetry_text(getattr(app.link.info, "backend_telemetry", {}))
        if telemetry:
            fields.append(Content(telemetry))
        if self.link_label:
            fields.append(Content(self.link_label))
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
        self.mode = app.link.info.mode
        self.model_name = app.link.info.model_label
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
        name = app.link.info.session_name or "marim-harness"
        app.title = f"{mark} {name}"  # in-app Header
        if app._driver is not None:  # the actual terminal tab
            # Best-effort: refresh_title runs from set_busy, which fires in
            # the session.status idle-edge handler. If the driver is
            # mid-teardown (e.g. /exit fired mid-turn) write/flush can raise
            # BrokenPipeError — letting it escape would skip after_turn() and
            # stall the queue / autonomous-wake chain. Swallow it, mirroring
            # on_unmount.
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
