"""The base for models that delegate a whole turn to an external coding CLI.

``ClaudeCliModel`` (claude-cli) and ``CodexCliModel`` (codex-cli) both make
marim a *launcher*: the external process runs its own tool loop and marim
receives a single text-only ``ModelResponse`` plus out-of-band activity. The
harness binds the same late seams on either at three call sites (bootstrap,
``Harness.bind_ui``, ``Harness.set_model``) through ``Harness.wire_cli_model``,
and ``session.ctrl.aux_model_for`` swaps either for an ``ephemeral_clone``
before building the aux (titler/summarizer) agents. Keeping those seams on one
base means a new external CLI never grows a second isinstance ladder.

Only the seams live here. Each subclass owns its transport, its parsing and
its ``ephemeral_clone``; ``TextFolder`` (the vendor-part-id bookkeeping the
streamed responses share) is here too because both providers interleave prose
with tool cards the same way.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic_ai.models import Model

if TYPE_CHECKING:
    from ..ask_user import Question


class CliModelError(Exception):
    """The external CLI was unavailable or produced no terminal result."""


class ExternalCliModel(Model):
    """Seams shared by claude-cli and codex-cli. All are late-bound (None
    until ``Harness.wire_cli_model`` runs) so a model can be built off-loop
    and without a UI, exactly like ``Deps.ui``."""

    provider_id: ClassVar[str] = "external-cli"

    def __init__(self) -> None:
        super().__init__()
        # Live marim approval mode ("auto"/"ask"/"plan"); read per turn.
        self.mode_getter: Callable[[], str] | None = None
        # Real workspace (or worktree) root. Running in the process cwd (".")
        # would make the CLI read/edit the WRONG directory — destructively so
        # under --worktree.
        self.cwd: str = "."
        # Ephemeral models serve one-shot aux agents (titler/summarizer): no
        # session resume/persist, always their own instructions, read-only.
        self.ephemeral: bool = False
        # TUI side-channels (None headless). on_activity renders the CLI's own
        # tool calls as native tool cards; on_subagent/on_subagent_model route
        # the CLI's sub-agents to the sub-agents screen.
        self.on_activity: Callable[[list], Awaitable[None]] | None = None
        self.on_subagent: Callable[[str, object, object], Awaitable[None]] | None = None
        self.on_subagent_model: Callable[[str, str], Awaitable[None]] | None = None
        # Interactive gating (Deps.ui.request_approval / ask_user). claude-cli
        # cannot use them (headless claude can't prompt); codex-cli brokers its
        # server-side approval requests through them.
        self.request_approval: Callable[[object], Awaitable[object]] | None = None
        self.ask_user: Callable[[list[Question]], Awaitable[dict | None]] | None = None
        # The session scratchpad (auto-approved writes in ask mode).
        self.scratchpad_getter: Callable[[], Path | None] | None = None
        # The live thinking level id (Harness.thinking_level_id), read per turn.
        self.thinking_getter: Callable[[], str | None] | None = None
        # The provider-side conversation reference persisted with the marim
        # session (SessionStore.cli_thread_id, Task 10): read on the first turn
        # after a resume, written whenever the CLI reports a new one.
        self.session_ref_getter: Callable[[], str | None] | None = None
        self.on_session_ref: Callable[[str], None] | None = None

    @property
    def system(self) -> str:
        return self.provider_id

    def ephemeral_clone(self, *, cwd: str) -> ExternalCliModel:
        """A stateless, read-only copy for one-shot aux agents. Subclasses must
        override; the base raises so a forgotten override is loud."""
        raise NotImplementedError(f"{type(self).__name__} must implement ephemeral_clone")

    def steer(self, text: str) -> bool:
        """Inject ``text`` into the CLI's in-flight turn. Returns True when the
        CLI accepted it (so the harness must NOT also buffer it for the next
        turn), False when the provider cannot steer (default)."""
        return False

    async def compact_remote(self) -> None:
        """Ask the CLI to compact its own context (after marim compacts its
        copy). No-op by default."""
        return None


class TextFolder:
    """The vendor-part-id bookkeeping for a streamed response's two rendering
    modes.

    With a UI side-channel (cards mode, ``on_activity`` set), the CLI's
    tool calls/results become native tool cards pushed out-of-band, and each
    run of assistant prose gets its own text part (a fresh vendor_part_id after
    every tool) so the cards interleave between text blocks. Headless (fold
    mode, no side-channel) folds tool use into the text as ``▸`` lines in one
    growing part. ``part_n`` (cards mode) and ``folded_any`` (fold mode, for
    blank-line separation) both mutate across chunks AND across the text/tool
    arms, so they're threaded via this small stateful object rather than loose
    locals passed in/out of each arm.

    ``activity_events`` / ``fold_text`` are the provider's chunk -> events and
    chunk -> ``▸`` line translators (claude-cli passes ``cli_activity_events``
    and ``fold_chunk_text``; codex-cli passes its own in Task 8)."""

    def __init__(
        self,
        parts_manager,
        on_activity: Callable[[list], Awaitable[None]] | None,
        *,
        activity_events: Callable[[object], list],
        fold_text: Callable[[object, bool], str],
        is_call: Callable[[object], bool],
    ) -> None:
        self._parts_manager = parts_manager
        self._on_activity = on_activity
        self._cards = on_activity is not None
        self._activity_events = activity_events
        self._fold_text = fold_text
        self._is_call = is_call
        self.part_n = 0
        self.folded_any = False
        self._started_ids: set[str] = set()

    async def _emit(self, content: str, part_id: str):
        # pydantic_ai's parts manager reports a brand-new vendor_part_id's
        # first call as a PartStartEvent (the part's initial content), never
        # a PartDeltaEvent — even when that first call carries real text. A
        # consumer that only accumulates PartDeltaEvent.delta (the common,
        # and here required, shape for live streaming) would silently drop
        # a fresh part's first chunk. Bootstrap the part with an empty delta
        # so the manager treats it as already-started; the real content then
        # arrives as a proper delta on the very next call.
        if part_id not in self._started_ids:
            self._started_ids.add(part_id)
            for event in self._parts_manager.handle_text_delta(vendor_part_id=part_id, content=""):
                yield event
        for event in self._parts_manager.handle_text_delta(vendor_part_id=part_id, content=content):
            yield event

    async def emit_text(self, delta: str):
        """Cards mode gives prose its own vendor part id (``text-{part_n}``);
        fold mode grows the single ``text-0`` part, blank-line-separated from
        anything already folded into it."""
        if self._cards:
            async for ev in self._emit(delta, f"text-{self.part_n}"):
                yield ev
            return
        seg = delta if not self.folded_any else f"\n\n{delta}"
        async for ev in self._emit(seg, "text-0"):
            yield ev
        self.folded_any = True

    async def emit_tool(self, chunk):
        """Cards mode pushes the chunk out-of-band via ``on_activity`` and, for
        a tool *call*, bumps ``part_n`` so following prose starts a fresh part
        below the card; fold mode folds it into the text as a ``▸`` line."""
        if self._on_activity is not None:
            events = self._activity_events(chunk)
            if events:
                await self._on_activity(events)
            if self._is_call(chunk):
                self.part_n += 1
            return
        seg = self._fold_text(chunk, not self.folded_any)
        if seg:
            async for ev in self._emit(seg, "text-0"):
                yield ev
            self.folded_any = True
