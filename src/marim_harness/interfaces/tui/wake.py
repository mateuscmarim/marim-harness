"""Autonomous wake-on-completion policy for the interactive TUI.

When a background job finishes while the turn worker is idle, the TUI fires one
digest-only turn so the agent reacts without waiting for the user. This object
holds the *decision* — whether to wake right now and the depth counter that
bounds runaway wake→spawn→wake chains — while the App keeps the *effect*
(mounting the notice, spawning the turn worker) and owns the user-facing
``autonomous_wake`` toggle. Kept free of Textual so the policy is unit-testable
on its own, without spinning up an App."""

from __future__ import annotations


class WakeController:
    """Bounds and decides autonomous wakes. ``enabled`` is passed into
    :meth:`should_wake` rather than held here because it is the App's public,
    runtime-toggled flag (``/jobs wake on|off``); this object owns only the
    consecutive-auto-turn depth that the cap guards against."""

    def __init__(self, depth_cap: int) -> None:
        self._depth_cap = depth_cap
        # Consecutive autonomous turns since the last user turn; reset on any
        # user-initiated turn. This is the loop guard the cap bounds.
        self._depth = 0

    @property
    def depth(self) -> int:
        return self._depth

    @property
    def depth_cap(self) -> int:
        return self._depth_cap

    def should_wake(
        self, *, enabled: bool, turn_busy: bool, has_finished_pending: bool
    ) -> bool:
        """True iff an idle TUI should fire one autonomous digest turn now: wake
        is enabled, no turn is in flight, the depth cap is not yet reached, and a
        finished-job digest is pending. A pure predicate — it never mutates."""
        return (
            enabled
            and not turn_busy
            and self._depth < self._depth_cap
            and has_finished_pending
        )

    def record_auto_turn(self) -> None:
        """Count one autonomous turn toward the cap; call when a wake fires."""
        self._depth += 1

    def reset(self) -> None:
        """Reset the chain — called on any user-initiated turn."""
        self._depth = 0
