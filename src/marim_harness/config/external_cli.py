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
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic_ai.messages import FunctionToolCallEvent, FunctionToolResultEvent, PartStartEvent
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
        # Interactive gating (Deps.ui.request_approval / ask_user). Both
        # external CLIs broker their tool-permission requests through them:
        # codex-cli its server-side approval requests, claude-cli the
        # `can_use_tool` control requests of its long-lived process.
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

    def adopt(self, previous: Model) -> None:
        """Take over whatever ``previous`` (the model being switched away
        from) holds that this model can keep using — claude-cli moves the
        live process across a same-provider ``/model`` switch so the switch
        is one control request instead of a respawn. Called by
        ``Harness.set_model`` before it schedules ``previous.aclose()``; the
        base keeps nothing."""
        return None

    async def compact_remote(self) -> None:
        """Ask the CLI to compact its own context (after marim compacts its
        copy). No-op by default."""
        return None

    async def aclose(self) -> None:
        """Release whatever the provider holds open (a subprocess, a server
        thread). Called by the harness when the model is switched away from
        and at teardown. The base does nothing."""
        return None


# The ``ModelResponse.provider_details`` key under which a CLI provider's
# streamed response carries its tool-activity ledger (see ``ActivityLedger``).
# The controller expands it into real ToolCallPart/ToolReturnPart messages at
# persist time (``runtime.cli_activity.expand_cli_activity``) and strips the
# key, so a persisted history never carries it.
CLI_ACTIVITY_KEY = "cli_activity"

# A tool result longer than this is cut at persist time. The CLI's own
# context already saw the full output; marim's copy is for transcripts and
# resumes, and one unbounded `cat` must not balloon the session file.
MAX_RECORDED_RESULT_CHARS = 16_000


class ActivityLedger:
    """The ordered record of a CLI turn's tool activity, interleaved with the
    response's own parts.

    The CLI runs its tools itself, so its calls/results must NOT become
    ``ToolCallPart``s of the streamed ``ModelResponse`` — the agent graph would
    try to execute them. They are pushed out-of-band for the live UI
    (``TextFolder.emit_tool``) and, until now, forgotten: a resumed transcript
    and ``GET .../history`` showed the prose with holes where the tool cards
    had been. The ledger closes that gap without touching the graph: it
    records every tool call/result in stream order, plus a ``part`` marker
    for each part the response starts (its index in ``get_parts()``, which
    keeps creation order), and attaches itself to the streamed response's
    ``provider_details`` under ``CLI_ACTIVITY_KEY`` — on the FIRST tool entry,
    so an interrupted turn still carries what ran before the interrupt and a
    tool-free turn stays byte-identical to before. After the run, the
    controller expands the ledger into real tool-call/tool-return messages
    (see ``runtime.cli_activity``) so every consumer of persisted history
    (TUI replay, ``GET history``, compaction, a mid-session provider switch)
    sees the CLI's tools exactly like marim's own.

    Entries are plain JSON-able dicts: ``{"kind": "part", "index": n}``,
    ``{"kind": "call", "id", "name", "args"}`` and ``{"kind": "result", "id",
    "content", "outcome"}``."""

    def __init__(self, attach: Callable[[list[dict[str, Any]]], None]) -> None:
        self.entries: list[dict[str, Any]] = []
        self._attach = attach
        self._attached = False

    def note_event(self, event: object) -> None:
        """Record a stream event; only ``PartStartEvent`` matters (the response
        grew a part, remember where it sits relative to the tool activity)."""
        if isinstance(event, PartStartEvent):
            self.entries.append({"kind": "part", "index": event.index})

    def recording(
        self, on_activity: Callable[[list], Awaitable[None]] | None
    ) -> Callable[[list], Awaitable[None]] | None:
        """``on_activity`` wrapped to record every batch it forwards. ``None``
        stays ``None``: fold mode (headless) has no side-channel and needs no
        ledger — its ``▸`` lines ARE the persisted record — and TextFolder
        keys its mode on the callback's presence."""
        if on_activity is None:
            return None

        async def forward(events: list) -> None:
            self.note_activity(events)
            await on_activity(events)

        return forward

    def note_activity(self, events: list) -> None:
        """Record the out-of-band tool events a provider built for the UI
        side-channel (the same ``FunctionToolCallEvent`` /
        ``FunctionToolResultEvent`` shapes ``on_activity`` receives)."""
        for event in events:
            if isinstance(event, FunctionToolCallEvent):
                part = event.part
                self._record(
                    {
                        "kind": "call",
                        "id": part.tool_call_id,
                        "name": part.tool_name,
                        "args": part.args,
                    }
                )
            elif isinstance(event, FunctionToolResultEvent):
                part = event.part
                content = getattr(part, "content", "")
                self._record(
                    {
                        "kind": "result",
                        "id": part.tool_call_id,
                        "content": _cap_result(
                            content if isinstance(content, str) else str(content)
                        ),
                        "outcome": getattr(part, "outcome", "success"),
                    }
                )

    def _record(self, entry: dict[str, Any]) -> None:
        self.entries.append(entry)
        if not self._attached:
            # Attach the LIST (not a copy) so entries recorded after this
            # point are visible to whoever reads provider_details later.
            self._attached = True
            self._attach(self.entries)


def _cap_result(content: str) -> str:
    if len(content) <= MAX_RECORDED_RESULT_CHARS:
        return content
    dropped = len(content) - MAX_RECORDED_RESULT_CHARS
    return content[:MAX_RECORDED_RESULT_CHARS] + f"\n…[truncated {dropped} chars]"


class TextFolder:
    """The vendor-part-id bookkeeping for a streamed response's two rendering
    modes.

    With a UI side-channel (cards mode, ``on_activity`` set), the CLI's
    tool calls/results become native tool cards pushed out-of-band, and each
    run of assistant prose gets its own text part (a fresh vendor_part_id after
    every tool) so the cards interleave between text blocks. Headless (fold
    mode, no side-channel) folds tool use into the text as ``▸`` lines in one
    growing part. ``part_n`` (cards mode) and ``folded_any``/``after_tool``
    (fold mode, for blank-line separation) all mutate across chunks AND across
    the text/tool arms, so they're threaded via this small stateful object
    rather than loose locals passed in/out of each arm.

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
        # Whether the LAST thing folded into text-0 was a ``▸`` tool line. Prose
        # arrives one small delta at a time (claude-cli streams a few characters
        # per text_delta), so the blank line below must separate a tool line
        # from the prose around it — never one delta from the next, which would
        # shred every sentence.
        self.after_tool = False
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
        any ``▸`` tool line already folded into it."""
        if self._cards:
            async for ev in self._emit(delta, f"text-{self.part_n}"):
                yield ev
            return
        seg = f"\n\n{delta}" if self.after_tool else delta
        async for ev in self._emit(seg, "text-0"):
            yield ev
        self.folded_any = True
        self.after_tool = False

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
            self.after_tool = True
