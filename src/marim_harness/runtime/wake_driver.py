"""Autonomous-wake orchestration shared by the interactive TUI and the serve-mode
SessionHost.

`WakeController` (``runtime/wake.py``) owns the *decision* — whether to wake now
and the depth counter that bounds runaway wake -> spawn -> wake chains. This
object owns the *effect* both interfaces would otherwise duplicate: on a
job-settle or turn-end signal, run the policy and, if it says wake, count the
turn and enqueue exactly one digest-only turn through the injected callback.

Kept free of Textual and of the server so the orchestration is unit-testable with
plain predicates. Each consumer injects its own notion of "a turn is in flight"
(``turn_busy``), the job-registry predicates, the runtime-toggled ``is_enabled``
flag, and how to actually enqueue a digest turn (``enqueue_digest_turn``).

Two trigger points, both required, both routed through :meth:`maybe_wake`:
job-settle (a job finished) and turn-end (a digest that arrived while a turn was
busy must still wake once that turn drains). Call :meth:`note_user_turn` on every
user-initiated turn to reset the depth chain.
"""

from __future__ import annotations

from collections.abc import Callable

from .wake import WakeController


class WakeDriver:
    def __init__(
        self,
        controller: WakeController,
        *,
        is_enabled: Callable[[], bool],
        turn_busy: Callable[[], bool],
        has_finished_pending: Callable[[], bool],
        all_jobs_settled: Callable[[], bool],
        enqueue_digest_turn: Callable[[], None],
    ) -> None:
        self._controller = controller
        self._is_enabled = is_enabled
        self._turn_busy = turn_busy
        self._has_finished_pending = has_finished_pending
        self._all_jobs_settled = all_jobs_settled
        self._enqueue_digest_turn = enqueue_digest_turn

    def maybe_wake(self) -> bool:
        """Run the wake policy for the current signal; if it fires, count the turn
        and enqueue one digest-only turn. Returns whether it enqueued. Safe to call
        on both the job-settle and turn-end triggers — the policy's guards make a
        redundant call a no-op."""
        if not self._controller.should_wake(
            enabled=self._is_enabled(),
            turn_busy=self._turn_busy(),
            has_finished_pending=self._has_finished_pending(),
            all_jobs_settled=self._all_jobs_settled(),
        ):
            return False
        self._controller.record_auto_turn()
        self._enqueue_digest_turn()
        return True

    def note_user_turn(self) -> None:
        """Reset the depth chain — call when a user-initiated turn is submitted."""
        self._controller.reset()

    @property
    def controller(self) -> WakeController:
        """Read-only access to the wrapped policy (depth counter and cap) for
        introspection and tests. The driver's effect surface stays maybe_wake /
        note_user_turn; this only exposes the already-tested policy for reads."""
        return self._controller
