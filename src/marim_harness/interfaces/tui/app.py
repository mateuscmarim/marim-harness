import json
import logging
import time
from asyncio import CancelledError, Event, Task, create_task
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

import rich.markup
from textual import events
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Footer, Header

from ...ask_user import Choice, Question
from ...jobs import JobsView
from ...runtime.harness import Harness
from ...runtime.permissions import Mode
from ...server.attach import RemoteTarget
from ...server.bus import EventBus
from ...server.client import RemoteSessionHost
from ...server.host import HostClosed, SessionHost, TurnQueueFull
from ...server.schema import STREAM_EVENT_TYPES
from ...server.wire_events import (
    AskPending,
    AskResolved,
    BackendTaskChanged,
    CompactionFinished,
    CompactionStarted,
    JobsChanged,
    SessionBackendState,
    SessionModeChanged,
    SessionNotice,
    SessionRenamed,
    SessionStatus,
    SessionTtft,
    SteerAccepted,
    StreamGap,
    SubagentEvent,
    SubagentModel,
    SubagentNotice,
    SubagentThinking,
    SubagentUsage,
    TasksChanged,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolResult,
    TurnError,
    TurnFinished,
    TurnStarted,
    TurnUsage,
    WireEvent,
    WorkflowFinished,
    WorkflowLogged,
    WorkflowSpawned,
    WorkflowSpawnFinished,
    WorkflowStarted,
    parse_wire_event,
)
from ...session import SessionManager
from ...session.claim import read_holder
from ...usage import resolve_cost, usage_from_dump
from ..history import PromptHistory
from ..prefs import load_theme, save_theme
from .activity import ActivityMonitor
from .commands import dispatch, refresh_worktree_view
from .interactions import (
    ApprovalPanel,
    AskUserPanel,
    InteractionPanel,
    PlanCard,
    mount_panel,
    unmount_panel,
)
from .link import Feed, LocalSessionLink, RemoteOnly, SessionLink
from .pickers import ModelPickers
from .queue_control import QueueController
from .remote_jobs import JobMirror
from .session_picker import SessionPickerModal
from .session_view import SessionView
from .settings import SettingsScreen
from .shell_passthrough import (
    SudoPasswordModal,
    format_transcript_block,
    needs_sudo_password,
    parse_bang,
    run_passthrough,
)
from .stream_render import StreamRenderer
from .subagents import SubAgentsScreen, SubAgentsView
from .themes import MARIM_THEMES
from .trust_flow import prompt_project_trust
from .turn_state import Transition, TurnTracker
from .widgets import (
    AssistantMessage,
    CommandAutocomplete,
    ErrorMessage,
    JobPanel,
    NoticeMessage,
    PromptInput,
    TaskPanel,
    TurnMeta,
    UserMessage,
    format_cost,
    human_tokens,
)
from .widgets.compact_notice import CompactNotice
from .widgets.format import _CLOCK_TICK_INTERVAL, _SPINNER_TICK_INTERVAL, format_duration
from .widgets.queue_display import QueueDisplay
from .widgets.status_bar import StatusBar, osc_title

logger = logging.getLogger(__name__)

# How often (seconds) buffered streaming text is rendered. ~12 flushes/sec reads
# as smooth while collapsing many per-token markdown re-parses into one.
_STREAM_FLUSH_INTERVAL = 0.08

# A second ctrl+c within this many seconds of the first confirms the quit; after
# it elapses the next attempt warns again. A short deliberate window, not a
# latch, so the warning always resurfaces after real inactivity instead of being
# spent once and forgotten for the rest of the process.
_QUIT_CONFIRM_WINDOW = 2.0

# The same confirmation for a *typed* quit (/exit, /quit). Much wider, because
# re-typing a command is far slower than double-tapping a key — 2s would make
# the confirmation unreachable and so effectively a hard block.
_TYPED_QUIT_CONFIRM_WINDOW = 20.0

_WELCOME = (
    "Type a message below to start, or `/help` for commands.\n\n"
    "- `/` opens the command menu — `↑`/`↓` to move, `tab` to complete\n"
    "- `enter` sends · `shift+enter` (or `ctrl+j`) inserts a newline\n"
    "- `ctrl+v` attaches a copied image (the terminal's own paste is text-only)\n"
    "- `ctrl+t` cycles the approval mode (ask → auto → plan)\n"
    "- `esc` cancels the running turn\n"
    "- `ctrl+g` (or `alt+enter`) steers the running turn\n"
    "- `/exit` (or `/quit`, `ctrl+c`) quits — `ctrl+c` requires a double-press to confirm"
)

# The value type is loosely typed (Any, not WireEvent) because each handler
# only needs to accept its OWN member of the WireEvent union; the dict's key
# (the concrete wire class) is what a caller uses to pick the right one, so
# the narrower per-handler signatures below are never actually mismatched at
# a call site — see _dispatch_wire.
_WireHandler = Callable[["HarnessApp", Any], Awaitable[None]]


async def _handle_stream_wire(app: "HarnessApp", wire: WireEvent) -> None:
    await app.stream.on_wire(wire)


async def _handle_subagent_event(app: "HarnessApp", wire: SubagentEvent) -> None:
    # The host publishes the sub-agent's stream event verbatim — the daemon
    # contract (serve-api.md) keeps the inner "type" as the raw stream-event
    # kind (text/thinking/tool_call/tool_result) — while parse_wire_event only
    # speaks wire types (text.delta/...). Remap here, the way the host does for
    # the turn stream, or every sub-agent stream parses as unknown and vanishes.
    wire_type = STREAM_EVENT_TYPES.get(str(wire.event.get("type")))
    if wire_type is None:
        return
    nested = parse_wire_event({**wire.event, "type": wire_type})
    if nested is None:
        return
    await app.stream.on_subagent_wire(wire.stream_id, nested, wire.usage)


async def _handle_subagent_notice(app: "HarnessApp", wire: SubagentNotice) -> None:
    await app.stream.on_subagent_notice(wire.stream_id, wire.message)


async def _handle_subagent_model(app: "HarnessApp", wire: SubagentModel) -> None:
    await app.stream.on_subagent_model(wire.stream_id, wire.model)


async def _handle_subagent_thinking(app: "HarnessApp", wire: SubagentThinking) -> None:
    await app.stream.on_subagent_thinking(wire.stream_id, wire.level)


async def _handle_subagent_usage(app: "HarnessApp", wire: SubagentUsage) -> None:
    await app.stream.on_subagent_usage(wire.stream_id, usage_from_dump(wire.usage))


async def _handle_workflow_spawned(app: "HarnessApp", wire: WorkflowSpawned) -> None:
    await app._on_workflow_spawn(
        wire.stream_id, wire.spawn_type, wire.task, wire.parent_tool_call_id
    )


async def _handle_workflow_started(app: "HarnessApp", wire: WorkflowStarted) -> None:
    app.stream.claim_workflow_card(wire.tool_call_id, wire.title)


async def _handle_workflow_logged(app: "HarnessApp", wire: WorkflowLogged) -> None:
    app._on_workflow_log(wire.tool_call_id, wire.message)


async def _handle_workflow_finished(app: "HarnessApp", wire: WorkflowFinished) -> None:
    app.stream.finish_workflow_card(wire.tool_call_id, wire.outcome, wire.failed)


async def _handle_workflow_spawn_finished(app: "HarnessApp", wire: WorkflowSpawnFinished) -> None:
    app.stream.finish_workflow_child(wire.stream_id, wire.report)


async def _handle_session_ttft(app: "HarnessApp", wire: SessionTtft) -> None:
    app.stream.on_ttft(wire.seconds)


async def _handle_session_mode_changed(app: "HarnessApp", _wire: SessionModeChanged) -> None:
    app._refresh_mode_display()


async def _handle_backend_state(app: "HarnessApp", _wire: SessionBackendState) -> None:
    # The link has folded this snapshot before dispatch. Observations never
    # drive TurnTracker or resolve an outstanding approval panel.
    app.status.refresh_status()
    if app._autocomplete is not None:
        app._autocomplete.refresh_inventory(app.link.info.backend_inventory)
    await refresh_worktree_view(app, _wire.telemetry)


async def _handle_session_notice(app: "HarnessApp", wire: SessionNotice) -> None:
    await app.stream.on_wire(wire)


async def _handle_session_renamed(app: "HarnessApp", wire: SessionRenamed) -> None:
    app.session.on_rename(wire.from_, wire.to)


async def _handle_tasks_changed(app: "HarnessApp", _wire: TasksChanged) -> None:
    app.activity.on_tasks_changed()


async def _handle_jobs_changed(app: "HarnessApp", _wire: JobsChanged) -> None:
    if app.attached:
        # The event carries no rows; GET jobs is the authority. A worker
        # (exclusive: a burst restarts it and the last read wins) so the pump
        # is never held on a round trip.
        app.run_worker(
            app.refresh_remote_jobs(), group="jobs-refresh", exclusive=True, exit_on_error=False
        )
        return
    app.activity.on_jobs_changed()


async def _handle_compaction_started(app: "HarnessApp", _wire: CompactionStarted) -> None:
    app.session.on_compact_start()


async def _handle_compaction_finished(app: "HarnessApp", wire: CompactionFinished) -> None:
    app.session.on_compact(
        wire.before,
        wire.after,
        changed=wire.changed,
        summary=wire.summary,
        post_tokens=wire.post_tokens,
        stage=wire.stage,
    )


async def _handle_turn_started(app: "HarnessApp", wire: TurnStarted) -> None:
    await app._on_turn_started(wire)


async def _handle_turn_usage(app: "HarnessApp", wire: TurnUsage) -> None:
    # Folded into the status bar's live "+N" counter on the next flush tick
    # (flush_streams syncs it to the StatusBar reactive); begin_run zeroes it.
    app.stream.live_run_tokens = wire.total_tokens


async def _handle_turn_finished(app: "HarnessApp", wire: TurnFinished) -> None:
    await app._on_turn_finished(wire)


async def _handle_turn_error(app: "HarnessApp", wire: TurnError) -> None:
    app._on_turn_error(wire)


async def _handle_session_status(app: "HarnessApp", wire: SessionStatus) -> None:
    await app._on_session_status(wire)


async def _handle_steer_accepted(app: "HarnessApp", wire: SteerAccepted) -> None:
    # Rendered from the wire, not at the keystroke, so a steer sent from
    # another client (phase 4) shows in this transcript the same way.
    tag = f"  📎 {wire.attachments}" if wire.attachments else ""
    app.append_log(NoticeMessage(f"↪ steering: {wire.text}{tag}"))


def _approval_args(raw: Any) -> dict:
    """The wire carries the deferred call's ``args`` as-is — a dict for native
    tool calls, occasionally a JSON string depending on the model's tool-call
    encoding. ApprovalPanel renders a dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _question_from_dict(d: dict) -> Question:
    return Question(
        question=str(d.get("question", "")),
        header=str(d.get("header", "")),
        multi=bool(d.get("multi", False)),
        options=[
            Choice(label=str(c.get("label", "")), description=c.get("description"))
            for c in d.get("options", [])
        ],
    )


def _ask_payload(panel: InteractionPanel, result: Any) -> dict:
    """Map a panel's local verdict onto the answer contract SessionHost's parked
    awaits read (see host._request_approval/_ask_user/_present_plan)."""
    if isinstance(panel, ApprovalPanel):
        return {"approve": bool(result)}
    if isinstance(panel, AskUserPanel):
        return {"cancel": True} if result is None else {"answers": result}
    if isinstance(panel, PlanCard):
        # "Keep planning" (bare Esc or typed revise-feedback) travels as a real
        # choice so the feedback survives the round-trip; the host only falls
        # back to cancel for answers it cannot read.
        return {"choice": result.choice, "feedback": result.feedback}
    return {}


async def _handle_ask_pending(app: "HarnessApp", wire: AskPending) -> None:
    await app._mount_ask(wire)


async def _handle_ask_resolved(app: "HarnessApp", wire: AskResolved) -> None:
    app._dismiss_ask(wire)


async def _handle_stream_gap(app: "HarnessApp", wire: StreamGap) -> None:
    await app._on_stream_gap(wire)


def _answered_elsewhere(panel: InteractionPanel, answer: dict | None) -> str:
    """The notice for an ask this client was showing that another client
    answered first (phase 4 — or a second local subscriber). Approval wording
    reads the verdict so the user knows which way it went."""
    if isinstance(panel, ApprovalPanel):
        verdict = "granted" if (answer or {}).get("approve") else "denied"
        return f"Approval {verdict} from another client"
    if isinstance(panel, AskUserPanel):
        return "Question answered from another client"
    return "Plan decided from another client"


# turn.finished's output/usage payload is not rendered — the transcript
# already streamed the text, and the status bar reads usage from the link's
# read model; the event's job here is the turn-end effects. stream.gap is a
# remote-only resync marker (see _on_stream_gap); the in-process subscriber
# never falls behind the ring, so locally it is a logged no-op.
_WIRE_HANDLERS: dict[type, _WireHandler] = {
    TextDelta: _handle_stream_wire,
    ThinkingDelta: _handle_stream_wire,
    ToolCall: _handle_stream_wire,
    ToolResult: _handle_stream_wire,
    SubagentEvent: _handle_subagent_event,
    SubagentNotice: _handle_subagent_notice,
    SubagentModel: _handle_subagent_model,
    SubagentThinking: _handle_subagent_thinking,
    SubagentUsage: _handle_subagent_usage,
    WorkflowSpawned: _handle_workflow_spawned,
    WorkflowStarted: _handle_workflow_started,
    WorkflowLogged: _handle_workflow_logged,
    WorkflowFinished: _handle_workflow_finished,
    WorkflowSpawnFinished: _handle_workflow_spawn_finished,
    SessionTtft: _handle_session_ttft,
    SessionModeChanged: _handle_session_mode_changed,
    SessionNotice: _handle_session_notice,
    SessionBackendState: _handle_backend_state,
    BackendTaskChanged: _handle_session_notice,
    SessionRenamed: _handle_session_renamed,
    TasksChanged: _handle_tasks_changed,
    JobsChanged: _handle_jobs_changed,
    CompactionStarted: _handle_compaction_started,
    CompactionFinished: _handle_compaction_finished,
    TurnStarted: _handle_turn_started,
    TurnUsage: _handle_turn_usage,
    TurnFinished: _handle_turn_finished,
    TurnError: _handle_turn_error,
    SessionStatus: _handle_session_status,
    SteerAccepted: _handle_steer_accepted,
    AskPending: _handle_ask_pending,
    AskResolved: _handle_ask_resolved,
    StreamGap: _handle_stream_gap,
}


class HarnessApp(App):
    """The interactive TUI.

    This class is the Textual surface — bindings, compose, message handlers,
    actions — plus the turn lifecycle, which is the one thing every other part
    of the UI is timed against. Concerns with their own state and their own
    invariants are collaborators constructed here and reachable as attributes:

    - ``stream`` renders the model's output (stream_render.py)
    - ``session`` rebuilds the log across new/switch/clear (session_view.py)
    - ``queue`` holds submissions made mid-turn (queue_control.py)
    - ``activity`` owns the panels, notifications and wake (activity.py)
    - ``pickers`` opens the live model/advisor/thinking pickers (pickers.py)
    - ``subagents`` drives the ctrl+x screen (subagents/screen.py)

    Since phase 4a the app drives its session through ``link`` (link.py): the
    in-process ``SessionHost`` when it was launched with a ``Harness``, or a
    daemon-owned session over HTTP when launched with a ``RemoteTarget``. The
    widgets read ``link.info``; the turn path awaits ``link.*``; anything that
    needs the ``Harness`` object itself goes through ``require_local``.
    """

    CSS_PATH = "styles.tcss"
    # Textual binds its command palette to ctrl+p with priority, which would
    # shadow the plain show_plan binding below and make the plan screen
    # unreachable by key. Move the palette off to ctrl+shift+p (distinct only
    # on terminals with the extended keyboard protocol — elsewhere the palette
    # simply has no key, which is the right trade: the plan screen is ours).
    COMMAND_PALETTE_BINDING = "ctrl+shift+p"
    BINDINGS = [
        ("ctrl+t", "cycle_mode", "Cycle mode"),
        ("ctrl+o", "toggle_outputs", "Show all output"),
        ("ctrl+x", "toggle_subagents", "Subagents"),
        ("ctrl+p", "show_plan", "Plan"),
        ("escape", "cancel_turn", "Cancel turn"),
        ("ctrl+r", "run_queued", "Run queued"),
        ("ctrl+c", "quit", "Quit"),
    ]

    def __init__(
        self,
        harness: Harness | None,
        history: PromptHistory | None = None,
        *,
        remote: RemoteTarget | None = None,
        notices: Sequence[str] = (),
    ) -> None:
        super().__init__()
        if (harness is None) == (remote is None):
            raise ValueError("HarnessApp takes exactly one of a harness or a remote target")
        # Launch-time lines the CLI could only print to stderr, which Textual
        # paints over (a stale daemon claim taken over locally, say). Shown
        # in the transcript on mount, after the session-default notices.
        self._startup_notices = list(notices)
        # The process-local harness, or None when this TUI is attached to a
        # daemon-owned session. Nothing reads it directly for a value the
        # widgets need (that is ``link.info``); the reaches that remain are
        # the process-local features, each behind ``require_local``.
        self.harness = harness
        self._remote = remote
        # The session seam. Bound in on_mount with the host (both loop-bound).
        self.link: SessionLink
        # The feed the pump reads. Swapped in place by a stream.gap resync,
        # which is why the pump reads the attribute rather than a local.
        self._feed: Feed | None = None
        # Remote only: the replay source (the last transcript snapshot fetched
        # from the daemon) and the mirror of the daemon's jobs the panels and
        # the replay settle read from. See ``history_messages`` / ``jobs``.
        self._remote_history: list[Any] = []
        self._remote_jobs = JobMirror()
        self._remote_manager: SessionManager | None = None
        self.status = StatusBar()
        self.stream = StreamRenderer(self)
        self.session = SessionView(self)
        # Recallable prompt history. Defaults to in-memory; the CLI passes a
        # persistent one so Up/Down recall prompts across restarts.
        self._history = history if history is not None else PromptHistory()
        # Is a turn running? Folded from the host's session.status events and
        # the submit latch (see turn_state.py); ``turn_busy`` reads it. The
        # turn itself lives on the host — the app holds no worker for it.
        self.turns = TurnTracker()
        # Set on every idle edge, cleared on submit / turn.started. A wait
        # helper for tests and for any flow that must outlast the current turn;
        # production code reads ``turn_busy`` instead.
        self.turns_idle = Event()
        self.turns_idle.set()
        # True while the /compact worker (group "compact") is mid-run. A
        # summarize can take seconds, and starting a turn or rebinding the
        # session store under it risks silent turn loss, cross-session history
        # contamination (a late `self.history=…; persist()` after the store was
        # swapped), and the only path to concurrent persist_elided calls. So the
        # session-teardown/turn-start flows gate on it, symmetric with the guard
        # /compact itself applies against turn_busy. Set in _cmd_compact, cleared
        # in its worker's finally.
        self.compact_busy = False
        # Confirm-to-quit guard (see _QUIT_CONFIRM_WINDOW): timestamp of the last
        # unconfirmed quit attempt, or None if there isn't one outstanding.
        self._quit_warned_at: float | None = None
        # Autonomous wake-on-completion (interactive TUI only). When a background
        # job finishes while the turn worker is idle, fire a digest-only turn so
        # the agent reacts without waiting for the user. Seeded from config;
        # toggled at runtime by `/jobs wake on|off` and from the settings screen,
        # which is why it stays a plain App attribute rather than moving into the
        # ActivityMonitor that reads it.
        # Attached: False, and not a toggle — the daemon's own host wakes the
        # session; a second driver here would race it for the same digests.
        self.autonomous_wake = harness.autonomous_wake if harness is not None else False
        self.activity = ActivityMonitor(self)
        self.queue = QueueController(self)
        self.pickers = ModelPickers(self)
        # The event pump's task (started in on_mount, cancelled in on_unmount).
        # A plain asyncio task, NOT a Textual worker: workers count toward
        # app.workers.wait_for_complete(), which a never-ending pump would
        # block forever.
        self._pump_task: Task[None] | None = None
        # Panels mounted for parked asks, keyed by ask id: ask.pending mounts one,
        # ask.resolved (local OR another client's answer, or an interrupt) removes
        # it. Value is (panel, focus-before-mount) so removal restores focus the
        # way run_panel's finally always did.
        self._ask_panels: dict[str, tuple[InteractionPanel, Any]] = {}
        self._bus = EventBus()
        # The in-process SessionHost (the SOLE bind_ui consumer; the pump is the
        # only path events reach the renderer). Built in on_mount, NOT here: its
        # __init__ spawns the worker task and reads the loop clock, and the real
        # launch path constructs HarnessApp synchronously before .run() starts
        # the loop (tests build it inside an anyio loop, which is why they never
        # noticed). Bare annotation — reading it before mount is a bug. Local
        # only: an attached app has no host in this process.
        self.host: SessionHost
        self._autocomplete: CommandAutocomplete | None = None
        self._worktree_view: tuple[Path, AssistantMessage] | None = None
        self._worktree_vcs_marker: tuple[int, int] | None = None
        # Full-bleed sub-agents screen (ctrl+x): its open/navigate/close lifecycle
        # and the per-frame repaint coalescing live in this collaborator.
        self.subagents = SubAgentsScreen(self)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield VerticalScroll(id="log")
        yield JobPanel()
        yield TaskPanel()
        yield QueueDisplay()
        yield self.status
        yield CompactNotice()
        yield CommandAutocomplete(id="cmd-autocomplete")
        yield PromptInput(history=self._history)
        # The full-bleed sub-agents screen (hidden until ctrl+x). Its detail host
        # owns the live transcript panes; the renderer mounts each spawn's stream
        # into them whether or not the screen is open, so opening mid-run shows an
        # already-current transcript.
        yield SubAgentsView()
        yield Footer()

    async def on_mount(self) -> None:
        if self.harness is not None:
            # Attach the pump before anything else touches the harness: an
            # event published before a subscriber attaches (no after_seq
            # backlog replay for a fresh subscription — see EventBus.attach)
            # is gone for good. attach() runs here, synchronously, rather than
            # inside the pump task: create_task only schedules the body, so
            # attaching there would leave a window (until the loop next
            # yields) where a published event has no subscriber yet.
            self._bind_host()
            self.link = LocalSessionLink(self.harness, self.host)
            self._start_pump(self.link.attach())
        else:
            assert self._remote is not None
            self.link = RemoteSessionHost(
                self._remote, self._remote.workspace_root, on_state=self._on_link_state
            )
        for theme in MARIM_THEMES:
            self.register_theme(theme)
        self.theme = load_theme()
        self.sub_title = str(self.link.info.workspace_root)
        self.status.mode = self.link.info.mode
        self.status.refresh_title()
        log = self.query_one("#log", VerticalScroll)
        # Hand the renderer the persistent transcript host so spawns create their
        # panes there.
        self.stream.detail_host = self.query_one(SubAgentsView).host
        if self.harness is None:
            self.status.link_label = "daemon"
            try:
                await self._attach_remote(log)
            except HostClosed as exc:
                # The daemon answered the launch probe but not this: gone
                # between the two, or refusing us now. Say so and stay up so
                # the message is readable; every command from here reports
                # the same way.
                self.append_log(ErrorMessage(f"attach failed: {exc}"))
                self._on_link_state("lost")
        else:
            await self._mount_transcript(log)
        self.activity.render_tasks()  # reflect any checklist restored with the session
        self.activity.render_jobs()  # process-scoped jobs survive session switches
        self.queue.render()
        self._announce_session_defaults()
        for text in self._startup_notices:
            self.append_log(NoticeMessage(text))
        # Coalesce streaming text deltas: render buffered AssistantMessages on a
        # shared interval instead of re-parsing the markdown on every token.
        self.set_interval(_STREAM_FLUSH_INTERVAL, self.stream.flush_streams)
        # Anchor the session timer at mount and tick the status bar while idle so
        # the session duration advances even with no turn running.
        self.status.session_start = time.monotonic()
        self.set_interval(_CLOCK_TICK_INTERVAL, self.status.refresh_status)
        # Animate the working indicator while a turn runs (no-op when idle).
        self.set_interval(_SPINNER_TICK_INTERVAL, self.status.tick_spinner)
        # Land focus on the prompt so the user can type immediately.
        self.query_one(PromptInput).focus()
        if self.harness is not None:
            await self._start_local_session(self.harness, log)

    async def _mount_transcript(self, log: VerticalScroll) -> None:
        """The intro header (welcome, or the resumed/attached summary) and
        the replay of whatever history the link reports."""
        intro = await self.session.mount_header(log)
        history = self.history_messages
        if self.harness is None:
            name = self.link.info.session_name or self.link.info.session_id
            self.stream.append_stream(
                intro,
                f"**Attached** to `{name}` on the daemon — {len(history)} messages, "
                f"{self.link.info.usage.total_tokens} tokens.",
            )
        elif history:
            n = len(history)
            tokens = self.link.info.usage.total_tokens
            self.stream.append_stream(
                intro,
                f"**Resumed session** — {n} messages, {tokens} tokens restored.",
            )
        else:
            self.stream.append_stream(intro, _WELCOME)
        # Replay the restored history AND settle its sub-agent cards through the
        # same seam the switch/clear path uses (SessionView.replay_and_settle).
        # Routing startup resume through it is what makes a spawn killed mid-run
        # surface here as an interrupted card — replaying alone (the old behavior)
        # left the killed spawn's sidecar unsettled and the card invisible.
        await self.session.replay_and_settle(log)
        self.stream.flush_streams()  # render the static intro/replay before first paint
        # A resumed session opens at the bottom (where you left off); a fresh one
        # starts top-aligned with the header pinned at the top and only anchors
        # once a turn's output overflows the viewport (see _anchor_on_overflow).
        if history:
            log.anchor()
            # Already anchored at the bottom — latch so a later flush won't re-anchor
            # and yank the user back down after they scroll up.
            self.stream._anchored_on_overflow = True

    async def _start_local_session(self, harness: Harness, log: VerticalScroll) -> None:
        """The in-process session's own start-up: catalog, active-time clock,
        MCP, lifecycle hooks, first-open trust. None of it exists for an
        attached TUI — the daemon did all of this when it opened the session."""
        # Seed vision capabilities in the background so the text-only-model
        # warning can fire even before the user opens the model picker.
        source = harness.model_source
        if source is not None:
            self.run_worker(self.pickers.refresh_vision_caps(source.list_models), exclusive=False)
        # Start the active-time clock on a fresh session (resume()/new_session
        # already do it for resumed/new ones).
        harness.session.ensure_segment_started()
        await self._connect_mcp(harness, log)
        await harness.session_start("resume" if harness.session.history else "startup")
        # First-open trust prompt: bootstrap only sets trust_prompt when the
        # project ships a gated surface AND no decision (env/store) already
        # resolved it. Kicked off as its own worker (not awaited inline) so
        # on_mount itself isn't held hostage to the user answering the panel.
        if getattr(harness, "trust_prompt", None) is not None:
            self.run_worker(prompt_project_trust(self), group="trust", exit_on_error=False)

    async def _attach_remote(self, log: VerticalScroll) -> None:
        """Attach-time reconciliation, in the 4a spec's order: ``GET session``
        seeds the read model and the busy tracker so the status bar is right
        before the first event; the persisted transcript is replayed; the feed
        starts at the persisted boundary, so a running turn's ``turn.started``
        and deltas (all above it) catch the transcript up through the normal
        handlers; then ``GET asks`` mounts any panel the tail did not (an ask
        can outlive its ``ask.pending`` in the ring — the route is the
        authority, the tail is merely faster)."""
        link = self.link
        status = await link.load_session()
        self.turns.on_status(status)
        self.status.mode = link.info.mode
        self.status.refresh_title()
        await self._load_remote_jobs()
        snapshot = await link.history()
        self._remote_history = snapshot.messages
        await self._mount_transcript(log)
        self._start_pump(link.attach(after_seq=snapshot.history_seq))
        if status != "idle":
            self.status.set_busy(True)
            self.turns_idle.clear()
        await self._reconcile_asks()

    async def _reconcile_asks(self) -> None:
        """Make the mounted panels match ``GET asks``: mount what is parked
        and not shown (idempotent by id — the tail may have mounted it
        already), and drop a panel whose ask is no longer parked (its
        ``ask.resolved`` was lost in a gap)."""
        pending = await self.link.pending_asks()
        live = {str(raw.get("id")) for raw in pending}
        for stale in [aid for aid in self._ask_panels if aid not in live]:
            self._dismiss_ask(AskResolved(type="ask.resolved", id=stale, cancelled=True))
        for raw in pending:
            wire = parse_wire_event({"type": "ask.pending", **raw})
            if isinstance(wire, AskPending):
                await self._mount_ask(wire)

    def _start_pump(self, feed: Feed) -> None:
        self._feed = feed
        self._pump_task = create_task(self._event_pump(feed))

    def _on_link_state(self, state: str) -> None:
        """The remote feed's connection state (see RemoteSubscription): shown
        in the status bar, and a notice when the daemon is given up on."""
        labels = {"connected": "daemon", "reconnecting": "daemon · reconnecting…"}
        self.status.link_label = labels.get(state, "daemon · lost")
        if state == "lost":
            sid = self.link.info.session_id
            self.append_log(
                ErrorMessage(
                    f"daemon unreachable — `marim --session {sid}` will take the "
                    "session over locally once the daemon's pid is gone."
                )
            )

    async def _on_stream_gap(self, wire: StreamGap) -> None:
        """The feed fell behind the daemon's ring (a reconnect longer than the
        ring, or a seq regression): the events in between are gone. Re-render
        from the persisted transcript and re-attach at its boundary; whatever
        the running turn streamed before the gap is not in history yet and is
        not shown. The pump reads ``self._feed`` each iteration, so swapping it
        here (inside a dispatch) is enough — ``link.attach`` closes the old
        feed."""
        if self.harness is not None:
            logger.debug("stream.gap on the in-process feed (ignored)")
            return
        logger.info("stream gap (%s): resyncing from history", wire.resync)
        try:
            status = await self.link.load_session()
            snapshot = await self.link.history()
        except HostClosed as exc:
            self.append_log(ErrorMessage(f"resync failed: {exc}"))
            return
        self._remote_history = snapshot.messages
        self.turns.on_status(status)
        self.status.set_busy(status != "idle")
        await self._load_remote_jobs()
        await self.session.render_session(
            "resynced from history; the running turn's earlier output is not shown."
        )
        self._feed = self.link.attach(after_seq=snapshot.history_seq)
        try:
            await self._reconcile_asks()
        except HostClosed as exc:
            # The panels stay as they are: pending_asks raises rather than
            # reading as "no asks", so a failed read never dismisses them.
            self.append_log(ErrorMessage(f"asks not resynced: {exc}"))

    def _bind_host(self) -> None:
        """Build the in-process SessionHost (loop-bound, so on_mount not
        __init__ — see the ``self.host`` annotation there). The harness's session
        claim stays on the harness (claim hygiene) — the host gets claim=None so
        it never tries to flock a second open-file-description in this process
        (self-denial: the harness already holds it). autonomous_wake=False: the
        ActivityMonitor owns wake in-process (the host's own driver is the
        daemon's; two live drivers would race for the same job-finished digests
        — see SessionHost.__init__)."""
        harness = self.require_local("the in-process host")
        self.host = SessionHost(harness, self._bus, autonomous_wake=False)
        # The wake's job-settle trigger must run synchronously inside the jobs
        # registry's on_change callback: jobs.wait() marks a completion
        # wake-consumed the instant it returns, so a wake delivered one bus hop
        # later (jobs.changed -> pump -> activity) already sees
        # has_finished_pending() False and never fires. Wrap the host's callback
        # (bound in SessionHost.__init__) so the wake check stays in the
        # callback; the pump still delivers jobs.changed for the panel repaint.
        jobs = harness.deps.jobs
        host_jobs_changed = jobs.on_change

        def _jobs_changed() -> None:
            if host_jobs_changed is not None:
                host_jobs_changed()
            self.activity.maybe_wake()

        jobs.on_change = _jobs_changed

    async def _event_pump(self, feed: Feed) -> None:
        """Render from the feed: the sole path from session events to the
        widgets — the in-process bus subscription, or the daemon's WebSocket
        behind the same ``next_event``/``close`` surface. One feed for the
        app's whole lifetime, except a remote resync (``_on_stream_gap``),
        which swaps ``self._feed`` under this loop. Dies with the app; see
        on_unmount for the link's own teardown, which this pump does NOT own.

        Ordering is the bus's: every turn-end effect (duration stamp, error
        card, queue drain) is a handler on the turn's own events, so it lands
        after the rendering it follows by construction — there is no barrier
        to drain and nothing to race."""
        self._feed = feed
        try:
            while True:
                event = await self._feed.next_event()
                if event is None:
                    continue
                wire = parse_wire_event({"type": event.type, **event.data})
                if wire is None:
                    # Unknown type or a payload that failed validation. Debug,
                    # not warning: a newer daemon's vocabulary is expected to
                    # outrun an older client's, and this is the one place a
                    # silently dropped event leaves any trace.
                    logger.debug("event pump: dropped %s (seq %s)", event.type, event.seq)
                if wire is not None:
                    try:
                        await self._dispatch_wire(wire)
                    except Exception:  # noqa: BLE001 - one bad event must not blind the app
                        logger.exception("event pump: dispatch failed for %s", type(wire).__name__)
        finally:
            self._feed.close()

    async def _dispatch_wire(self, wire: WireEvent) -> None:
        handler = _WIRE_HANDLERS.get(type(wire))
        if handler is not None:
            await handler(self, wire)

    def _announce_session_defaults(self) -> None:
        """One-line advisor/thinking status at session start, so a setting
        inherited from .env or restored with the session is visible without
        opening settings. An off/unset level stays silent — that's the default."""
        advisor = self.link.info.advisor_model_id
        if advisor is not None:
            self.append_log(NoticeMessage(f"Advisor: {advisor} · /advisor"))
        level = self.link.info.thinking_level_id
        if level is not None and level != "off":
            self.append_log(NoticeMessage(f"Thinking: {level} · /think"))

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        """Keep the prompt focused. When focus lands on a non-input main-screen
        widget — the conversation transcript or a panel header, reachable by a
        click or Tab — snap it straight back to the prompt so a keystroke always
        lands in the input. Three scopes are deliberately left alone: a pushed
        modal/overlay (it's a separate screen, ``screen_stack > 1``, and owns its
        own focus), the ctrl+x sub-agents screen (``subagents.open``, which
        drives its list/pane focus), and an active InteractionPanel (ask-user/
        approval) — unlike the ModalScreens it replaces, it's mounted in this
        same base screen, so its OptionList/SelectionList/buttons need this
        guard to back off or Enter/Space could never reach them. Refocusing the
        prompt re-fires this for the prompt itself, which the identity check
        below makes a no-op — no loop."""
        if len(self.screen_stack) > 1 or self.subagents.open or self.query(InteractionPanel):
            return
        try:
            prompt = self.query_one(PromptInput)
        except NoMatches:
            return
        if event.widget is not prompt:
            prompt.focus()

    async def _connect_mcp(self, harness: Harness, log: VerticalScroll) -> None:
        """Open the configured MCP servers and note the outcome. Connection
        failures are surfaced as a notice, never fatal — the app runs fine with
        the servers that did come up (or none at all)."""
        if not harness.mcp.mcp_servers:
            return
        status = await harness.connect()
        if status["connected"]:
            await log.mount(NoticeMessage(f"MCP connected: {', '.join(status['connected'])}"))
        for name, error in status["failed"]:
            await log.mount(ErrorMessage(f"MCP {name} failed: {error}"))

    async def on_unmount(self) -> None:
        """Jobs are process-scoped — kill any still running when the app exits so
        no detached shell or agent run is left behind, and close MCP connections."""
        # The pump is a plain asyncio task (see _pump_task) — cancel it first,
        # before the host stops, so the teardown's own events (the cancelled
        # turn's ask.resolved, the final session.status) never reach a widget
        # tree that is going away.
        if self._pump_task is not None:
            self._pump_task.cancel()
            with suppress(CancelledError):
                await self._pump_task
        if self.harness is None:
            # Attached: close the feed and the HTTP client, print the summary
            # from the read model, and nothing else — the daemon owns the
            # persist, the lifecycle hooks and the session's jobs.
            await self.link.close()
            info = self.link.info
            self._write_exit_summary(info.duration_seconds, info.usage, info.model_id)
            return
        # host.stop(), not host.aclose(): stop() interrupts the running turn
        # and ends the queue worker (plain asyncio, not a Textual worker — left
        # running it warns at interpreter shutdown) and publishes nothing more;
        # aclose() would then wait for an in-flight autoname, the opposite of
        # this snappy-exit path, which keeps its own teardown below.
        await self.host.stop()
        # Persist session duration before tearing down. Fold this run's active
        # time into the total and force the save: the final segment must land
        # even when history is unchanged (an idle exit would otherwise skip the
        # cache-gated persist and lose it).
        session = self.harness.session
        # Don't hold exit hostage to an in-flight background autoname (a titler
        # LLM call). auto_named stays True, so the next resume simply retries.
        session.cancel_autoname()
        session.finalize_active_time()
        session.persist(force=True)
        # Show a brief session summary in the terminal after exit.
        self._write_exit_summary(session.duration_seconds, session.usage, self.harness.model_id)
        await self.harness.deps.jobs.cancel_all()
        await self.harness.session_end("exit")
        await self.harness.aclose()

    def _write_exit_summary(self, total: float, usage: Any, model_id: str | None) -> None:
        total_tokens = usage.input_tokens + usage.output_tokens
        cost, _ = resolve_cost(usage, model_id)
        parts = [f"Session: {format_duration(total)}"]
        parts.append(f"Tokens: {human_tokens(total_tokens)}")
        if cost is not None:
            parts.append(f"Cost: {format_cost(cost)}")
        summary = " · ".join(parts)
        # Reset the terminal tab title so a stale "● working" mark doesn't linger
        # after exit. Best-effort: the driver may already be tearing down.
        if self._driver is not None:
            try:
                self._driver.write(osc_title("marim-harness"))
                self._driver.write(f"\r\n{summary}\r\n")
                self._driver.flush()
            except OSError:
                pass

    # --- The seam: local vs attached ---

    @property
    def attached(self) -> bool:
        """True when this TUI drives a daemon-owned session (no harness here)."""
        return self.harness is None

    def require_local(self, what: str) -> Harness:
        """The harness, for a feature that needs the process that owns it.
        Raises ``RemoteOnly`` on an attached TUI; the command dispatcher and
        the key actions catch it and post the one notice."""
        if self.harness is None:
            raise RemoteOnly(what)
        return self.harness

    def note_remote_only(self, exc: RemoteOnly) -> None:
        self.append_log(NoticeMessage(str(exc)))

    @property
    def history_messages(self) -> Sequence[Any]:
        """What the replay reads: the live history in process, the last
        fetched snapshot when attached."""
        if self.harness is not None:
            return self.harness.session.history
        return self._remote_history

    def session_manager(self) -> SessionManager | None:
        """The workspace's session manager: the harness's own in process; when
        attached, one over the same on-disk workspace (the picker lists and
        deletes from disk either way — the daemon does not own the listing)."""
        if self.harness is not None:
            return self.harness.session.manager
        if self._remote_manager is None:
            self._remote_manager = SessionManager(self.link.info.workspace_root)
        return self._remote_manager

    @property
    def jobs(self) -> JobsView:
        """The session's jobs as the panels and the replay read them: the
        live registry in process, the daemon's mirrored rows attached."""
        if self.harness is not None:
            return self.harness.deps.jobs
        return self._remote_jobs

    @property
    def jobs_known(self) -> bool:
        """True when ``jobs`` reflects the session's real registry — always
        in process; attached, once a ``GET jobs`` read has succeeded. Until
        then the replay cannot tell a spawn the daemon is driving from one
        that died with it (see ``SessionView._unknown_running``)."""
        return self.harness is not None or self._remote_jobs.synced

    async def _load_remote_jobs(self) -> None:
        """The attach/resync jobs read, before the transcript is replayed:
        the replay settles sub-agent cards against these rows. A failed read
        is reported and leaves the mirror as it was (unsynced on a first
        attach), so the replay degrades to the 4a behaviour rather than
        flagging every running sidecar interrupted."""
        try:
            self._remote_jobs.apply(await self.link.jobs())
        except HostClosed as exc:
            self.append_log(ErrorMessage(f"jobs not readable: {exc}"))

    async def refresh_remote_jobs(self) -> None:
        """Re-read the daemon's jobs after a ``jobs.changed``. A job that
        settled while its detached card is still waiting gets its full
        result from ``GET jobs/{id}`` before the repaint — the list carries
        only tails — so the card reads exactly as it does in process. Then
        the same repaint the local registry hook runs. Runs as an exclusive
        worker: a restart mid-read drops this pass before ``apply``, so the
        mirror is never half-applied."""
        mirror = self._remote_jobs
        try:
            mirror.apply(await self.link.jobs())
            for job_id in mirror.settled_needing_result(self.stream.detached_job_ids()):
                mirror.set_result(job_id, await self.link.job_output(job_id))
        except HostClosed as exc:
            self.append_log(ErrorMessage(f"jobs not refreshed: {exc}"))
            return
        self.activity.on_jobs_changed()

    @property
    def turn_busy(self) -> bool:
        """True from the moment a turn is submitted (user submit, drained
        queue, system command, or autonomous wake) until the host reports
        idle after it. The single guard against submitting a second turn
        behind a running one or tearing down the conversation/session under
        it. Read from the wire plus the submit latch — see TurnTracker."""
        return self.turns.busy

    def _refresh_mode_display(self) -> None:
        """Push the current mode into the status bar's ``mode`` reactive.

        Called from action_cycle_mode, the /mode command, and via the
        on_mode_change UIHooks callback so a tool that flips workspace.mode
        mid-turn (e.g. present_plan) can nudge the status bar to redraw.
        Runs on the event-loop thread: the exclusive turn worker is an asyncio
        task, so no call_from_thread marshalling is needed — Textual widget
        mutations from asyncio tasks are safe.
        """
        self.status.mode = self.link.info.mode

    async def action_cycle_mode(self) -> None:
        await self.set_mode(Mode(self.link.info.mode).cycle())

    async def set_mode(self, mode: Mode) -> None:
        """Switch the approval mode through the link (the harness's own setter
        in process; ``POST mode`` when attached, where the daemon may refuse
        mid-turn — reported, not raised). The status bar follows either way:
        the local setter is synchronous, the remote read model is updated by
        the link on success."""
        try:
            await self.link.set_mode(mode.value)
        except HostClosed as exc:
            self.append_log(NoticeMessage(f"Mode not switched: {exc}"))
        self._refresh_mode_display()

    def action_toggle_outputs(self) -> None:
        """Ctrl+O: reveal every tool output in full (expand groups, uncap edit
        diffs), or restore the default view on a second press."""
        self.stream.toggle_reveal_all()

    # --- Sub-agents screen (ctrl+x) — driven by the SubAgentsScreen collaborator ---

    def action_toggle_subagents(self) -> None:
        self.subagents.toggle()

    def action_close_subagents(self) -> None:
        self.subagents.close()

    def action_show_plan(self) -> None:
        """Open the full plan overlay, or flash a hint when no plan exists yet."""
        from .plan_screen import PlanScreen

        plan = self.harness.deps.plan if self.harness is not None else None
        if plan is None or self.harness is None:
            self.notify(
                "No plan yet — the agent presents one in plan mode.", severity="information"
            )
            return
        self.push_screen(PlanScreen(plan.summary, plan.path, self.harness.deps.tasks.items))

    def on_data_table_row_highlighted(self, event) -> None:
        # Textual bubbles the DataTable message to the App; forward to the viewer.
        self.subagents.on_row_highlighted(event)

    def watch_theme(self, theme: str) -> None:
        """Persist the active theme so it's the startup theme next run. Only the
        marim themes are saved; Textual may set built-in defaults during init,
        which save_theme ignores."""
        save_theme(theme)

    # --- Turn lifecycle ---

    async def start_turn(
        self, text: str, attachments: list[tuple[bytes, str]] | None = None
    ) -> None:
        """Submit a user turn to the host. Shared by a fresh submit and a
        drained queue item. Resets the autonomous-wake chain; everything the
        transcript shows for the turn (the user bubble included) arrives back
        through the pump on ``turn.started``.

        The busy latch is set before the submit is awaited (see
        ``_submit_turn``), so no concurrent submit can slip past ``turn_busy``
        while a remote round trip is in flight."""
        self.activity.note_user_turn()
        await self._submit_turn(text, attachments, "user")

    async def start_system_turn(self, prompt: str) -> bool:
        """Submit a turn for a system-initiated prompt — a slash command like
        /remember or /skill that injects its own prompt. Unlike start_turn it
        leaves the autonomous-wake chain untouched, and the transcript mounts
        no user message for it (``turn.started.trigger == "system"``).

        Refused (returns False, no turn started) while a turn is already
        running: a system prompt behind the user's turn would run against a
        conversation they haven't seen finish. Returns True when submitted."""
        if self.turn_busy:
            self.query_one("#log", VerticalScroll).mount(
                NoticeMessage("A turn is already running — wait for it to finish or press Esc.")
            )
            return False
        return await self._submit_turn(prompt, None, "system")

    def mount_wake_turn(self) -> None:
        """The wake effect the ActivityMonitor's driver invokes: submit the
        digest-only turn. Synchronous (we may be in a sync on_change callback):
        the pending latch is set here, before the submit task gets its first
        slice, so the app is busy from this call on; the "resumed" notice is
        posted when its ``turn.started`` arrives."""
        self.turns.note_pending()
        self.turns_idle.clear()
        create_task(self._submit_turn("", None, "autonomous"))

    async def _submit_turn(
        self, text: str, attachments: list[tuple[bytes, str]] | None, trigger: str
    ) -> bool:
        """``link.submit`` plus the busy latch. The pending latch goes up
        before the await: in process the submit never yields, so it is set and
        replaced by the turn id in one step; over HTTP a second Enter during
        the round trip must queue, not double-submit. A full host queue
        re-stages a user prompt at the front of the TUI's own queue and pauses
        it, so the text is kept and the user decides when to retry; a
        system/autonomous prompt is dropped with a notice (there is nothing to
        keep). A closed host (exit in progress) drops silently; an unreachable
        daemon says so."""
        self.turns.note_pending()
        self.turns_idle.clear()
        try:
            turn_id = await self.link.submit(text, attachments, trigger=trigger)
        except TurnQueueFull:
            if trigger == "user":
                self.queue.prepend(text, attachments)
                self.queue.paused = True
            self.append_log(
                NoticeMessage("The session's turn queue is full — press ctrl+r to retry.")
            )
            return self._submit_refused()
        except HostClosed as exc:
            if str(exc):
                self.append_log(ErrorMessage(f"turn not submitted: {exc}"))
            logger.warning("turn submitted during teardown was dropped: %r", text[:60])
            return self._submit_refused()
        except Exception as exc:  # noqa: BLE001 - a submit must never leave the latch stuck
            # SessionClaimed (someone took the session between attach and
            # now) or anything else the link raises: report, release.
            self.append_log(ErrorMessage(f"turn not submitted: {exc}"))
            return self._submit_refused()
        self.turns.note_submitted(turn_id)
        return True

    def _submit_refused(self) -> bool:
        self.turns.clear_pending()
        if not self.turns.busy:
            self.turns_idle.set()
        return False

    async def action_cancel_turn(self) -> None:
        # Esc between submit and turn.started finds no task to cancel; the
        # turn starts anyway and a second Esc lands. Not worth a "pending
        # cancel" latch — the window is one worker hop.
        if self.turns.busy:
            await self.link.interrupt()

    async def _on_turn_started(self, wire: TurnStarted) -> None:
        self.turns.on_started(wire.turn_id)
        self.turns_idle.clear()
        self.status.set_busy(True)
        # Drop finished tool-widget entries from the prior turn(s) so the per-turn
        # tracking dict doesn't grow unbounded across a long session. Done at the
        # turn boundary (not per approval round) so the within-turn duplicate guard
        # for gated tools keeps its entries while the turn is live.
        self.stream.prune_completed()
        # The pure-wire pump never calls StreamRenderer.on_events, so turn.started
        # is the only run boundary it sees — this is where the per-run reset lives
        # (stale text_open reopening into the new turn was the leak fixed for
        # on_events in commit 23462072; stale tool_group/solo_tool would splice the
        # new turn's first tool call into the previous turn's group).
        #
        # Deliberately NOT a per-agent-run reset: an approval round inside a turn
        # starts a fresh agent.run on the harness side but publishes nothing on the
        # wire, so tool cards either side of an approval share one group when the
        # continuation opens with more tool calls. The old on_events path reset per
        # run and split them — but the persisted history carries no approval marker,
        # so session replay (session_view.replay_history) groups that same burst as
        # ONE run. Keeping the group across the approval is what makes the live
        # transcript and a resumed one agree; text, thinking, a user prompt, an
        # ask_user call, and a workflow spawn still break the run as before.
        self.stream.begin_run()
        # The user bubble is rendered from the wire, after the reset above, so
        # it sits at the run boundary whichever client submitted the turn. A
        # system prompt shows nothing (it is the command's own business); an
        # autonomous wake shows why the agent woke.
        if wire.trigger == "user":
            await self.query_one("#log", VerticalScroll).mount(UserMessage(wire.prompt))
        elif wire.trigger == "autonomous":
            self.append_log(NoticeMessage("⏰ Resumed — background job(s) finished"))

    async def _on_turn_finished(self, wire: TurnFinished) -> None:
        # A turn cancelled before its first step ends without a turn.started:
        # this is the only event that can free its submit latch.
        self.turns.on_finished(wire.turn_id)
        # The run-end counterpart of turn.started: on_events finalized the
        # trailing thought/text block when the stream generator ran dry; on the
        # wire that moment is turn.finished. Interrupted turns publish it too,
        # so a cancelled thought still collapses to its preview.
        self.stream.end_run()
        if wire.interrupted:
            # Esc (or a remote interrupt). Pause the queue so the next staged
            # prompt doesn't run behind a turn the user just killed, and settle
            # anything still pending: a cancelled turn otherwise leaves its
            # tool rows and sub-agent cards "pending" forever, each holding a
            # 10Hz repaint timer and rendering a spinner for work that is dead.
            self.queue.paused = True
            self.append_log(ErrorMessage("turn cancelled"))
            self.stream.settle_pending("cancelled")
            return
        # Stamp the just-finished turn's duration under its reply (success
        # only; cancelled/errored turns surface an ErrorMessage instead).
        elapsed = format_duration(time.monotonic() - self.status.turn_start, precise=True)
        await self.query_one("#log", VerticalScroll).mount(TurnMeta(elapsed))
        self.activity.desktop_notify("Turn complete", f"Finished in {elapsed}", "turn_complete")

    def _on_turn_error(self, wire: TurnError) -> None:
        self.turns.on_finished(wire.turn_id)  # same latch release as finished
        # The host publishes turn.error INSTEAD of turn.finished, so the wire
        # never reaches the run-end finalize on its own: the thought/text the
        # turn died on would stay open above the error card until the next
        # turn's first event swept it as stale. Close it the way the finished
        # path does, then the same pause/settle as a cancel — a turn that dies
        # mid tool-call leaves that row "pending" just as surely as an Esc.
        self.stream.end_run()
        self.queue.paused = True
        self.append_log(ErrorMessage(wire.error))
        self.activity.desktop_notify("Turn error", wire.error, "error")
        self.stream.settle_pending(wire.error)

    async def _on_session_status(self, wire: SessionStatus) -> None:
        """Status events drive state, turn events drive the transcript: the
        idle edge is where busy drops and the after-turn hand-off runs
        (queue drain or wake), whichever way the turn ended."""
        if self.turns.on_status(wire.status) is not Transition.BECAME_IDLE:
            return
        self.status.set_busy(False)
        # Guard against an orphaned compaction notice if maybe_compact raised
        # between on_compact_start() and on_compact(). query_one is guarded
        # because this can run during teardown, where the widget may already
        # be gone: a NoMatches here would skip after_turn() below and strand
        # the queue and the wake chain.
        with suppress(NoMatches):
            self.query_one(CompactNotice).compacting = False
        # Attached: the fields only the daemon knows (usage, threshold, name,
        # the persisted tail) move at turn end — one re-read per turn, so the
        # context gauge is "as of the last turn end", which is when it changes.
        if self.harness is None:
            with suppress(HostClosed):
                await self.link.refresh()
            self.status.refresh_status()
        # Set before the hand-off: a drained queue item re-clears it on submit,
        # and a waiter woken by this edge is woken by a real one either way.
        self.turns_idle.set()
        await self.queue.after_turn()  # drain next queued item, or wake on jobs

    # --- Queue actions (the Textual surface; QueueController does the work) ---

    async def action_run_queued(self) -> None:
        await self.queue.resume()

    def action_remove_queued(self, id: str) -> None:
        self.queue.remove(id)

    async def action_edit_queued(self, id: str) -> None:
        await self.queue.edit_in_prompt(id)

    # --- Quitting ---

    def _maybe_warn_pending_quit(self) -> bool:
        """Confirm-to-quit guard against an accidental Ctrl+C. Returns True if
        the quit should be cancelled (a warning was just shown); False to let a
        second attempt within _QUIT_CONFIRM_WINDOW of the first proceed. Always
        warns on the first attempt, even with an empty queue — a stray keypress
        is just as disruptive either way."""
        now = time.monotonic()
        if self._quit_warned_at is not None and now - self._quit_warned_at <= _QUIT_CONFIRM_WINDOW:
            return False
        self._quit_warned_at = now
        if self.queue:
            message = (
                f"{len(self.queue.items)} queued message(s) will be discarded. "
                "Quit again to confirm."
            )
        else:
            message = "Quit again to confirm."
        self.query_one("#log", VerticalScroll).mount(NoticeMessage(message))
        return True

    async def action_quit(self) -> None:
        if self._maybe_warn_pending_quit():
            return
        await super().action_quit()

    def warn_typed_quit_discards(self) -> bool:
        """The /exit · /quit counterpart of ``_maybe_warn_pending_quit``. Returns
        True if the quit should be cancelled (a warning was just shown).

        Deliberately *not* the same guard as ctrl+c. That one always warns,
        because the risk it defends against is a stray keypress — which doesn't
        apply to six characters typed on purpose. What does still apply is the
        silent data loss: before this existed, /exit called app.exit() directly
        and threw away everything the user had queued without a word. So this
        warns only when there is actually something to discard, and over a window
        wide enough to re-type the command (_TYPED_QUIT_CONFIRM_WINDOW).

        Shares ``_quit_warned_at`` with the ctrl+c guard on purpose: a user who
        was just told what a quit would cost shouldn't be told twice for
        switching from the key to the command."""
        if not self.queue:
            return False
        now = time.monotonic()
        if (
            self._quit_warned_at is not None
            and now - self._quit_warned_at <= _TYPED_QUIT_CONFIRM_WINDOW
        ):
            return False
        self._quit_warned_at = now
        self.query_one("#log", VerticalScroll).mount(
            NoticeMessage(
                f"{len(self.queue.items)} queued message(s) will be discarded. "
                "Run /exit again to confirm."
            )
        )
        return True

    # --- Log helpers ---

    async def post_system(self, markdown: str) -> AssistantMessage:
        """Render a system/command message into the log (markdown)."""
        log = self.query_one("#log", VerticalScroll)
        msg = AssistantMessage()
        await log.mount(msg)
        self.stream.append_stream(msg, markdown)
        self.stream.flush_streams()  # one-shot system text: render it now, no tick wait
        return msg

    def append_log(self, widget) -> None:
        """Mount a notice/error into the log, keeping the viewport pinned to the
        bottom only if it was already there. A user who scrolled up to read history
        isn't yanked back down, but a user following live still sees new messages
        (errors, steering echoes) scroll into view instead of landing off-screen."""
        log = self.query_one("#log", VerticalScroll)
        at_bottom = log.scroll_offset.y >= log.max_scroll_y
        log.mount(widget)
        if at_bottom:
            log.scroll_end(animate=False)

    # --- Session lifecycle (guards here; SessionView does the rebuild) ---

    async def reset_conversation(self) -> None:
        """Wipe the conversation and re-show the welcome screen (the /clear cmd).
        Refused mid-turn — clearing would tear down the log the running turn is
        still streaming into and wipe history it is appending to."""
        if await self._refuse_if_session_busy("clear"):
            return
        self.require_local("/clear")
        await self.session.reset_conversation()

    async def start_new_session(self, name: str | None = None) -> None:
        """Begin a fresh named session, leaving existing ones on disk. Refused
        mid-turn — switching the active session out from under a running turn
        would race its history persist."""
        if await self._refuse_if_session_busy("start a new session"):
            return
        self.require_local("/new")
        await self.session.start_new_session(name)

    async def switch_to_session_id(self, session_id: str) -> None:
        """Load an existing session and show where it left off. Refused mid-turn
        for the same reason as /new — the running turn writes to the session it
        would be switched away from — and refused when another process owns the
        target: claims follow the active view, so driving a session means
        claiming it, and a claimed session is off-limits until its holder
        releases it. Also refused (posted, not raised) when the target's store
        won't load, including the file having vanished between being listed in
        the picker and being claimed here."""
        if await self._refuse_if_session_busy("switch sessions"):
            return
        from ...session.claim import SessionClaimed
        from ...session.store import SessionLoadError

        if self.harness is None or self._owned_by_daemon(session_id):
            # Neither direction can be switched in place yet: an attached TUI
            # has no harness to swap the session on, and a local one cannot
            # attach mid-run (4b). Say what works instead.
            await self.post_system(
                "attached sessions can't be switched in place yet — run "
                f"`marim --session {session_id}`"
            )
            return
        try:
            await self.session.switch_to_session_id(session_id)
        except SessionClaimed as exc:
            who = exc.holder.describe() if exc.holder is not None else "another process"
            await self.post_system(f"Can't switch sessions: {exc.session_id} is owned by {who}.")
        except SessionLoadError as exc:
            await self.post_system(f"Can't switch sessions: {exc}")

    def _owned_by_daemon(self, session_id: str) -> bool:
        manager = self.session_manager()
        if manager is None:
            return False
        holder = read_holder(manager.session_path(session_id))
        return holder is not None and holder.kind == "daemon"

    async def _refuse_if_session_busy(self, what: str) -> bool:
        """True (with a notice posted) when ``what`` must not run right now.
        Both flows below tear down or rebind the session store, so both have to
        wait out a running turn *and* an in-flight compaction."""
        if self.turn_busy:
            await self.post_system(f"Can't {what} while a turn is running. Press Esc first.")
            return True
        if self.compact_busy:
            await self.post_system("Compaction in progress — wait for it to finish.")
            return True
        return False

    async def rewind_to_checkpoint(self, index: int) -> None:
        """Rewind the session to checkpoint ``index`` and rebuild the log.
        Refused mid-turn — rewinding under a running turn would race history.
        Checks both busy flags: ``turn_busy`` covers the turn worker, and
        ``status.busy`` guards any other flow that marks the app busy without it."""
        if self.turn_busy or self.status.busy:
            await self.post_system("Can't rewind while a turn is running. Press Esc first.")
            return
        harness = self.require_local("/rewind")
        try:
            result = harness.checkpoints.rewind(index)
        except KeyError:
            await self.post_system(f"No checkpoint #{index}. Try `/rewind` to list them.")
            return
        note = f"rewound to checkpoint #{index}"
        if result.restored_files:
            note += " (files restored)"
        elif result.restore_failed:
            note += (
                " — ⚠ file restore failed; the working tree may be partial "
                "(`/rewind undo` to recover the pre-rewind state)"
            )
        await self.session.render_session(note)
        self.status.refresh_status()

    async def undo_rewind(self) -> None:
        """Undo the last rewind, restoring the conversation (and the working tree, if
        the rewind touched files) to their pre-rewind state. Re-renders the log since
        the conversation changed. Refused mid-turn (same double-flag check as
        ``rewind_to_checkpoint``)."""
        if self.turn_busy or self.status.busy:
            await self.post_system("Can't undo a rewind while a turn is running. Press Esc first.")
            return
        if self.require_local("/rewind undo").checkpoints.undo_rewind():
            await self.session.render_session(
                "undid the rewind — restored the pre-rewind conversation and files"
            )
            self.status.refresh_status()
        else:
            await self.post_system("Nothing to undo — no rewind in this session.")

    def open_settings(self) -> None:
        """Open the full-bleed settings screen: runtime settings apply live;
        env-backed settings save to the global .env on demand."""
        from ...config import load_config

        self.push_screen(
            SettingsScreen(
                harness=self.require_local("the settings screen"),
                current_theme=self.theme,
                env_cfg=load_config(),
            )
        )

    async def open_session_picker(self) -> None:
        """Open the session picker and let the user browse/filter/switch/delete
        saved sessions. Sessions are fetched synchronously up front (listing is
        a cheap header-only parse — see session/store.py's _header_fields), so
        unlike the model pickers there's no async fetch/loading state to manage.

        Uses the callback form of push_screen (not push_screen_wait) for the same
        reason ModelPickers.open_model does: /sessions dispatches from the command
        path, which is not a worker — push_screen_wait would raise NoActiveWorker
        there.
        """
        manager = self.session_manager()
        infos = manager.list() if manager is not None else []
        # A holder tag per row (`· daemon`, `· tui (pid N)`): one small sidecar
        # read each, no lock taken.
        holders = {}
        if manager is not None:
            for info in infos:
                holder = read_holder(manager.session_path(info.id))
                if holder is not None:
                    holders[info.id] = holder
        active = self.link.info.session_id
        self.push_screen(
            SessionPickerModal(infos, active, holders=holders), self._on_session_chosen
        )

    async def _on_session_chosen(self, chosen: str | None) -> None:
        """Apply a session selected in the picker. Invoked by push_screen when the
        modal is dismissed; a None result (cancelled) is a no-op. Routes through
        switch_to_session_id above, so the mid-turn refusal guard applies here too."""
        if not chosen:
            return
        try:
            await self.switch_to_session_id(chosen)
        except RemoteOnly as exc:
            self.note_remote_only(exc)

    def _find_session_picker_modal(self) -> SessionPickerModal | None:
        """Find the session picker on the screen stack if it is still mounted.

        Found on the screen stack, not via self.query(): a pushed Screen is not
        a DOM descendant of the App, so `query` returns nothing for it."""
        for screen in self.screen_stack:
            if isinstance(screen, SessionPickerModal):
                return screen
        return None

    async def on_session_picker_modal_deleted(self, message: SessionPickerModal.Deleted) -> None:
        """The picker already removed the row optimistically; this performs the
        actual on-disk teardown via the same SessionManager.delete used by
        `marim sessions delete` (interfaces/cli/sessions.py). SessionManager.delete
        refuses a session claimed by another live process — report that instead
        of crashing, AND tell the still-open picker to undo its optimistic
        removal, so the user isn't left looking at a vanished row for a session
        that still exists. (Waiting for the picker's next open to re-list from
        disk isn't enough: the picker is usually still on screen.) On success,
        confirm the deletion in the picker. Either way, tolerate the picker
        having already been dismissed."""
        from ...session.claim import SessionClaimed

        manager = self.session_manager()
        if manager is None:
            return
        try:
            manager.delete(message.session_id)
        except SessionClaimed as exc:
            who = exc.holder.describe() if exc.holder is not None else "another process"
            picker = self._find_session_picker_modal()
            if picker is not None:
                picker.note_delete_failed(exc.session_id, f"Can't delete: owned by {who}")
            await self.post_system(f"Can't delete {exc.session_id}: it is owned by {who}.")
        else:
            picker = self._find_session_picker_modal()
            if picker is not None:
                picker.note_deleted(message.session_id)

    # --- Interaction panels (event-driven, via ask.pending / ask.resolved) ---
    # SessionHost is the sole bind_ui consumer: approval/ask/plan park an ask
    # and publish ask.pending; the pump mounts the matching panel here. Answers
    # ride host.answer_ask; ask.resolved — published for a LOCAL answer, for
    # ANOTHER client's answer, and for an interrupt's cancel — is the single
    # dismissal path (_dismiss_ask), so a panel can never outlive its ask.

    def _panel_for_ask(self, wire: AskPending) -> InteractionPanel | None:
        payload = wire.payload
        if wire.kind == "approval":
            tool_name = str(payload.get("tool_name") or "")
            self.activity.desktop_notify("Approval needed", f"Tool: {tool_name}", "approval_needed")
            return ApprovalPanel(tool_name, _approval_args(payload.get("args")))
        if wire.kind == "question":
            questions = [_question_from_dict(q) for q in payload.get("questions", [])]
            prompt = questions[0].question if questions else ""
            self.activity.desktop_notify("Question from agent", prompt, "ask_user")
            return AskUserPanel(questions)
        if wire.kind == "plan":
            summary = str(payload.get("summary") or "")
            steps = [str(s) for s in payload.get("steps", [])]
            choices = [
                Choice(label=str(c.get("label", "")), description=c.get("description"))
                for c in payload.get("choices", [])
            ]
            self.activity.desktop_notify("Plan ready", summary, "ask_user")
            # Refresh the TaskPanel title now that deps.plan is set.
            self.activity.render_tasks()
            return PlanCard(summary, steps, choices)
        return None

    async def _mount_ask(self, wire: AskPending) -> None:
        if wire.id in self._ask_panels:
            return  # already shown (the tail and GET asks both reported it)
        panel = self._panel_for_ask(wire)
        if panel is None:
            return
        previous = await mount_panel(self, panel)
        self._ask_panels[wire.id] = (panel, previous)
        self.run_worker(self._answer_ask(wire.id, panel), group="asks")

    async def _answer_ask(self, ask_id: str, panel: InteractionPanel) -> None:
        """Local verdict → host.answer_ask. The panel itself comes down off the
        ask.resolved this answer triggers (a single dismissal path)."""
        try:
            result = await panel.result
        except CancelledError:
            # The ask was resolved elsewhere before the user answered (an
            # interrupt's cancel, or another client). Nothing to send.
            return
        try:
            await self.link.answer_ask(ask_id, _ask_payload(panel, result))
        except HostClosed as exc:
            await self._answer_undelivered(ask_id, exc)

    async def _answer_undelivered(self, ask_id: str, exc: HostClosed) -> None:
        """A verdict the host did not take (attached: a transport failure or
        a refusal other than "already answered"; see RemoteSessionHost.answer_ask).
        The ask is still parked on the daemon, so: say so, drop the panel
        whose verdict is spent, and re-read ``GET asks`` so the ask comes back
        as a fresh panel the user can answer again. When even that read fails
        the resync after the link recovers reconciles the panels."""
        self.append_log(ErrorMessage(f"answer not delivered: {exc}"))
        self._dismiss_ask(AskResolved(type="ask.resolved", id=ask_id, cancelled=True))
        try:
            await self._reconcile_asks()
        except HostClosed as again:
            logger.info("asks not re-read after an undelivered answer: %s", again)

    def _dismiss_ask(self, wire: AskResolved) -> None:
        entry = self._ask_panels.pop(wire.id, None)
        if entry is None:
            return
        panel, previous = entry
        if not panel.result.done():
            # Resolved with the panel still awaiting a local verdict — release
            # the _answer_ask worker. An interrupt's cancel is silent (the
            # "turn cancelled" card says it all); an answer from elsewhere is
            # not, or the panel would just vanish under the user's cursor.
            panel.result.cancel()
            if not wire.cancelled:
                self.append_log(NoticeMessage(_answered_elsewhere(panel, wire.answer)))
        unmount_panel(self, panel, previous)

    async def _on_workflow_spawn(
        self, stream_id: str, type_: str, task: str, parent_id: str
    ) -> None:
        """Claim a card for a workflow-spawned sub-agent (see bind_ui). Fired on
        the app's event loop by the workflow engine before it launches the child,
        so — like on_subagent_event — direct widget mutation via the renderer is
        safe with no call_from_thread marshalling."""
        await self.stream.claim_workflow_spawn(stream_id, type_, task, parent_id)

    def _on_workflow_log(self, tool_call_id: str, message: str) -> None:
        """Route a workflow script's log() line: persist it into the run
        card's pane (so it survives past the toast) and raise the transient
        toast. Fired on the app's event loop by the engine, so direct
        renderer mutation is safe — same as _on_workflow_spawn."""
        self.stream.append_workflow_log(tool_call_id, message)
        self.notify(rich.markup.escape(message), title="workflow", timeout=4)

    # --- Slash-command autocomplete ---

    def _show_autocomplete(self, query: str) -> None:
        if self._autocomplete is None:
            self._autocomplete = self.query_one("#cmd-autocomplete", CommandAutocomplete)
        # Re-derive the float offset from the prompt's *current* height every
        # time: the box grows with its content, so a menu positioned once (or by
        # a stylesheet constant) ends up covering a multi-line draft.
        self._autocomplete.position_above(self.query_one(PromptInput).box_height)
        self._autocomplete.filter(query, backend_inventory=self.link.info.backend_inventory)

    def _hide_autocomplete(self) -> None:
        if self._autocomplete is not None:
            self._autocomplete.visible = False

    def autocomplete_navigate(self, delta: int) -> bool:
        """Move the open slash-menu's highlight (the prompt forwards Up/Down here
        while the menu is showing). Returns True when it consumed the key."""
        if self._autocomplete is None:
            return False
        return self._autocomplete.move_highlight(delta)

    def autocomplete_accept(self) -> bool:
        """Complete the highlighted slash command into the prompt (the prompt
        forwards Tab here while the menu is showing). Returns True when a command
        was filled in, False when there's nothing to accept."""
        if self._autocomplete is None:
            return False
        return self._autocomplete.accept_highlighted()

    def on_prompt_input_slash_changed(self, event: PromptInput.SlashChanged) -> None:
        first_line = event.value.split("\n", 1)[0]
        query = first_line[1:]  # strip the leading /
        self._show_autocomplete(query)

    def on_prompt_input_slash_dismissed(self, _event: PromptInput.SlashDismissed) -> None:
        self._hide_autocomplete()

    def on_command_autocomplete_command_selected(
        self, event: CommandAutocomplete.CommandSelected
    ) -> None:
        prompt = self.query_one(PromptInput)
        prompt.text = f"/{event.command_name} "
        prompt.move_cursor(prompt.document.end)
        self._hide_autocomplete()
        prompt.focus()

    # --- Submission routing ---

    async def on_prompt_input_steer(self, event: PromptInput.Steer) -> None:
        text = event.value.strip()
        if not text and not event.attachments:
            return  # nothing to steer
        if not self.turn_busy:
            # No turn running (or starting) — an idle steer is just a
            # submission, so it takes the same path as Enter: history recall,
            # slash/! routing, image gate. Bypassing that sent "/help" to the
            # model as prose and lost the entry from prompt history.
            self._hide_autocomplete()
            self._history.add(text)
            await self._route_submission(text, event.attachments)
            return
        reason = self.pickers.image_block_reason(event.attachments)
        if reason is not None:
            self.append_log(NoticeMessage(reason))
            return
        if event.attachments and self.harness is None:
            # The daemon's steer route is text-only (a 4a non-goal).
            self.append_log(
                NoticeMessage("Steering with an image needs the session's own process.")
            )
            return
        # The "↪ steering" notice renders off steer.accepted (see the handler).
        try:
            await self.link.steer(text, event.attachments)
        except HostClosed as exc:
            # Attached: the daemon refused it (the turn ended under the
            # keypress) or did not answer. The text is in the box's history;
            # say why it did not go rather than lose it silently.
            self.append_log(ErrorMessage(f"steer not delivered: {exc}"))

    async def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        self._hide_autocomplete()
        text = event.value.strip()
        if not text:
            return
        self._history.add(text)  # capture every submission, commands included
        self.query_one(PromptInput).text = ""
        await self._route_submission(text, event.attachments)

    async def _route_submission(
        self, text: str, attachments: list[tuple[bytes, str]] | None
    ) -> None:
        """Shared routing for submitted prompt text (Enter and idle steer):
        slash commands, `!` passthrough, image gate, then queue-or-start."""
        if text.startswith("/"):
            await dispatch(self, text)
            return
        if (command := parse_bang(text)) is not None:
            try:
                await self._handle_bang(command)
            except RemoteOnly as exc:
                self.note_remote_only(exc)
            return
        reason = self.pickers.image_block_reason(attachments)
        if reason is not None:
            self.append_log(NoticeMessage(reason))
            return
        if self.compact_busy:
            # Refuse (don't enqueue) so the turn isn't silently lost or run against
            # a session the compact worker is mid-summarize on. Symmetric with the
            # notice /compact posts when a turn is running.
            self.append_log(NoticeMessage("Compaction in progress — wait for it to finish."))
            return
        if self.turn_busy:
            # turn_busy covers the submit→turn.started gap too, so a second
            # Enter there is staged rather than submitted behind the first.
            self.queue.enqueue(text, attachments)
            return
        self.queue.paused = False
        await self.start_turn(text, attachments)

    # --- `!` shell passthrough (pure helpers live in shell_passthrough.py) ---

    async def _handle_bang(self, command: str) -> None:
        """Route a `!` submission: usage hint for a bare `!`, refusal mid-turn,
        otherwise run in a worker. A worker (not this handler) because sudo's
        modal needs push_screen_wait — invalid outside a worker, the same
        constraint the model picker documents — and because the command may
        legitimately run for up to PASSTHROUGH_TIMEOUT."""
        if not command:
            await self.post_system(
                "Usage: `! <command>` — run a shell command here; its output is "
                "shared with the model on your next message."
            )
            return
        if self.turn_busy:
            self.append_log(
                NoticeMessage("Can't run a shell command while a turn is running. Press Esc first.")
            )
            return
        harness = self.require_local("`!` shell passthrough")
        # group="shell-passthrough": Textual's WorkerManager cancels every worker
        # sharing a group when a new *exclusive* worker joins that group. The turn
        # worker (start_turn) runs exclusive=True in the default group, so leaving
        # this one there too would let a chat message silently kill an in-flight
        # `!` command with no notice and no queued output. Its own group keeps it
        # immune to that sweep; a turn starting mid-passthrough is fine — the
        # passthrough's output still lands in the transcript and queues normally.
        self.run_worker(
            self._run_shell_passthrough(harness, command),
            group="shell-passthrough",
            exclusive=False,
            # Belt for anything the except clauses in _run_shell_passthrough miss:
            # an arbitrary user command (up to PASSTHROUGH_TIMEOUT) must never be
            # able to take down the whole session via Textual's default
            # exit_on_error=True (see the notification worker for the same
            # pattern).
            exit_on_error=False,
        )

    async def _run_shell_passthrough(self, harness: Harness, command: str) -> None:
        """Execute a `!` command, render its output into the transcript, and
        queue it for the next turn's context. Leading-sudo commands collect a
        password first; it only ever transits the subprocess stdin pipe."""
        password: str | None = None
        if needs_sudo_password(command):
            password = await self.push_screen_wait(SudoPasswordModal(command))
            if password is None:
                self.append_log(NoticeMessage("sudo command cancelled"))
                return
        try:
            output = await run_passthrough(harness.deps.workspace.root, command, password)
        except OSError as exc:
            self.append_log(ErrorMessage(f"! {command} failed to start: {exc}"))
            return
        try:
            # Queue before rendering: if the render below fails, the model still
            # gets the output on the next turn even though the transcript never
            # showed it — losing the render is recoverable (the user can scroll
            # up or re-run), losing the model-context entry silently is worse.
            harness.add_shell_result(command, output)
            await self.post_system(format_transcript_block(command, output))
        except Exception as exc:  # keep the session alive on any render failure
            self.append_log(ErrorMessage(f"! {command}: {type(exc).__name__}: {exc}"))
            logger.warning("failed to render shell passthrough output", exc_info=True)
