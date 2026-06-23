"""Session-view orchestration — extracted from HarnessApp.

Rebuilds the log when the active session changes (new / switch / clear), replays a
restored conversation, and notes auto-renames. Behavior only — it holds no state;
it reaches the app, status presenter, and stream renderer through ``self.app``."""

from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Static

from ...agent import strip_turn_context
from ...compaction import summary_text
from .widgets import (
    AssistantMessage,
    NoticeMessage,
    SummaryWidget,
    ThinkingWidget,
    ToolCallWidget,
    ToolGroupWidget,
    UserMessage,
)


class SessionView:
    """Owns rebuilding/replaying the log for the active session. Constructed by the
    HarnessApp, which delegates new/switch/clear and the auto-rename callback here."""

    def __init__(self, app) -> None:
        self.app = app

    async def replay_history(self, log: VerticalScroll) -> None:
        """Re-render a restored conversation into the log so a resumed session
        looks like where you left off."""
        from pydantic_ai.messages import (
            ModelRequest,
            ModelResponse,
            TextPart,
            ThinkingPart,
            ToolCallPart,
            ToolReturnPart,
            UserPromptPart,
        )

        tool_widgets: dict[str, ToolCallWidget] = {}
        # The current run of consecutive tool calls during replay, mirroring the
        # live path so a resumed session groups bursts the same way: a lone call
        # stays bare, a burst folds into a group.
        group: ToolGroupWidget | None = None
        solo: ToolCallWidget | None = None
        for message in self.app.harness.session.history:
            if isinstance(message, (ModelRequest, ModelResponse)):
                for part in message.parts:
                    if isinstance(part, UserPromptPart):
                        group = None
                        solo = None
                        content = part.content
                        if isinstance(content, str):
                            text = content
                        elif isinstance(content, list):
                            text = " ".join(
                                item for item in content
                                if isinstance(item, str)
                            )
                        else:
                            text = str(content)
                        # A compaction summary renders as its own collapsed block,
                        # not as a user message it would otherwise be mistaken for.
                        body = summary_text(text)
                        if body is not None:
                            await log.mount(SummaryWidget(body))
                            continue
                        # Drop any turn-context envelope (job digests, hook
                        # output, error notes) so the log shows only what the
                        # user typed — as the live path already does.
                        await log.mount(UserMessage(strip_turn_context(text)))
                    elif isinstance(part, TextPart):
                        if part.content:
                            group = None
                            solo = None
                            msg = AssistantMessage()
                            await log.mount(msg)
                            self.app.stream.append_stream(msg, part.content)
                    elif isinstance(part, ThinkingPart):
                        if part.content:
                            group = None
                            solo = None
                            widget = ThinkingWidget()
                            await log.mount(widget)
                            self.app.stream.append_stream(widget.body, part.content)
                    elif isinstance(part, ToolCallPart):
                        widget = ToolCallWidget(part.tool_name, part.args_as_dict())
                        tool_widgets[part.tool_call_id] = widget
                        group, solo = await self.app.stream.add_tool_to_run(
                            widget, log, group, solo
                        )
                    elif isinstance(part, ToolReturnPart):
                        widget = tool_widgets.get(part.tool_call_id)
                        if widget is not None:
                            widget.finish(str(part.content))

    async def mount_header(self, log: VerticalScroll) -> AssistantMessage:
        """Mount the two-column intro header — the MARIM banner on the left, the
        welcome/resume text on the right (Claude-style) — and return the intro
        AssistantMessage for the caller to stream into."""
        from .app import _BANNER

        intro = AssistantMessage()
        await log.mount(
            Horizontal(
                Static(_BANNER, id="banner", markup=False),
                intro,
                id="intro-header",
            )
        )
        return intro

    def on_rename(self, old: str, new: str) -> None:
        """Note an automatic session title in the log. Called synchronously from
        run_turn; mount without awaiting."""
        log = self.app.query_one("#log", VerticalScroll)
        log.mount(NoticeMessage(f"session renamed: {new}"))
        self.app.status.refresh_title()  # the new name shows in the terminal title
        self.app.status.refresh_status()

    async def render_session(self, note: str) -> None:
        """Rebuild the log for a fresh view of the active session: banner, an
        intro note, then a replay of any restored history."""
        self.app.stream.reset()
        log = self.app.query_one("#log", VerticalScroll)
        # Guard the rebuild: while old content is torn down and new content mounted,
        # the log's max_scroll_y is stale, so an interval flush tick must not anchor
        # off it (that left a cleared session bottom-aligned). We set the final
        # anchor state ourselves once the new content is laid out.
        self.app.stream.rebuilding = True
        try:
            log.anchor(False)  # drop any anchor inherited from the previous session
            await log.remove_children()
            intro = await self.mount_header(log)
            self.app.stream.append_stream(intro, note)
            if self.app.harness.session.history:
                await self.replay_history(log)
            self.app.stream.flush_streams()  # render the rebuilt log before first paint
        finally:
            self.app.stream.rebuilding = False
        # A restored session opens at the bottom; a fresh/cleared one stays top-
        # aligned (header pinned) until a turn's output overflows the viewport.
        log.anchor(bool(self.app.harness.session.history))
        self.app.status.refresh_title()  # reflect the switched-to session's name
        self.app.status.refresh_status()
        self.app._render_tasks()
        self.app._render_jobs()  # jobs are process-scoped, not per-session

    async def reset_conversation(self) -> None:
        """Wipe the conversation and re-show the welcome screen (the /clear cmd)."""
        from .app import _WELCOME

        self.app.harness.reset()
        await self.app.harness.session_start("clear")
        await self.render_session(_WELCOME)

    async def start_new_session(self, name: str | None = None) -> None:
        """Begin a fresh named session, leaving existing ones on disk."""
        self.app.harness.new_session(name)
        await self.app.harness.session_start("startup")
        label = self.app.harness.session.session_name or "new session"
        await self.render_session(f"**New session** — `{label}`.")

    async def switch_to_session_id(self, session_id: str) -> None:
        """Load an existing session and show where it left off."""
        n = self.app.harness.switch_session(session_id)
        await self.app.harness.session_start("resume")
        label = self.app.harness.session.session_name or session_id
        await self.render_session(
            f"**Switched to** `{label}` — {n} messages restored."
        )
